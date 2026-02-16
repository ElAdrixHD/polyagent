import logging
from datetime import datetime, timezone

from src.core.client import PolymarketClient
from src.core.config import Config

from .chainlink_feed import ChainlinkPriceFeed
from .models import TightMarketOpportunity
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")


class SignalEngine:
    def __init__(
        self,
        config: Config,
        tracker: TightnessTracker,
        client: PolymarketClient,
        price_feed: ChainlinkPriceFeed,
    ):
        self.config = config
        self.tracker = tracker
        self.client = client
        self.price_feed = price_feed
        self._fired: set[str] = set()  # condition_ids already fired
        self._skipped_signals: dict[str, list[dict]] = {}  # cid -> list of skip entries

    def check_signals(self) -> list[TightMarketOpportunity]:
        opportunities: list[TightMarketOpportunity] = []
        profiles = self.tracker.get_all_profiles()
        K = self.config.tmc_volatility_multiplier

        for profile in profiles:
            cid = profile.market.condition_id
            asset = profile.market.asset
            q = profile.market.question[:50]
            remaining = profile.seconds_remaining

            if cid in self._fired:
                continue

            if profile.market.strike_price is None:
                continue

            if remaining <= 0 or remaining > self.config.tmc_entry_window:
                continue

            if remaining < self.config.tmc_min_seconds_remaining:
                continue

            # Get live crypto price and volatility
            current_price = self.price_feed.get_price(asset)
            raw_expected_move = self.price_feed.get_expected_move(
                asset, remaining, self.config.tmc_volatility_window
            )

            if current_price is None or raw_expected_move is None:
                if remaining <= self.config.tmc_execution_window:
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"no Chainlink data (price={current_price} expected_move={raw_expected_move})"
                    )
                continue

            strike = profile.market.strike_price
            distance = abs(current_price - strike)
            in_exec = remaining <= self.config.tmc_execution_window

            # Build reusable context for skip recording
            ctx = self._build_context(
                cid, remaining, distance, current_price, strike,
                raw_expected_move, profile, in_exec,
            )

            # Boost expected_move inside execution window
            expected_move = (
                raw_expected_move * self.config.tmc_volatility_boost_factor
                if in_exec else raw_expected_move
            )

            # KEY SIGNAL: is the price close enough that a reversal is plausible?
            signal_passes = expected_move > 0 and distance / expected_move <= K

            # Skip low-volatility markets UNLESS distance ratio is already favorable
            volatility = self.price_feed.get_volatility(
                asset, self.config.tmc_volatility_window
            )
            if volatility is not None and volatility < self.config.tmc_min_volatility:
                if not signal_passes:
                    if in_exec:
                        logger.info(
                            f"[TMC] SKIP {asset} '{q}' | "
                            f"low volatility + price too far"
                        )
                    continue

            if not signal_passes:
                if in_exec or remaining % 5 < 0.6:
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"dist=${distance:.2f} > {K}x em=${expected_move:.2f} | "
                        f"remaining={remaining:.0f}s"
                    )
                self._record_skip(ctx)
                continue

            # Signal passes — but must be in execution window
            if not in_exec:
                logger.info(
                    f"[TMC] PRE-SIGNAL {asset} '{q}' | "
                    f"price=${current_price:,.2f} strike=${strike:,.2f} | "
                    f"remaining={remaining:.0f}s | waiting for exec window"
                )
                self._record_skip(ctx, would_have_fired=True)
                continue

            # === EXECUTION WINDOW GATES ===

            raw_ratio = distance / raw_expected_move if raw_expected_move > 0 else float("inf")

            # Gate 1: raw distance ratio — with odds-based bypass
            if raw_ratio > self.config.tmc_max_distance_ratio:
                # Check if odds justify bypassing the distance gate
                if self._has_odds_bypass(profile, current_price, strike):
                    logger.info(
                        f"[TMC] BYPASS {asset} '{q}' | "
                        f"raw_ratio={raw_ratio:.1f} > max BUT cheap side is cheap + contrarian momentum | "
                        f"remaining={remaining:.0f}s"
                    )
                else:
                    self._record_skip(ctx, skip_reason="distance_ratio_too_high")
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"raw_ratio={raw_ratio:.1f} > max {self.config.tmc_max_distance_ratio} | "
                        f"remaining={remaining:.0f}s"
                    )
                    continue

            # Gate 2: block confirming momentum
            # If price momentum in last 3s is moving WITH the majority, skip
            # (0% historical reversal rate when momentum confirms the favorite)
            if self.config.tmc_block_confirming_momentum:
                momentum = self._get_price_momentum(asset, remaining)
                if momentum is not None and self._is_confirming_momentum(
                    momentum, current_price, strike
                ):
                    self._record_skip(ctx, skip_reason="confirming_momentum")
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"momentum confirms majority (${momentum:+.2f}/s) | "
                        f"remaining={remaining:.0f}s"
                    )
                    continue

            # Gate 3: live CLOB asks
            token_ids = profile.market.token_ids
            yes_ask = self.client.get_best_ask(token_ids[0])
            no_ask = self.client.get_best_ask(token_ids[1])

            if yes_ask is None or no_ask is None or yes_ask <= 0 or no_ask <= 0:
                self._record_skip(ctx, skip_reason="no_valid_asks")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"no valid asks (YES={yes_ask} NO={no_ask})"
                )
                continue

            # Gate 4: cheap side too expensive
            if current_price > strike:
                cheap_side_ask, cheap_side = no_ask, "NO"
            else:
                cheap_side_ask, cheap_side = yes_ask, "YES"

            max_entry = self.config.tmc_max_entry_ask
            if max_entry > 0 and cheap_side_ask > max_entry:
                self._record_skip(ctx, skip_reason="cheap_side_too_expensive")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"cheap side ({cheap_side}) ask={cheap_side_ask:.3f} > max {max_entry:.3f}"
                )
                continue

            # === FIRE SIGNAL ===
            buy_token_id = token_ids[1] if cheap_side == "NO" else token_ids[0]
            amount = self.config.tmc_max_investment

            opp = TightMarketOpportunity(
                market=profile.market,
                profile=profile,
                yes_ask=yes_ask,
                no_ask=no_ask,
                buy_side=cheap_side,
                buy_token_id=buy_token_id,
                buy_ask=cheap_side_ask,
                amount=amount,
                total_cost=amount,
                strike_price=strike,
                current_crypto_price=current_price,
                distance=distance,
                expected_move=expected_move,
            )
            opportunities.append(opp)
            self._fired.add(cid)

            logger.info(
                f"[TMC] >>> SIGNAL FIRED: {asset} '{q}' | "
                f"BUY {cheap_side}@${cheap_side_ask:.3f} ${amount:.2f} | "
                f"YES=${yes_ask:.3f} NO=${no_ask:.3f} | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} "
                f"dist=${distance:.2f} em=${expected_move:.2f} | "
                f"remaining={remaining:.0f}s"
            )

        return opportunities

    # --- Odds-based bypass logic ---

    def _has_odds_bypass(self, profile, current_price: float, strike: float) -> bool:
        """Allow bypassing the distance ratio gate when odds signal a reversal.

        Conditions:
          1. Cheap side ask is very cheap (< tmc_odds_bypass_max_ask, e.g. $0.15)
          2. Odds momentum is contrarian — moving AGAINST the current majority
        """
        if not profile.snapshots or len(profile.snapshots) < 3:
            return False

        # Determine cheap side from price vs strike
        if current_price > strike:
            cheap_price = profile.current_no
        else:
            cheap_price = profile.current_yes

        # Condition 1: cheap side must be very cheap
        if cheap_price > self.config.tmc_odds_bypass_max_ask:
            return False

        # Condition 2: odds must be moving in a contrarian direction
        # Compare recent odds trend (last 5 vs first 5 snapshots in exec window)
        snaps = profile.snapshots
        n = min(5, len(snaps) // 2)
        if n < 2:
            return False

        early_avg = sum(s.yes_price for s in snaps[:n]) / n
        late_avg = sum(s.yes_price for s in snaps[-n:]) / n
        odds_shift = late_avg - early_avg  # positive = YES trending up

        # If price > strike, majority is YES, so contrarian = YES trending DOWN (shift < 0)
        # If price < strike, majority is NO,  so contrarian = YES trending UP  (shift > 0)
        if current_price > strike:
            return odds_shift < -0.01  # YES dropping = contrarian to majority
        else:
            return odds_shift > 0.01  # YES rising = contrarian to majority

    # --- Momentum analysis ---

    def _get_price_momentum(self, asset: str, remaining: float) -> float | None:
        """Get price momentum ($/sec) over the last 3 seconds."""
        import time
        end_ts = time.time()
        start_ts = end_ts - 3.0
        history = self.price_feed.get_price_history(asset, start_ts, end_ts)
        if len(history) < 2:
            return None
        dt = history[-1][0] - history[0][0]
        dp = history[-1][1] - history[0][1]
        return dp / dt if dt > 0 else None

    def _is_confirming_momentum(
        self, momentum: float, current_price: float, strike: float
    ) -> bool:
        """Check if momentum is confirming (reinforcing) the majority side.

        If price > strike → majority is YES → confirming = price going UP (momentum > 0)
        If price < strike → majority is NO  → confirming = price going DOWN (momentum < 0)

        Returns True only for significant momentum above the threshold.
        """
        threshold = self.config.tmc_momentum_threshold
        if current_price > strike:
            return momentum > threshold  # moving further above strike
        else:
            return momentum < -threshold  # moving further below strike

    # --- Skip recording (simplified) ---

    def get_skipped_signals(self, condition_id: str) -> list[dict]:
        return self._skipped_signals.get(condition_id, [])

    def mark_expired(self, condition_id: str) -> None:
        self._fired.discard(condition_id)
        self._skipped_signals.pop(condition_id, None)

    def _build_context(
        self,
        cid: str,
        remaining: float,
        distance: float,
        current_price: float,
        strike: float,
        raw_expected_move: float,
        profile,
        in_execution_window: bool,
    ) -> dict:
        """Build a reusable context dict for skip recording."""
        boosted_em = raw_expected_move * self.config.tmc_volatility_boost_factor
        decimals = 6 if strike < 10 else (4 if strike < 1000 else 2)
        return {
            "cid": cid,
            "remaining": round(remaining, 1),
            "in_execution_window": in_execution_window,
            "distance": round(distance, decimals),
            "raw_expected_move": round(raw_expected_move, decimals),
            "boosted_expected_move": round(boosted_em, decimals),
            "ratio_raw": round(
                distance / raw_expected_move if raw_expected_move > 0 else float("inf"), 2
            ),
            "ratio_boosted": round(
                distance / boosted_em if boosted_em > 0 else float("inf"), 2
            ),
            "current_price": round(current_price, decimals),
            "strike": round(strike, decimals),
            "yes_price": round(profile.current_yes, 4),
            "no_price": round(profile.current_no, 4),
            "price_side": "above" if current_price > strike else "below",
        }

    def _record_skip(
        self,
        ctx: dict,
        would_have_fired: bool = False,
        skip_reason: str = "",
    ) -> None:
        K = self.config.tmc_volatility_multiplier
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **{k: v for k, v in ctx.items() if k != "cid"},
            "would_have_fired": would_have_fired,
            "would_have_passed_with_boost": ctx["ratio_boosted"] <= K,
            "skip_reason": skip_reason,
        }
        cid = ctx["cid"]
        if cid not in self._skipped_signals:
            self._skipped_signals[cid] = []
        self._skipped_signals[cid].append(entry)
