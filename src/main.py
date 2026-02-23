import signal
import sys
import threading

from colorama import Fore, Style

from src.core.config import Config
from src.core.logger import setup_logger
from src.strategies.arbitrage import ArbitrageCoordinator
from src.strategies.tight_market_crypto import TightMarketCryptoCoordinator

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

    # Initialize strategies
    arb_coordinator: ArbitrageCoordinator | None = None
    tmc_coordinator: TightMarketCryptoCoordinator | None = None

    if config.arbitrage_enabled:
        logger.info(f"Arbitrage: {Fore.GREEN}enabled{Style.RESET_ALL}")
        arb_coordinator = ArbitrageCoordinator(config)
    else:
        logger.info(f"Arbitrage: {Fore.RED}disabled{Style.RESET_ALL}")

    if config.tmc_enabled:
        logger.info(f"Tight Market Crypto: {Fore.GREEN}enabled{Style.RESET_ALL}")
        logger.info(f"  Max investment: ${config.tmc_max_investment:.2f}")
        logger.info(f"  Entry window: {config.tmc_entry_window:.1f}s")
        logger.info(f"  Execution window: {config.tmc_execution_window:.1f}s")
        logger.info(f"  Min edge: {config.tmc_min_edge}")
        logger.info(f"  Min ask: {config.tmc_min_ask}")
        logger.info(f"  Assets: {config.tmc_crypto_assets}")
        logger.info(f"  Max daily loss: ${config.tmc_max_daily_loss:.2f}")
        if config.tmc_asset_overrides:
            for asset in sorted(config.tmc_asset_overrides):
                ov = config.tmc_asset_overrides[asset]
                parts = []
                if "min_vol" in ov:
                    parts.append(f"vol≥{ov['min_vol']}")
                if "min_edge" in ov:
                    parts.append(f"edge≥{ov['min_edge']}")
                logger.info(f"  {asset} override: {', '.join(parts)}")
        tmc_coordinator = TightMarketCryptoCoordinator(config)
    else:
        logger.info(f"Tight Market Crypto: {Fore.RED}disabled{Style.RESET_ALL}")

    if not arb_coordinator and not tmc_coordinator:
        logger.error("No strategies enabled. Set ARBITRAGE_ENABLED=true or TMC_ENABLED=true")
        sys.exit(1)

    stop_event = threading.Event()

    def shutdown(sig, frame):
        if stop_event.is_set():
            logger.info("Force exit.")
            sys.exit(1)
        logger.info("Shutting down (Ctrl+C again to force)...")
        if arb_coordinator:
            arb_coordinator.stop()
        if tmc_coordinator:
            tmc_coordinator.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if arb_coordinator:
        arb_coordinator.start()
    if tmc_coordinator:
        tmc_coordinator.start()

    # Block main thread until shutdown signal
    while not stop_event.is_set():
        stop_event.wait(timeout=1)
    if arb_coordinator:
        arb_coordinator.join(timeout=5)
    if tmc_coordinator:
        tmc_coordinator.join(timeout=5)
    logger.info("Polyagent stopped.")


if __name__ == "__main__":
    main()
