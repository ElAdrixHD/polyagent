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
        # --- Build execution window analysis ---
        exec_window = self.config.tmc_execution_window
        entry_window = self.config.tmc_entry_window
        end_ts = market.end_date.timestamp()
        exec_start_ts = end_ts - exec_window
        entry_start_ts = end_ts - entry_window
        strike = market.strike_price

        # Decimal precision based on asset price magnitude
        decimals = 6 if strike and strike < 10 else (4 if strike and strike < 1000 else 2)

        # Crypto price trail during execution window (last N seconds)
        crypto_exec_trail = []
        crypto_entry_trail = []
        price_at_exec_start = None
        price_crossed_strike = False
        min_distance = None
        max_distance = None
        price_momentum = None  # $/sec in last 3 seconds

        raw_exec_history = self._binance_feed.get_price_history(
            market.asset, exec_start_ts, end_ts
        )
        raw_entry_history = self._binance_feed.get_price_history(
            market.asset, entry_start_ts, end_ts
        )

        if raw_exec_history and strike is not None:
            # Sampled trail: one point per second for compactness
            seen_seconds = set()
            for ts, px in raw_exec_history:
                sec_key = int(ts)
                if sec_key not in seen_seconds:
                    seen_seconds.add(sec_key)
                    crypto_exec_trail.append({
                        "t": round(end_ts - ts, 1),  # seconds before expiry
                        "price": round(px, decimals),
                        "dist": round(abs(px - strike), decimals),
                    })

            price_at_exec_start = round(raw_exec_history[0][1], decimals)

            # Strike crossing detection
            prev_side = None
            for _, px in raw_exec_history:
                side = "above" if px > strike else "below"
                if prev_side is not None and side != prev_side:
                    price_crossed_strike = True
                    break
                prev_side = side

            # Min/max distance to strike during execution window
            distances = [abs(px - strike) for _, px in raw_exec_history]
            min_distance = round(min(distances), decimals)
            max_distance = round(max(distances), decimals)

            # Price momentum: avg $/sec over last 3 seconds of data
            last_3s = [(ts, px) for ts, px in raw_exec_history if ts >= end_ts - 3]
            if len(last_3s) >= 2:
                dt = last_3s[-1][0] - last_3s[0][0]
                dp = last_3s[-1][1] - last_3s[0][1]
                price_momentum = round(dp / dt, decimals) if dt > 0 else None

        # Sampled entry-window trail (one per ~5 seconds to keep compact)
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

            # Entry-window odds sampled every ~5 seconds
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

        # Volatility metrics at expiry
        volatility = self._binance_feed.get_volatility(
            market.asset, self.config.tmc_volatility_window
        )
        expected_move_5s = self._binance_feed.get_expected_move(
            market.asset, exec_window, self.config.tmc_volatility_window
        )

        # --- Reversal analysis ---
        # Did outcome flip in last seconds? Compare majority side at exec_start vs final
        reversal_detected = False
        majority_at_exec_start = None
        if odds_exec_trail:
            first = odds_exec_trail[0] if odds_exec_trail else None
            if first:
                majority_at_exec_start = "YES" if first["yes"] > first["no"] else "NO"
            if majority_at_exec_start and outcome:
                reversal_detected = majority_at_exec_start != outcome

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
            "tight_ratio": profile.tight_ratio if profile else None,
            "final_yes": profile.current_yes if profile else None,
            "final_no": profile.current_no if profile else None,
            # --- New: execution window analysis ---
            "volatility": round(volatility, 8) if volatility else None,
            "expected_move_exec_window": round(expected_move_5s, decimals) if expected_move_5s else None,
            "price_at_exec_window_start": price_at_exec_start,
            "price_crossed_strike": price_crossed_strike,
            "min_distance_to_strike": min_distance,
            "max_distance_to_strike": max_distance,
            "price_momentum_last_3s": price_momentum,
            "reversal_detected": reversal_detected,
            "majority_at_exec_start": majority_at_exec_start,
            # --- Trails (compact, 1/sec for exec window, 1/5sec for entry window) ---
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
            f"outcome={outcome} traded={was_traded} skips={len(skipped_signals)}"
        )
