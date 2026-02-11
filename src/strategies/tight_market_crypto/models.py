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
    amount_per_side: float  # USD amount per order (e.g. $1)
    total_cost: float  # amount_per_side * 2
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class TightMarketTradeResult:
    opportunity: TightMarketOpportunity
    success: bool
    order_ids: list[str] = field(default_factory=list)
    cost: float = 0.0
    error: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
