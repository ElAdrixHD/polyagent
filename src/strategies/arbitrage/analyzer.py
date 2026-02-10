import json
import logging

import requests

from src.core.config import Config
from src.core.models import ArbitrageOpportunity, LLMAnalysis

logger = logging.getLogger("polyagent")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are a risk analyst for a Polymarket arbitrage bot. Your job is to evaluate whether a binary prediction market is safe to trade for mathematical arbitrage (buying both YES and NO when their combined ask price is less than $1.00).

You will receive market details. Evaluate the following risks:
1. **Resolution ambiguity**: Could the resolution criteria be disputed or unclear?
2. **Cancellation risk**: Is the market likely to be voided or cancelled?
3. **Already resolved**: Has the event already occurred, making prices stale?
4. **Manipulation risk**: Could the market be manipulated by a few actors?
5. **Temporal risk**: Is the market about to expire, creating settlement risk?

Respond with ONLY a JSON object (no markdown, no explanation outside the JSON):
{
  "safe": true/false,
  "risk_level": "low"/"medium"/"high",
  "reason": "Brief explanation (1-2 sentences)"
}"""


class LLMAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.enabled = config.llm_enabled

    def validate(self, opportunity: ArbitrageOpportunity) -> LLMAnalysis:
        if not self.enabled:
            return LLMAnalysis(
                safe=True,
                risk_level="unknown",
                reason="LLM validation disabled",
                model_used="none",
            )

        user_prompt = (
            f"Market: {opportunity.question}\n"
            f"YES ask: ${opportunity.yes_price:.4f}\n"
            f"NO ask: ${opportunity.no_price:.4f}\n"
            f"Combined: ${opportunity.yes_price + opportunity.no_price:.4f}\n"
            f"Profit margin: {opportunity.profit:.2%}\n"
            f"Volume: ${opportunity.volume:,.0f}\n"
            f"Liquidity: ${opportunity.liquidity:,.0f}\n"
            f"End date: {opportunity.end_date or 'Unknown'}\n"
        )

        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {self.config.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.llm_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            content = data["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            result = json.loads(content)

            analysis = LLMAnalysis(
                safe=result.get("safe", False),
                risk_level=result.get("risk_level", "high"),
                reason=result.get("reason", "No reason provided"),
                model_used=self.config.llm_model,
            )

            level = "INFO" if analysis.safe else "WARNING"
            logger.log(
                logging.getLevelName(level),
                f"LLM [{analysis.risk_level}]: {analysis.reason}",
            )
            return analysis

        except requests.RequestException as e:
            logger.error(f"OpenRouter API error: {e}")
            return LLMAnalysis(
                safe=False,
                risk_level="high",
                reason=f"LLM API error: {e}",
                model_used=self.config.llm_model,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse LLM response: {e}")
            return LLMAnalysis(
                safe=False,
                risk_level="high",
                reason=f"LLM response parse error: {e}",
                model_used=self.config.llm_model,
            )
