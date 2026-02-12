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

            # Only execute in the last N seconds (W-rebound window)
            if remaining > self.config.tmc_execution_window:
                continue

            # Get live crypto price and volatility
            current_price = self.binance_feed.get_price(asset)
            raw_expected_move = self.binance_feed.get_expected_move(
                asset, remaining, self.config.tmc_volatility_window
            )

            if current_price is None or raw_expected_move is None:
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

            # KEY SIGNAL: is the price close enough that a reversal is plausible?
            if expected_move <= 0 or distance / expected_move > K:
                boost_tag = f" [BOOST x{self.config.tmc_volatility_boost_factor}]" if boosted else ""
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"dist=${distance:.2f} > {K}x expected_move=${expected_move:.2f}{boost_tag} | "
                    f"remaining={remaining:.0f}s"
                )
                # Record skipped signal for shadow log
                ratio_raw = distance / raw_expected_move if raw_expected_move > 0 else float("inf")
                boosted_em = raw_expected_move * self.config.tmc_volatility_boost_factor
                ratio_boosted = distance / boosted_em if boosted_em > 0 else float("inf")
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
                )
                continue

            ratio = distance / expected_move if expected_move > 0 else 0
            boost_tag = f" [BOOST x{self.config.tmc_volatility_boost_factor}]" if boosted else ""

            logger.info(
                f"[TMC] CHECK {asset} '{q}' | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} dist=${distance:.2f} | "
                f"expected_move=${expected_move:.2f} (ratio={ratio:.2f} < K={K}){boost_tag} | "
                f"remaining={remaining:.0f}s | snaps={len(profile.snapshots)} | "
                f"waiting for ≤{self.config.tmc_execution_window:.0f}s window"
            )

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

            amount_per_side = self.config.tmc_max_investment / 2
            total_cost = self.config.tmc_max_investment

            opp = TightMarketOpportunity(
                market=profile.market,
                profile=profile,
                yes_ask=yes_ask,
                no_ask=no_ask,
                amount_per_side=amount_per_side,
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
                f"YES=${yes_ask:.3f} NO=${no_ask:.3f} | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} "
                f"dist=${distance:.2f} expected=${expected_move:.2f}{boost_tag} | "
                f"remaining={remaining:.0f}s | "
                f"${amount_per_side:.2f}/side = ${total_cost:.2f} total"
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
    ) -> None:
        K = self.config.tmc_volatility_multiplier
        # Use enough decimal places for small-price assets (XRP, SOL)
        decimals = 6 if strike < 10 else (4 if strike < 1000 else 2)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "remaining": round(remaining, 1),
            "distance": round(distance, decimals),
            "raw_expected_move": round(raw_expected_move, decimals),
            "boosted_expected_move": round(boosted_expected_move, decimals),
            "ratio_raw": round(ratio_raw, 2),
            "ratio_boosted": round(ratio_boosted, 2),
            "would_have_passed_with_boost": ratio_boosted <= K,
            "current_price": round(current_price, decimals),
            "strike": round(strike, decimals),
        }
        if cid not in self._skipped_signals:
            self._skipped_signals[cid] = []
        self._skipped_signals[cid].append(entry)
