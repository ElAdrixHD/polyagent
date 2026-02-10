from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    token_ids: list[str]
    volume: float
    liquidity: float
    end_date: str
    active: bool = True
    outcome_prices: list[float] = field(default_factory=list)  # [yes_price, no_price] from Gamma


@dataclass
class ArbitrageOpportunity:
    market_id: str
    question: str
    token_ids: list[str]
    yes_price: float
    no_price: float
    profit: float
    size: float
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    end_date: str = ""
    volume: float = 0.0
    liquidity: float = 0.0


@dataclass
class LLMAnalysis:
    safe: bool
    risk_level: str  # low, medium, high
    reason: str
    model_used: str


@dataclass
class TradeResult:
    opportunity: ArbitrageOpportunity
    analysis: Optional[LLMAnalysis]
    success: bool
    order_ids: list[str] = field(default_factory=list)
    cost: float = 0.0
    profit: float = 0.0
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
