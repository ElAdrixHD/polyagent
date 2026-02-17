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
        """Evaluate all tracked markets for reversal-based entry.

        REVERSAL THESIS: Buy the cheap side (underdog) when:
        - The underdog is genuinely cheap (high payout potential)
        - The market has a clear favorite (not hovering at 50/50)
        - Momentum shows signs of reversal (price moving toward strike)
        """
        opportunities: list[TightMarketOpportunity] = []
        profiles = self.tracker.get_all_profiles()

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

            # Get live crypto price
            current_price = self.price_feed.get_price(asset)
            raw_expected_move = self.price_feed.get_expected_move(
                asset, remaining, self.config.tmc_volatility_window
            )

            if current_price is None:
                continue

            strike = profile.market.strike_price
            distance = abs(current_price - strike)
            in_exec = remaining <= self.config.tmc_execution_window

            # Build reusable context for skip recording
            ctx = self._build_context(
                cid, remaining, distance, current_price, strike,
                raw_expected_move or 0, profile, in_exec,
            )

            # Must be in execution window to fire
            if not in_exec:
                # Log pre-signal status periodically
                if remaining % 5 < 0.6:
                    logger.info(
                        f"[TMC] WATCH {asset} '{q}' | "
                        f"price=${current_price:,.2f} strike=${strike:,.2f} | "
                        f"remaining={remaining:.0f}s"
                    )
                continue

            # === REVERSAL ENTRY GATES ===

            # Gate 1: Live CLOB asks — need valid prices to evaluate
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

            # Determine the cheap side (underdog)
            cheap_side, cheap_side_ask, buy_token_id = self._get_cheap_side(
                current_price, strike, yes_ask, no_ask, token_ids
            )

            # Gate 2: Cheap side must be in target range
            # Too cheap (< min) = basically no chance, too expensive (> max) = low payout
            min_ask = self.config.tmc_min_cheap_ask
            max_ask = self.config.tmc_max_entry_ask

            if cheap_side_ask < min_ask:
                self._record_skip(ctx, skip_reason="cheap_side_too_cheap")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"{cheap_side} ask={cheap_side_ask:.3f} < min {min_ask:.3f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            if cheap_side_ask > max_ask:
                self._record_skip(ctx, skip_reason="cheap_side_too_expensive")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"{cheap_side} ask={cheap_side_ask:.3f} > max {max_ask:.3f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Gate 3: Tight ratio — market must have a clear favorite
            # Low tight_ratio = decisive market (good), high = hovering at 50/50 (bad)
            if profile.tight_ratio >= self.config.tmc_max_tight_ratio:
                self._record_skip(ctx, skip_reason="tight_ratio_too_high")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"tight_ratio={profile.tight_ratio:.3f} >= max {self.config.tmc_max_tight_ratio:.3f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Gate 4: Block confirming momentum
            # If price momentum is CONFIRMING the majority (moving away from strike),
            # there is no reversal happening — skip.
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

            # Gate 5: Odds momentum — check if underdog odds are trending up
            # This is optional: if odds are moving TOWARD the underdog, it's a
            # stronger reversal signal. We only BLOCK if odds are moving strongly
            # AGAINST the underdog (the favorite is getting even stronger).
            odds_direction = self._get_odds_momentum(profile, current_price, strike)
            if odds_direction == "confirming":
                self._record_skip(ctx, skip_reason="odds_confirming_majority")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"odds trending against underdog | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # === FIRE SIGNAL ===
            amount = self.config.tmc_max_investment
            payout_ratio = 1.0 / cheap_side_ask if cheap_side_ask > 0 else 0

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
                expected_move=raw_expected_move or 0,
            )
            opportunities.append(opp)
            self._fired.add(cid)

            logger.info(
                f"[TMC] >>> SIGNAL FIRED: {asset} '{q}' | "
                f"BUY {cheap_side}@${cheap_side_ask:.3f} ${amount:.2f} "
                f"(payout={payout_ratio:.1f}x) | "
                f"YES=${yes_ask:.3f} NO=${no_ask:.3f} | "
                f"tight_ratio={profile.tight_ratio:.3f} | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} "
                f"dist=${distance:.2f} | "
                f"remaining={remaining:.0f}s | "
                f"odds_dir={odds_direction}"
            )

        return opportunities

    # --- Helpers ---

    def _get_cheap_side(
        self,
        current_price: float,
        strike: float,
        yes_ask: float,
        no_ask: float,
        token_ids: list[str],
    ) -> tuple[str, float, str]:
        """Determine the cheap (underdog) side based on price vs strike.

        Returns (side_name, side_ask, token_id).
        """
        if current_price > strike:
            # Price above strike → majority is YES → underdog is NO
            return "NO", no_ask, token_ids[1]
        else:
            # Price below strike → majority is NO → underdog is YES
            return "YES", yes_ask, token_ids[0]

    def _get_odds_momentum(
        self, profile, current_price: float, strike: float
    ) -> str:
        """Check if odds are moving toward the underdog (contrarian) or the favorite.

        Returns:
          "contrarian" — underdog gaining probability (good for reversal)
          "confirming" — favorite getting even stronger (bad for reversal)
          "neutral"    — no significant movement
        """
        if not profile.snapshots or len(profile.snapshots) < 3:
            return "neutral"

        snaps = profile.snapshots
        n = min(5, len(snaps) // 2)
        if n < 2:
            return "neutral"

        early_avg = sum(s.yes_price for s in snaps[:n]) / n
        late_avg = sum(s.yes_price for s in snaps[-n:]) / n
        odds_shift = late_avg - early_avg  # positive = YES trending up

        threshold = 0.02  # minimum shift to be considered directional

        if abs(odds_shift) < threshold:
            return "neutral"

        if current_price > strike:
            # Majority is YES. Contrarian = YES dropping (shift < 0)
            return "contrarian" if odds_shift < -threshold else "confirming"
        else:
            # Majority is NO. Contrarian = YES rising (shift > 0)
            return "contrarian" if odds_shift > threshold else "confirming"

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

    # --- Skip recording ---

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
        decimals = 6 if strike < 10 else (4 if strike < 1000 else 2)
        return {
            "cid": cid,
            "remaining": round(remaining, 1),
            "in_execution_window": in_execution_window,
            "distance": round(distance, decimals),
            "raw_expected_move": round(raw_expected_move, decimals),
            "current_price": round(current_price, decimals),
            "strike": round(strike, decimals),
            "yes_price": round(profile.current_yes, 4),
            "no_price": round(profile.current_no, 4),
            "tight_ratio": round(profile.tight_ratio, 4),
            "price_side": "above" if current_price > strike else "below",
        }

    def _record_skip(
        self,
        ctx: dict,
        would_have_fired: bool = False,
        skip_reason: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **{k: v for k, v in ctx.items() if k != "cid"},
            "would_have_fired": would_have_fired,
            "skip_reason": skip_reason,
        }
        cid = ctx["cid"]
        if cid not in self._skipped_signals:
            self._skipped_signals[cid] = []
        self._skipped_signals[cid].append(entry)
