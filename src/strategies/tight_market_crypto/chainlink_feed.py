import json
import logging
import math
import threading
import time
from collections import deque

import websocket

logger = logging.getLogger("polyagent")

CHAINLINK_WS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIBE_TOPIC = "crypto_prices_chainlink"
RESPONSE_TOPIC = "crypto_prices"

# Map canonical asset name to Chainlink symbol
ASSET_TO_SYMBOL = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
}
SYMBOL_TO_ASSET = {v: k for k, v in ASSET_TO_SYMBOL.items()}

MAX_HISTORY = 1800  # ~30 minutes at 1 update/sec
POLL_INTERVAL = 0.5  # Re-subscribe every N seconds to get fresh data


class ChainlinkPriceFeed:
    """Real-time crypto price feed from Polymarket's Chainlink RTDS WebSocket.

    Uses the exact same Chainlink data streams that Polymarket uses for
    market resolution, eliminating price source mismatch.

    The RTDS WS sends a batch of ~59 data points on each subscribe call
    but does not stream continuously. We re-subscribe periodically to
    maintain a live price feed.
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}  # asset -> latest price
        self._history: dict[str, deque[tuple[float, float]]] = {
            asset: deque(maxlen=MAX_HISTORY) for asset in ASSET_TO_SYMBOL
        }
        self._seen_ts: dict[str, set[int]] = {
            asset: set() for asset in ASSET_TO_SYMBOL
        }
        self._lock = threading.Lock()
        self._running = False
        self._ws_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None
        self._ws: websocket.WebSocketApp | None = None
        self._ws_ready = threading.Event()
        self._last_log = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._ws_loop, name="Chainlink-WS", daemon=True
        )
        self._ws_thread.start()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="Chainlink-Poll", daemon=True
        )
        self._poll_thread.start()
        logger.info("[TMC] Chainlink price feed starting...")

    def stop(self) -> None:
        self._running = False
        self._ws_ready.set()  # unblock poll loop
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_price(self, asset: str) -> float | None:
        with self._lock:
            return self._prices.get(asset)

    def get_price_at(self, asset: str, target_ts: float) -> float | None:
        """Return the price closest to target_ts from history.

        Falls back to latest price if no history is available.
        """
        with self._lock:
            hist = self._history.get(asset)
            if not hist:
                return self._prices.get(asset)

            best_price = None
            best_diff = float("inf")
            for ts, px in hist:
                diff = abs(ts - target_ts)
                if diff < best_diff:
                    best_diff = diff
                    best_price = px

            # If closest point is more than 60s away, not reliable
            if best_price is not None and best_diff <= 60:
                return best_price

            return self._prices.get(asset)

    def get_volatility(self, asset: str, window_seconds: int = 300) -> float | None:
        """Compute stddev of 1-second log-returns over the given window.

        Returns None if insufficient data (< 10 data points in window).
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            hist = self._history.get(asset)
            if not hist:
                return None
            points = [(ts, px) for ts, px in hist if ts >= cutoff]

        if len(points) < 10:
            return None

        # Compute log-returns between consecutive points
        returns = []
        for i in range(1, len(points)):
            prev_px = points[i - 1][1]
            curr_px = points[i][1]
            if prev_px > 0:
                returns.append(math.log(curr_px / prev_px))

        if len(returns) < 5:
            return None

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        return math.sqrt(variance)

    def get_expected_move(
        self, asset: str, seconds_remaining: float, window_seconds: int = 300
    ) -> float | None:
        """Expected $ move = volatility * current_price * sqrt(seconds_remaining)."""
        vol = self.get_volatility(asset, window_seconds)
        if vol is None:
            return None

        price = self.get_price(asset)
        if price is None or price <= 0:
            return None

        return vol * price * math.sqrt(max(0, seconds_remaining))

    def has_price_crossed(
        self, asset: str, strike: float, since_ts: float
    ) -> bool:
        """Check if the price has been on both sides of strike since since_ts."""
        now = time.time()
        with self._lock:
            hist = self._history.get(asset)
            if not hist:
                return False
            points = [px for ts, px in hist if ts >= since_ts and ts <= now]

        if len(points) < 2:
            return False

        seen_above = False
        seen_below = False
        for px in points:
            if px > strike:
                seen_above = True
            elif px < strike:
                seen_below = True
            if seen_above and seen_below:
                return True
        return False

    def get_price_history(
        self, asset: str, start_ts: float, end_ts: float
    ) -> list[tuple[float, float]]:
        """Return list of (timestamp, price) between start_ts and end_ts."""
        with self._lock:
            hist = self._history.get(asset)
            if not hist:
                return []
            return [(ts, px) for ts, px in hist if start_ts <= ts <= end_ts]

    # --- WebSocket internals ---

    def _ws_loop(self) -> None:
        while self._running:
            self._ws_ready.clear()
            try:
                self._connect()
            except Exception as e:
                logger.error(f"[TMC] Chainlink WS error: {e}")
            self._ws_ready.clear()
            if self._running:
                time.sleep(2)

    def _poll_loop(self) -> None:
        """Periodically re-subscribe to get fresh Chainlink prices."""
        while self._running:
            self._ws_ready.wait(timeout=10)
            if not self._running:
                break
            time.sleep(POLL_INTERVAL)
            if self._running and self._ws:
                try:
                    self._send_subscriptions(self._ws)
                except Exception:
                    pass

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            CHAINLINK_WS_URL,
            on_open=lambda ws: self._on_open(ws),
            on_message=lambda ws, msg: self._on_message(msg),
            on_error=lambda ws, err: logger.debug(f"[TMC] Chainlink WS error: {err}"),
            on_close=lambda ws, code, msg: logger.debug(
                f"[TMC] Chainlink WS closed: {code} {msg}"
            ),
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        logger.info("[TMC] Chainlink WS connected, subscribing...")
        self._send_subscriptions(ws)
        self._ws_ready.set()

    def _send_subscriptions(self, ws) -> None:
        for symbol in ASSET_TO_SYMBOL.values():
            sub_msg = json.dumps({
                "action": "subscribe",
                "subscriptions": [{
                    "topic": SUBSCRIBE_TOPIC,
                    "type": "*",
                    "filters": json.dumps({"symbol": symbol}),
                }],
            })
            ws.send(sub_msg)

    def _on_message(self, message: str) -> None:
        if not message or not message.strip():
            return

        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Response topic is "crypto_prices" (not the subscribe topic)
        if data.get("topic") != RESPONSE_TOPIC:
            return

        payload = data.get("payload")
        if not payload:
            return

        symbol = payload.get("symbol", "")
        asset = SYMBOL_TO_ASSET.get(symbol)
        if not asset:
            return

        # Payload contains a "data" array of {timestamp, value} objects
        points = payload.get("data")
        if not isinstance(points, list) or not points:
            return

        new_count = 0
        with self._lock:
            seen = self._seen_ts[asset]
            for point in points:
                try:
                    ts_ms = int(point["timestamp"])
                    price = float(point["value"])
                except (KeyError, ValueError, TypeError):
                    continue

                if price <= 0:
                    continue

                # Deduplicate by timestamp (ms precision)
                if ts_ms in seen:
                    continue
                seen.add(ts_ms)

                ts_sec = ts_ms / 1000.0
                self._prices[asset] = price
                self._history[asset].append((ts_sec, price))
                new_count += 1

            # Prune seen_ts to avoid memory growth (keep last 5 min)
            if len(seen) > 600:
                cutoff_ms = int((time.time() - 300) * 1000)
                to_remove = {t for t in seen if t < cutoff_ms}
                seen -= to_remove

        # Log prices every 30 seconds
        wall_now = time.time()
        if wall_now - self._last_log >= 30:
            self._last_log = wall_now
            with self._lock:
                parts = []
                for a in ("BTC", "ETH", "SOL", "XRP"):
                    p = self._prices.get(a)
                    if p is not None:
                        parts.append(f"{a}=${p:,.2f}")
                    hist = self._history.get(a)
                    hist_len = len(hist) if hist else 0
                    parts.append(f"({hist_len}pts)")
            if parts:
                logger.info(f"[TMC] Chainlink: {' '.join(parts)}")
