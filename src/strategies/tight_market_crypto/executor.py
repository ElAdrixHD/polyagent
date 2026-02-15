import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from py_clob_client.clob_types import MarketOrderArgs, OrderType

from src.core.client import PolymarketClient
from src.core.config import Config

from .models import TightMarketOpportunity, TightMarketTradeResult

logger = logging.getLogger("polyagent")

TRADES_FILE = Path("data/tight_market_crypto_trades.json")


class TightMarketCryptoExecutor:
    def __init__(self, client: PolymarketClient, config: Config):
        self.client = client
        self.config = config
        self._daily_loss = 0.0
        self._daily_reset = datetime.now(timezone.utc).date()
        self._killed = False
        self._lock = threading.Lock()

    def execute(self, opp: TightMarketOpportunity) -> TightMarketTradeResult:
        with self._lock:
            return self._execute_inner(opp)

    def _execute_inner(self, opp: TightMarketOpportunity) -> TightMarketTradeResult:
        if self._killed:
            return TightMarketTradeResult(
                opportunity=opp,
                success=False,
                error="Kill switch activated - max daily loss reached",
            )

        self._maybe_reset_daily()

        if self._daily_loss >= self.config.tmc_max_daily_loss:
            self._killed = True
            logger.critical(
                f"[TMC] KILL SWITCH: Daily loss ${self._daily_loss:.2f} >= "
                f"${self.config.tmc_max_daily_loss:.2f}"
            )
            return TightMarketTradeResult(
                opportunity=opp,
                success=False,
                error="Kill switch - max daily loss",
            )

        if self.config.dry_run:
            logger.info(
                f"[TMC] [DRY RUN] Would buy "
                f"{opp.buy_side}@{opp.buy_ask:.4f} ${opp.amount:.2f} "
                f"on '{opp.market.question[:50]}' | "
                f"strike=${opp.strike_price:,.2f} dist=${opp.distance:.2f} "
                f"expected_move=${opp.expected_move:.2f}"
            )
            result = TightMarketTradeResult(
                opportunity=opp,
                success=True,
                cost=opp.total_cost,
            )
            self._save_trade(result)
            return result

        return self._execute_live(opp)

    def _execute_live(self, opp: TightMarketOpportunity) -> TightMarketTradeResult:
        order_ids = []
        asset = opp.market.asset
        q = opp.market.question[:50]
        logger.info(
            f"[TMC] EXECUTING {asset} '{q}' | "
            f"BUY {opp.buy_side}@${opp.buy_ask:.4f} ${opp.amount:.2f} | "
            f"daily_loss=${self._daily_loss:.2f}"
        )
        try:
            # Buy underdog — single market order
            logger.info(
                f"[TMC] Creating {opp.buy_side} market order: "
                f"token={opp.buy_token_id[:12]}... amount=${opp.amount:.2f}"
            )
            signed = self.client.clob.create_market_order(
                MarketOrderArgs(
                    token_id=opp.buy_token_id,
                    amount=opp.amount,
                    side="BUY",
                    order_type=OrderType.FOK,
                )
            )
            logger.info(f"[TMC] {opp.buy_side} order signed, posting...")
            resp = self.client.clob.post_order(signed, OrderType.FOK)
            order_id = resp.get("orderID", "")
            order_ids.append(order_id)
            logger.info(f"[TMC] {opp.buy_side} order posted: {order_id} | response: {resp}")

            self._daily_loss += opp.total_cost

            result = TightMarketTradeResult(
                opportunity=opp,
                success=True,
                order_ids=order_ids,
                cost=opp.total_cost,
            )
            logger.info(
                f"[TMC] Trade complete: {asset} '{q}' | "
                f"cost=${opp.total_cost:.2f} | daily_loss=${self._daily_loss:.2f}"
            )

        except Exception as e:
            logger.error(f"[TMC] Trade FAILED: {asset} '{q}' | error={e}")
            self._daily_loss += opp.total_cost * 0.5
            result = TightMarketTradeResult(
                opportunity=opp,
                success=False,
                order_ids=order_ids,
                error=str(e),
            )

        self._save_trade(result)
        return result

    def update_outcomes_for_condition(
        self, condition_id: str, outcome: str, final_price: float | None = None
    ) -> None:
        """Enrich log entries for condition_id with resolution outcome and return metrics.

        Uses the Binance final_price vs strike to determine win/loss.
        """
        if not TRADES_FILE.exists():
            return
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return

        changed = False
        for entry in trades:
            if entry.get("condition_id") != condition_id:
                continue
            if entry.get("outcome") is not None:
                continue  # Already filled

            buy_side = entry.get("buy_side", "")
            buy_ask = entry.get("buy_ask", 0)
            amount = entry.get("amount", 0)

            if buy_side == outcome and buy_ask > 0:
                payout = amount / buy_ask
            else:
                payout = 0

            net_return = payout - amount
            return_pct = (net_return / amount * 100) if amount > 0 else 0.0

            entry["outcome"] = outcome
            entry["payout"] = round(payout, 4)
            entry["net_return"] = round(net_return, 4)
            entry["return_pct"] = round(return_pct, 2)
            if final_price is not None:
                entry["final_crypto_price"] = final_price
            changed = True

            logger.info(
                f"[TMC] Outcome recorded: {entry['asset']} '{entry['question'][:50]}' | "
                f"winner={outcome} payout=${payout:.2f} net=${net_return:+.2f} ({return_pct:+.1f}%)"
                f"{f' | final_price=${final_price:,.2f}' if final_price else ''}"
            )

        if changed:
            TRADES_FILE.write_text(json.dumps(trades, indent=2))

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset:
            self._daily_loss = 0.0
            self._daily_reset = today
            self._killed = False
            logger.info("[TMC] Daily loss counter reset")

    def get_traded_condition_ids(self) -> set[str]:
        if not TRADES_FILE.exists():
            return set()
        try:
            trades = json.loads(TRADES_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return set()
        return {t["condition_id"] for t in trades if t.get("condition_id")}

    def _save_trade(self, result: TightMarketTradeResult) -> None:
        TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)

        trades = []
        if TRADES_FILE.exists():
            try:
                trades = json.loads(TRADES_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                trades = []

        entry = {
            "timestamp": result.timestamp,
            "strategy": "tight_market_crypto",
            "condition_id": result.opportunity.market.condition_id,
            "question": result.opportunity.market.question,
            "asset": result.opportunity.market.asset,
            "yes_ask": result.opportunity.yes_ask,
            "no_ask": result.opportunity.no_ask,
            "buy_side": result.opportunity.buy_side,
            "buy_ask": result.opportunity.buy_ask,
            "amount": result.opportunity.amount,
            "total_cost": result.opportunity.total_cost,
            "strike_price": result.opportunity.strike_price,
            "current_crypto_price": result.opportunity.current_crypto_price,
            "distance": result.opportunity.distance,
            "expected_move": result.opportunity.expected_move,
            "tight_ratio": result.opportunity.profile.tight_ratio,
            "avg_spread": result.opportunity.profile.avg_spread,
            "seconds_remaining": result.opportunity.profile.seconds_remaining,
            "success": result.success,
            "order_ids": result.order_ids,
            "cost": result.cost,
            "error": result.error,
            "dry_run": self.config.dry_run,
            # Filled in post-resolution by update_outcomes_for_condition()
            "outcome": None,
            "final_crypto_price": None,
            "payout": None,
            "net_return": None,
            "return_pct": None,
        }

        trades.append(entry)
        TRADES_FILE.write_text(json.dumps(trades, indent=2))
