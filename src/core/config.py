from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass
class Config:
    # Polymarket
    polymarket_host: str
    chain_id: int
    wallet_mode: str
    private_key: str
    proxy_wallet_address: str
    clob_api_key: str
    clob_secret: str
    clob_passphrase: str

    # OpenRouter
    openrouter_api_key: str
    llm_model: str
    llm_enabled: bool

    # Strategy
    min_profit_threshold: float
    max_trade_size: float
    scan_interval: int

    # Filters
    min_market_liquidity: float
    only_active_markets: bool

    # Risk
    max_total_exposure: float
    max_daily_loss: float

    # Mode
    dry_run: bool
    use_websocket: bool
    log_level: str

    # Parallel scanning
    scanner_workers: int
    markets_per_worker: int

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "Config":
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        def _bool(val: str) -> bool:
            return val.strip().lower() in ("true", "1", "yes")

        private_key = os.getenv("PRIVATE_KEY", "")
        if not private_key or private_key == "0x...":
            raise ValueError("PRIVATE_KEY is required in .env")

        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        llm_enabled = _bool(os.getenv("LLM_ENABLED", "true"))
        if llm_enabled and (not openrouter_key or openrouter_key == "sk-or-..."):
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_ENABLED=true"
            )

        return cls(
            polymarket_host=os.getenv(
                "POLYMARKET_HOST", "https://clob.polymarket.com"
            ),
            chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
            wallet_mode=os.getenv("WALLET_MODE", "own"),
            private_key=private_key,
            proxy_wallet_address=os.getenv("PROXY_WALLET_ADDRESS", ""),
            clob_api_key=os.getenv("CLOB_API_KEY", ""),
            clob_secret=os.getenv("CLOB_SECRET", ""),
            clob_passphrase=os.getenv("CLOB_PASSPHRASE", ""),
            openrouter_api_key=openrouter_key,
            llm_model=os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-20250514"),
            llm_enabled=llm_enabled,
            min_profit_threshold=float(
                os.getenv("MIN_PROFIT_THRESHOLD", "0.025")
            ),
            max_trade_size=float(os.getenv("MAX_TRADE_SIZE", "100")),
            scan_interval=int(os.getenv("SCAN_INTERVAL", "10")),
            min_market_liquidity=float(
                os.getenv("MIN_MARKET_LIQUIDITY", "1000")
            ),
            only_active_markets=_bool(
                os.getenv("ONLY_ACTIVE_MARKETS", "true")
            ),
            max_total_exposure=float(
                os.getenv("MAX_TOTAL_EXPOSURE", "1000")
            ),
            max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "50")),
            dry_run=_bool(os.getenv("DRY_RUN", "true")),
            use_websocket=_bool(os.getenv("USE_WEBSOCKET", "true")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            scanner_workers=int(os.getenv("SCANNER_WORKERS", "5")),
            markets_per_worker=int(os.getenv("MARKETS_PER_WORKER", "200")),
        )

    @property
    def has_api_credentials(self) -> bool:
        return bool(
            self.clob_api_key and self.clob_secret and self.clob_passphrase
        )
