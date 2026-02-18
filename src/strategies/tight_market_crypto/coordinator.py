import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from src.core.client import PolymarketClient
from src.core.config import Config

from .chainlink_feed import ChainlinkPriceFeed
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
        self._chainlink_feed = ChainlinkPriceFeed()
        self._signal_engine = SignalEngine(
            config, self._tracker, self._client, self._chainlink_feed
        )
        self._executor = TightMarketCryptoExecutor(self._client, config)

        self._last_discovery = 0.0

    def start(self) -> None:
        self._running = True
        assets = self.config.tmc_crypto_assets
        logger.info(f"[TMC] Starting — tracking: {assets}")

        self._tracker.start()
        self._chainlink_feed.start()

        self._thread = threading.Thread(
            target=self._main_loop, name="TMC-Main", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        logger.info("[TMC] Shutting down...")
        self._running = False
        self._tracker.stop()
        self._chainlink_feed.stop()

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

            # Faster polling when markets are close to expiry
            profiles = self._tracker.get_all_profiles()
            min_remaining = min((p.seconds_remaining for p in profiles), default=999)
            if min_remaining <= self.config.tmc_execution_window + 5:
                time.sleep(0.15)
            else:
                time.sleep(0.5)

    def _discover_and_clean(self) -> None:
        now = datetime.now(timezone.utc)

        # Clean expired markets and record outcomes using Chainlink price
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

                # Determine outcome from Chainlink price at window close vs strike
                final_price = None
                outcome = None
                if market.strike_price is not None:
                    end_ts = market.end_date.timestamp()
                    final_price = self._chainlink_feed.get_price_at(
                        market.asset, end_ts
                    )
                    if final_price is not None:
                        outcome = "YES" if final_price > market.strike_price else "NO"
                        self._executor.update_outcomes_for_condition(
                            cid, outcome, final_price
                        )
                        logger.info(
                            f"[TMC] Resolved via Chainlink: {market.asset} "
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
                price = self._chainlink_feed.get_price_at(
                    market.asset, market.start_date.timestamp()
                )
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
                    f"YES={p.current_yes:.3f} NO={p.current_no:.3f}"
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
        exec_window = self.config.tmc_execution_window
        entry_window = self.config.tmc_entry_window
        end_ts = market.end_date.timestamp()
        exec_start_ts = end_ts - exec_window
        entry_start_ts = end_ts - entry_window
        strike = market.strike_price

        # Decimal precision based on asset price magnitude
        decimals = 6 if strike and strike < 10 else (4 if strike and strike < 1000 else 2)

        # Crypto price trail during execution window
        crypto_exec_trail = []
        crypto_entry_trail = []

        raw_exec_history = self._chainlink_feed.get_price_history(
            market.asset, exec_start_ts, end_ts
        )
        raw_entry_history = self._chainlink_feed.get_price_history(
            market.asset, entry_start_ts, end_ts
        )

        if raw_exec_history:
            seen_seconds = set()
            for ts, px in raw_exec_history:
                sec_key = int(ts)
                if sec_key not in seen_seconds:
                    seen_seconds.add(sec_key)
                    trail_entry = {
                        "t": round(end_ts - ts, 1),
                        "price": round(px, decimals),
                    }
                    if strike is not None:
                        trail_entry["dist"] = round(abs(px - strike), decimals)
                    crypto_exec_trail.append(trail_entry)

        if raw_entry_history:
            seen_5s = set()
            for ts, px in raw_entry_history:
                bucket = int(ts) // 5
                if bucket not in seen_5s:
                    seen_5s.add(bucket)
                    crypto_entry_trail.append({
                        "t": round(end_ts - ts, 1),
                        "price": round(px, decimals),
                    })

        # YES/NO odds trail during execution window
        odds_exec_trail = []
        odds_entry_trail = []
        if profile and profile.snapshots:
            seen_seconds_odds = set()
            for snap in profile.snapshots:
                if snap.timestamp >= exec_start_ts:
                    sec_key = int(snap.timestamp)
                    if sec_key not in seen_seconds_odds:
                        seen_seconds_odds.add(sec_key)
                        odds_exec_trail.append({
                            "t": round(end_ts - snap.timestamp, 1),
                            "yes": round(snap.yes_price, 4),
                            "no": round(snap.no_price, 4),
                        })

            seen_5s_odds = set()
            for snap in profile.snapshots:
                if snap.timestamp >= entry_start_ts:
                    bucket = int(snap.timestamp) // 5
                    if bucket not in seen_5s_odds:
                        seen_5s_odds.add(bucket)
                        odds_entry_trail.append({
                            "t": round(end_ts - snap.timestamp, 1),
                            "yes": round(snap.yes_price, 4),
                            "no": round(snap.no_price, 4),
                        })

        # Volatility at expiry
        volatility = self._chainlink_feed.get_volatility(
            market.asset, self.config.tmc_volatility_window
        )

        # Extract model fields from skipped signals (last evaluation)
        model_prob = None
        market_prob = None
        edge = None
        bet_side = None
        if skipped_signals:
            last_skip = skipped_signals[-1]
            model_prob = last_skip.get("model_prob")
            market_prob = last_skip.get("market_prob")
            edge = last_skip.get("edge")
            bet_side = last_skip.get("bet_side")

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "condition_id": cid,
            "question": market.question,
            "asset": market.asset,
            "strike_price": strike,
            "final_price": final_price,
            "outcome": outcome,
            "was_traded": was_traded,
            "total_snapshots": len(profile.snapshots) if profile else 0,
            "final_yes": profile.current_yes if profile else None,
            "final_no": profile.current_no if profile else None,
            # Black-Scholes model fields
            "volatility": round(volatility, 8) if volatility else None,
            "model_prob": model_prob,
            "market_prob": market_prob,
            "edge": edge,
            "bet_side": bet_side,
            # Trails (compact, 1/sec for exec window, 1/5sec for entry window)
            "crypto_price_trail_exec_window": crypto_exec_trail,
            "crypto_price_trail_entry_window": crypto_entry_trail,
            "odds_trail_exec_window": odds_exec_trail,
            "odds_trail_entry_window": odds_entry_trail,
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
            f"outcome={outcome} traded={was_traded} skips={len(skipped_signals)} | "
            f"model_prob={model_prob} edge={edge}"
        )
