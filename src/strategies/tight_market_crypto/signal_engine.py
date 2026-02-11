import logging

from src.core.client import PolymarketClient
from src.core.config import Config

from .models import TightMarketOpportunity
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")


class SignalEngine:
    def __init__(
        self,
        config: Config,
        tracker: TightnessTracker,
        client: PolymarketClient,
    ):
        self.config = config
        self.tracker = tracker
        self.client = client
        self._fired: set[str] = set()  # condition_ids already fired

    def check_signals(self) -> list[TightMarketOpportunity]:
        opportunities: list[TightMarketOpportunity] = []
        profiles = self.tracker.get_all_profiles()

        for profile in profiles:
            cid = profile.market.condition_id
            asset = profile.market.asset
            q = profile.market.question[:50]
            remaining = profile.seconds_remaining

            if cid in self._fired:
                continue

            # Log markets approaching entry window
            if 0 < remaining <= self.config.tmc_entry_window + 5:
                current_spread = abs(profile.current_yes - 0.5)
                logger.info(
                    f"[TMC] CHECK {asset} '{q}' | "
                    f"remaining={remaining:.1f}s | "
                    f"snapshots={len(profile.snapshots)} | "
                    f"YES={profile.current_yes:.3f} NO={profile.current_no:.3f} | "
                    f"spread={current_spread:.4f} | "
                    f"tight_ratio={profile.tight_ratio:.0%} avg_spread={profile.avg_spread:.4f}"
                )

            # Condition 1: in the last N seconds
            if remaining <= 0 or remaining > self.config.tmc_entry_window:
                continue

            # Condition 2: tight throughout the window
            if profile.tight_ratio < self.config.tmc_min_tight_ratio:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"tight_ratio={profile.tight_ratio:.0%} < "
                    f"min={self.config.tmc_min_tight_ratio:.0%}"
                )
                continue

            # Condition 3: still tight NOW
            current_spread = abs(profile.current_yes - 0.5)
            if current_spread > self.config.tmc_tightness_threshold:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"current_spread={current_spread:.4f} > "
                    f"threshold={self.config.tmc_tightness_threshold}"
                )
                continue

            # Condition 4: need enough snapshots to be meaningful
            if len(profile.snapshots) < 5:
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"only {len(profile.snapshots)} snapshots (need >= 5)"
                )
                continue

            # Condition 5: get live asks from CLOB
            token_ids = profile.market.token_ids
            logger.info(f"[TMC] Fetching live asks for {asset} '{q}'...")
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
            )
            opportunities.append(opp)
            self._fired.add(cid)

            logger.info(
                f"[TMC] >>> SIGNAL FIRED: {asset} "
                f"'{q}' | "
                f"YES=${yes_ask:.3f} NO=${no_ask:.3f} | "
                f"tight_ratio={profile.tight_ratio:.0%} "
                f"avg_spread={profile.avg_spread:.4f} | "
                f"remaining={remaining:.1f}s | "
                f"${amount_per_side:.2f}/side = ${total_cost:.2f} total"
            )

        return opportunities

    def mark_expired(self, condition_id: str) -> None:
        self._fired.discard(condition_id)
