#!/usr/bin/env python3
"""
Exporta los JSON de polyagent a CSVs estructurados para análisis.

Genera en data/exports/:
  - trades.csv              : Trades ejecutados (dry_run o real)
  - shadow_markets.csv      : Mercados observados con métricas de outcome
  - shadow_signals.csv      : Señales skipped por mercado (granular)
  - price_trails.csv        : Trail de precios crypto en execution window
  - odds_trails.csv         : Trail de odds en execution window

Uso:
  python scripts/export_data.py
"""

import csv
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
OUT_DIR = DATA_DIR / "exports"


def load_json(filename):
    path = DATA_DIR / filename
    if not path.exists():
        print(f"  [SKIP] {filename} no encontrado")
        return []
    with open(path) as f:
        return json.load(f)


# ── Trades ───────────────────────────────────────────────────────────────────

TRADE_COLS = [
    "timestamp", "asset", "question", "outcome",
    "buy_side", "buy_ask", "yes_ask", "no_ask",
    "amount", "total_cost", "payout", "net_return", "return_pct",
    "strike_price", "current_crypto_price", "final_crypto_price",
    "model_prob", "market_prob", "edge", "volatility",
    "seconds_remaining", "dry_run", "condition_id",
]

def export_trades(trades):
    if not trades:
        return
    path = OUT_DIR / "trades.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"  trades.csv              ({len(trades)} rows)")


# ── Shadow markets ───────────────────────────────────────────────────────────

SHADOW_COLS = [
    "timestamp", "asset", "question", "outcome", "was_traded",
    "strike_price", "final_price",
    "volatility", "model_prob", "market_prob", "edge", "bet_side",
    "final_yes", "final_no",
    "total_snapshots", "num_skipped_signals", "condition_id",
]

def export_shadow_markets(shadow):
    if not shadow:
        return
    path = OUT_DIR / "shadow_markets.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHADOW_COLS, extrasaction="ignore")
        w.writeheader()
        for s in shadow:
            row = {**s, "num_skipped_signals": len(s.get("skipped_signals", []))}
            w.writerow(row)
    print(f"  shadow_markets.csv      ({len(shadow)} rows)")


# ── Shadow skipped signals ───────────────────────────────────────────────────

SIGNAL_COLS = [
    "market_timestamp", "market_question", "asset", "condition_id",
    "signal_timestamp", "remaining",
    "in_execution_window",
    "current_price", "strike", "yes_price", "no_price",
    "volatility", "model_prob", "market_prob", "edge", "bet_side",
    "skip_reason",
]

def export_shadow_signals(shadow):
    if not shadow:
        return
    path = OUT_DIR / "shadow_signals.csv"
    count = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SIGNAL_COLS, extrasaction="ignore")
        w.writeheader()
        for s in shadow:
            asset = s.get("asset", _extract_asset(s["question"]))
            for sig in s.get("skipped_signals", []):
                row = {
                    "market_timestamp": s["timestamp"],
                    "market_question": s["question"],
                    "asset": asset,
                    "condition_id": s["condition_id"],
                    "signal_timestamp": sig.get("timestamp"),
                    **sig,
                }
                w.writerow(row)
                count += 1
    print(f"  shadow_signals.csv      ({count} rows)")


# ── Price trails ─────────────────────────────────────────────────────────────

PRICE_TRAIL_COLS = [
    "market_timestamp", "asset", "condition_id", "window_type",
    "t", "price", "dist",
]

def export_price_trails(shadow):
    if not shadow:
        return
    path = OUT_DIR / "price_trails.csv"
    count = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PRICE_TRAIL_COLS, extrasaction="ignore")
        w.writeheader()
        for s in shadow:
            asset = s.get("asset", _extract_asset(s["question"]))
            base = {
                "market_timestamp": s["timestamp"],
                "asset": asset,
                "condition_id": s["condition_id"],
            }
            for pt in s.get("crypto_price_trail_exec_window", []):
                w.writerow({**base, "window_type": "exec", **pt})
                count += 1
            for pt in s.get("crypto_price_trail_entry_window", []):
                w.writerow({**base, "window_type": "entry", **pt})
                count += 1
    print(f"  price_trails.csv        ({count} rows)")


# ── Odds trails ──────────────────────────────────────────────────────────────

ODDS_TRAIL_COLS = [
    "market_timestamp", "asset", "condition_id", "window_type",
    "t", "yes", "no",
]

def export_odds_trails(shadow):
    if not shadow:
        return
    path = OUT_DIR / "odds_trails.csv"
    count = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ODDS_TRAIL_COLS, extrasaction="ignore")
        w.writeheader()
        for s in shadow:
            asset = s.get("asset", _extract_asset(s["question"]))
            base = {
                "market_timestamp": s["timestamp"],
                "asset": asset,
                "condition_id": s["condition_id"],
            }
            for pt in s.get("odds_trail_exec_window", []):
                w.writerow({**base, "window_type": "exec", **pt})
                count += 1
            for pt in s.get("odds_trail_entry_window", []):
                w.writerow({**base, "window_type": "entry", **pt})
                count += 1
    print(f"  odds_trails.csv         ({count} rows)")


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary(trades, shadow):
    print("\n" + "=" * 60)
    print("RESUMEN RÁPIDO")
    print("=" * 60)

    if trades:
        wins = [t for t in trades if t["net_return"] > 0]
        losses = [t for t in trades if t["net_return"] <= 0]
        total_pnl = sum(t["net_return"] for t in trades)
        assets = {}
        for t in trades:
            a = t["asset"]
            assets.setdefault(a, []).append(t["net_return"])

        print(f"\nTRADES: {len(trades)} total | {len(wins)} wins | {len(losses)} losses")
        print(f"  Win rate:    {len(wins)/len(trades)*100:.1f}%")
        print(f"  Total P&L:   ${total_pnl:.2f}")
        print(f"  Avg return:  {sum(t['return_pct'] for t in trades)/len(trades):.1f}%")
        print(f"  Best trade:  {max(t['return_pct'] for t in trades):.1f}%")
        print(f"  Worst trade: {min(t['return_pct'] for t in trades):.1f}%")
        print(f"\n  Por asset:")
        for asset, returns in sorted(assets.items()):
            n = len(returns)
            pnl = sum(returns)
            wr = sum(1 for r in returns if r > 0) / n * 100
            print(f"    {asset:5s}  {n:3d} trades | P&L ${pnl:+.2f} | WR {wr:.0f}%")

    if shadow:
        traded = [s for s in shadow if s.get("was_traded")]
        with_edge = [s for s in shadow if s.get("edge") is not None]
        assets_s = {}
        for s in shadow:
            a = s.get("asset", _extract_asset(s["question"]))
            assets_s.setdefault(a, []).append(s)

        print(f"\nSHADOW: {len(shadow)} mercados observados")
        print(f"  Traded:          {len(traded)}")
        print(f"  With model edge: {len(with_edge)} ({len(with_edge)/len(shadow)*100:.0f}%)")
        vols = [s["volatility"] for s in shadow if s.get("volatility")]
        if vols:
            print(f"  Avg volatility:  {sum(vols)/len(vols):.6f}")
        edges = [s["edge"] for s in with_edge]
        if edges:
            print(f"  Avg model edge:  {sum(edges)/len(edges):.3f}")
        print(f"\n  Por asset:")
        for asset, records in sorted(assets_s.items()):
            n = len(records)
            edge_recs = [r for r in records if r.get("edge") is not None]
            avg_e = sum(r["edge"] for r in edge_recs) / len(edge_recs) if edge_recs else 0
            print(f"    {asset:5s}  {n:3d} markets | avg edge {avg_e:+.3f}")

    print()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_asset(question: str) -> str:
    q = question.lower()
    for asset in ["bitcoin", "btc"]:
        if asset in q:
            return "BTC"
    for asset in ["ethereum", "eth"]:
        if asset in q:
            return "ETH"
    for asset in ["solana", "sol"]:
        if asset in q:
            return "SOL"
    return question.split()[0]


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Cargando datos...")
    trades = load_json("tight_market_crypto_trades.json")
    shadow = load_json("tight_market_crypto_shadow.json")

    print(f"\nExportando a {OUT_DIR}/")
    export_trades(trades)
    export_shadow_markets(shadow)
    export_shadow_signals(shadow)
    export_price_trails(shadow)
    export_odds_trails(shadow)

    print_summary(trades, shadow)
    print(f"Archivos exportados en: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
