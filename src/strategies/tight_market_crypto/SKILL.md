# Tight Market Crypto (TMC) Strategy

## What This Strategy Does

TMC trades **short-duration crypto prediction markets** on Polymarket (5 or 15-minute windows like "Will BTC be above $X at 2:15PM?"). It detects markets where the current crypto price is **close to the strike price** relative to expected volatility, then buys the side the price is currently on — betting that the price won't move enough to cross the strike before expiry.

**Core thesis**: If the price hasn't moved much from the strike and there's little time left, the current side (YES or NO) is statistically likely to win. The strategy enters when `distance_from_strike / expected_move <= K`.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     COORDINATOR (main loop, 0.5s tick)       │
│                                                               │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐ │
│  │ Discovery     │──>│ Signal Engine │──>│ Executor         │ │
│  │ (every 30s)   │   │ (every tick)  │   │ (on signal fire) │ │
│  └──────┬───────┘   └──────┬───────┘   └──────────────────┘ │
│         │                   │                                  │
└─────────┼───────────────────┼──────────────────────────────────┘
          │                   │
    ┌─────▼──────┐    ┌──────▼──────────┐
    │ Market     │    │ Binance Feed    │  (WebSocket thread)
    │ Finder     │    │ (live prices +  │
    │ (Gamma API)│    │  volatility)    │
    └────────────┘    └─────────────────┘
                      ┌─────────────────┐
                      │ Tightness       │  (WebSocket thread)
                      │ Tracker         │
                      │ (Polymarket     │
                      │  odds feed)     │
                      └─────────────────┘
```

### Files

| File | Role |
|------|------|
| `coordinator.py` | Main orchestrator — runs the event loop, discovers markets, triggers signals, executes trades, handles market expiry and outcome recording |
| `signal_engine.py` | Core decision logic — evaluates whether a market meets entry criteria based on price distance vs. expected volatility move |
| `executor.py` | Trade execution — places FOK orders via CLOB API, tracks daily loss, records trades to JSON, updates outcomes post-resolution |
| `market_finder.py` | Market discovery — queries Gamma API for active 15-min crypto markets, parses token IDs and time windows |
| `binance_feed.py` | Real-time price data — Binance WebSocket feed for BTC/ETH/SOL/XRP, calculates rolling volatility and expected moves |
| `tightness_tracker.py` | Odds monitoring — Polymarket WebSocket feed, records YES/NO price snapshots, tracks how "tight" (close to 50/50) a market is |
| `models.py` | Dataclasses for all domain objects (CryptoMarket, OddsSnapshot, TightnessProfile, TightMarketOpportunity, TightMarketTradeResult) |

---

## The Signal: How Entry Decisions Work

### Key Formula

```
signal_fires = (distance / expected_move) <= K
```

Where:
- **distance** = `|current_crypto_price - strike_price|` — how far the price has moved from where it was at the start of the 15-min window
- **expected_move** = `volatility * price * sqrt(seconds_remaining)` — statistically expected price movement in the remaining time
- **volatility** = standard deviation of log-returns over the last 5 minutes (from Binance tick data)
- **K** = `tmc_volatility_multiplier` (default: 1.0) — the threshold. Lower K = stricter filter

### Intuition

If `distance / expected_move = 0.3`, the price has only moved 0.3 standard deviations from strike. With little time left, the price is unlikely to cross the strike — so the current winning side is a good bet.

If `distance / expected_move = 2.0`, the price has moved 2 standard deviations — the outcome is already strongly decided and the odds will reflect that (no edge left).

### Timeline and Windows

```
Market lifetime (15 minutes):
|=====================================================|
0min                                               15min

                    Entry Window (last 180s):
                              |===================|
                              180s            0s (expiry)

                         Execution Window (last 60s):
                                       |==========|
                                       60s     0s

                              Volatility Boost (last 15s):
                                            |=====|
                                            15s  0s
```

1. **Entry Window** (180s before expiry): Signal engine starts evaluating. Signals detected here but outside the execution window are logged as **PRE-SIGNALS** (shadow data, no trade).
2. **Execution Window** (60s before expiry): Signals fire for real and trigger trades. This narrow window maximizes signal quality.
3. **Volatility Boost** (15s before expiry): `expected_move` is multiplied by `tmc_volatility_boost_factor` (default 2.0x) to account for erratic price behavior near expiry.

### Skip Conditions (Signal Does NOT Fire)

- Already fired for this market (one-shot per market)
- Strike price not captured yet (market window hasn't started)
- Outside entry window (>180s or already expired)
- No Binance price available
- `distance / expected_move > K` (main rejection — price moved too far)
- CLOB asks not available or invalid (<=0)
- Market too one-sided: `min(yes_ask, no_ask) < tmc_min_minority_ask` (default 0.01)

---

## Discovery and Market Lifecycle

### How Markets Are Found (`market_finder.py`)

1. Polls Gamma API (`GET /markets?active=true&closed=false`)
2. Filters for crypto markets with 15-minute time windows (regex: `HH:MM[AP]M-HH:MM[AP]M`)
3. Validates the asset is in `tmc_crypto_assets` (BTC, ETH, SOL, XRP)
4. Filters by expiry: between 60s and 1200s from now
5. Parses `clobTokenIds` (JSON-encoded string!) to get YES/NO token IDs
6. Returns `CryptoMarket` objects

### Market Lifecycle (managed by `coordinator.py`)

```
DISCOVERED ──> TRACKED ──> STRIKE CAPTURED ──> SIGNAL EVALUATION ──> EXPIRED
    │              │              │                    │                  │
    │              │              │                    │                  ├─ Get final Binance price
    │              │              │                    │                  ├─ Determine outcome (YES/NO)
    │              │              │                    │                  ├─ Update trade logs with P&L
    │              │              │                    │                  └─ Save shadow log entry
    │              │              │                    │
    │              │              │                    └─ Signal fires → executor.execute()
    │              │              │
    │              │              └─ Binance price at start_date = strike
    │              │
    │              └─ TightnessTracker starts WebSocket subscription
    │
    └─ Gamma API returns market, added to tracked dict
```

---

## Trade Execution

### Order Flow (`executor.py`)

1. **Kill switch check**: If `daily_loss >= tmc_max_daily_loss` ($50 default), reject all trades
2. **Dry run mode** (`config.dry_run=True`): Log simulated trade, save to JSON, return success
3. **Live mode**:
   - Place YES market buy order (FOK) for `amount_per_side` ($5 default)
   - Place NO market buy order (FOK) for `amount_per_side`
   - Record order IDs
   - Add `total_cost` to daily loss tracker
4. Save trade record to `data/tight_market_crypto_trades.json`

### What Gets Bought

The strategy buys **both sides** (YES and NO) at their current ask prices. Since one side will always pay out $1.00 per share at resolution, the profit/loss depends on the total cost:

- **Cost** = `yes_ask + no_ask` per share (both bought at market)
- **Payout** = $1.00 per share (winning side)
- **P&L** = payout - total_cost

Wait — this is NOT pure arbitrage (yes_ask + no_ask is almost always >= $1.00). The strategy actually bets on **one side winning**, buying both sides as a form of position entry. The `amount_per_side` is the same for each, so the net return depends on which side wins and its ask price at entry.

**Correction**: Looking more carefully, the payout calculation is `amount_per_side / winning_ask`, meaning the strategy gets more shares on the cheaper side. The net return is: `(amount_per_side / winning_ask) - total_cost`.

---

## Risk Management

| Mechanism | Detail |
|-----------|--------|
| **Daily loss limit** | Tracks cumulative cost of trades. Kill switch at $50/day (configurable). Resets at midnight UTC. |
| **One-shot signals** | Each market can only fire once — prevents repeated entries on the same market. |
| **One-sided market skip** | Skips markets where the minority side ask is below threshold (too imbalanced = no edge). |
| **Execution window** | Only trades in the final 60s — maximizes information before committing. |
| **Volatility boost** | Increases expected_move threshold near expiry to avoid false signals from noise. |
| **FOK orders** | Fill-or-Kill ensures full fill or no fill — no partial positions. |
| **Dry run default** | `dry_run=True` by default — must explicitly enable live trading. |

---

## Data Logging

### Trade Log (`data/tight_market_crypto_trades.json`)

Every trade (dry run or live) is recorded with:
- Market info (condition_id, question, asset)
- Entry prices (yes_ask, no_ask)
- Signal metrics (distance, expected_move, tight_ratio)
- Execution result (success, order_ids, cost, error)
- **Post-resolution fields** (filled when market expires):
  - `outcome`: "YES" or "NO"
  - `final_crypto_price`: Binance price at expiry
  - `payout`: amount_per_side / winning_ask
  - `net_return`: payout - total_cost
  - `return_pct`: percentage return

### Shadow Log (`data/tight_market_crypto_shadow.json`)

Records **every expiring market** (traded or not) with comprehensive analysis:
- Full price and odds trails during entry/execution windows (1/sec for execution, 1/5sec for entry)
- Skipped signal analysis with reasons
- Reversal detection (did the majority side flip?)
- Price momentum in final seconds
- Min/max distance to strike during execution window

This shadow data is critical for backtesting and tuning parameters.

---

## Configuration Reference

All parameters are set via environment variables (`.env`) and loaded into `Config`:

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `TMC_CRYPTO_ASSETS` | "BTC,ETH,SOL,XRP" | Comma-separated list of assets to track |
| `TMC_ENTRY_WINDOW` | 180.0 | Seconds before expiry to begin signal evaluation |
| `TMC_EXECUTION_WINDOW` | 60.0 | Seconds before expiry where trades can actually execute |
| `TMC_VOLATILITY_MULTIPLIER` | 1.0 | K threshold: lower = stricter signal filter |
| `TMC_VOLATILITY_WINDOW` | 300.0 | Seconds of Binance data for volatility calc |
| `TMC_VOLATILITY_BOOST_FACTOR` | 2.0 | Multiplier for expected_move inside execution window |
| `TMC_MAX_DISTANCE_RATIO` | 8.0 | Max raw distance/expected_move ratio to enter trade |
| `TMC_MIN_MINORITY_ASK` | 0.01 | Minimum ask on weaker side (skip if below) |
| `TMC_MAX_INVESTMENT` | 10.0 | Total USD per trade (split equally: $5 YES + $5 NO) |
| `TMC_MAX_DAILY_LOSS` | 50.0 | Kill switch: stop trading if daily loss exceeds this |
| `TMC_DISCOVERY_INTERVAL` | 30.0 | Seconds between Gamma API market scans |
| `DRY_RUN` | true | Simulate trades without placing real orders |

---

## Threading Model

```
Main Thread (coordinator._main_loop):
  - Discovery every 30s
  - Signal evaluation every 0.5s
  - Trade execution (serial)
  - Outcome recording on market expiry

Thread 2 (TightnessTracker):
  - Polymarket WebSocket connection
  - Receives YES/NO ask updates
  - Stores OddsSnapshots in thread-safe dict

Thread 3 (BinancePriceFeed):
  - Binance WebSocket connection
  - Receives price ticks for all tracked assets
  - Maintains price history deques (900 points per asset)
  - Computes volatility on-demand from stored data
```

Data flows from WebSocket threads to main thread via shared dicts. No explicit locks are used on the data structures — the deques and dicts are treated as effectively thread-safe for the read/append patterns used.

---

## Key Gotchas for Future Development

1. **`clobTokenIds` from Gamma API** is a JSON-encoded string, not a list. Must `json.loads()` before indexing.
2. **Signals are one-shot**: Once fired for a market, that market is permanently marked. There's no retry mechanism.
3. **Volatility can be zero**: If Binance has insufficient data, `expected_move=0` and the signal formula divides by zero — this is guarded by checking `expected_move > 0`.
4. **No LLM validation**: Unlike the arbitrage strategy, TMC does NOT use an LLM to validate trades. Decisions are purely mathematical.
5. **Both sides are bought**: The strategy buys YES and NO for the same dollar amount, but payout depends on which side wins and its ask price.
6. **Shadow log grows unbounded**: `tight_market_crypto_shadow.json` accumulates entries forever — may need rotation for long-running deployments.
7. **WebSocket reconnection**: Both Binance and Polymarket WebSocket connections have basic reconnection logic but can silently fail — check for stale data.
8. **Outcome resolution uses Binance price**: The "truth" for whether YES or NO wins comes from Binance (not Polymarket), so there could be edge cases where Polymarket resolves differently than what the bot calculates.
