#!/usr/bin/env python3
"""
Reversal Analysis para Tight-Market Crypto Strategy.
Analiza la dinámica de "cheap side wins" usando datos exportados.

Lee de data/exports/ (generados por export_data.py).

Uso:
  python scripts/reversal_analysis.py
"""

import pandas as pd
import numpy as np
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORTS_DIR = PROJECT_ROOT / "data" / "exports"

SEP = "=" * 70

# ── Load data ────────────────────────────────────────────────────────────────

sm = pd.read_csv(EXPORTS_DIR / "shadow_markets.csv") if (EXPORTS_DIR / "shadow_markets.csv").exists() else pd.DataFrame()
ot = pd.read_csv(EXPORTS_DIR / "odds_trails.csv") if (EXPORTS_DIR / "odds_trails.csv").exists() else pd.DataFrame()
pt = pd.read_csv(EXPORTS_DIR / "price_trails.csv") if (EXPORTS_DIR / "price_trails.csv").exists() else pd.DataFrame()

if len(sm) == 0:
    print("No shadow_markets.csv found. Run export_data.py first.")
    exit(1)

# ── Derived columns ──────────────────────────────────────────────────────────

sm["majority_wins"] = sm["outcome"] == sm["majority_at_exec_start"]
sm["underdog_wins"] = ~sm["majority_wins"]

sm["cheap_side_ask"] = sm.apply(
    lambda r: r["final_no"] if r["majority_at_exec_start"] == "YES" else r["final_yes"],
    axis=1,
)
sm["expensive_side_ask"] = sm.apply(
    lambda r: r["final_yes"] if r["majority_at_exec_start"] == "YES" else r["final_no"],
    axis=1,
)
sm["total_ask"] = sm["final_yes"] + sm["final_no"]
sm["directional_pnl"] = sm.apply(
    lambda r: (1.0 - r["cheap_side_ask"]) if r["underdog_wins"] else -r["cheap_side_ask"],
    axis=1,
)

# ─────────────────────────────────────────────────────────────────────────────
# 0. DATA OVERVIEW
# ─────────────────────────────────────────────────────────────────────────────

reversal_agree = (sm["reversal_detected"] == sm["underdog_wins"]).sum()
print(SEP)
print("DATA OVERVIEW")
print(SEP)
print(f"Total shadow markets: {len(sm)}")
print(f"  Was traded:   {sm['was_traded'].sum()} ({sm['was_traded'].mean()*100:.1f}%)")
print(f"  Not traded:   {(~sm['was_traded']).sum()}")
print(f"  YES outcome:  {(sm['outcome']=='YES').sum()}")
print(f"  NO outcome:   {(sm['outcome']=='NO').sum()}")
print()
print(f"reversal_detected vs underdog_wins agreement: {reversal_agree}/{len(sm)} ({reversal_agree/len(sm)*100:.1f}%)")
print()
print(f"majority_at_exec_start:")
print(f"  YES majority: {(sm['majority_at_exec_start']=='YES').sum()}")
print(f"  NO  majority: {(sm['majority_at_exec_start']=='NO').sum()}")

# By asset
print(f"\nBy asset:")
for asset in sorted(sm["asset"].dropna().unique()):
    sub = sm[sm["asset"] == asset]
    print(f"  {asset}: {len(sub)} markets  traded: {sub['was_traded'].sum()}  "
          f"underdog_wins: {sub['underdog_wins'].sum()} ({sub['underdog_wins'].mean()*100:.1f}%)")

# ─────────────────────────────────────────────────────────────────────────────
# 1. REVERSAL RATE BY CONTEXT
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 1: REVERSAL RATE BY CONTEXT")
print(SEP)

# 1a. Overall
overall_reversal = sm["underdog_wins"].mean()
print(f"\n1a. Overall reversal rate: {sm['underdog_wins'].sum()}/{len(sm)} = {overall_reversal*100:.2f}%")

# 1b. By min_distance_to_strike quartile
print("\n1b. By min_distance_to_strike quartile:")
sm["dist_quartile"] = pd.qcut(sm["min_distance_to_strike"], q=4,
                                labels=["Q1 (closest)", "Q2", "Q3", "Q4 (farthest)"], duplicates="drop")
dist_stats = sm.groupby("dist_quartile", observed=True).agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
    dist_min=("min_distance_to_strike", "min"),
    dist_max=("min_distance_to_strike", "max"),
).reset_index()
for _, row in dist_stats.iterrows():
    print(f"  {row['dist_quartile']:20s}: {row['reversals']:3.0f}/{row['count']:3.0f} = {row['reversal_rate']*100:5.1f}%  "
          f"[{row['dist_min']:.3f} – {row['dist_max']:.3f}]")

# Threshold splits
print("\n  Threshold splits:")
for thresh in [0.5, 2.0, 10.0, 50.0]:
    below = sm[sm["min_distance_to_strike"] < thresh]
    above = sm[sm["min_distance_to_strike"] >= thresh]
    if len(below) > 0 and len(above) > 0:
        print(f"  < {thresh:6.1f}: n={len(below):3d}  reversal={below['underdog_wins'].mean()*100:.1f}%  |  "
              f">= {thresh:6.1f}: n={len(above):3d}  reversal={above['underdog_wins'].mean()*100:.1f}%")

# 1c. By volatility quartile
print("\n1c. By volatility quartile:")
sm["vol_quartile"] = pd.qcut(sm["volatility"], q=4,
                               labels=["Q1 (lowest)", "Q2", "Q3", "Q4 (highest)"], duplicates="drop")
vol_stats = sm.groupby("vol_quartile", observed=True).agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
    vol_min=("volatility", "min"),
    vol_max=("volatility", "max"),
).reset_index()
for _, row in vol_stats.iterrows():
    print(f"  {str(row['vol_quartile']):20s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%  "
          f"[{row['vol_min']:.6f} – {row['vol_max']:.6f}]")

# Finer volatility bins
print("\n  Volatility bins:")
vol_edges = [0, 0.00006, 0.00008, 0.0001, 0.00013, 0.00020, 1.0]
vol_labels = ["<6e-5", "6-8e-5", "8-10e-5", "10-13e-5", "13-20e-5", ">20e-5"]
sm["vol_bin"] = pd.cut(sm["volatility"], bins=vol_edges, labels=vol_labels)
vol_bin_stats = sm.groupby("vol_bin", observed=True).agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
).reset_index()
for _, row in vol_bin_stats.iterrows():
    print(f"  {str(row['vol_bin']):12s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%")

# 1d. By momentum direction
print("\n1d. By price_momentum_last_3s direction:")
MOMENTUM_ZERO_THRESH = 0.01
sm["momentum_dir"] = "flat"
sm.loc[sm["price_momentum_last_3s"] > MOMENTUM_ZERO_THRESH, "momentum_dir"] = "bullish"
sm.loc[sm["price_momentum_last_3s"] < -MOMENTUM_ZERO_THRESH, "momentum_dir"] = "bearish"

mom_stats = sm.groupby("momentum_dir").agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
    avg_momentum=("price_momentum_last_3s", "mean"),
).reset_index()
for _, row in mom_stats.iterrows():
    print(f"  {row['momentum_dir']:10s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%  "
          f"(avg: {row['avg_momentum']:+.4f})")

# Momentum relative to majority
sm["momentum_vs_majority"] = "neutral"
sm.loc[(sm["majority_at_exec_start"] == "YES") & (sm["price_momentum_last_3s"] > MOMENTUM_ZERO_THRESH), "momentum_vs_majority"] = "confirming"
sm.loc[(sm["majority_at_exec_start"] == "YES") & (sm["price_momentum_last_3s"] < -MOMENTUM_ZERO_THRESH), "momentum_vs_majority"] = "contrarian"
sm.loc[(sm["majority_at_exec_start"] == "NO") & (sm["price_momentum_last_3s"] < -MOMENTUM_ZERO_THRESH), "momentum_vs_majority"] = "confirming"
sm.loc[(sm["majority_at_exec_start"] == "NO") & (sm["price_momentum_last_3s"] > MOMENTUM_ZERO_THRESH), "momentum_vs_majority"] = "contrarian"

mom_rel = sm.groupby("momentum_vs_majority").agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
).reset_index()
print("\n  Momentum vs majority:")
for _, row in mom_rel.iterrows():
    print(f"  {row['momentum_vs_majority']:12s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%")

# 1e. By price_crossed_strike
print("\n1e. By price_crossed_strike:")
pcs = sm.groupby("price_crossed_strike").agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
).reset_index()
for _, row in pcs.iterrows():
    print(f"  crossed={str(row['price_crossed_strike']):5s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%")

# 1f. By asset
print("\n1f. Reversal rate by asset:")
asset_rev = sm.groupby("asset").agg(
    count=("underdog_wins", "count"),
    reversals=("underdog_wins", "sum"),
    reversal_rate=("underdog_wins", "mean"),
    avg_vol=("volatility", "mean"),
    avg_dist=("min_distance_to_strike", "mean"),
).reset_index()
for _, row in asset_rev.iterrows():
    print(f"  {row['asset']:5s}: {int(row['reversals'])}/{int(row['count'])} = {row['reversal_rate']*100:.1f}%  "
          f"avg_vol={row['avg_vol']:.6f}  avg_dist={row['avg_dist']:.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# 2. TRADED vs NOT-TRADED
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 2: REVERSAL RATE — TRADED vs NOT-TRADED")
print(SEP)

traded = sm[sm["was_traded"]]
not_traded = sm[~sm["was_traded"]]

print(f"\nTraded   (n={len(traded):3d}): reversal rate = {traded['underdog_wins'].mean()*100:.1f}%  "
      f"({int(traded['underdog_wins'].sum())} reversals)")
print(f"Not-traded (n={len(not_traded):3d}): reversal rate = {not_traded['underdog_wins'].mean()*100:.1f}%  "
      f"({int(not_traded['underdog_wins'].sum())} reversals)")

print(f"\n  Traded markets context:")
print(f"    avg cheap_side_ask:         {traded['cheap_side_ask'].mean():.3f} (overall: {sm['cheap_side_ask'].mean():.3f})")
print(f"    avg volatility:             {traded['volatility'].mean():.6f} (overall: {sm['volatility'].mean():.6f})")
print(f"    avg min_distance_to_strike: {traded['min_distance_to_strike'].mean():.2f} (overall: {sm['min_distance_to_strike'].mean():.2f})")
print(f"    price_crossed_strike=True:  {traded['price_crossed_strike'].sum()}/{len(traded)} = {traded['price_crossed_strike'].mean()*100:.1f}%")

# By asset for traded vs not-traded
print(f"\n  By asset (traded):")
for asset in sorted(traded["asset"].dropna().unique()):
    sub = traded[traded["asset"] == asset]
    print(f"    {asset}: n={len(sub)}  reversal={sub['underdog_wins'].mean()*100:.1f}%")
if len(not_traded) > 0:
    print(f"  By asset (not-traded):")
    for asset in sorted(not_traded["asset"].dropna().unique()):
        sub = not_traded[not_traded["asset"] == asset]
        print(f"    {asset}: n={len(sub)}  reversal={sub['underdog_wins'].mean()*100:.1f}%")

# ─────────────────────────────────────────────────────────────────────────────
# 3. REVERSAL TIMING FROM ODDS_TRAILS
# ─────────────────────────────────────────────────────────────────────────────

if len(ot) > 0:
    print()
    print(SEP)
    print("SECTION 3: REVERSAL TIMING FROM ODDS_TRAILS")
    print(SEP)

    trail_meta = sm[["condition_id", "majority_at_exec_start", "underdog_wins", "outcome", "asset"]].copy()
    ot_merged = ot.merge(trail_meta, on="condition_id", how="left")
    exec_trails = ot_merged[ot_merged["window_type"] == "exec"].copy()

    reversal_ids = sm[sm["underdog_wins"] == True]["condition_id"].tolist()

    flip_times = []
    flip_assets = []
    odds_speeds = []

    for cid in reversal_ids:
        mkt_trail = exec_trails[exec_trails["condition_id"] == cid].copy()
        if mkt_trail.empty:
            continue
        mkt_trail = mkt_trail.sort_values("t", ascending=False)
        majority = mkt_trail["majority_at_exec_start"].iloc[0]
        asset = mkt_trail["asset"].iloc[0] if "asset" in mkt_trail.columns else "?"

        if majority == "YES":
            flipped = mkt_trail[mkt_trail["yes"] < 0.5]
        else:
            flipped = mkt_trail[mkt_trail["yes"] > 0.5]

        if flipped.empty:
            continue

        flip_t = flipped["t"].max()
        flip_times.append(flip_t)
        flip_assets.append(asset)

        near_flip = mkt_trail[mkt_trail["t"] >= flip_t].tail(3)
        if len(near_flip) >= 2:
            dt = near_flip["t"].max() - near_flip["t"].min()
            dyes = near_flip["yes"].max() - near_flip["yes"].min()
            if dt > 0:
                odds_speeds.append(abs(dyes) / dt)

    flip_times = np.array(flip_times)
    odds_speeds = np.array(odds_speeds)

    print(f"\nReversal markets: {len(reversal_ids)}  |  Detectable flip in trail: {len(flip_times)}")

    if len(flip_times) > 0:
        print(f"\nFlip timing (seconds before expiry):")
        print(f"  Mean: {flip_times.mean():.2f}s  Med: {np.median(flip_times):.2f}s  Std: {flip_times.std():.2f}s")
        print(f"  Min: {flip_times.min():.2f}s  Max: {flip_times.max():.2f}s")

        bins_t = [0, 1, 2, 3, 5, 8, 12, 100]
        bin_labels = ["0-1s", "1-2s", "2-3s", "3-5s", "5-8s", "8-12s", ">12s"]
        print(f"\n  Distribution:")
        for i in range(len(bins_t) - 1):
            mask = (flip_times >= bins_t[i]) & (flip_times < bins_t[i + 1])
            pct = mask.sum() / len(flip_times) * 100
            print(f"    {bin_labels[i]:8s}: {mask.sum():3d} ({pct:.1f}%)")

        if len(odds_speeds) > 0:
            print(f"\n  Odds movement speed near reversal:")
            print(f"    Mean: {odds_speeds.mean():.4f} prob/s  Med: {np.median(odds_speeds):.4f}  Max: {odds_speeds.max():.4f}")

        # Flip timing by asset
        if len(flip_assets) == len(flip_times):
            flip_df = pd.DataFrame({"asset": flip_assets, "flip_t": flip_times})
            print(f"\n  Flip timing by asset:")
            for asset in sorted(flip_df["asset"].dropna().unique()):
                sub = flip_df[flip_df["asset"] == asset]
                print(f"    {asset}: n={len(sub)}  mean={sub['flip_t'].mean():.2f}s  med={sub['flip_t'].median():.2f}s")

    # Non-reversal markets with 0.5-cross
    non_rev_ids = sm[sm["underdog_wins"] == False]["condition_id"].tolist()
    non_flip_times = []
    for cid in non_rev_ids:
        mkt_trail = exec_trails[exec_trails["condition_id"] == cid].copy()
        if mkt_trail.empty:
            continue
        majority = mkt_trail["majority_at_exec_start"].dropna()
        if majority.empty:
            continue
        majority = majority.iloc[0]
        mkt_trail = mkt_trail.sort_values("t", ascending=False)
        if majority == "YES":
            flipped = mkt_trail[mkt_trail["yes"] < 0.5]
        else:
            flipped = mkt_trail[mkt_trail["yes"] > 0.5]
        if not flipped.empty:
            non_flip_times.append(flipped["t"].max())

    print(f"\n  Non-reversal markets with brief 0.5-cross: {len(non_flip_times)}")
    if len(non_flip_times) > 0:
        nft = np.array(non_flip_times)
        print(f"    avg cross time: {nft.mean():.2f}s  (brief excursions)")

# ─────────────────────────────────────────────────────────────────────────────
# 4. CRYPTO PRICE VARIANCE vs STRIKE (by asset)
# ─────────────────────────────────────────────────────────────────────────────

if len(pt) > 0:
    print()
    print(SEP)
    print("SECTION 4: CRYPTO PRICE VARIANCE vs STRIKE (exec window, last 30s)")
    print(SEP)

    exec_prices = pt[(pt["window_type"] == "exec") & (pt["t"] <= 30)].copy()

    if len(exec_prices) > 0:
        mkt_pstats = exec_prices.groupby("condition_id").agg(
            price_mean=("price", "mean"),
            price_std=("price", "std"),
            price_range=("price", lambda x: x.max() - x.min()),
            dist_mean=("dist", "mean"),
            dist_std=("dist", "std"),
            dist_range=("dist", lambda x: x.max() - x.min()),
            snapshots=("price", "count"),
        ).reset_index()

        mkt_pstats = mkt_pstats.merge(
            sm[["condition_id", "was_traded", "underdog_wins", "strike_price", "asset"]],
            on="condition_id", how="left",
        )
        mkt_pstats["price_range_pct"] = mkt_pstats["price_range"] / mkt_pstats["strike_price"] * 100

        print(f"\n  Markets with price trail data: {len(mkt_pstats)}")

        # Overall
        print(f"\n  Overall:")
        print(f"    avg price_range      : ${mkt_pstats['price_range'].mean():.2f}")
        print(f"    avg price_range_%    : {mkt_pstats['price_range_pct'].mean():.4f}%")
        print(f"    avg dist_to_strike   : ${mkt_pstats['dist_mean'].mean():.2f}")
        print(f"    avg dist_std         : ${mkt_pstats['dist_std'].mean():.2f}")

        # By asset
        print(f"\n  By asset:")
        for asset in sorted(mkt_pstats["asset"].dropna().unique()):
            sub = mkt_pstats[mkt_pstats["asset"] == asset]
            print(f"    {asset} (n={len(sub)}):")
            print(f"      price_range: ${sub['price_range'].mean():.2f}  "
                  f"range_%: {sub['price_range_pct'].mean():.4f}%  "
                  f"dist_mean: ${sub['dist_mean'].mean():.2f}  "
                  f"dist_std: ${sub['dist_std'].mean():.2f}")

        # Reversal vs no reversal
        rev = mkt_pstats[mkt_pstats["underdog_wins"] == True]
        no_rev = mkt_pstats[mkt_pstats["underdog_wins"] == False]
        print(f"\n  Reversal vs No-reversal price behavior:")
        for label, sub in [("Reversal", rev), ("No reversal", no_rev)]:
            if len(sub) == 0:
                continue
            print(f"    {label} (n={len(sub)}): price_range=${sub['price_range'].mean():.2f}  "
                  f"dist_std=${sub['dist_std'].mean():.2f}  dist_mean=${sub['dist_mean'].mean():.2f}")

        # Traded vs not traded
        for label, mask in [("Traded", True), ("Not traded", False)]:
            sub = mkt_pstats[mkt_pstats["was_traded"] == mask]
            if len(sub) == 0:
                continue
            print(f"    {label} (n={len(sub)}): price_range=${sub['price_range'].mean():.2f}  "
                  f"range_%={sub['price_range_pct'].mean():.4f}%  dist_mean=${sub['dist_mean'].mean():.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# 5. POLYMARKET ODDS VARIANCE (last 30s, by asset)
# ─────────────────────────────────────────────────────────────────────────────

if len(ot) > 0:
    print()
    print(SEP)
    print("SECTION 5: POLYMARKET ODDS VARIANCE (exec window, last 30s)")
    print(SEP)

    exec_odds = ot[(ot["window_type"] == "exec") & (ot["t"] <= 30)].copy()

    if len(exec_odds) > 0:
        mkt_ostats = exec_odds.groupby("condition_id").agg(
            yes_mean=("yes", "mean"),
            yes_std=("yes", "std"),
            yes_range=("yes", lambda x: x.max() - x.min()),
            no_std=("no", "std"),
            snapshots=("yes", "count"),
        ).reset_index()

        mkt_ostats = mkt_ostats.merge(
            sm[["condition_id", "was_traded", "underdog_wins", "asset", "majority_at_exec_start"]],
            on="condition_id", how="left",
        )
        mkt_ostats["odds_swing"] = mkt_ostats["yes_range"]

        print(f"\n  Markets with odds trail data: {len(mkt_ostats)}")
        print(f"\n  Overall:")
        print(f"    avg odds_swing : {mkt_ostats['odds_swing'].mean():.4f}")
        print(f"    avg yes_std    : {mkt_ostats['yes_std'].mean():.4f}")
        print(f"    swing > 0.1    : {(mkt_ostats['odds_swing'] > 0.1).sum()} ({(mkt_ostats['odds_swing'] > 0.1).mean()*100:.1f}%)")
        print(f"    swing > 0.5    : {(mkt_ostats['odds_swing'] > 0.5).sum()} ({(mkt_ostats['odds_swing'] > 0.5).mean()*100:.1f}%)")

        # By asset
        print(f"\n  By asset:")
        for asset in sorted(mkt_ostats["asset"].dropna().unique()):
            sub = mkt_ostats[mkt_ostats["asset"] == asset]
            print(f"    {asset} (n={len(sub)}): avg_swing={sub['odds_swing'].mean():.4f}  "
                  f"yes_std={sub['yes_std'].mean():.4f}  swing>0.1: {(sub['odds_swing'] > 0.1).sum()}")

        # Reversal vs no reversal
        print(f"\n  Reversal vs No-reversal odds behavior:")
        for label, val in [("Reversal", True), ("No reversal", False)]:
            sub = mkt_ostats[mkt_ostats["underdog_wins"] == val]
            if len(sub) == 0:
                continue
            print(f"    {label} (n={len(sub)}): odds_swing={sub['odds_swing'].mean():.4f}  "
                  f"yes_std={sub['yes_std'].mean():.4f}  "
                  f"swing>0.1: {(sub['odds_swing'] > 0.1).sum()} ({(sub['odds_swing'] > 0.1).mean()*100:.1f}%)")

        # Traded vs not traded
        for label, val in [("Traded", True), ("Not traded", False)]:
            sub = mkt_ostats[mkt_ostats["was_traded"] == val]
            if len(sub) == 0:
                continue
            print(f"    {label} (n={len(sub)}): odds_swing={sub['odds_swing'].mean():.4f}  "
                  f"yes_std={sub['yes_std'].mean():.4f}")

        # Quartile analysis
        if len(mkt_ostats) >= 4:
            try:
                n_bins = pd.qcut(mkt_ostats["odds_swing"], q=4, duplicates="drop", retbins=True)[1]
                q_labels = [f"Q{i+1}" for i in range(len(n_bins) - 1)]
                if len(q_labels) >= 2:
                    q_labels[0] += "(calm)"
                    q_labels[-1] += "(wild)"
                mkt_ostats["swing_q"] = pd.qcut(
                    mkt_ostats["odds_swing"], q=4, labels=q_labels, duplicates="drop"
                )
                oq = mkt_ostats.groupby("swing_q", observed=True).agg(
                    count=("condition_id", "count"),
                    reversal_rate=("underdog_wins", lambda x: x.mean() * 100),
                    was_traded_pct=("was_traded", lambda x: x.mean() * 100),
                    avg_swing=("odds_swing", "mean"),
                )
                print(f"\n  By odds swing quartile:")
                print(oq.to_string())
            except ValueError:
                pass

# ─────────────────────────────────────────────────────────────────────────────
# 6. SIMULATED FILTER PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 6: SIMULATED FILTER PERFORMANCE")
print(SEP)


def evaluate_filter(mask, label, df=sm):
    subset = df[mask]
    n = len(subset)
    if n == 0:
        return f"  {label}: 0 markets"

    reversals = int(subset["underdog_wins"].sum())
    win_rate = reversals / n
    dir_pnl_total = subset["directional_pnl"].sum()
    dir_pnl_mean = subset["directional_pnl"].mean()

    return (f"  {label}\n"
            f"    n={n:3d}  reversals={reversals:3d}  win_rate={win_rate*100:.1f}%\n"
            f"    directional_pnl: total={dir_pnl_total:+.2f}  per_trade={dir_pnl_mean:+.4f}")


mask_A = sm["cheap_side_ask"] < 0.50
print()
print(evaluate_filter(pd.Series([True] * len(sm), index=sm.index), "Baseline (all markets)"))
print()
print(evaluate_filter(mask_A, "A: cheap_side_ask < 0.50"))
print()

for thresh in [1.0, 5.0, 10.0, 25.0]:
    mask_B = mask_A & (sm["min_distance_to_strike"] < thresh)
    print(evaluate_filter(mask_B, f"B: A + dist_to_strike < {thresh}"))

mask_C = mask_A & (sm["price_crossed_strike"] == True)
print()
print(evaluate_filter(mask_C, "C: A + price_crossed_strike=True"))

mask_D = mask_A & (sm["volatility"] > 0.00007)
print()
print(evaluate_filter(mask_D, "D: A + volatility > 7e-5"))

mask_E = mask_A & (sm["price_crossed_strike"] == True) & (sm["volatility"] > 0.00007)
print()
print(evaluate_filter(mask_E, "E: A + crossed + high_vol"))

mask_H = mask_A & (sm["momentum_vs_majority"] == "contrarian")
print()
print(evaluate_filter(mask_H, "H: A + contrarian_momentum"))

# ─────────────────────────────────────────────────────────────────────────────
# 7. 2D ANALYSIS: VOLATILITY x DISTANCE
# ─────────────────────────────────────────────────────────────────────────────

print()
print(SEP)
print("SECTION 7: 2D ANALYSIS — VOLATILITY x DISTANCE (with cheap_side filter)")
print(SEP)
print("  (reversal% / n / EV per trade)")
print()

vol_cuts = [(0, 0.00007, "low_vol"), (0.00007, 0.00010, "med_vol"), (0.00010, 1.0, "high_vol")]
dist_cuts = [(0, 1.0, "very_close"), (1.0, 10.0, "close"), (10.0, 50.0, "medium"), (50.0, 9999, "far")]

header = f"{'':15s}" + "".join(f"{d[2]:>18s}" for d in dist_cuts)
print("  " + header)
for vlo, vhi, vlab in vol_cuts:
    row_parts = [f"  {vlab:15s}"]
    for dlo, dhi, dlab in dist_cuts:
        mask = (mask_A
                & (sm["volatility"] >= vlo) & (sm["volatility"] < vhi)
                & (sm["min_distance_to_strike"] >= dlo) & (sm["min_distance_to_strike"] < dhi))
        subset = sm[mask]
        n = len(subset)
        if n == 0:
            row_parts.append(f"{'—':>18s}")
        else:
            rr = subset["underdog_wins"].mean() * 100
            ev = subset["directional_pnl"].mean()
            row_parts.append(f"{rr:5.1f}%/{n:3d}/{ev:+.3f}".rjust(18))
    print("".join(row_parts))

# By asset 2D (only if multiple assets)
assets = sorted(sm["asset"].dropna().unique())
if len(assets) > 1:
    print(f"\n  Per-asset breakdown:")
    for asset in assets:
        asset_mask = sm["asset"] == asset
        print(f"\n  {asset}:")
        header = f"{'':15s}" + "".join(f"{d[2]:>18s}" for d in dist_cuts)
        print("  " + header)
        for vlo, vhi, vlab in vol_cuts:
            row_parts = [f"  {vlab:15s}"]
            for dlo, dhi, dlab in dist_cuts:
                mask = (mask_A & asset_mask
                        & (sm["volatility"] >= vlo) & (sm["volatility"] < vhi)
                        & (sm["min_distance_to_strike"] >= dlo) & (sm["min_distance_to_strike"] < dhi))
                subset = sm[mask]
                n = len(subset)
                if n == 0:
                    row_parts.append(f"{'—':>18s}")
                else:
                    rr = subset["underdog_wins"].mean() * 100
                    ev = subset["directional_pnl"].mean()
                    row_parts.append(f"{rr:5.1f}%/{n:3d}/{ev:+.3f}".rjust(18))
            print("".join(row_parts))


print()
print(SEP)
print("Analysis complete.")
print(SEP)
