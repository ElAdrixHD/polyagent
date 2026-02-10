import signal
import sys
import threading

from colorama import Fore, Style

from src.core.config import Config
from src.core.logger import setup_logger
from src.strategies.arbitrage import ArbitrageCoordinator

BANNER = f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║          POLYAGENT - Arbitrage Bot               ║
║        Mathematical Arbitrage on Polymarket      ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
"""


def main() -> None:
    try:
        config = Config.from_env()
    except ValueError as e:
        print(f"{Fore.RED}Config error: {e}{Style.RESET_ALL}")
        sys.exit(1)

    logger = setup_logger(config.log_level)
    print(BANNER)

    mode = "DRY RUN" if config.dry_run else "LIVE"
    logger.info(f"Mode: {Fore.YELLOW}{mode}{Style.RESET_ALL}")
    logger.info(f"Min profit threshold: {config.min_profit_threshold:.1%}")
    logger.info(f"Max trade size: ${config.max_trade_size:.0f}")
    logger.info(f"Scanner workers: {config.scanner_workers} x {config.markets_per_worker} markets")
    logger.info(f"LLM validation: {'enabled' if config.llm_enabled else 'disabled'}")
    logger.info(f"WebSocket: {'enabled' if config.use_websocket else 'disabled'}")

    # Initialize and start coordinator
    coordinator = ArbitrageCoordinator(config)
    stop_event = threading.Event()

    def shutdown(sig, frame):
        if stop_event.is_set():
            # Second Ctrl+C → force exit
            logger.info("Force exit.")
            sys.exit(1)
        logger.info("Shutting down (Ctrl+C again to force)...")
        coordinator.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    coordinator.start()

    # Block main thread until shutdown signal
    while not stop_event.is_set():
        stop_event.wait(timeout=1)
    coordinator.join(timeout=5)
    logger.info("Polyagent stopped.")


if __name__ == "__main__":
    main()
