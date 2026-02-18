#!/usr/bin/env python3
"""
Análisis del TMC strategy: Black-Scholes N(d₂) pricing model.

Lee CSVs generados por export_data.py y produce métricas de:
  1. Win/Loss summary
  2. Signal quality by asset
  3. Edge analysis — win rate & PnL by edge threshold
  4. Model calibration — predicted prob vs actual win rate
  5. Volatility analysis
  6. Timing analysis
  7. Shadow market analysis

Uso:
  python scripts/analyze.py
"""

import csv
import sys
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "exports"


# ── Data loading ─────────────────────────────────────────────────────────────

def load_csv(name):
    path = DATA_DIR / name
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def safe_float(val, default=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ── Section helpers ──────────────────────────────────────────────────────────

def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def subsection(title):
    print(f"\n  --- {title} ---")


# ── 1. Win/Loss summary ─────────────────────────────────────────────────────

def analyze_winloss(trades):
    section("1. WIN/LOSS SUMMARY")
    if not trades:
        print("  No trades found.")
        return

    wins, losses = [], []
    for t in trades:
        nr = safe_float(t.get("net_return"), 0)
        if nr > 0:
            wins.append(t)
        else:
            losses.append(t)

    total = len(trades)
    total_pnl = sum(safe_float(t.get("net_return"), 0) for t in trades)
    avg_win = sum(safe_float(t.get("net_return"), 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(safe_float(t.get("net_return"), 0) for t in losses) / len(losses) if losses else 0

    print(f"\n  Total trades:  {total}")
    print(f"  Wins:          {len(wins)} ({len(wins)/total*100:.1f}%)")
    print(f"  Losses:        {len(losses)} ({len(losses)/total*100:.1f}%)")
    print(f"  Total P&L:     ${total_pnl:+.2f}")
    print(f"  Avg win:       ${avg_win:+.2f}")
    print(f"  Avg loss:      ${avg_loss:+.2f}")

    returns = [safe_float(t.get("return_pct"), 0) for t in trades]
    if returns:
        print(f"  Avg return %:  {sum(returns)/len(returns):+.1f}%")
        print(f"  Best trade:    {max(returns):+.1f}%")
        print(f"  Worst trade:   {min(returns):+.1f}%")


# ── 2. Signal quality by asset ───────────────────────────────────────────────

def analyze_by_asset(trades):
    section("2. SIGNAL QUALITY BY ASSET")
    if not trades:
        print("  No trades found.")
        return

    by_asset = defaultdict(list)
    for t in trades:
        by_asset[t.get("asset", "?")].append(t)

    print(f"\n  {'Asset':>6s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}  {'Avg Edge':>9s}  {'Avg Vol':>10s}")
    print(f"  {'-'*6}  {'-'*4}  {'-'*6}  {'-'*9}  {'-'*9}  {'-'*10}")

    for asset in sorted(by_asset):
        recs = by_asset[asset]
        n = len(recs)
        wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
        pnl = sum(safe_float(t.get("net_return"), 0) for t in recs)
        edges = [safe_float(t.get("edge")) for t in recs if safe_float(t.get("edge")) is not None]
        vols = [safe_float(t.get("volatility")) for t in recs if safe_float(t.get("volatility")) is not None]
        avg_edge = sum(edges) / len(edges) if edges else 0
        avg_vol = sum(vols) / len(vols) if vols else 0
        wr = wins / n * 100 if n else 0
        print(f"  {asset:>6s}  {n:4d}  {wr:5.1f}%  ${pnl:+7.2f}  {avg_edge:+8.3f}  {avg_vol:10.6f}")


# ── 3. Edge analysis ─────────────────────────────────────────────────────────

def analyze_edge(trades):
    section("3. EDGE ANALYSIS")
    if not trades:
        print("  No trades found.")
        return

    buckets = [
        (0.00, 0.03, "0.00–0.03"),
        (0.03, 0.05, "0.03–0.05"),
        (0.05, 0.10, "0.05–0.10"),
        (0.10, 0.15, "0.10–0.15"),
        (0.15, 0.20, "0.15–0.20"),
        (0.20, 0.30, "0.20–0.30"),
        (0.30, 1.00, "0.30+"),
    ]

    subsection("Win Rate & PnL by Edge Bucket")
    print(f"\n  {'Bucket':>10s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}  {'Avg Ret':>8s}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*6}  {'-'*9}  {'-'*8}")

    for lo, hi, label in buckets:
        recs = [t for t in trades
                if lo <= safe_float(t.get("edge"), -1) < hi]
        if not recs:
            continue
        n = len(recs)
        wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
        pnl = sum(safe_float(t.get("net_return"), 0) for t in recs)
        avg_ret = sum(safe_float(t.get("return_pct"), 0) for t in recs) / n
        wr = wins / n * 100
        print(f"  {label:>10s}  {n:4d}  {wr:5.1f}%  ${pnl:+7.2f}  {avg_ret:+7.1f}%")

    # Optimal edge threshold search
    subsection("Optimal Edge Threshold (cumulative)")
    thresholds = [0.02, 0.03, 0.05, 0.07, 0.10, 0.15, 0.20, 0.25, 0.30]
    print(f"\n  {'Min Edge':>9s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}")
    print(f"  {'-'*9}  {'-'*4}  {'-'*6}  {'-'*9}")
    for thresh in thresholds:
        recs = [t for t in trades if safe_float(t.get("edge"), -1) >= thresh]
        if not recs:
            continue
        n = len(recs)
        wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
        pnl = sum(safe_float(t.get("net_return"), 0) for t in recs)
        wr = wins / n * 100
        marker = " <<" if thresh == 0.05 else ""
        print(f"  {thresh:9.2f}  {n:4d}  {wr:5.1f}%  ${pnl:+7.2f}{marker}")


# ── 4. Model calibration ────────────────────────────────────────────────────

def analyze_calibration(trades):
    section("4. MODEL CALIBRATION (predicted vs actual)")
    if not trades:
        print("  No trades found.")
        return

    buckets = [
        (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
        (0.80, 0.90), (0.90, 0.95), (0.95, 1.01),
    ]

    print(f"\n  {'Predicted':>12s}  {'N':>4s}  {'Actual WR':>10s}  {'Diff':>7s}")
    print(f"  {'-'*12}  {'-'*4}  {'-'*10}  {'-'*7}")

    for lo, hi in buckets:
        recs = [t for t in trades
                if lo <= safe_float(t.get("model_prob"), -1) < hi]
        if not recs:
            continue
        n = len(recs)
        wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
        actual_wr = wins / n
        predicted = (lo + min(hi, 1.0)) / 2
        diff = actual_wr - predicted
        label = f"{lo:.2f}–{min(hi,1.0):.2f}"
        print(f"  {label:>12s}  {n:4d}  {actual_wr:9.1%}  {diff:+6.1%}")


# ── 5. Volatility analysis ──────────────────────────────────────────────────

def analyze_volatility(trades, shadow):
    section("5. VOLATILITY ANALYSIS")
    data = trades if trades else shadow
    if not data:
        print("  No data found.")
        return

    vols = [safe_float(r.get("volatility")) for r in data if safe_float(r.get("volatility")) is not None]
    if not vols:
        print("  No volatility data.")
        return

    vols.sort()
    n = len(vols)
    print(f"\n  Total samples:  {n}")
    print(f"  Min:            {vols[0]:.8f}")
    print(f"  P25:            {vols[n//4]:.8f}")
    print(f"  Median:         {vols[n//2]:.8f}")
    print(f"  P75:            {vols[3*n//4]:.8f}")
    print(f"  Max:            {vols[-1]:.8f}")
    print(f"  Mean:           {sum(vols)/n:.8f}")

    if trades:
        subsection("Win Rate by Volatility Bucket")
        vol_buckets = [
            (0.00000, 0.00007, "<0.00007"),
            (0.00007, 0.00015, "0.00007–0.00015"),
            (0.00015, 0.00030, "0.00015–0.00030"),
            (0.00030, 0.00100, "0.00030–0.00100"),
            (0.00100, 1.00000, ">0.00100"),
        ]
        print(f"\n  {'Vol Bucket':>18s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}")
        print(f"  {'-'*18}  {'-'*4}  {'-'*6}  {'-'*9}")
        for lo, hi, label in vol_buckets:
            recs = [t for t in trades
                    if lo <= safe_float(t.get("volatility"), -1) < hi]
            if not recs:
                continue
            nn = len(recs)
            wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
            pnl = sum(safe_float(t.get("net_return"), 0) for t in recs)
            wr = wins / nn * 100
            print(f"  {label:>18s}  {nn:4d}  {wr:5.1f}%  ${pnl:+7.2f}")


# ── 6. Timing analysis ──────────────────────────────────────────────────────

def analyze_timing(trades):
    section("6. TIMING ANALYSIS")
    if not trades:
        print("  No trades found.")
        return

    buckets = [
        (0, 5, "0–5s"),
        (5, 7, "5–7s"),
        (7, 10, "7–10s"),
        (10, 15, "10–15s"),
        (15, 30, "15–30s"),
        (30, 90, "30–90s"),
    ]

    print(f"\n  {'Remaining':>10s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}")
    print(f"  {'-'*10}  {'-'*4}  {'-'*6}  {'-'*9}")

    for lo, hi, label in buckets:
        recs = [t for t in trades
                if lo <= safe_float(t.get("seconds_remaining"), -1) < hi]
        if not recs:
            continue
        n = len(recs)
        wins = sum(1 for t in recs if safe_float(t.get("net_return"), 0) > 0)
        pnl = sum(safe_float(t.get("net_return"), 0) for t in recs)
        wr = wins / n * 100
        print(f"  {label:>10s}  {n:4d}  {wr:5.1f}%  ${pnl:+7.2f}")


# ── 7. Shadow market analysis ───────────────────────────────────────────────

def analyze_shadow(shadow):
    section("7. SHADOW MARKET ANALYSIS")
    if not shadow:
        print("  No shadow data found.")
        return

    total = len(shadow)
    traded = sum(1 for s in shadow if s.get("was_traded") == "True")
    outcomes = defaultdict(int)
    for s in shadow:
        outcomes[s.get("outcome", "unknown")] += 1

    print(f"\n  Total markets observed:  {total}")
    print(f"  Actually traded:         {traded} ({traded/total*100:.1f}%)")
    print(f"  Outcomes breakdown:")
    for outcome, count in sorted(outcomes.items()):
        print(f"    {outcome:>8s}:  {count:4d} ({count/total*100:.1f}%)")

    # Missed opportunities — high edge markets we didn't trade
    subsection("Missed Opportunities (edge > 0.05, not traded)")
    missed = [s for s in shadow
              if s.get("was_traded") == "False"
              and safe_float(s.get("edge")) is not None
              and safe_float(s.get("edge"), 0) > 0.05]

    if missed:
        bet_would_win = 0
        for s in missed:
            bet_side = s.get("bet_side")
            outcome = s.get("outcome")
            if bet_side and outcome and bet_side == outcome:
                bet_would_win += 1

        print(f"\n  High-edge markets missed:  {len(missed)}")
        print(f"  Would have won:            {bet_would_win} ({bet_would_win/len(missed)*100:.1f}%)")

        edges = [safe_float(s.get("edge"), 0) for s in missed]
        print(f"  Avg missed edge:           {sum(edges)/len(edges):.3f}")

        # Show top 5 missed
        missed_sorted = sorted(missed, key=lambda s: safe_float(s.get("edge"), 0), reverse=True)
        print(f"\n  Top 5 missed:")
        for s in missed_sorted[:5]:
            asset = s.get("asset", "?")
            edge = safe_float(s.get("edge"), 0)
            side = s.get("bet_side", "?")
            outcome = s.get("outcome", "?")
            won = "✓" if side == outcome else "✗"
            q = s.get("question", "")[:40]
            print(f"    {asset:5s} edge={edge:+.3f} side={side} outcome={outcome} {won} | {q}")
    else:
        print("\n  No missed high-edge opportunities.")

    # Skip reason breakdown
    subsection("Skip Reason Frequency")
    reasons = defaultdict(int)
    total_skips = 0
    for s in shadow:
        n_skips = safe_int(s.get("num_skipped_signals"), 0)
        total_skips += n_skips

    print(f"\n  Total skipped signal evaluations: {total_skips}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  TMC STRATEGY ANALYSIS — Black-Scholes N(d₂) Model")
    print("=" * 70)
    print(f"\n  Data dir: {DATA_DIR}")

    trades = load_csv("trades.csv")
    shadow = load_csv("shadow_markets.csv")
    signals = load_csv("shadow_signals.csv")

    print(f"  Trades loaded:   {len(trades)}")
    print(f"  Shadow loaded:   {len(shadow)}")
    print(f"  Signals loaded:  {len(signals)}")

    analyze_winloss(trades)
    analyze_by_asset(trades)
    analyze_edge(trades)
    analyze_calibration(trades)
    analyze_volatility(trades, shadow)
    analyze_timing(trades)
    analyze_shadow(shadow)

    print(f"\n{'='*70}")
    print("  ANALYSIS COMPLETE")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
