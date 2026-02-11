import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from src.core.config import Config

from .models import CryptoMarket

logger = logging.getLogger("polyagent")

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Maps regex patterns to canonical asset symbols
ASSET_PATTERNS: dict[str, re.Pattern] = {
    "BTC": re.compile(r"\b(BTC|Bitcoin)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(ETH|Ethereum)\b", re.IGNORECASE),
    "SOL": re.compile(r"\b(SOL|Solana)\b", re.IGNORECASE),
    "XRP": re.compile(r"\bXRP\b", re.IGNORECASE),
}

# Pattern matching 15-minute window markets (e.g. "11:15AM-11:30AM")
# Group 1 captures start time (e.g. "11:15AM"), group 2 captures end time
FIFTEEN_MIN_WINDOW_PATTERN = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE
)


class CryptoMarketFinder:
    def __init__(self, config: Config):
        self.config = config
        self._allowed_assets = set(
            a.strip().upper() for a in config.tmc_crypto_assets.split(",")
        )

    def find_upcoming_markets(self) -> list[CryptoMarket]:
        now = datetime.now(timezone.utc)
        markets: list[CryptoMarket] = []
        offset = 0
        limit = 100

        while True:
            params: dict[str, Any] = {
                "limit": limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
            }

            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/markets", params=params, timeout=15
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error(f"[TMC] Gamma API error: {e}")
                break

            items = resp.json()
            if not isinstance(items, list) or not items:
                break

            for m in items:
                market = self._parse_crypto_market(m, now)
                if market:
                    markets.append(market)

            if len(items) < limit:
                break
            offset += limit

        logger.info(f"[TMC] Found {len(markets)} upcoming crypto markets")
        return markets

    def _parse_crypto_market(
        self, m: dict, now: datetime
    ) -> CryptoMarket | None:
        question = m.get("question", "")

        # Must match a crypto asset
        asset = self._extract_asset(question)
        if not asset or asset not in self._allowed_assets:
            return None

        # Must be a 15-minute window market (e.g. "11:15AM-11:30AM")
        if not FIFTEEN_MIN_WINDOW_PATTERN.search(question):
            return None

        # Parse token IDs
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

        # Parse end date, must be 1-20 minutes from now
        raw_end = m.get("endDate", m.get("end_date_iso", ""))
        if not raw_end:
            return None
        try:
            end_date = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

        seconds_until_end = (end_date - now).total_seconds()
        if seconds_until_end < 60 or seconds_until_end > 1200:
            return None

        if not m.get("active", True):
            return None

        # Parse start time from question (e.g. "2:00PM" from "2:00PM-2:15PM")
        start_date = self._parse_start_time(question, end_date)

        return CryptoMarket(
            condition_id=m.get("conditionId", m.get("id", "")),
            question=question,
            token_ids=tokens,
            end_date=end_date,
            asset=asset,
            volume=float(m.get("volume", 0) or 0),
            liquidity=float(m.get("liquidity", 0) or 0),
            start_date=start_date,
        )

    def _extract_asset(self, question: str) -> str | None:
        for asset, pattern in ASSET_PATTERNS.items():
            if pattern.search(question):
                return asset
        return None

    @staticmethod
    def _parse_start_time(question: str, end_date: datetime) -> datetime | None:
        """Parse start time from question text like '2:00PM-2:15PM'.

        Uses end_date's date and assumes ET (US/Eastern) timezone,
        then converts to UTC.
        """
        match = FIFTEEN_MIN_WINDOW_PATTERN.search(question)
        if not match:
            return None

        start_str = match.group(1).strip()  # e.g. "2:00PM"
        try:
            et = ZoneInfo("America/New_York")
            # Parse "2:00PM" into hour/minute
            t = datetime.strptime(start_str, "%I:%M%p")
            # Combine with end_date's date in ET, then convert to UTC
            start_et = end_date.astimezone(et).replace(
                hour=t.hour, minute=t.minute, second=0, microsecond=0
            )
            return start_et.astimezone(timezone.utc)
        except (ValueError, AttributeError):
            return None
