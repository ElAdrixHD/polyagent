#!/usr/bin/env python3
"""
Análisis completo de datos de trading para Tight Market Crypto.
Lee CSVs de data/exports/ y genera análisis puro (sin recomendaciones).

Uso:
  python scripts/analyze.py
"""

import pandas as pd
import numpy as np
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = PROJECT_ROOT / "data" / "exports"

# ── Load data ────────────────────────────────────────────────────────────────

trades = pd.read_csv(EXPORTS_DIR / "trades.csv") if (EXPORTS_DIR / "trades.csv").exists() else pd.DataFrame()
shadow = pd.read_csv(EXPORTS_DIR / "shadow_markets.csv") if (EXPORTS_DIR / "shadow_markets.csv").exists() else pd.DataFrame()
signals = pd.read_csv(EXPORTS_DIR / "shadow_signals.csv") if (EXPORTS_DIR / "shadow_signals.csv").exists() else pd.DataFrame()
price_trails = pd.read_csv(EXPORTS_DIR / "price_trails.csv") if (EXPORTS_DIR / "price_trails.csv").exists() else pd.DataFrame()
odds_trails = pd.read_csv(EXPORTS_DIR / "odds_trails.csv") if (EXPORTS_DIR / "odds_trails.csv").exists() else pd.DataFrame()

HAS_TRADES = len(trades) > 0
HAS_SHADOW = len(shadow) > 0
HAS_SIGNALS = len(signals) > 0
HAS_PRICE_TRAILS = len(price_trails) > 0
HAS_ODDS_TRAILS = len(odds_trails) > 0

# ── Derived columns on trades ────────────────────────────────────────────────

if HAS_TRADES:
    trades["win"] = trades["net_return"] > 0
    trades["outcome_dir"] = trades["outcome"].str.upper()
    trades["dist_em_ratio"] = trades["distance"] / trades["expected_move"].replace(0, np.nan)
    trades["min_ask"] = trades[["yes_ask", "no_ask"]].min(axis=1)
    trades["max_ask"] = trades[["yes_ask", "no_ask"]].max(axis=1)
    trades["ask_imbalance"] = trades["max_ask"] - trades["min_ask"]
    trades["bought_ask"] = trades["buy_ask"]
    win_df = trades[trades["win"]].copy()
    loss_df = trades[~trades["win"]].copy()

# ── Derived columns on shadow ────────────────────────────────────────────────

if HAS_SHADOW:
    shadow["yes_resolved"] = shadow["final_yes"] >= 0.95
    shadow["no_resolved"] = shadow["final_no"] >= 0.95
    shadow["hyp_win"] = (
        ((shadow["outcome"] == "YES") & shadow["yes_resolved"])
        | ((shadow["outcome"] == "NO") & shadow["no_resolved"])
    )


print("=" * 70)
print("POLYAGENT — TIGHT MARKET CRYPTO ANALYSIS")
print("=" * 70)

# ══════════════════════════════════════════════════════════════════════════════
# 1. WIN/LOSS SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[1] WIN / LOSS SUMMARY")
    print("-" * 40)
    total = len(trades)
    wins = trades["win"].sum()
    losses = total - wins

    print(f"  Total trades        : {total}")
    print(f"  Wins                : {wins}  ({wins/total*100:.1f}%)")
    print(f"  Losses              : {losses}  ({losses/total*100:.1f}%)")
    print(f"\n  Avg return  (wins)  : ${win_df['net_return'].mean():.4f}  ({win_df['return_pct'].mean():.2f}%)")
    print(f"  Avg return  (losses): ${loss_df['net_return'].mean():.4f}  ({loss_df['return_pct'].mean():.2f}%)")
    print(f"\n  Total net P&L       : ${trades['net_return'].sum():.4f}")
    print(f"  Median trade return : ${trades['net_return'].median():.4f}")
    print(f"  Best trade          : ${trades['net_return'].max():.4f}  ({trades['return_pct'].max():.2f}%)")
    print(f"  Worst trade         : ${trades['net_return'].min():.4f}  ({trades['return_pct'].min():.2f}%)")

    profit_factor = (
        abs(win_df["net_return"].sum() / loss_df["net_return"].sum())
        if loss_df["net_return"].sum() != 0
        else float("inf")
    )
    print(f"  Profit factor       : {profit_factor:.4f}")

# ══════════════════════════════════════════════════════════════════════════════
# 2. SIGNAL QUALITY BY ASSET
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[2] SIGNAL QUALITY BY ASSET")
    print("-" * 40)
    asset_grp = trades.groupby("asset").agg(
        trades=("win", "count"),
        wins=("win", "sum"),
        avg_ret=("net_return", "mean"),
        total_pnl=("net_return", "sum"),
        avg_win_ret=("net_return", lambda x: x[x > 0].mean()),
        avg_loss_ret=("net_return", lambda x: x[x < 0].mean()),
    ).assign(win_rate=lambda d: d["wins"] / d["trades"] * 100)
    print(asset_grp[["trades", "wins", "win_rate", "avg_ret", "total_pnl", "avg_win_ret", "avg_loss_ret"]].to_string())

# ══════════════════════════════════════════════════════════════════════════════
# 3. TIMING ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[3] TIMING ANALYSIS (seconds_remaining)")
    print("-" * 40)
    bins = [0, 2, 4, 6, 8, 10, 15, 999]
    labels = ["0-2s", "2-4s", "4-6s", "6-8s", "8-10s", "10-15s", "15s+"]
    trades["timing_bin"] = pd.cut(trades["seconds_remaining"], bins=bins, labels=labels, right=False)
    timing = trades.groupby("timing_bin", observed=True).agg(
        count=("win", "count"),
        win_rate=("win", lambda x: x.mean() * 100),
        avg_return=("net_return", "mean"),
    )
    print(timing.to_string())
    print(f"\n  Wins  avg seconds_remaining : {win_df['seconds_remaining'].mean():.2f}s")
    print(f"  Losses avg seconds_remaining: {loss_df['seconds_remaining'].mean():.2f}s")

# ══════════════════════════════════════════════════════════════════════════════
# 4. TIGHT RATIO ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[4] TIGHT_RATIO: WINS vs LOSSES")
    print("-" * 40)
    print(f"  Wins   avg: {win_df['tight_ratio'].mean():.4f}   med: {win_df['tight_ratio'].median():.4f}")
    print(f"  Losses avg: {loss_df['tight_ratio'].mean():.4f}   med: {loss_df['tight_ratio'].median():.4f}")

    trades["tr_q"] = pd.qcut(
        trades["tight_ratio"], q=4, labels=["Q1(low)", "Q2", "Q3", "Q4(high)"], duplicates="drop"
    )
    tr_q = trades.groupby("tr_q", observed=True).agg(
        count=("win", "count"),
        win_rate=("win", lambda x: x.mean() * 100),
        avg_return=("net_return", "mean"),
    )
    print("\n  By tight_ratio quartile:")
    print(tr_q.to_string())

# ══════════════════════════════════════════════════════════════════════════════
# 5. DISTANCE / EXPECTED_MOVE RATIO
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[5] DISTANCE / EXPECTED_MOVE RATIO")
    print("-" * 40)
    valid = trades.dropna(subset=["dist_em_ratio"])
    vw = valid[valid["win"]]
    vl = valid[~valid["win"]]
    print(f"  Wins   avg: {vw['dist_em_ratio'].mean():.4f}   med: {vw['dist_em_ratio'].median():.4f}")
    print(f"  Losses avg: {vl['dist_em_ratio'].mean():.4f}   med: {vl['dist_em_ratio'].median():.4f}")

    valid = valid.copy()
    valid["ratio_bin"] = pd.cut(
        valid["dist_em_ratio"],
        bins=[0, 0.5, 1.0, 2.0, 5.0, 9999],
        labels=["<0.5", "0.5-1", "1-2", "2-5", ">5"],
    )
    ratio_grp = valid.groupby("ratio_bin", observed=True).agg(
        count=("win", "count"),
        win_rate=("win", lambda x: x.mean() * 100),
        avg_return=("net_return", "mean"),
    )
    print("\n  By distance/expected_move bucket:")
    print(ratio_grp.to_string())

# ══════════════════════════════════════════════════════════════════════════════
# 6. MISSED OPPORTUNITIES (SHADOW DATA)
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SHADOW:
    print("\n[6] MISSED OPPORTUNITIES (shadow not traded)")
    print("-" * 40)
    not_traded = shadow[shadow["was_traded"] == False].copy()
    traded_sh = shadow[shadow["was_traded"] == True].copy()

    print(f"  Total shadow markets  : {len(shadow)}")
    print(f"  Traded                : {len(traded_sh)}")
    print(f"  Not traded            : {len(not_traded)}")

    nt_resolved = not_traded[(not_traded["yes_resolved"] | not_traded["no_resolved"])].copy()
    hyp_wins = nt_resolved["hyp_win"].sum()
    hyp_total = len(nt_resolved)

    if hyp_total > 0:
        hyp_loss = hyp_total - hyp_wins
        print(f"\n  Resolved not-traded: {hyp_total}")
        print(f"    Hypothetical wins : {hyp_wins}  ({hyp_wins/hyp_total*100:.1f}%)")
        print(f"    Hypothetical losses: {hyp_loss}  ({hyp_loss/hyp_total*100:.1f}%)")

        missed_w = nt_resolved[nt_resolved["hyp_win"]]
        missed_l = nt_resolved[~nt_resolved["hyp_win"]]

        for label, subset in [("Missed WINNERS", missed_w), ("Missed LOSERS", missed_l)]:
            if len(subset) == 0:
                continue
            print(f"\n  {label} (n={len(subset)}):")
            print(f"    avg tight_ratio        : {subset['tight_ratio'].mean():.4f}")
            print(f"    avg volatility         : {subset['volatility'].mean():.6f}")
            print(f"    avg min_dist_strike    : {subset['min_distance_to_strike'].mean():.4f}")
            print(f"    avg num_skipped_signals: {subset['num_skipped_signals'].mean():.1f}")
            print(f"    price_crossed_strike   : {subset['price_crossed_strike'].mean()*100:.1f}%")
            print(f"    reversal_detected      : {subset['reversal_detected'].mean()*100:.1f}%")

    print(f"\n  Skipped signal counts:")
    print(f"    Not traded avg: {not_traded['num_skipped_signals'].mean():.1f}")
    print(f"    Traded avg    : {traded_sh['num_skipped_signals'].mean():.1f}")

    # By asset
    nt_res_asset = nt_resolved.groupby("asset").agg(
        total=("hyp_win", "count"),
        hyp_wins=("hyp_win", "sum"),
        avg_tr=("tight_ratio", "mean"),
        avg_vol=("volatility", "mean"),
    ).assign(hyp_win_rate=lambda d: d["hyp_wins"] / d["total"] * 100)
    print(f"\n  By asset:")
    print(nt_res_asset.to_string())

# ══════════════════════════════════════════════════════════════════════════════
# 7. REVERSAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SHADOW and HAS_TRADES:
    print("\n[7] REVERSAL ANALYSIS")
    print("-" * 40)
    traded_sh = shadow[shadow["was_traded"] == True].copy()
    traded_merged = traded_sh.merge(
        trades[["condition_id", "net_return", "win"]], on="condition_id", how="inner"
    )
    print(f"  Matched: {len(traded_merged)}")

    if len(traded_merged) > 0:
        for val, label in [(True, "reversal_detected=True"), (False, "reversal_detected=False")]:
            sub = traded_merged[traded_merged["reversal_detected"] == val]
            if len(sub) > 0:
                print(f"  {label} (n={len(sub)}): WR {sub['win'].mean()*100:.1f}%  avg P&L ${sub['net_return'].mean():.4f}")

    not_traded = shadow[shadow["was_traded"] == False].copy()
    print(f"\n  price_crossed_strike rates:")
    print(f"    Traded     : {traded_sh['price_crossed_strike'].mean()*100:.1f}%")
    print(f"    Not-traded : {not_traded['price_crossed_strike'].mean()*100:.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
# 8. ONE-SIDED MARKET ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_TRADES:
    print("\n[8] ONE-SIDED MARKET ANALYSIS (ask imbalance)")
    print("-" * 40)
    print(f"  Wins   avg min_ask: {win_df['min_ask'].mean():.4f}  max_ask: {win_df['max_ask'].mean():.4f}  imbalance: {win_df['ask_imbalance'].mean():.4f}")
    print(f"  Losses avg min_ask: {loss_df['min_ask'].mean():.4f}  max_ask: {loss_df['max_ask'].mean():.4f}  imbalance: {loss_df['ask_imbalance'].mean():.4f}")

    print(f"\n  bought_ask (price paid for directional bet):")
    print(f"    Wins   avg: {win_df['bought_ask'].mean():.4f}")
    print(f"    Losses avg: {loss_df['bought_ask'].mean():.4f}")

    trades["bought_bin"] = pd.cut(
        trades["bought_ask"],
        bins=[0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0],
        labels=["<0.1", "0.1-0.2", "0.2-0.3", "0.3-0.5", "0.5-0.7", "0.7+"],
    )
    ba_grp = trades.groupby("bought_bin", observed=True).agg(
        count=("win", "count"),
        win_rate=("win", lambda x: x.mean() * 100),
        avg_return=("net_return", "mean"),
    )
    print("\n  By bought_ask bucket:")
    print(ba_grp.to_string())

# ══════════════════════════════════════════════════════════════════════════════
# 9. VOLATILITY ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SHADOW and HAS_TRADES:
    print("\n[9] VOLATILITY ANALYSIS")
    print("-" * 40)
    traded_sh = shadow[shadow["was_traded"] == True].copy()
    vol_merge = traded_sh.merge(
        trades[["condition_id", "net_return", "win", "return_pct"]], on="condition_id", how="inner"
    )
    if len(vol_merge) > 0:
        vmw = vol_merge[vol_merge["win"]]
        vml = vol_merge[~vol_merge["win"]]
        print(f"  Wins   avg vol: {vmw['volatility'].mean():.7f}  med: {vmw['volatility'].median():.7f}")
        print(f"  Losses avg vol: {vml['volatility'].mean():.7f}  med: {vml['volatility'].median():.7f}")

        vm = vol_merge.copy()
        vm["vol_q"] = pd.qcut(vm["volatility"], q=4, labels=["Q1(low)", "Q2", "Q3", "Q4(high)"], duplicates="drop")
        vq = vm.groupby("vol_q", observed=True).agg(
            count=("win", "count"),
            win_rate=("win", lambda x: x.mean() * 100),
            avg_return=("net_return", "mean"),
            vol_range_low=("volatility", lambda x: x.quantile(0.05)),
            vol_range_high=("volatility", lambda x: x.quantile(0.95)),
        )
        print("\n  By volatility quartile:")
        print(vq.to_string())

        if len(vmw) > 0:
            print(f"\n  Winning vol IQR: [{vmw['volatility'].quantile(0.25):.7f} - {vmw['volatility'].quantile(0.75):.7f}]")
        print(f"  All trades vol IQR: [{vol_merge['volatility'].quantile(0.25):.7f} - {vol_merge['volatility'].quantile(0.75):.7f}]")

# ══════════════════════════════════════════════════════════════════════════════
# 10. EXECUTION WINDOW ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SHADOW and HAS_TRADES:
    print("\n[10] EXECUTION WINDOW ANALYSIS")
    print("-" * 40)
    traded_sh = shadow[shadow["was_traded"] == True].copy()
    vol_merge = traded_sh.merge(
        trades[["condition_id", "net_return", "win"]], on="condition_id", how="inner"
    )
    if len(vol_merge) > 0:
        ew_w = vol_merge[vol_merge["win"]]
        ew_l = vol_merge[~vol_merge["win"]]
        for col in ["min_distance_to_strike", "max_distance_to_strike", "price_momentum_last_3s", "expected_move_exec_window"]:
            print(f"  {col}:")
            print(f"    Wins   avg: {ew_w[col].mean():.4f}   med: {ew_w[col].median():.4f}")
            print(f"    Losses avg: {ew_l[col].mean():.4f}   med: {ew_l[col].median():.4f}")

# ══════════════════════════════════════════════════════════════════════════════
# 11. CRYPTO PRICE VARIANCE vs STRIKE (last 30s)
# ══════════════════════════════════════════════════════════════════════════════

if HAS_PRICE_TRAILS and HAS_SHADOW:
    print("\n[11] CRYPTO PRICE VARIANCE vs STRIKE (exec window)")
    print("-" * 40)

    # Filter exec window trails with t <= 30
    exec_prices = price_trails[(price_trails["window_type"] == "exec") & (price_trails["t"] <= 30)].copy()

    if len(exec_prices) > 0:
        # Per-market stats: variance of price, variance of distance to strike
        mkt_price_stats = exec_prices.groupby("condition_id").agg(
            price_mean=("price", "mean"),
            price_std=("price", "std"),
            price_min=("price", "min"),
            price_max=("price", "max"),
            price_range=("price", lambda x: x.max() - x.min()),
            dist_mean=("dist", "mean"),
            dist_std=("dist", "std"),
            dist_min=("dist", "min"),
            dist_max=("dist", "max"),
            dist_range=("dist", lambda x: x.max() - x.min()),
            snapshots=("price", "count"),
        ).reset_index()

        # Merge with shadow to get outcome info
        mkt_price_stats = mkt_price_stats.merge(
            shadow[["condition_id", "was_traded", "outcome", "strike_price", "asset"]],
            on="condition_id",
            how="left",
        )
        # Relative price range as % of strike
        mkt_price_stats["price_range_pct"] = mkt_price_stats["price_range"] / mkt_price_stats["strike_price"] * 100

        print(f"  Markets with exec window price data: {len(mkt_price_stats)}")
        print(f"\n  Overall price stats in exec window:")
        print(f"    avg price_range (abs)     : ${mkt_price_stats['price_range'].mean():.2f}")
        print(f"    avg price_range (% strike): {mkt_price_stats['price_range_pct'].mean():.4f}%")
        print(f"    avg price_std             : ${mkt_price_stats['price_std'].mean():.2f}")
        print(f"    avg dist_to_strike range  : ${mkt_price_stats['dist_range'].mean():.2f}")
        print(f"    avg dist_to_strike std    : ${mkt_price_stats['dist_std'].mean():.2f}")

        # Traded vs not traded
        for label, mask in [("Traded", mkt_price_stats["was_traded"] == True), ("Not traded", mkt_price_stats["was_traded"] == False)]:
            sub = mkt_price_stats[mask]
            if len(sub) == 0:
                continue
            print(f"\n  {label} (n={len(sub)}):")
            print(f"    avg price_range     : ${sub['price_range'].mean():.2f}")
            print(f"    avg price_range_pct : {sub['price_range_pct'].mean():.4f}%")
            print(f"    avg dist_std        : ${sub['dist_std'].mean():.2f}")
            print(f"    avg dist_mean       : ${sub['dist_mean'].mean():.2f}")

        # By asset
        print(f"\n  By asset:")
        for asset in sorted(mkt_price_stats["asset"].dropna().unique()):
            sub = mkt_price_stats[mkt_price_stats["asset"] == asset]
            print(f"    {asset}: n={len(sub)}  price_range=${sub['price_range'].mean():.2f}  "
                  f"dist_std=${sub['dist_std'].mean():.2f}  dist_mean=${sub['dist_mean'].mean():.2f}")

        # If we have trades, merge for win/loss analysis
        if HAS_TRADES:
            price_trade_merge = mkt_price_stats.merge(
                trades[["condition_id", "win", "net_return"]], on="condition_id", how="inner"
            )
            if len(price_trade_merge) > 0:
                pw = price_trade_merge[price_trade_merge["win"]]
                pl = price_trade_merge[~price_trade_merge["win"]]
                print(f"\n  Wins vs Losses — price variance in exec window:")
                print(f"    Wins   (n={len(pw)}): price_range=${pw['price_range'].mean():.2f}  dist_std=${pw['dist_std'].mean():.2f}  dist_mean=${pw['dist_mean'].mean():.2f}")
                print(f"    Losses (n={len(pl)}): price_range=${pl['price_range'].mean():.2f}  dist_std=${pl['dist_std'].mean():.2f}  dist_mean=${pl['dist_mean'].mean():.2f}")

        # Quartile analysis on dist_std
        if len(mkt_price_stats) >= 4:
            try:
                _, bins = pd.qcut(mkt_price_stats["dist_std"], q=4, duplicates="drop", retbins=True)
                q_labels = [f"Q{i+1}" for i in range(len(bins) - 1)]
                if len(q_labels) >= 2:
                    q_labels[0] += "(stable)"
                    q_labels[-1] += "(volatile)"
                mkt_price_stats["dist_std_q"] = pd.qcut(mkt_price_stats["dist_std"], q=4, labels=q_labels, duplicates="drop")
                q_stats = mkt_price_stats.groupby("dist_std_q", observed=True).agg(
                    count=("condition_id", "count"),
                    was_traded_pct=("was_traded", lambda x: x.mean() * 100),
                    avg_dist_mean=("dist_mean", "mean"),
                    avg_price_range=("price_range", "mean"),
                )
                print(f"\n  By distance-to-strike stability (dist_std quartile):")
                print(q_stats.to_string())
            except ValueError:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# 12. POLYMARKET ODDS VARIANCE (last 30s)
# ══════════════════════════════════════════════════════════════════════════════

if HAS_ODDS_TRAILS and HAS_SHADOW:
    print("\n[12] POLYMARKET ODDS VARIANCE (exec window, last 30s)")
    print("-" * 40)

    exec_odds = odds_trails[(odds_trails["window_type"] == "exec") & (odds_trails["t"] <= 30)].copy()

    if len(exec_odds) > 0:
        # Per-market odds stats
        mkt_odds_stats = exec_odds.groupby("condition_id").agg(
            yes_mean=("yes", "mean"),
            yes_std=("yes", "std"),
            yes_min=("yes", "min"),
            yes_max=("yes", "max"),
            yes_range=("yes", lambda x: x.max() - x.min()),
            no_mean=("no", "mean"),
            no_std=("no", "std"),
            no_range=("no", lambda x: x.max() - x.min()),
            snapshots=("yes", "count"),
        ).reset_index()

        mkt_odds_stats = mkt_odds_stats.merge(
            shadow[["condition_id", "was_traded", "outcome", "asset", "majority_at_exec_start"]],
            on="condition_id",
            how="left",
        )

        # Total odds swing = yes_range (since no = 1 - yes basically)
        mkt_odds_stats["odds_swing"] = mkt_odds_stats["yes_range"]

        print(f"  Markets with odds data: {len(mkt_odds_stats)}")
        print(f"\n  Overall odds stats in last 30s of exec window:")
        print(f"    avg yes_range (swing)  : {mkt_odds_stats['odds_swing'].mean():.4f}")
        print(f"    avg yes_std            : {mkt_odds_stats['yes_std'].mean():.4f}")
        print(f"    max yes_range observed : {mkt_odds_stats['odds_swing'].max():.4f}")
        print(f"    markets with swing > 0.1: {(mkt_odds_stats['odds_swing'] > 0.1).sum()}")
        print(f"    markets with swing > 0.5: {(mkt_odds_stats['odds_swing'] > 0.5).sum()}")

        # Traded vs not traded
        for label, mask in [("Traded", mkt_odds_stats["was_traded"] == True), ("Not traded", mkt_odds_stats["was_traded"] == False)]:
            sub = mkt_odds_stats[mask]
            if len(sub) == 0:
                continue
            print(f"\n  {label} (n={len(sub)}):")
            print(f"    avg odds_swing : {sub['odds_swing'].mean():.4f}")
            print(f"    avg yes_std    : {sub['yes_std'].mean():.4f}")
            print(f"    swing > 0.1    : {(sub['odds_swing'] > 0.1).sum()} ({(sub['odds_swing'] > 0.1).mean()*100:.1f}%)")

        # By asset
        print(f"\n  By asset:")
        for asset in sorted(mkt_odds_stats["asset"].dropna().unique()):
            sub = mkt_odds_stats[mkt_odds_stats["asset"] == asset]
            print(f"    {asset}: n={len(sub)}  avg_swing={sub['odds_swing'].mean():.4f}  "
                  f"avg_yes_std={sub['yes_std'].mean():.4f}  swing>0.1: {(sub['odds_swing'] > 0.1).sum()}")

        # Merge with trades for win/loss
        if HAS_TRADES:
            odds_trade_merge = mkt_odds_stats.merge(
                trades[["condition_id", "win", "net_return"]], on="condition_id", how="inner"
            )
            if len(odds_trade_merge) > 0:
                ow = odds_trade_merge[odds_trade_merge["win"]]
                ol = odds_trade_merge[~odds_trade_merge["win"]]
                print(f"\n  Wins vs Losses — odds variance:")
                print(f"    Wins   (n={len(ow)}): odds_swing={ow['odds_swing'].mean():.4f}  yes_std={ow['yes_std'].mean():.4f}")
                print(f"    Losses (n={len(ol)}): odds_swing={ol['odds_swing'].mean():.4f}  yes_std={ol['yes_std'].mean():.4f}")

        # Quartile analysis on odds swing
        if len(mkt_odds_stats) >= 4:
            try:
                _, bins = pd.qcut(mkt_odds_stats["odds_swing"], q=4, duplicates="drop", retbins=True)
                q_labels = [f"Q{i+1}" for i in range(len(bins) - 1)]
                if len(q_labels) >= 2:
                    q_labels[0] += "(calm)"
                    q_labels[-1] += "(wild)"
                mkt_odds_stats["swing_q"] = pd.qcut(mkt_odds_stats["odds_swing"], q=4, labels=q_labels, duplicates="drop")
                oq = mkt_odds_stats.groupby("swing_q", observed=True).agg(
                    count=("condition_id", "count"),
                    was_traded_pct=("was_traded", lambda x: x.mean() * 100),
                    avg_swing=("odds_swing", "mean"),
                    avg_yes_std=("yes_std", "mean"),
                )
                print(f"\n  By odds swing quartile:")
                print(oq.to_string())
            except ValueError:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# 13. SIGNAL ENGINE STATISTICS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SIGNALS:
    print("\n[13] SIGNAL ENGINE STATISTICS")
    print("-" * 40)
    total_sigs = len(signals)
    fired = signals[signals["would_have_fired"] == True]
    not_fired = signals[signals["would_have_fired"] == False]
    boost_only = not_fired[not_fired["would_have_passed_with_boost"] == True]
    in_ew = signals[signals["in_execution_window"] == True]

    print(f"  Total signal evaluations       : {total_sigs}")
    print(f"  In execution window            : {len(in_ew)}  ({len(in_ew)/total_sigs*100:.1f}%)")
    print(f"  Would have fired (raw)         : {len(fired)}  ({len(fired)/total_sigs*100:.2f}%)")
    print(f"  Only passes with boost         : {len(boost_only)}  ({len(boost_only)/total_sigs*100:.2f}%)")

    if len(fired) > 0:
        print(f"\n  ratio_raw stats for FIRED:")
        print(f"    mean: {fired['ratio_raw'].mean():.4f}  med: {fired['ratio_raw'].median():.4f}  "
              f"min: {fired['ratio_raw'].min():.4f}  max: {fired['ratio_raw'].max():.4f}")
    if len(not_fired) > 0:
        print(f"  ratio_raw stats for NOT-FIRED:")
        print(f"    mean: {not_fired['ratio_raw'].mean():.4f}  med: {not_fired['ratio_raw'].median():.4f}")

    print(f"\n  Fire rate by asset:")
    sig_asset = signals.groupby("asset").agg(
        total=("would_have_fired", "count"),
        fired=("would_have_fired", "sum"),
        fire_rate=("would_have_fired", lambda x: x.mean() * 100),
        avg_ratio=("ratio_raw", "mean"),
        avg_dist=("distance", "mean"),
    )
    print(sig_asset.to_string())

    if len(fired) > 0:
        print(f"\n  Remaining seconds when FIRED:")
        print(f"    mean: {fired['remaining'].mean():.2f}s  med: {fired['remaining'].median():.2f}s  "
              f"min: {fired['remaining'].min():.2f}s  max: {fired['remaining'].max():.2f}s")
    if len(in_ew) > 0:
        print(f"\n  In-execution-window fire rate: {in_ew['would_have_fired'].mean()*100:.1f}%")

# ══════════════════════════════════════════════════════════════════════════════
# 14. ENTRY/NO-ENTRY DECISION ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

if HAS_SHADOW:
    print("\n[14] ENTRY/NO-ENTRY DECISION ANALYSIS")
    print("-" * 40)
    print("  Why did we NOT enter markets? Comparing traded vs not-traded:")
    traded_sh = shadow[shadow["was_traded"] == True]
    not_traded_sh = shadow[shadow["was_traded"] == False]

    compare_cols = [
        "tight_ratio", "volatility", "min_distance_to_strike",
        "max_distance_to_strike", "expected_move_exec_window",
        "price_momentum_last_3s", "total_snapshots", "num_skipped_signals",
    ]
    print(f"\n  {'metric':30s} {'traded_avg':>14s} {'not_traded_avg':>14s} {'diff':>10s}")
    print(f"  {'-'*68}")
    for col in compare_cols:
        t_val = traded_sh[col].mean()
        nt_val = not_traded_sh[col].mean()
        diff = t_val - nt_val
        print(f"  {col:30s} {t_val:14.4f} {nt_val:14.4f} {diff:+10.4f}")

    # Reversal & crossed strike rates
    print(f"\n  Boolean rates:")
    for col in ["price_crossed_strike", "reversal_detected"]:
        t_pct = traded_sh[col].mean() * 100
        nt_pct = not_traded_sh[col].mean() * 100
        print(f"    {col:30s}  traded: {t_pct:.1f}%  not_traded: {nt_pct:.1f}%")

    # Majority side distribution
    print(f"\n  majority_at_exec_start distribution:")
    for side in ["YES", "NO"]:
        t_pct = (traded_sh["majority_at_exec_start"] == side).mean() * 100
        nt_pct = (not_traded_sh["majority_at_exec_start"] == side).mean() * 100
        print(f"    {side}: traded {t_pct:.1f}%  not_traded {nt_pct:.1f}%")


print("\n" + "=" * 70)
print("Analysis complete.")
print("=" * 70)
