from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CryptoMarket:
    condition_id: str
    question: str
    token_ids: list[str]  # [yes_token, no_token]
    end_date: datetime
    asset: str  # BTC, ETH, SOL, XRP
    volume: float = 0.0
    liquidity: float = 0.0
    start_date: datetime | None = None  # When the 15-min window opens
    strike_price: float | None = None  # Chainlink price captured at start_date


@dataclass
class OddsSnapshot:
    timestamp: float  # time.time()
    yes_price: float
    no_price: float

    @property
    def spread(self) -> float:
        return abs(self.yes_price - 0.5)


@dataclass
class TightnessProfile:
    market: CryptoMarket
    snapshots: list[OddsSnapshot]
    tight_ratio: float  # fraction of snapshots within threshold
    avg_spread: float
    current_yes: float
    current_no: float
    seconds_remaining: float


@dataclass
class TightMarketOpportunity:
    market: CryptoMarket
    profile: TightnessProfile
    yes_ask: float
    no_ask: float
    buy_side: str  # "YES" or "NO" (the favorite — model confirms majority)
    buy_token_id: str
    buy_ask: float
    amount: float  # USD total bet
    total_cost: float  # = amount (single side)
    strike_price: float  # captured at market open
    current_crypto_price: float  # Chainlink price at signal time
    # Black-Scholes model fields
    model_prob: float  # N(d₂) probability
    market_prob: float  # market implied probability for our bet side
    edge: float  # model_prob - market_prob
    volatility: float  # σ used in calculation
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TightMarketTradeResult:
    opportunity: TightMarketOpportunity
    success: bool
    order_ids: list[str] = field(default_factory=list)
    cost: float = 0.0
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
