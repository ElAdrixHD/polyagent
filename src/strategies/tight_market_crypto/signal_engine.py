import logging
from datetime import datetime, timezone

from src.core.client import PolymarketClient
from src.core.config import Config

from .binance_feed import BinancePriceFeed
from .models import TightMarketOpportunity
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")


class SignalEngine:
    def __init__(
        self,
        config: Config,
        tracker: TightnessTracker,
        client: PolymarketClient,
        binance_feed: BinancePriceFeed,
    ):
        self.config = config
        self.tracker = tracker
        self.client = client
        self.binance_feed = binance_feed
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

            # Need strike price to evaluate
            if profile.market.strike_price is None:
                continue

            # Only track within the broad entry window
            if remaining <= 0 or remaining > self.config.tmc_entry_window:
                continue

            # Skip if too close to expiry (trades <7s have ~0% win rate)
            if remaining < self.config.tmc_min_seconds_remaining:
                continue

            # Get live crypto price and volatility
            current_price = self.binance_feed.get_price(asset)
            volatility = self.binance_feed.get_volatility(
                asset, self.config.tmc_volatility_window
            )
            raw_expected_move = self.binance_feed.get_expected_move(
                asset, remaining, self.config.tmc_volatility_window
            )

            # Skip low-volatility markets (Q1 vol has 21% WR vs 38% in Q4)
            if volatility is not None and volatility < self.config.tmc_min_volatility:
                if remaining <= self.config.tmc_execution_window:
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"low volatility: {volatility:.8f} < {self.config.tmc_min_volatility:.8f}"
                    )
                continue

            if current_price is None or raw_expected_move is None:
                # Only log when close to expiry to avoid spam
                if remaining <= self.config.tmc_execution_window:
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"no Binance data (price={current_price} expected_move={raw_expected_move})"
                    )
                continue

            # Boost expected_move in final seconds (volatility explodes near expiry)
            boosted = remaining <= self.config.tmc_volatility_boost_threshold
            if boosted:
                expected_move = raw_expected_move * self.config.tmc_volatility_boost_factor
            else:
                expected_move = raw_expected_move

            strike = profile.market.strike_price
            distance = abs(current_price - strike)
            in_execution_window = remaining <= self.config.tmc_execution_window

            # KEY SIGNAL: is the price close enough that a reversal is plausible?
            signal_passes = expected_move > 0 and distance / expected_move <= K

            if not signal_passes:
                boost_tag = f" [BOOST x{self.config.tmc_volatility_boost_factor}]" if boosted else ""
                # Verbose log only inside execution window, sparse outside
                if in_execution_window or remaining % 5 < 0.6:
                    logger.info(
                        f"[TMC] SKIP {asset} '{q}' | "
                        f"dist=${distance:.2f} > {K}x expected_move=${expected_move:.2f}{boost_tag} | "
                        f"remaining={remaining:.0f}s"
                    )
                # Always record skip for shadow log (entire entry window)
                ratio_raw = distance / raw_expected_move if raw_expected_move > 0 else float("inf")
                boosted_em = raw_expected_move * self.config.tmc_volatility_boost_factor
                ratio_boosted = distance / boosted_em if boosted_em > 0 else float("inf")
                yes_price = profile.current_yes
                no_price = profile.current_no
                self._record_skip(
                    cid=cid,
                    remaining=remaining,
                    distance=distance,
                    raw_expected_move=raw_expected_move,
                    boosted_expected_move=boosted_em,
                    ratio_raw=ratio_raw,
                    ratio_boosted=ratio_boosted,
                    current_price=current_price,
                    strike=strike,
                    yes_price=yes_price,
                    no_price=no_price,
                    in_execution_window=in_execution_window,
                )
                continue

            # Signal passes volatility check
            ratio = distance / expected_move if expected_move > 0 else 0
            boost_tag = f" [BOOST x{self.config.tmc_volatility_boost_factor}]" if boosted else ""

            # Outside execution window: log as pre-signal, record for shadow
            if not in_execution_window:
                logger.info(
                    f"[TMC] PRE-SIGNAL {asset} '{q}' | "
                    f"price=${current_price:,.2f} strike=${strike:,.2f} dist=${distance:.2f} | "
                    f"expected_move=${expected_move:.2f} (ratio={ratio:.2f} < K={K}){boost_tag} | "
                    f"remaining={remaining:.0f}s | "
                    f"waiting for ≤{self.config.tmc_execution_window:.0f}s exec window"
                )
                boosted_em = raw_expected_move * self.config.tmc_volatility_boost_factor
                self._record_skip(
                    cid=cid,
                    remaining=remaining,
                    distance=distance,
                    raw_expected_move=raw_expected_move,
                    boosted_expected_move=boosted_em,
                    ratio_raw=distance / raw_expected_move if raw_expected_move > 0 else float("inf"),
                    ratio_boosted=distance / boosted_em if boosted_em > 0 else float("inf"),
                    current_price=current_price,
                    strike=strike,
                    yes_price=profile.current_yes,
                    no_price=profile.current_no,
                    in_execution_window=False,
                    would_have_fired=True,
                )
                continue

            # Check if price has crossed the strike during execution window
            # This is the strongest reversal predictor: 57% reversal rate when True vs 14% when False
            exec_start_ts = profile.market.end_date.timestamp() - self.config.tmc_execution_window
            price_crossed = self.binance_feed.has_price_crossed(asset, strike, exec_start_ts)

            logger.info(
                f"[TMC] CHECK {asset} '{q}' | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} dist=${distance:.2f} | "
                f"expected_move=${expected_move:.2f} (ratio={ratio:.2f} < K={K}){boost_tag} | "
                f"remaining={remaining:.0f}s | snaps={len(profile.snapshots)} | "
                f"crossed_strike={price_crossed} | "
                f"IN EXECUTION WINDOW"
            )

            # Require price to have crossed the strike during execution window
            # Data shows 57.1% reversal rate when True vs 14.1% when False
            if self.config.tmc_require_strike_cross and not price_crossed:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"price has NOT crossed strike=${strike:,.2f} during exec window | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Get live asks from CLOB
            token_ids = profile.market.token_ids
            yes_ask = self.client.get_best_ask(token_ids[0])
            no_ask = self.client.get_best_ask(token_ids[1])

            if yes_ask is None or no_ask is None:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"no live asks (YES={yes_ask} NO={no_ask})"
                )
                continue

            if yes_ask <= 0 or no_ask <= 0:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"invalid asks (YES={yes_ask} NO={no_ask})"
                )
                continue

            # Skip if market already too one-sided (minority ask too cheap)
            min_ask = min(yes_ask, no_ask)
            threshold = self.config.tmc_min_minority_ask
            if threshold > 0 and min_ask < threshold:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"market too one-sided: min(ask)={min_ask:.3f} < {threshold:.3f} | "
                    f"YES={yes_ask:.3f} NO={no_ask:.3f}"
                )
                continue

            # Determine the "cheap" side based on price vs strike
            # If price > strike → YES is favorite → buy NO (the cheap underdog)
            # If price < strike → NO is favorite → buy YES (the cheap underdog)
            if current_price > strike:
                cheap_side_ask = no_ask
                cheap_side = "NO"
            else:
                cheap_side_ask = yes_ask
                cheap_side = "YES"

            max_entry = self.config.tmc_max_entry_ask
            if max_entry > 0 and cheap_side_ask > max_entry:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"cheap side ({cheap_side}) ask={cheap_side_ask:.3f} > max {max_entry:.3f} | "
                    f"YES={yes_ask:.3f} NO={no_ask:.3f}"
                )
                continue

            # Determine buy token_id for the cheap side
            if cheap_side == "NO":
                buy_token_id = token_ids[1]
            else:
                buy_token_id = token_ids[0]

            amount = self.config.tmc_max_investment
            total_cost = amount

            opp = TightMarketOpportunity(
                market=profile.market,
                profile=profile,
                yes_ask=yes_ask,
                no_ask=no_ask,
                buy_side=cheap_side,
                buy_token_id=buy_token_id,
                buy_ask=cheap_side_ask,
                amount=amount,
                total_cost=total_cost,
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
                f"dist=${distance:.2f} expected=${expected_move:.2f}{boost_tag} | "
                f"remaining={remaining:.0f}s"
            )

        return opportunities

    def get_skipped_signals(self, condition_id: str) -> list[dict]:
        return self._skipped_signals.get(condition_id, [])

    def mark_expired(self, condition_id: str) -> None:
        self._fired.discard(condition_id)
        self._skipped_signals.pop(condition_id, None)

    def _record_skip(
        self,
        cid: str,
        remaining: float,
        distance: float,
        raw_expected_move: float,
        boosted_expected_move: float,
        ratio_raw: float,
        ratio_boosted: float,
        current_price: float,
        strike: float,
        yes_price: float = 0.0,
        no_price: float = 0.0,
        in_execution_window: bool = True,
        would_have_fired: bool = False,
    ) -> None:
        K = self.config.tmc_volatility_multiplier
        # Use enough decimal places for small-price assets (XRP, SOL)
        decimals = 6 if strike < 10 else (4 if strike < 1000 else 2)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "remaining": round(remaining, 1),
            "in_execution_window": in_execution_window,
            "would_have_fired": would_have_fired,
            "distance": round(distance, decimals),
            "raw_expected_move": round(raw_expected_move, decimals),
            "boosted_expected_move": round(boosted_expected_move, decimals),
            "ratio_raw": round(ratio_raw, 2),
            "ratio_boosted": round(ratio_boosted, 2),
            "would_have_passed_with_boost": ratio_boosted <= K,
            "current_price": round(current_price, decimals),
            "strike": round(strike, decimals),
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "price_side": "above" if current_price > strike else "below",
        }
        if cid not in self._skipped_signals:
            self._skipped_signals[cid] = []
        self._skipped_signals[cid].append(entry)
