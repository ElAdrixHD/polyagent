import logging
import queue
import threading
import time

from src.core.client import PolymarketClient
from src.core.config import Config
from src.core.models import ArbitrageOpportunity, MarketInfo

from .analyzer import LLMAnalyzer
from .executor import TradeExecutor
from .scanner import ArbitrageScanner
from .websocket_feed import WebSocketFeed

logger = logging.getLogger("polyagent")

MARKET_REFRESH_INTERVAL = 300  # 5 minutes
DEDUP_TTL = 60  # seconds


class ArbitrageCoordinator:
    def __init__(self, config: Config):
        self.config = config
        self._queue: queue.Queue[ArbitrageOpportunity] = queue.Queue()
        self._markets: list[MarketInfo] = []
        self._markets_lock = threading.RLock()
        self._dedup: dict[str, float] = {}  # market_id -> expiry timestamp
        self._dedup_lock = threading.Lock()
        self._running = False
        self._threads: list[threading.Thread] = []
        self._ws_feed: WebSocketFeed | None = None

        # Shared executor (thread-safe via its internal lock)
        self._analyzer = LLMAnalyzer(config)
        # Executor uses a dedicated client (only executor thread touches it)
        self._executor_client = PolymarketClient(config)
        self._executor = TradeExecutor(
            self._executor_client, self._analyzer, config
        )

    def start(self) -> None:
        self._running = True

        # Fetch initial market list
        logger.info("ArbitrageCoordinator: fetching initial market list...")
        client_for_fetch = PolymarketClient(self.config)
        with self._markets_lock:
            self._markets = client_for_fetch.get_active_markets()
            self._markets.sort(key=lambda m: m.liquidity, reverse=True)
        total_coverage = self.config.scanner_workers * self.config.markets_per_worker
        logger.info(
            f"ArbitrageCoordinator: starting {self.config.scanner_workers} "
            f"scanner workers x {self.config.markets_per_worker} markets each "
            f"= {total_coverage} markets/cycle "
            f"(of {len(self._markets)} available)"
        )

        # Start executor worker
        t = threading.Thread(target=self._executor_loop, name="ExecutorWorker", daemon=True)
        t.start()
        self._threads.append(t)
        logger.info("ExecutorWorker: started")

        # Start scanner workers — log slice assignments for verification
        for i in range(self.config.scanner_workers):
            s = self._get_slice(i)
            if s:
                logger.info(
                    f"ScannerWorker-{i}: markets[{i * self.config.markets_per_worker}"
                    f"..{i * self.config.markets_per_worker + len(s)}] "
                    f"first='{s[0].question[:40]}' last='{s[-1].question[:40]}'"
                )
            else:
                logger.warning(f"ScannerWorker-{i}: empty slice (not enough markets)")
            t = threading.Thread(
                target=self._scanner_loop,
                args=(i,),
                name=f"ScannerWorker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # Start market refresh thread
        t = threading.Thread(target=self._refresh_loop, name="MarketRefresh", daemon=True)
        t.start()
        self._threads.append(t)

        # Start WebSocket feed
        if self.config.use_websocket:
            self._start_websocket()

    def stop(self) -> None:
        logger.info("ArbitrageCoordinator: shutting down...")
        self._running = False
        if self._ws_feed:
            self._ws_feed.stop()
        # Unblock executor if waiting on queue
        self._queue.put(None)  # type: ignore[arg-type]

    def join(self, timeout: float = 10.0) -> None:
        for t in self._threads:
            t.join(timeout=timeout)

    def _get_slice(self, worker_id: int) -> list[MarketInfo]:
        with self._markets_lock:
            chunk = self.config.markets_per_worker
            start = worker_id * chunk
            end = start + chunk
            return list(self._markets[start:end])

    def _scanner_loop(self, worker_id: int) -> None:
        # Each worker gets its own client (each has its own HTTP session)
        client = PolymarketClient(self.config)
        scanner = ArbitrageScanner(client, self.config)

        while self._running:
            try:
                markets_slice = self._get_slice(worker_id)
                if not markets_slice:
                    time.sleep(self.config.scan_interval)
                    continue

                logger.debug(
                    f"ScannerWorker-{worker_id}: scanning {len(markets_slice)} markets"
                )
                opportunities = scanner.scan_slice(markets_slice)

                if opportunities:
                    logger.info(
                        f"ScannerWorker-{worker_id}: found {len(opportunities)} opportunities"
                    )
                    for opp in opportunities:
                        if not self._running:
                            break
                        if self._try_dedup(opp.market_id):
                            self._queue.put(opp)
                else:
                    logger.info(
                        f"ScannerWorker-{worker_id}: no arbitrage found in {len(markets_slice)} markets"
                    )

            except Exception as e:
                logger.error(f"ScannerWorker-{worker_id} error: {e}")

            # Sleep with early exit check
            for _ in range(self.config.scan_interval):
                if not self._running:
                    break
                time.sleep(1)

    def _executor_loop(self) -> None:
        while self._running:
            try:
                opp = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            # Poison pill for shutdown
            if opp is None:
                break

            try:
                result = self._executor.execute(opp)
                if result.success and not self.config.dry_run:
                    logger.info(f"Trade success: ${result.profit:.2f} profit")
            except Exception as e:
                logger.error(f"ExecutorWorker error: {e}")

    def _refresh_loop(self) -> None:
        while self._running:
            # Sleep first — initial fetch already done in start()
            for _ in range(MARKET_REFRESH_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)

            try:
                logger.info("ArbitrageCoordinator: refreshing market list...")
                client = PolymarketClient(self.config)
                new_markets = client.get_active_markets()
                new_markets.sort(key=lambda m: m.liquidity, reverse=True)
                with self._markets_lock:
                    self._markets = new_markets
                logger.info(
                    f"ArbitrageCoordinator: refreshed {len(new_markets)} markets"
                )
            except Exception as e:
                logger.error(f"Market refresh error: {e}")

    def _try_dedup(self, market_id: str) -> bool:
        """Return True if this market_id is NOT a duplicate (i.e. should be processed)."""
        now = time.time()
        with self._dedup_lock:
            # Clean expired entries
            expired = [k for k, v in self._dedup.items() if v < now]
            for k in expired:
                del self._dedup[k]

            if market_id in self._dedup:
                return False
            self._dedup[market_id] = now + DEDUP_TTL
            return True

    def _get_covered_markets(self) -> list[MarketInfo]:
        """Return only the markets covered by scanner workers."""
        with self._markets_lock:
            total_coverage = self.config.scanner_workers * self.config.markets_per_worker
            return list(self._markets[:total_coverage])

    def _start_websocket(self) -> None:
        markets = self._get_covered_markets()

        if not markets:
            logger.warning("No markets for WebSocket feed")
            return

        token_pairs = {}
        for m in markets:
            token_pairs[m.condition_id] = {
                "question": m.question,
                "yes_token": m.token_ids[0],
                "no_token": m.token_ids[1],
                "end_date": m.end_date,
                "volume": m.volume,
                "liquidity": m.liquidity,
            }

        def on_ws_opportunity(opp: ArbitrageOpportunity) -> None:
            if self._try_dedup(opp.market_id):
                self._queue.put(opp)

        logger.info(
            f"WebSocket: subscribing to {len(markets)} markets "
            f"({len(markets) * 2} tokens)"
        )
        try:
            self._ws_feed = WebSocketFeed(self.config, token_pairs, on_ws_opportunity)
            self._ws_feed.start()
        except Exception as e:
            logger.error(f"WebSocket init failed: {e}")
