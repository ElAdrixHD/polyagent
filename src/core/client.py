import json
import logging
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderBookSummary

from .config import Config
from .models import MarketInfo

logger = logging.getLogger("polyagent")

GAMMA_API_URL = "https://gamma-api.polymarket.com"


class PolymarketClient:
    def __init__(self, config: Config):
        self.config = config
        self._init_clob_client()

    def _init_clob_client(self) -> None:
        cfg = self.config
        sig_type = 1 if cfg.wallet_mode == "proxy" else 0
        funder = cfg.proxy_wallet_address if cfg.wallet_mode == "proxy" else None

        self.clob = ClobClient(
            cfg.polymarket_host,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            signature_type=sig_type,
            funder=funder,
        )

        if cfg.has_api_credentials:
            self.clob.set_api_creds(
                self.clob.create_or_derive_api_creds()
            )
            logger.info("Using existing API credentials")
        else:
            creds = self.clob.create_or_derive_api_creds()
            self.clob.set_api_creds(creds)
            logger.info("Auto-generated API credentials")

    def get_active_markets(self) -> list[MarketInfo]:
        """Fetch ALL active markets from Gamma API using offset pagination."""
        markets: list[MarketInfo] = []
        offset = 0
        limit = 100

        while True:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if self.config.only_active_markets:
                params["active"] = "true"
                params["closed"] = "false"

            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/markets", params=params, timeout=15
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"Gamma API error: {e}")
                break

            items = resp.json()
            if not isinstance(items, list) or not items:
                break

            for m in items:
                market = self._parse_market(m)
                if market:
                    markets.append(market)

            if len(items) < limit:
                break
            offset += limit

        logger.info(f"Fetched {len(markets)} active markets from Gamma API")
        return markets

    def get_candidate_markets(self, max_sum: float = 0.995) -> list[MarketInfo]:
        """Fetch markets and pre-filter using Gamma outcomePrices.

        Only returns markets where YES + NO < max_sum, avoiding
        the need to query CLOB order books for obviously non-arbitrageable markets.
        This scans all ~27K markets via Gamma (fast, no auth) and returns
        only the handful worth checking on-chain.
        """
        candidates: list[MarketInfo] = []
        offset = 0
        limit = 100
        total_scanned = 0

        while True:
            params: dict[str, Any] = {"limit": limit, "offset": offset}
            if self.config.only_active_markets:
                params["active"] = "true"
                params["closed"] = "false"

            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/markets", params=params, timeout=15
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"Gamma API error at offset {offset}: {e}")
                break

            items = resp.json()
            if not isinstance(items, list) or not items:
                break

            for m in items:
                total_scanned += 1
                market = self._parse_market(m)
                if not market:
                    continue

                # Pre-filter: check outcomePrices from Gamma
                if len(market.outcome_prices) == 2:
                    gamma_sum = market.outcome_prices[0] + market.outcome_prices[1]
                    if gamma_sum < max_sum:
                        candidates.append(market)

            if len(items) < limit:
                break
            offset += limit

        logger.info(
            f"Scanned {total_scanned} markets, "
            f"found {len(candidates)} candidates (sum < {max_sum})"
        )
        return candidates

    def _parse_market(self, m: dict) -> MarketInfo | None:
        raw_tokens = m.get("clobTokenIds")
        if not raw_tokens:
            return None

        if isinstance(raw_tokens, str):
            try:
                tokens = json.loads(raw_tokens)
            except json.JSONDecodeError:
                return None
        else:
            tokens = raw_tokens

        if not isinstance(tokens, list) or len(tokens) != 2:
            return None

        liquidity = float(m.get("liquidity", 0) or 0)
        if liquidity < self.config.min_market_liquidity:
            return None

        # Parse outcomePrices (JSON-encoded string)
        outcome_prices: list[float] = []
        raw_prices = m.get("outcomePrices", "")
        if raw_prices:
            try:
                parsed = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                outcome_prices = [float(p) for p in parsed]
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        return MarketInfo(
            condition_id=m.get("conditionId", m.get("id", "")),
            question=m.get("question", "Unknown"),
            token_ids=tokens,
            volume=float(m.get("volume", 0) or 0),
            liquidity=liquidity,
            end_date=m.get("endDate", m.get("end_date_iso", "")),
            active=m.get("active", True),
            outcome_prices=outcome_prices,
        )

    def get_order_book(self, token_id: str) -> OrderBookSummary | None:
        try:
            return self.clob.get_order_book(token_id)
        except Exception as e:
            logger.debug(f"Order book error for {token_id}: {e}")
            return None

    def get_best_ask(self, token_id: str) -> float | None:
        book = self.get_order_book(token_id)
        if not book or not book.asks:
            return None
        # Asks come sorted descending (highest first), best ask = lowest price
        return min(float(a.price) for a in book.asks)
