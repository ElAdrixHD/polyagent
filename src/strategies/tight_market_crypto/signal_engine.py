import logging
import math
from datetime import datetime, timezone

from src.core.client import PolymarketClient
from src.core.config import Config

from .chainlink_feed import ChainlinkPriceFeed
from .models import TightMarketOpportunity
from .tightness_tracker import TightnessTracker

logger = logging.getLogger("polyagent")


# ── Black-Scholes helpers ────────────────────────────────────────────────────


def norm_cdf(x: float) -> float:
    """Standard normal CDF via Abramowitz & Stegun approximation."""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    a1, a2, a3, a4, a5 = (
        0.254829592,
        -0.284496736,
        1.421413741,
        -1.453152027,
        1.061405429,
    )
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    t = 1.0 / (1.0 + p * abs(x))
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(
        -x * x / 2.0
    )
    return 0.5 * (1.0 + sign * y)


def calc_prob_above(price: float, strike: float, vol: float, T: float) -> float:
    """P(price > strike at expiry) using Black-Scholes N(d₂).

    Args:
        price: Current asset price S
        strike: Strike price K
        vol: Volatility σ (stddev of 1-second log-returns)
        T: Time remaining in seconds

    Returns:
        Probability between 0 and 1.
    """
    if price <= 0 or strike <= 0 or vol <= 0 or T <= 0:
        return 0.5  # degenerate — no information
    denom = vol * math.sqrt(T)
    if denom < 1e-12:
        return 1.0 if price > strike else 0.0
    d2 = math.log(price / strike) / denom
    return norm_cdf(d2)


# ── Signal Engine ────────────────────────────────────────────────────────────


class SignalEngine:
    def __init__(
        self,
        config: Config,
        tracker: TightnessTracker,
        client: PolymarketClient,
        price_feed: ChainlinkPriceFeed,
    ):
        self.config = config
        self.tracker = tracker
        self.client = client
        self.price_feed = price_feed
        self._fired: set[str] = set()  # condition_ids already fired
        self._skipped_signals: dict[str, list[dict]] = {}  # cid -> list of skip entries

    def check_signals(self) -> list[TightMarketOpportunity]:
        """Evaluate all tracked markets using Black-Scholes N(d₂) pricing.

        BET-WITH-FAVORITE THESIS: Buy the favorite side when our model
        says the market underprices it (edge = model_prob - market_prob > min_edge).
        """
        opportunities: list[TightMarketOpportunity] = []
        profiles = self.tracker.get_all_profiles()

        for profile in profiles:
            cid = profile.market.condition_id
            asset = profile.market.asset
            q = profile.market.question[:50]
            remaining = profile.seconds_remaining

            if cid in self._fired:
                continue

            if profile.market.strike_price is None:
                continue

            if remaining <= 0 or remaining > self.config.tmc_entry_window:
                continue

            if remaining < self.config.tmc_min_seconds_remaining:
                continue

            # Get live crypto price and volatility from Chainlink
            current_price = self.price_feed.get_price(asset)
            volatility = self.price_feed.get_volatility(
                asset, self.config.tmc_volatility_window
            )

            if current_price is None:
                continue

            strike = profile.market.strike_price
            in_exec = remaining <= self.config.tmc_execution_window

            # Compute model probability
            if volatility is not None and volatility > 0:
                model_prob = calc_prob_above(current_price, strike, volatility, remaining)
            else:
                model_prob = None

            # Build reusable context for skip recording
            ctx = self._build_context(
                cid, remaining, current_price, strike,
                volatility, model_prob, profile, in_exec,
            )

            # Must be in execution window to fire
            if not in_exec:
                if remaining % 5 < 0.6:
                    logger.info(
                        f"[TMC] WATCH {asset} '{q}' | "
                        f"price=${current_price:,.2f} strike=${strike:,.2f} | "
                        f"model_prob={model_prob:.3f} | "
                        f"remaining={remaining:.0f}s"
                        if model_prob is not None
                        else f"[TMC] WATCH {asset} '{q}' | "
                        f"price=${current_price:,.2f} strike=${strike:,.2f} | "
                        f"remaining={remaining:.0f}s"
                    )
                continue

            # === BLACK-SCHOLES ENTRY GATES ===

            # Gate 1: Need valid volatility
            if volatility is None or volatility < self.config.tmc_min_volatility:
                self._record_skip(ctx, skip_reason="low_volatility")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"vol={volatility} < min {self.config.tmc_min_volatility} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Gate 2: Need valid model probability
            if model_prob is None:
                self._record_skip(ctx, skip_reason="no_model_prob")
                continue

            # Gate 3: Live CLOB asks — need valid prices
            token_ids = profile.market.token_ids
            yes_ask = self.client.get_best_ask(token_ids[0])
            no_ask = self.client.get_best_ask(token_ids[1])

            if yes_ask is None or no_ask is None or yes_ask <= 0 or no_ask <= 0:
                self._record_skip(ctx, skip_reason="no_valid_asks")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"no valid asks (YES={yes_ask} NO={no_ask})"
                )
                continue

            # Gate 4: Determine best side and edge
            edge_yes = model_prob - yes_ask
            edge_no = (1 - model_prob) - no_ask

            if edge_yes >= edge_no:
                bet_side = "YES"
                bet_ask = yes_ask
                bet_token_id = token_ids[0]
                edge = edge_yes
                market_prob = yes_ask
            else:
                bet_side = "NO"
                bet_ask = no_ask
                bet_token_id = token_ids[1]
                edge = edge_no
                market_prob = no_ask

            # Update context with computed values
            ctx["model_prob"] = round(model_prob, 4)
            ctx["market_prob"] = round(market_prob, 4)
            ctx["edge"] = round(edge, 4)
            ctx["bet_side"] = bet_side

            # Gate 5: Must bet on the FAVORITE (majority) side
            favorite_side = "YES" if model_prob > 0.5 else "NO"
            if bet_side != favorite_side:
                self._record_skip(ctx, skip_reason="not_favorite_side")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"bet_side={bet_side} != favorite={favorite_side} | "
                    f"model_prob={model_prob:.3f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Gate 6: Minimum edge threshold
            if edge < self.config.tmc_min_edge:
                self._record_skip(ctx, skip_reason="edge_too_low")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"edge={edge:.3f} < min {self.config.tmc_min_edge} | "
                    f"model_prob={model_prob:.3f} market={market_prob:.3f} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # Gate 7: Minimum ask to avoid illiquid extremes
            if bet_ask < self.config.tmc_min_ask:
                self._record_skip(ctx, skip_reason="ask_too_low")
                logger.info(
                    f"[TMC] SKIP {asset} '{q}' | "
                    f"ask={bet_ask:.3f} < min {self.config.tmc_min_ask} | "
                    f"remaining={remaining:.0f}s"
                )
                continue

            # === FIRE SIGNAL ===
            amount = self.config.tmc_max_investment
            payout_ratio = 1.0 / bet_ask if bet_ask > 0 else 0

            opp = TightMarketOpportunity(
                market=profile.market,
                profile=profile,
                yes_ask=yes_ask,
                no_ask=no_ask,
                buy_side=bet_side,
                buy_token_id=bet_token_id,
                buy_ask=bet_ask,
                amount=amount,
                total_cost=amount,
                strike_price=strike,
                current_crypto_price=current_price,
                model_prob=model_prob,
                market_prob=market_prob,
                edge=edge,
                volatility=volatility,
            )
            opportunities.append(opp)
            self._fired.add(cid)

            logger.info(
                f"[TMC] >>> SIGNAL FIRED: {asset} '{q}' | "
                f"BUY {bet_side}@${bet_ask:.3f} ${amount:.2f} "
                f"(payout={payout_ratio:.1f}x) | "
                f"model_prob={model_prob:.3f} market={market_prob:.3f} "
                f"edge={edge:.3f} | "
                f"YES=${yes_ask:.3f} NO=${no_ask:.3f} | "
                f"price=${current_price:,.2f} strike=${strike:,.2f} "
                f"vol={volatility:.6f} | "
                f"remaining={remaining:.0f}s"
            )

        return opportunities

    # --- Helpers ---

    def get_skipped_signals(self, condition_id: str) -> list[dict]:
        return self._skipped_signals.get(condition_id, [])

    def mark_expired(self, condition_id: str) -> None:
        self._fired.discard(condition_id)
        self._skipped_signals.pop(condition_id, None)

    def _build_context(
        self,
        cid: str,
        remaining: float,
        current_price: float,
        strike: float,
        volatility: float | None,
        model_prob: float | None,
        profile,
        in_execution_window: bool,
    ) -> dict:
        """Build a reusable context dict for skip recording."""
        decimals = 6 if strike < 10 else (4 if strike < 1000 else 2)
        return {
            "cid": cid,
            "remaining": round(remaining, 1),
            "in_execution_window": in_execution_window,
            "current_price": round(current_price, decimals),
            "strike": round(strike, decimals),
            "volatility": round(volatility, 8) if volatility is not None else None,
            "model_prob": round(model_prob, 4) if model_prob is not None else None,
            "yes_price": round(profile.current_yes, 4),
            "no_price": round(profile.current_no, 4),
        }

    def _record_skip(
        self,
        ctx: dict,
        skip_reason: str = "",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **{k: v for k, v in ctx.items() if k != "cid"},
            "skip_reason": skip_reason,
        }
        cid = ctx["cid"]
        if cid not in self._skipped_signals:
            self._skipped_signals[cid] = []
        self._skipped_signals[cid].append(entry)
