import logging
import threading
import time
from datetime import datetime, timezone

from src.core.client import PolymarketClient
from src.core.config import Config

from .executor import TightMarketCryptoExecutor
from .market_finder import CryptoMarketFinder
from .signal_engine import SignalEngine
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")


class TightMarketCryptoCoordinator:
    """Single-loop coordinator for tight market crypto strategy.

    Only two threads total:
    - WebSocket thread (inside TightnessTracker) for real-time odds
    - Main loop thread: discovery → signal check → execute → sleep
    """

    def __init__(self, config: Config):
        self.config = config
        self._running = False
        self._thread: threading.Thread | None = None

        self._client = PolymarketClient(config)
        self._finder = CryptoMarketFinder(config)
        self._tracker = TightnessTracker(config)
        self._signal_engine = SignalEngine(config, self._tracker, self._client)
        self._executor = TightMarketCryptoExecutor(self._client, config)

        self._last_discovery = 0.0

    def start(self) -> None:
        self._running = True
        assets = self.config.tmc_crypto_assets
        logger.info(f"[TMC] Starting — tracking: {assets}")

        self._tracker.start()

        self._thread = threading.Thread(
            target=self._main_loop, name="TMC-Main", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        logger.info("[TMC] Shutting down...")
        self._running = False
        self._tracker.stop()

    def join(self, timeout: float = 5.0) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _main_loop(self) -> None:
        # Initial discovery immediately
        self._discover_and_clean()

        while self._running:
            try:
                # Periodic discovery
                now = time.time()
                if now - self._last_discovery >= self.config.tmc_discovery_interval:
                    self._discover_and_clean()
                    self._last_discovery = now

                # Check signals and execute immediately
                opportunities = self._signal_engine.check_signals()
                for opp in opportunities:
                    if not self._running:
                        break
                    try:
                        result = self._executor.execute(opp)
                        if result.success:
                            logger.info(
                                f"[TMC] Trade {'simulated' if self.config.dry_run else 'executed'}: "
                                f"${result.cost:.2f} on {opp.market.asset}"
                            )
                    except Exception as e:
                        logger.error(f"[TMC] Executor error: {e}")

            except Exception as e:
                logger.error(f"[TMC] Main loop error: {e}")

            time.sleep(0.5)

    def _discover_and_clean(self) -> None:
        now = datetime.now(timezone.utc)

        # Clean expired markets
        expired_count = 0
        for cid in list(self._tracker.tracked_condition_ids()):
            market = self._tracker.get_tracked_market(cid)
            if market and market.end_date < now:
                self._tracker.remove_market(cid)
                self._signal_engine.mark_expired(cid)
                logger.info(
                    f"[TMC] Expired: {market.asset} '{market.question[:50]}'"
                )
                expired_count += 1
        if expired_count:
            logger.info(f"[TMC] Cleaned {expired_count} expired markets")

        # Discover new markets
        markets = self._finder.find_upcoming_markets()
        already_tracked = self._tracker.tracked_condition_ids()
        new_count = 0
        for market in markets:
            if market.condition_id not in already_tracked:
                self._tracker.add_market(market)
                new_count += 1

        tracked = len(self._tracker.tracked_condition_ids())
        if new_count:
            logger.info(f"[TMC] Added {new_count} new markets (tracking {tracked} total)")

        # Status summary of tracked markets
        if tracked > 0:
            profiles = self._tracker.get_all_profiles()
            for p in profiles:
                logger.info(
                    f"[TMC] STATUS: {p.market.asset} '{p.market.question[:45]}' | "
                    f"{p.seconds_remaining:.0f}s left | "
                    f"snaps={len(p.snapshots)} | "
                    f"YES={p.current_yes:.3f} NO={p.current_no:.3f} | "
                    f"tight={p.tight_ratio:.0%}"
                )
