import json
import logging
import threading
import time
from datetime import datetime, timezone

import websocket

from src.core.config import Config

from .models import CryptoMarket, OddsSnapshot, TightnessProfile

logger = logging.getLogger("polyagent")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketTracker:
    """Tracks odds snapshots for a single market."""

    def __init__(self, market: CryptoMarket, tightness_threshold: float):
        self.market = market
        self._threshold = tightness_threshold
        self._snapshots: list[OddsSnapshot] = []
        self._lock = threading.Lock()

    def record(self, yes_price: float, no_price: float) -> None:
        snap = OddsSnapshot(
            timestamp=time.time(),
            yes_price=yes_price,
            no_price=no_price,
        )
        with self._lock:
            self._snapshots.append(snap)

    def get_profile(self) -> TightnessProfile:
        now = time.time()
        end_ts = self.market.end_date.timestamp()
        seconds_remaining = max(0.0, end_ts - now)

        with self._lock:
            snapshots = list(self._snapshots)

        if not snapshots:
            return TightnessProfile(
                market=self.market,
                snapshots=[],
                tight_ratio=0.0,
                avg_spread=1.0,
                current_yes=0.5,
                current_no=0.5,
                seconds_remaining=seconds_remaining,
            )

        tight_count = sum(1 for s in snapshots if s.spread <= self._threshold)
        tight_ratio = tight_count / len(snapshots)
        avg_spread = sum(s.spread for s in snapshots) / len(snapshots)
        latest = snapshots[-1]

        return TightnessProfile(
            market=self.market,
            snapshots=snapshots,
            tight_ratio=tight_ratio,
            avg_spread=avg_spread,
            current_yes=latest.yes_price,
            current_no=latest.no_price,
            seconds_remaining=seconds_remaining,
        )


class TightnessTracker:
    """Manages WebSocket tracking for multiple crypto markets."""

    def __init__(self, config: Config):
        self.config = config
        self._trackers: dict[str, MarketTracker] = {}  # condition_id -> tracker
        self._token_to_market: dict[str, str] = {}  # token_id -> condition_id
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        # Track current prices per condition_id
        self._current_prices: dict[str, dict[str, float | None]] = {}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="TMC-WebSocket", daemon=True
        )
        self._ws_thread.start()
        logger.info("[TMC] TightnessTracker started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("[TMC] TightnessTracker stopped")

    def add_market(self, market: CryptoMarket) -> None:
        with self._lock:
            if market.condition_id in self._trackers:
                return
            tracker = MarketTracker(market, 0.10)
            self._trackers[market.condition_id] = tracker
            self._token_to_market[market.token_ids[0]] = market.condition_id
            self._token_to_market[market.token_ids[1]] = market.condition_id
            self._current_prices[market.condition_id] = {"yes": None, "no": None}
        logger.info(
            f"[TMC] Tracking: {market.asset} '{market.question[:50]}' "
            f"(ends in {(market.end_date - datetime.now(timezone.utc)).total_seconds():.0f}s)"
        )
        self._reconnect_ws()

    def remove_market(self, condition_id: str) -> None:
        removed = False
        with self._lock:
            tracker = self._trackers.pop(condition_id, None)
            if tracker:
                for tid in tracker.market.token_ids:
                    self._token_to_market.pop(tid, None)
                self._current_prices.pop(condition_id, None)
                removed = True
        if removed:
            self._reconnect_ws()

    def get_profile(self, condition_id: str) -> TightnessProfile | None:
        with self._lock:
            tracker = self._trackers.get(condition_id)
        if not tracker:
            return None
        return tracker.get_profile()

    def get_all_profiles(self) -> list[TightnessProfile]:
        with self._lock:
            trackers = list(self._trackers.values())
        return [t.get_profile() for t in trackers]

    def get_tracked_market(self, condition_id: str) -> CryptoMarket | None:
        with self._lock:
            tracker = self._trackers.get(condition_id)
        return tracker.market if tracker else None

    def tracked_condition_ids(self) -> set[str]:
        with self._lock:
            return set(self._trackers.keys())

    def _reconnect_ws(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _ws_loop(self) -> None:
        while self._running:
            with self._lock:
                tokens = list(self._token_to_market.keys())
            if not tokens:
                time.sleep(1)
                continue
            try:
                self._connect(tokens)
            except Exception as e:
                logger.error(f"[TMC] WebSocket error: {e}")
            if self._running:
                time.sleep(2)

    def _connect(self, tokens: list[str]) -> None:
        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=lambda ws: self._on_open(ws, tokens),
            on_message=lambda ws, msg: self._on_message(msg),
            on_error=lambda ws, err: logger.debug(f"[TMC] WS error: {err}"),
            on_close=lambda ws, code, msg: logger.debug(
                f"[TMC] WS closed: {code} {msg}"
            ),
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws: websocket.WebSocket, tokens: list[str]) -> None:
        batch_size = 50
        for i in range(0, len(tokens), batch_size):
            batch = tokens[i : i + batch_size]
            msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": batch,
            }
            ws.send(json.dumps(msg))
        logger.info(f"[TMC] WS subscribed to {len(tokens)} tokens")

    def _on_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        if isinstance(data, list):
            for item in data:
                self._process_update(item)
        elif isinstance(data, dict):
            self._process_update(data)

    def _process_update(self, data: dict) -> None:
        if not isinstance(data, dict):
            return

        asset_id = data.get("asset_id")
        if not asset_id:
            return

        with self._lock:
            condition_id = self._token_to_market.get(asset_id)
            if not condition_id:
                return
            tracker = self._trackers.get(condition_id)
            if not tracker:
                return
            prices = self._current_prices.get(condition_id)
            if prices is None:
                return

        # Extract best (lowest) ask price
        asks = data.get("asks", [])
        best_ask = None
        if asks:
            try:
                if isinstance(asks[0], dict):
                    all_asks = [float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0]
                elif isinstance(asks[0], (list, tuple)):
                    all_asks = [float(a[0]) for a in asks if float(a[0]) > 0]
                else:
                    all_asks = [float(a) for a in asks if float(a) > 0]
                if all_asks:
                    best_ask = min(all_asks)
            except (ValueError, IndexError, TypeError):
                pass

        if best_ask is None:
            return

        # Determine if YES or NO token
        market = tracker.market
        if asset_id == market.token_ids[0]:
            prices["yes"] = best_ask
        elif asset_id == market.token_ids[1]:
            prices["no"] = best_ask

        # Record snapshot when we have both sides
        yes_price = prices.get("yes")
        no_price = prices.get("no")
        if yes_price is not None and no_price is not None:
            tracker.record(yes_price, no_price)
            spread = abs(yes_price - 0.5)
            remaining = max(0.0, market.end_date.timestamp() - time.time())
            # Log every price update when close to expiry, otherwise sparse
            if remaining <= 15:
                logger.info(
                    f"[TMC] WS PRICE {market.asset} '{market.question[:40]}' | "
                    f"YES={yes_price:.3f} NO={no_price:.3f} spread={spread:.4f} | "
                    f"{remaining:.1f}s left"
                )
