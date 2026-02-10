import logging

from src.core.client import PolymarketClient
from src.core.config import Config
from src.core.models import ArbitrageOpportunity, MarketInfo

logger = logging.getLogger("polyagent")


class ArbitrageScanner:
    def __init__(self, client: PolymarketClient, config: Config):
        self.client = client
        self.config = config

    def scan_slice(self, markets: list[MarketInfo]) -> list[ArbitrageOpportunity]:
        """Scan a slice of markets against real CLOB order books."""
        opportunities: list[ArbitrageOpportunity] = []
        num_markets = len(markets)

        for i, market in enumerate(markets):
            if (i + 1) % 50 == 0 or i == 0:
                logger.info(f"Scanning market {i + 1}/{num_markets}...")

            yes_token, no_token = market.token_ids[0], market.token_ids[1]

            yes_ask = self.client.get_best_ask(yes_token)
            no_ask = self.client.get_best_ask(no_token)

            if yes_ask is None or no_ask is None:
                continue

            total = yes_ask + no_ask
            if total >= 1.0:
                continue

            profit = 1.0 - total

            if profit < self.config.min_profit_threshold:
                continue

            size = min(self.config.max_trade_size, market.liquidity * 0.01)

            opportunities.append(
                ArbitrageOpportunity(
                    market_id=market.condition_id,
                    question=market.question,
                    token_ids=market.token_ids,
                    yes_price=yes_ask,
                    no_price=no_ask,
                    profit=profit,
                    size=size,
                    end_date=market.end_date,
                    volume=market.volume,
                    liquidity=market.liquidity,
                )
            )

        opportunities.sort(key=lambda o: o.profit, reverse=True)
        return opportunities
