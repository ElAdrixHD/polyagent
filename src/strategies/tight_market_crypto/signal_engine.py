import logging

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

            # Must be in the entry window
            if remaining <= 0 or remaining > self.config.tmc_entry_window:
                continue

            # Get live crypto price and volatility
            current_price = self.binance_feed.get_price(asset)
            expected_move = self.binance_feed.get_expected_move(
                asset, remaining, self.config.tmc_volatility_window
            )

            if current_price is None or expected_move is None:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"no Binance data (price={current_price} expected_move={expected_move})"
                )
                continue

            strike = profile.market.strike_price
            distance = abs(current_price - strike)

            # KEY SIGNAL: is the price close enough that a reversal is plausible?
            if expected_move <= 0 or distance / expected_move > K:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"dist=${distance:.2f} > {K}x expected_move=${expected_move:.2f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            ratio = distance / expected_move if expected_move > 0 else 0

            logger.info(
                f"[TMC] CHECK {asset} '{q}' | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} dist=${distance:.2f} | "
                f"expected_move=${expected_move:.2f} (ratio={ratio:.2f} < K={K}) | "
                f"remaining={remaining:.0f}s | snaps={len(profile.snapshots)}"
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
                f"dist=${distance:.2f} expected=${expected_move:.2f} | "
                f"remaining={remaining:.0f}s | "
                f"${amount_per_side:.2f}/side = ${total_cost:.2f} total"
            )

        return opportunities

    def mark_expired(self, condition_id: str) -> None:
        self._fired.discard(condition_id)
