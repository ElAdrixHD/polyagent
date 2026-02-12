import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.core.client import PolymarketClient
from src.core.config import Config

from .binance_feed import BinancePriceFeed
from .executor import TightMarketCryptoExecutor
from .market_finder import CryptoMarketFinder
from .signal_engine import SignalEngine
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")

SHADOW_FILE = Path("data/tight_market_crypto_shadow.json")


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
        self._binance_feed = BinancePriceFeed()
        self._signal_engine = SignalEngine(
            config, self._tracker, self._client, self._binance_feed
        )
        self._executor = TightMarketCryptoExecutor(self._client, config)

        self._last_discovery = 0.0

    def start(self) -> None:
        self._running = True
        assets = self.config.tmc_crypto_assets
        logger.info(f"[TMC] Starting — tracking: {assets}")

        self._tracker.start()
        self._binance_feed.start()

        self._thread = threading.Thread(
            target=self._main_loop, name="TMC-Main", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        logger.info("[TMC] Shutting down...")
        self._running = False
        self._tracker.stop()
        self._binance_feed.stop()

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

        # Clean expired markets and record outcomes using Binance price
        expired_count = 0
        traded_cids = self._executor.get_traded_condition_ids()
        for cid in list(self._tracker.tracked_condition_ids()):
            market = self._tracker.get_tracked_market(cid)
            if market and market.end_date < now:
                # Capture profile BEFORE removal for shadow log
                profile = self._tracker.get_profile(cid)
                skipped = self._signal_engine.get_skipped_signals(cid)

                self._tracker.remove_market(cid)
                self._signal_engine.mark_expired(cid)
                logger.info(
                    f"[TMC] Expired: {market.asset} '{market.question[:50]}'"
                )

                # Determine outcome from Binance price at window close vs strike
                final_price = None
                outcome = None
                if market.strike_price is not None:
                    end_ts = market.end_date.timestamp()
                    final_price = self._binance_feed.get_price_at(
                        market.asset, end_ts
                    )
                    if final_price is not None:
                        outcome = "YES" if final_price > market.strike_price else "NO"
                        self._executor.update_outcomes_for_condition(
                            cid, outcome, final_price
                        )
                        logger.info(
                            f"[TMC] Resolved via Binance: {market.asset} "
                            f"strike={market.strike_price:,.2f} "
                            f"final={final_price:,.2f} → {outcome}"
                        )

                # Shadow log: record every expiring market
                self._save_shadow_entry(
                    cid=cid,
                    market=market,
                    profile=profile,
                    final_price=final_price,
                    outcome=outcome,
                    was_traded=cid in traded_cids,
                    skipped_signals=skipped,
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

        # Capture strike prices for markets whose window has opened
        for cid in self._tracker.tracked_condition_ids():
            market = self._tracker.get_tracked_market(cid)
            if not market or market.strike_price is not None:
                continue
            if market.start_date and market.start_date <= now:
                price = self._binance_feed.get_price(market.asset)
                if price is not None:
                    market.strike_price = price
                    logger.info(
                        f"[TMC] Strike captured: {market.asset}=${price:,.2f} "
                        f"for '{market.question[:50]}'"
                    )

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

    def _save_shadow_entry(
        self,
        cid: str,
        market,
        profile,
        final_price: float | None,
        outcome: str | None,
        was_traded: bool,
        skipped_signals: list[dict],
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "condition_id": cid,
            "question": market.question,
            "asset": market.asset,
            "strike_price": market.strike_price,
            "final_price": final_price,
            "outcome": outcome,
            "was_traded": was_traded,
            "total_snapshots": len(profile.snapshots) if profile else 0,
            "tight_ratio": profile.tight_ratio if profile else None,
            "final_yes": profile.current_yes if profile else None,
            "final_no": profile.current_no if profile else None,
            "skipped_signals": skipped_signals,
        }

        SHADOW_FILE.parent.mkdir(parents=True, exist_ok=True)
        entries = []
        if SHADOW_FILE.exists():
            try:
                entries = json.loads(SHADOW_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                entries = []

        entries.append(entry)
        SHADOW_FILE.write_text(json.dumps(entries, indent=2))
        logger.info(
            f"[TMC] Shadow logged: {market.asset} '{market.question[:40]}' | "
            f"outcome={outcome} traded={was_traded} skips={len(skipped_signals)}"
        )
