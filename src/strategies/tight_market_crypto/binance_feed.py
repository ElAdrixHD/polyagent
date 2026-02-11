import json
import logging
import math
import threading
import time
from collections import deque

import websocket

logger = logging.getLogger("polyagent")

# Binance miniTicker streams for supported assets
BINANCE_WS_URL = (
    "wss://stream.binance.com:9443/ws/"
    "btcusdt@miniTicker/ethusdt@miniTicker/"
    "solusdt@miniTicker/xrpusdt@miniTicker"
)

# Map Binance symbol to canonical asset name
SYMBOL_TO_ASSET = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
}

MAX_HISTORY = 900  # ~15 minutes at 1 update/sec


class BinancePriceFeed:
    """Real-time crypto price feed from Binance WebSocket.

    Tracks latest price and recent history per asset for volatility calculations.
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}  # asset -> latest price
        self._history: dict[str, deque[tuple[float, float]]] = {
            asset: deque(maxlen=MAX_HISTORY) for asset in SYMBOL_TO_ASSET.values()
        }
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_log = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._ws_loop, name="Binance-WS", daemon=True
        )
        self._thread.start()
        logger.info("[TMC] Binance price feed starting...")

    def stop(self) -> None:
        self._running = False
        if hasattr(self, "_ws") and self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def get_price(self, asset: str) -> float | None:
        with self._lock:
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

    # --- WebSocket internals ---

    def _ws_loop(self) -> None:
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"[TMC] Binance WS error: {e}")
            if self._running:
                time.sleep(2)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            BINANCE_WS_URL,
            on_open=lambda ws: logger.info("[TMC] Binance WS connected"),
            on_message=lambda ws, msg: self._on_message(msg),
            on_error=lambda ws, err: logger.debug(f"[TMC] Binance WS error: {err}"),
            on_close=lambda ws, code, msg: logger.debug(
                f"[TMC] Binance WS closed: {code} {msg}"
            ),
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        symbol = data.get("s", "")  # e.g. "BTCUSDT"
        asset = SYMBOL_TO_ASSET.get(symbol)
        if not asset:
            return

        try:
            price = float(data.get("c", 0))  # "c" = close price in miniTicker
        except (ValueError, TypeError):
            return

        if price <= 0:
            return

        now = time.time()
        with self._lock:
            self._prices[asset] = price
            self._history[asset].append((now, price))

        # Log prices every 30 seconds
        if now - self._last_log >= 30:
            self._last_log = now
            with self._lock:
                parts = []
                for a in ("BTC", "ETH", "SOL", "XRP"):
                    p = self._prices.get(a)
                    if p is not None:
                        parts.append(f"{a}=${p:,.2f}")
            if parts:
                logger.info(f"[TMC] Binance: {' '.join(parts)}")
