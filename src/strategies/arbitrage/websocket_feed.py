import json
import logging
import threading
import time
from typing import Callable

import websocket

from src.core.config import Config
from src.core.models import ArbitrageOpportunity

logger = logging.getLogger("polyagent")

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketFeed:
    def __init__(
        self,
        config: Config,
        token_pairs: dict[str, dict],
        on_opportunity: Callable[[ArbitrageOpportunity], None],
    ):
        """
        token_pairs: {condition_id: {"question": str, "yes_token": str, "no_token": str, ...}}
        """
        self.config = config
        self.token_pairs = token_pairs
        self.on_opportunity = on_opportunity
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._books: dict[str, dict[str, float | None]] = {}

        # Index token_id -> condition_id for fast lookup
        self._token_to_market: dict[str, str] = {}
        for cid, info in token_pairs.items():
            self._token_to_market[info["yes_token"]] = cid
            self._token_to_market[info["no_token"]] = cid
            self._books[cid] = {"yes": None, "no": None}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("WebSocket feed started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("WebSocket feed stopped")

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            if self._running:
                logger.info("WebSocket reconnecting in 5s...")
                time.sleep(5)

    def _connect(self) -> None:
        all_tokens = list(self._token_to_market.keys())
        if not all_tokens:
            logger.warning("No tokens to subscribe to")
            return

        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=lambda ws: self._on_open(ws, all_tokens),
            on_message=lambda ws, msg: self._on_message(msg),
            on_error=lambda ws, err: logger.error(f"WS error: {err}"),
            on_close=lambda ws, code, msg: logger.info(
                f"WS closed: {code} {msg}"
            ),
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws: websocket.WebSocket, tokens: list[str]) -> None:
        # Subscribe in batches of 50
        batch_size = 50
        for i in range(0, len(tokens), batch_size):
            batch = tokens[i : i + batch_size]
            msg = {
                "type": "subscribe",
                "channel": "book",
                "assets_ids": batch,
            }
            ws.send(json.dumps(msg))
        logger.info(f"Subscribed to {len(tokens)} token feeds")

    def _on_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Messages can be a single dict or a list of updates
        if isinstance(data, list):
            for item in data:
                self._process_book_update(item)
        elif isinstance(data, dict):
            self._process_book_update(data)

    def _process_book_update(self, data: dict) -> None:
        if not isinstance(data, dict):
            return

        asset_id = data.get("asset_id")
        if not asset_id or asset_id not in self._token_to_market:
            return

        condition_id = self._token_to_market[asset_id]
        info = self.token_pairs[condition_id]

        # Extract best ask from book update
        asks = data.get("asks", [])
        best_ask = None
        if asks and isinstance(asks[0], dict):
            best_ask = float(asks[0].get("price", 0))
        elif asks and isinstance(asks[0], (list, tuple)):
            best_ask = float(asks[0][0])

        if asset_id == info["yes_token"]:
            self._books[condition_id]["yes"] = best_ask
        elif asset_id == info["no_token"]:
            self._books[condition_id]["no"] = best_ask

        self._check_opportunity(condition_id)

    def _check_opportunity(self, condition_id: str) -> None:
        book = self._books.get(condition_id)
        if not book:
            return

        yes_ask = book.get("yes")
        no_ask = book.get("no")
        if yes_ask is None or no_ask is None:
            return

        total = yes_ask + no_ask
        if total >= 1.0:
            return

        profit = 1.0 - total
        if profit < self.config.min_profit_threshold:
            return

        info = self.token_pairs[condition_id]
        opp = ArbitrageOpportunity(
            market_id=condition_id,
            question=info.get("question", ""),
            token_ids=[info["yes_token"], info["no_token"]],
            yes_price=yes_ask,
            no_price=no_ask,
            profit=profit,
            size=min(self.config.max_trade_size, 10),
            end_date=info.get("end_date", ""),
            volume=info.get("volume", 0),
            liquidity=info.get("liquidity", 0),
        )

        logger.info(
            f"[WS] Opportunity: {profit:+.1%} on '{opp.question[:50]}...'"
        )
        self.on_opportunity(opp)
