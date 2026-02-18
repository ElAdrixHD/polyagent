#!/usr/bin/env python3
"""
Binary Option Pricing Backtest

Compares theoretical probabilities (Black-Scholes N(d2)) against Polymarket
implied probabilities for all shadow markets to find exploitable mispricing.

Volatility from Binance is stddev of 1-second log-returns.
Formula: d2 = ln(S/K) / (σ * √T), where T is in seconds.

Usage:
  python scripts/binary_option_backtest.py
"""

import csv
import math
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "exports"


# ── Normal CDF (no scipy dependency) ────────────────────────────────────────

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function (Abramowitz & Stegun)."""
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(
        -x_abs * x_abs / 2.0
    )
    return 0.5 * (1.0 + sign * y)


def calc_prob_above(price: float, strike: float, vol: float, T: float) -> float:
    """Calculate P(price > strike at expiry) using binary option formula.

    Args:
        price: Current crypto price
        strike: Strike price of the market
        vol: Volatility (stddev of 1-second log-returns)
        T: Time remaining in seconds

    Returns: Probability [0, 1]
    """
    if price <= 0 or strike <= 0 or vol <= 0 or T <= 0:
        return 0.5  # can't calculate, assume fair

    d2 = math.log(price / strike) / (vol * math.sqrt(T))
    return norm_cdf(d2)


# ── Load data ────────────────────────────────────────────────────────────────

def load_shadow_markets():
    path = DATA_DIR / "shadow_markets.csv"
    with open(path) as f:
        return list(csv.DictReader(f))


def load_shadow_signals():
    path = DATA_DIR / "shadow_signals.csv"
    with open(path) as f:
        return list(csv.DictReader(f))


# ── Main analysis ────────────────────────────────────────────────────────────

def main():
    markets = load_shadow_markets()
    signals = load_shadow_signals()

    print("=" * 70)
    print("BINARY OPTION PRICING BACKTEST")
    print("=" * 70)
    print(f"\nLoaded {len(markets)} shadow markets, {len(signals)} signal snapshots")

    # ── Per-market analysis using exec_start snapshot ────────────────────────
    # For each market, calculate our model probability vs market implied prob
    # at the START of the execution window

    print("\n" + "=" * 70)
    print("[1] MODEL vs MARKET — Per Market at Exec Window Start")
    print("=" * 70)

    results = []
    skipped = 0
    for m in markets:
        try:
            strike = float(m["strike_price"]) if m["strike_price"] else None
            vol = float(m["volatility"]) if m["volatility"] else None
            price_start = (
                float(m["price_at_exec_window_start"])
                if m["price_at_exec_window_start"]
                else None
            )
            outcome = m.get("outcome", "")
            cheap_ask = (
                float(m["cheap_side_at_exec_start"])
                if m["cheap_side_at_exec_start"]
                else None
            )
            majority = m.get("majority_at_exec_start", "")
        except (ValueError, KeyError):
            skipped += 1
            continue

        if not all([strike, vol, price_start, outcome, cheap_ask, majority]):
            skipped += 1
            continue

        # Remaining seconds: we use 11s as approximate exec window start
        # (exec window is typically 11s before expiry)
        T = 11.0

        # Our model probability
        prob_above = calc_prob_above(price_start, strike, vol, T)
        prob_yes = prob_above  # YES = price above strike
        prob_no = 1 - prob_above

        # Market implied probability
        if majority == "YES":
            market_prob_yes = 1 - cheap_ask  # cheap side = NO at cheap_ask
            market_prob_no = cheap_ask
        else:
            market_prob_no = 1 - cheap_ask  # cheap side = YES at cheap_ask
            market_prob_yes = cheap_ask

        # Edge = our probability - market probability
        edge_yes = prob_yes - market_prob_yes
        edge_no = prob_no - market_prob_no

        # Which side has edge?
        if edge_yes > edge_no:
            bet_side = "YES"
            edge = edge_yes
            bet_ask = market_prob_yes  # what we'd pay
        else:
            bet_side = "NO"
            edge = edge_no
            bet_ask = market_prob_no  # what we'd pay

        # Did this bet win?
        win = (bet_side == outcome)
        payout = (1.0 / bet_ask) if bet_ask > 0 else 0
        pnl = (payout - 1) if win else -1.0

        results.append({
            "asset": m["asset"],
            "question": m["question"][:40],
            "outcome": outcome,
            "majority": majority,
            "price": price_start,
            "strike": strike,
            "vol": vol,
            "prob_yes": prob_yes,
            "prob_no": prob_no,
            "mkt_prob_yes": market_prob_yes,
            "mkt_prob_no": market_prob_no,
            "bet_side": bet_side,
            "edge": edge,
            "bet_ask": bet_ask,
            "payout": payout,
            "win": win,
            "pnl": pnl,
            "crossed": m.get("price_crossed_strike", ""),
            "tight_ratio": float(m["tight_ratio"]) if m["tight_ratio"] else 0,
            "condition_id": m.get("condition_id", ""),
        })

    print(f"  Analyzed: {len(results)}, Skipped: {skipped}")

    # ── Results by edge threshold ────────────────────────────────────────────

    print("\n" + "-" * 70)
    print("[2] PnL BY EDGE THRESHOLD (only bet when edge > threshold)")
    print("-" * 70)
    print(
        f"  {'threshold':>10s} {'n_bets':>7s} {'wins':>5s} {'WR':>7s} "
        f"{'total_PnL':>10s} {'avg_PnL':>8s} {'avg_edge':>9s} {'avg_pay':>8s}"
    )

    for threshold in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        bets = [r for r in results if r["edge"] > threshold]
        if not bets:
            continue
        wins = sum(1 for r in bets if r["win"])
        total_pnl = sum(r["pnl"] for r in bets)
        avg_pnl = total_pnl / len(bets)
        wr = wins / len(bets) * 100
        avg_edge = sum(r["edge"] for r in bets) / len(bets)
        avg_pay = sum(r["payout"] for r in bets) / len(bets)
        marker = " <<< PROFITABLE" if total_pnl > 0 else ""
        print(
            f"  {threshold:>10.2f} {len(bets):>7d} {wins:>5d} {wr:>6.1f}% "
            f"{total_pnl:>+10.2f} {avg_pnl:>+8.4f} {avg_edge:>9.3f} {avg_pay:>8.1f}x{marker}"
        )

    # ── Breakdown: betting AGAINST majority (underdog) vs WITH majority ──────

    print("\n" + "-" * 70)
    print("[3] EDGE BETS vs MAJORITY DIRECTION")
    print("-" * 70)

    for threshold in [0.10, 0.20, 0.30, 0.40]:
        bets = [r for r in results if r["edge"] > threshold]
        if not bets:
            continue

        against = [r for r in bets if r["bet_side"] != r["majority"]]
        with_maj = [r for r in bets if r["bet_side"] == r["majority"]]

        print(f"\n  Edge > {threshold:.2f}:")
        for label, subset in [("  AGAINST majority (underdog)", against), ("  WITH majority (favorite)", with_maj)]:
            if not subset:
                print(f"  {label}: n=0")
                continue
            w = sum(1 for r in subset if r["win"])
            p = sum(r["pnl"] for r in subset)
            avg_pay = sum(r["payout"] for r in subset) / len(subset)
            print(
                f"  {label}: n={len(subset):4d} wins={w:3d} "
                f"WR={w/len(subset)*100:5.1f}% PnL={p:+.2f} avgPay={avg_pay:.1f}x"
            )

    # ── Per-asset breakdown at best threshold ────────────────────────────────

    print("\n" + "-" * 70)
    print("[4] BY ASSET (edge > 0.20)")
    print("-" * 70)

    for asset in ["BTC", "ETH", "SOL", "XRP"]:
        bets = [r for r in results if r["edge"] > 0.20 and r["asset"] == asset]
        if not bets:
            continue
        wins = sum(1 for r in bets if r["win"])
        total_pnl = sum(r["pnl"] for r in bets)
        avg_edge = sum(r["edge"] for r in bets) / len(bets)
        print(
            f"  {asset}: n={len(bets):4d} wins={wins:3d} "
            f"WR={wins/len(bets)*100:5.1f}% PnL={total_pnl:+.2f} edge={avg_edge:.3f}"
        )

    # ── Signal-level analysis: evaluate at each snapshot ─────────────────────

    print("\n" + "=" * 70)
    print("[5] SNAPSHOT-LEVEL ANALYSIS (evaluating at each signal evaluation)")
    print("=" * 70)

    # Group signals by condition_id, find best edge per market
    market_outcomes = {m["condition_id"]: m["outcome"] for m in markets}
    market_vols = {}
    for m in markets:
        try:
            market_vols[m["condition_id"]] = float(m["volatility"])
        except (ValueError, KeyError):
            pass

    snap_results = []
    by_cid = {}
    for sig in signals:
        cid = sig.get("condition_id", "")
        if cid not in by_cid:
            by_cid[cid] = []
        by_cid[cid].append(sig)

    for cid, sigs in by_cid.items():
        outcome = market_outcomes.get(cid, "")
        vol = market_vols.get(cid)
        if not outcome or not vol:
            continue

        best_edge = -1
        best_snap = None

        for sig in sigs:
            try:
                price = float(sig["current_price"]) if sig["current_price"] else None
                strike = float(sig["strike"]) if sig["strike"] else None
                remaining = float(sig["remaining"]) if sig["remaining"] else None
                yes_price = float(sig["yes_price"]) if sig["yes_price"] else None
                no_price = float(sig["no_price"]) if sig["no_price"] else None
            except (ValueError, KeyError):
                continue

            if not all([price, strike, remaining, yes_price, no_price]) or remaining <= 0:
                continue

            prob_above = calc_prob_above(price, strike, vol, remaining)

            # Edge for YES
            edge_yes = prob_above - yes_price
            # Edge for NO
            edge_no = (1 - prob_above) - no_price

            if edge_yes > edge_no:
                edge = edge_yes
                bet_side = "YES"
                bet_ask = yes_price
            else:
                edge = edge_no
                bet_side = "NO"
                bet_ask = no_price

            if edge > best_edge:
                best_edge = edge
                best_snap = {
                    "cid": cid,
                    "asset": sig.get("asset", ""),
                    "bet_side": bet_side,
                    "edge": edge,
                    "bet_ask": bet_ask,
                    "remaining": remaining,
                    "price": price,
                    "strike": strike,
                    "outcome": outcome,
                    "win": (bet_side == outcome),
                    "payout": (1.0 / bet_ask) if bet_ask > 0 else 0,
                    "pnl": ((1.0 / bet_ask) - 1) if (bet_side == outcome) and bet_ask > 0 else -1.0,
                }

        if best_snap:
            snap_results.append(best_snap)

    print(f"  Markets with snapshot data: {len(snap_results)}")

    print("\n  PnL by edge threshold (best edge per market):")
    print(
        f"  {'threshold':>10s} {'n_bets':>7s} {'wins':>5s} {'WR':>7s} "
        f"{'total_PnL':>10s} {'avg_PnL':>8s} {'avg_pay':>8s}"
    )

    for threshold in [0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]:
        bets = [r for r in snap_results if r["edge"] > threshold]
        if not bets:
            continue
        wins = sum(1 for r in bets if r["win"])
        total_pnl = sum(r["pnl"] for r in bets)
        avg_pnl = total_pnl / len(bets)
        wr = wins / len(bets) * 100
        avg_pay = sum(r["payout"] for r in bets) / len(bets)
        marker = " <<< PROFITABLE" if total_pnl > 0 else ""
        print(
            f"  {threshold:>10.2f} {len(bets):>7d} {wins:>5d} {wr:>6.1f}% "
            f"{total_pnl:>+10.2f} {avg_pnl:>+8.4f} {avg_pay:>8.1f}x{marker}"
        )

    # ── Distribution of model probabilities ──────────────────────────────────

    print("\n" + "-" * 70)
    print("[6] MODEL PROBABILITY DISTRIBUTION")
    print("-" * 70)

    prob_yeses = [r["prob_yes"] for r in results]
    buckets = [
        (0.0, 0.01, "0-1%"),
        (0.01, 0.05, "1-5%"),
        (0.05, 0.10, "5-10%"),
        (0.10, 0.20, "10-20%"),
        (0.20, 0.40, "20-40%"),
        (0.40, 0.60, "40-60%"),
        (0.60, 0.80, "60-80%"),
        (0.80, 0.90, "80-90%"),
        (0.90, 0.95, "90-95%"),
        (0.95, 0.99, "95-99%"),
        (0.99, 1.01, "99-100%"),
    ]
    for lo, hi, label in buckets:
        sub = [r for r in results if lo <= r["prob_yes"] < hi]
        if not sub:
            continue
        actual_yes = sum(1 for r in sub if r["outcome"] == "YES") / len(sub) * 100
        print(
            f"  Model P(YES) {label:>8s}: n={len(sub):4d}  "
            f"actual YES rate={actual_yes:5.1f}%  "
            f"(calibration: model says ~{(lo+hi)/2*100:.0f}%, actual={actual_yes:.0f}%)"
        )

    # ── Example: high-edge bets detail ───────────────────────────────────────

    print("\n" + "-" * 70)
    print("[7] HIGH-EDGE BETS DETAIL (edge > 0.30)")
    print("-" * 70)

    high_edge = sorted([r for r in results if r["edge"] > 0.30], key=lambda r: -r["edge"])
    for r in high_edge[:30]:
        print(
            f"  {r['asset']:4s} bet={r['bet_side']:3s} edge={r['edge']:.3f} "
            f"ask={r['bet_ask']:.3f} pay={r['payout']:.1f}x "
            f"{'WIN' if r['win'] else 'LOSS':4s} pnl={r['pnl']:+.2f} "
            f"P(Y)={r['prob_yes']:.3f} mkt={r['mkt_prob_yes']:.3f} "
            f"crossed={r['crossed']}"
        )

    print("\n" + "=" * 70)
    print("Backtest complete.")
    print("=" * 70)


if __name__ == "__main__":
    main()
