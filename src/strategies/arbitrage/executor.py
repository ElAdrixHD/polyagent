import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from py_clob_client.clob_types import OrderArgs, OrderType

from src.core.client import PolymarketClient
from src.core.config import Config
from src.core.models import ArbitrageOpportunity, TradeResult

from .analyzer import LLMAnalyzer

logger = logging.getLogger("polyagent")

TRADES_FILE = Path("data/trades.json")


class TradeExecutor:
    def __init__(
        self,
        client: PolymarketClient,
        analyzer: LLMAnalyzer,
        config: Config,
    ):
        self.client = client
        self.analyzer = analyzer
        self.config = config
        self._total_exposure = 0.0
        self._daily_loss = 0.0
        self._daily_reset = datetime.now(timezone.utc).date()
        self._killed = False
        self._lock = threading.Lock()

    def execute(self, opportunity: ArbitrageOpportunity) -> TradeResult:
        with self._lock:
            return self._execute_inner(opportunity)

    def _execute_inner(self, opportunity: ArbitrageOpportunity) -> TradeResult:
        if self._killed:
            return TradeResult(
                opportunity=opportunity,
                analysis=None,
                success=False,
                error="Kill switch activated - max daily loss reached",
            )

        self._maybe_reset_daily()

        # Risk checks
        if self._daily_loss >= self.config.max_daily_loss:
            self._killed = True
            logger.critical(
                f"KILL SWITCH: Daily loss ${self._daily_loss:.2f} >= "
                f"${self.config.max_daily_loss:.2f}"
            )
            return TradeResult(
                opportunity=opportunity,
                analysis=None,
                success=False,
                error="Kill switch - max daily loss",
            )

        trade_cost = opportunity.size * (
            opportunity.yes_price + opportunity.no_price
        )
        if self._total_exposure + trade_cost > self.config.max_total_exposure:
            logger.warning(
                f"Skipping: would exceed max exposure "
                f"(${self._total_exposure:.2f} + ${trade_cost:.2f} > "
                f"${self.config.max_total_exposure:.2f})"
            )
            return TradeResult(
                opportunity=opportunity,
                analysis=None,
                success=False,
                error="Would exceed max total exposure",
            )

        # LLM validation
        analysis = self.analyzer.validate(opportunity)
        if not analysis.safe:
            logger.warning(
                f"LLM rejected: {analysis.reason}"
            )
            result = TradeResult(
                opportunity=opportunity,
                analysis=analysis,
                success=False,
                error=f"LLM rejected: {analysis.reason}",
            )
            self._save_trade(result)
            return result

        # Dry run
        if self.config.dry_run:
            logger.info(
                f"[DRY RUN] Would buy YES@{opportunity.yes_price:.4f} + "
                f"NO@{opportunity.no_price:.4f} = "
                f"{opportunity.profit:+.2%} profit, "
                f"size={opportunity.size:.2f}"
            )
            result = TradeResult(
                opportunity=opportunity,
                analysis=analysis,
                success=True,
                cost=trade_cost,
                profit=opportunity.size * opportunity.profit,
            )
            self._save_trade(result)
            return result

        # Live execution
        return self._execute_live(opportunity, analysis, trade_cost)

    def _execute_live(
        self,
        opp: ArbitrageOpportunity,
        analysis,
        trade_cost: float,
    ) -> TradeResult:
        order_ids = []
        try:
            # Buy YES
            yes_order = self.client.clob.create_and_post_order(
                OrderArgs(
                    token_id=opp.token_ids[0],
                    price=opp.yes_price,
                    size=opp.size,
                    side="BUY",
                ),
                OrderType.FOK,
            )
            yes_id = yes_order.get("orderID", "")
            order_ids.append(yes_id)
            logger.info(f"YES order placed: {yes_id}")

            # Buy NO
            no_order = self.client.clob.create_and_post_order(
                OrderArgs(
                    token_id=opp.token_ids[1],
                    price=opp.no_price,
                    size=opp.size,
                    side="BUY",
                ),
                OrderType.FOK,
            )
            no_id = no_order.get("orderID", "")
            order_ids.append(no_id)
            logger.info(f"NO order placed: {no_id}")

            self._total_exposure += trade_cost
            expected_profit = opp.size * opp.profit

            result = TradeResult(
                opportunity=opp,
                analysis=analysis,
                success=True,
                order_ids=order_ids,
                cost=trade_cost,
                profit=expected_profit,
            )
            logger.info(
                f"Trade executed: cost=${trade_cost:.2f}, "
                f"expected profit=${expected_profit:.2f}"
            )

        except Exception as e:
            logger.error(f"Trade execution failed: {e}")
            self._daily_loss += trade_cost * 0.1  # Estimate partial loss
            result = TradeResult(
                opportunity=opp,
                analysis=analysis,
                success=False,
                order_ids=order_ids,
                error=str(e),
            )

        self._save_trade(result)
        return result

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset:
            self._daily_loss = 0.0
            self._daily_reset = today
            self._killed = False
            logger.info("Daily loss counter reset")

    def _save_trade(self, result: TradeResult) -> None:
        TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)

        trades = []
        if TRADES_FILE.exists():
            try:
                trades = json.loads(TRADES_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                trades = []

        entry = {
            "timestamp": result.timestamp,
            "market_id": result.opportunity.market_id,
            "question": result.opportunity.question,
            "yes_price": result.opportunity.yes_price,
            "no_price": result.opportunity.no_price,
            "profit_pct": result.opportunity.profit,
            "size": result.opportunity.size,
            "success": result.success,
            "order_ids": result.order_ids,
            "cost": result.cost,
            "profit": result.profit,
            "error": result.error,
            "dry_run": self.config.dry_run,
        }
        if result.analysis:
            entry["llm_safe"] = result.analysis.safe
            entry["llm_risk"] = result.analysis.risk_level
            entry["llm_reason"] = result.analysis.reason

        trades.append(entry)
        TRADES_FILE.write_text(json.dumps(trades, indent=2))
