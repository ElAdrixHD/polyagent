#!/usr/bin/env python3
"""
Per-crypto diagnostic: identifies optimal min_volatility and min_edge per asset.
Run: docker compose run --rm analyze python /app/scripts/per_crypto_diagnostic.py
"""
import csv, os, sys
from collections import defaultdict

DATA_DIR = os.environ.get("DATA_DIR", "/app/data/exports")

def load_csv(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))

def sf(v):
    """Safe float"""
    try:
        return float(v) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None

def main():
    trades = load_csv("trades.csv")
    shadow = load_csv("shadow_markets.csv")

    if not trades:
        print("No trades found!")
        return

    assets = sorted(set(t.get("asset", "?") for t in trades))

    print("=" * 80)
    print("  PER-CRYPTO DIAGNOSTIC")
    print("=" * 80)
    print(f"\n  Total trades: {len(trades)}\n")

    # ── 1. Volatility distribution per asset ──────────────────────────────
    print("=" * 80)
    print("  1. VOLATILITY DISTRIBUTION PER ASSET (from trades)")
    print("=" * 80)

    for asset in assets:
        recs = [t for t in trades if t.get("asset") == asset]
        vols = sorted([sf(t.get("volatility")) for t in recs if sf(t.get("volatility")) is not None])
        if not vols:
            continue
        n = len(vols)
        print(f"\n  {asset} ({n} trades):")
        print(f"    Min:    {vols[0]:.8f}")
        print(f"    P25:    {vols[n//4]:.8f}")
        print(f"    Median: {vols[n//2]:.8f}")
        print(f"    P75:    {vols[3*n//4]:.8f}")
        print(f"    Max:    {vols[-1]:.8f}")
        print(f"    Mean:   {sum(vols)/n:.8f}")

    # ── 2. Win rate by volatility bucket per asset ────────────────────────
    print(f"\n{'=' * 80}")
    print("  2. WIN RATE BY VOLATILITY BUCKET PER ASSET")
    print("=" * 80)

    vol_buckets = [
        (0.00000, 0.00007, "<0.00007"),
        (0.00007, 0.00010, "0.00007-0.00010"),
        (0.00010, 0.00015, "0.00010-0.00015"),
        (0.00015, 0.00020, "0.00015-0.00020"),
        (0.00020, 0.00030, "0.00020-0.00030"),
        (0.00030, 1.00000, ">0.00030"),
    ]

    for asset in assets:
        recs = [t for t in trades if t.get("asset") == asset]
        print(f"\n  {asset}:")
        print(f"    {'Vol Bucket':>18s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}  {'AvgRet':>8s}")
        print(f"    {'-'*18}  {'-'*4}  {'-'*6}  {'-'*9}  {'-'*8}")
        for lo, hi, label in vol_buckets:
            bucket = [t for t in recs
                      if lo <= (sf(t.get("volatility")) or -1) < hi]
            if not bucket:
                continue
            nn = len(bucket)
            wins = sum(1 for t in bucket if (sf(t.get("net_return")) or 0) > 0)
            pnl = sum(sf(t.get("net_return")) or 0 for t in bucket)
            wr = wins / nn * 100
            avg_ret = pnl / nn * 100
            print(f"    {label:>18s}  {nn:4d}  {wr:5.1f}%  ${pnl:+7.2f}  {avg_ret:+7.1f}%")

    # ── 3. Optimal min_volatility per asset ───────────────────────────────
    print(f"\n{'=' * 80}")
    print("  3. OPTIMAL MIN_VOLATILITY PER ASSET (cumulative from high to low)")
    print("=" * 80)

    vol_thresholds = [0.00003, 0.00004, 0.00005, 0.00006, 0.00007,
                      0.00008, 0.00010, 0.00012, 0.00015, 0.00020]

    for asset in assets:
        recs = [t for t in trades if t.get("asset") == asset]
        print(f"\n  {asset}:")
        print(f"    {'MinVol':>12s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}")
        print(f"    {'-'*12}  {'-'*4}  {'-'*6}  {'-'*9}")
        for thresh in vol_thresholds:
            bucket = [t for t in recs
                      if (sf(t.get("volatility")) or 0) >= thresh]
            if not bucket:
                continue
            nn = len(bucket)
            wins = sum(1 for t in bucket if (sf(t.get("net_return")) or 0) > 0)
            pnl = sum(sf(t.get("net_return")) or 0 for t in bucket)
            wr = wins / nn * 100
            print(f"    {thresh:12.5f}  {nn:4d}  {wr:5.1f}%  ${pnl:+7.2f}")

    # ── 4. Optimal min_edge per asset ─────────────────────────────────────
    print(f"\n{'=' * 80}")
    print("  4. OPTIMAL MIN_EDGE PER ASSET (cumulative)")
    print("=" * 80)

    edge_thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]

    for asset in assets:
        recs = [t for t in trades if t.get("asset") == asset]
        print(f"\n  {asset}:")
        print(f"    {'MinEdge':>8s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}  {'AvgPnL':>8s}")
        print(f"    {'-'*8}  {'-'*4}  {'-'*6}  {'-'*9}  {'-'*8}")
        for thresh in edge_thresholds:
            bucket = [t for t in recs
                      if (sf(t.get("edge")) or 0) >= thresh]
            if not bucket:
                continue
            nn = len(bucket)
            wins = sum(1 for t in bucket if (sf(t.get("net_return")) or 0) > 0)
            pnl = sum(sf(t.get("net_return")) or 0 for t in bucket)
            wr = wins / nn * 100
            avg_pnl = pnl / nn
            print(f"    {thresh:8.2f}  {nn:4d}  {wr:5.1f}%  ${pnl:+7.2f}  ${avg_pnl:+7.2f}")

    # ── 5. Combined optimal: vol + edge per asset ─────────────────────────
    print(f"\n{'=' * 80}")
    print("  5. COMBINED OPTIMAL (vol >= X AND edge >= Y) PER ASSET")
    print("=" * 80)

    vol_tests = [0.00007, 0.00010, 0.00012, 0.00015]
    edge_tests = [0.10, 0.15, 0.20, 0.25, 0.30]

    for asset in assets:
        recs = [t for t in trades if t.get("asset") == asset]
        print(f"\n  {asset}:")
        print(f"    {'MinVol':>12s}  {'MinEdge':>8s}  {'N':>4s}  {'WR':>6s}  {'PnL':>9s}")
        print(f"    {'-'*12}  {'-'*8}  {'-'*4}  {'-'*6}  {'-'*9}")
        for vt in vol_tests:
            for et in edge_tests:
                bucket = [t for t in recs
                          if (sf(t.get("volatility")) or 0) >= vt
                          and (sf(t.get("edge")) or 0) >= et]
                if not bucket or len(bucket) < 2:
                    continue
                nn = len(bucket)
                wins = sum(1 for t in bucket if (sf(t.get("net_return")) or 0) > 0)
                pnl = sum(sf(t.get("net_return")) or 0 for t in bucket)
                wr = wins / nn * 100
                marker = " ✅" if wr >= 70 and pnl > 0 else ""
                print(f"    {vt:12.5f}  {et:8.2f}  {nn:4d}  {wr:5.1f}%  ${pnl:+7.2f}{marker}")

    # ── 6. Shadow volatility distribution (what's available) ──────────────
    print(f"\n{'=' * 80}")
    print("  6. SHADOW MARKET VOLATILITY DISTRIBUTION (what we observe)")
    print("=" * 80)

    for asset in assets:
        recs = [s for s in shadow if s.get("asset") == asset]
        vols = sorted([sf(s.get("volatility")) for s in recs if sf(s.get("volatility")) is not None and sf(s.get("volatility")) > 0])
        if not vols:
            continue
        n = len(vols)
        print(f"\n  {asset} ({n} shadow markets):")
        print(f"    Min:    {vols[0]:.8f}")
        print(f"    P10:    {vols[n//10]:.8f}")
        print(f"    P25:    {vols[n//4]:.8f}")
        print(f"    Median: {vols[n//2]:.8f}")
        print(f"    P75:    {vols[3*n//4]:.8f}")
        print(f"    P90:    {vols[9*n//10]:.8f}")
        print(f"    Max:    {vols[-1]:.8f}")

        # How many pass each threshold
        print(f"    Markets above threshold:")
        for thresh in [0.00005, 0.00007, 0.00010, 0.00012, 0.00015, 0.00020]:
            above = sum(1 for v in vols if v >= thresh)
            pct = above / n * 100
            print(f"      >= {thresh:.5f}: {above:4d} ({pct:5.1f}%)")

    print(f"\n{'=' * 80}")
    print("  DIAGNOSTIC COMPLETE")
    print(f"{'=' * 80}\n")


if __name__ == "__main__":
    main()
