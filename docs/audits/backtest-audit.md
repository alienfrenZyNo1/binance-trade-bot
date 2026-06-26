# Backtest Audit Report — Momentum Strategy Validation

**Date:** 2026-06-26  
**Auditor:** Backtest-Agent (automated)  
**Scope:** All backtest code, result files, and live-vs-backtest discrepancy analysis  
**Verdict:** ⚠️ **EDGE NOT TRUSTWORTHY** — The claimed +79% return is an artifact of methodological flaws. The strategy may have a small positive edge, but the current backtest framework cannot establish this with confidence.

---

## 1. Files Reviewed

| File | Purpose | Status |
|------|---------|--------|
| `binance_trade_bot/backtest.py` | Original MockBinanceManager backtest (1-min granularity) | Legacy, superseded |
| `backtest_strategy.py` | Simplified mean-reversion backtest | Superseded |
| `backtest_full.py` | Full-feature backtest with all filters | Standalone, not used for optimization |
| `optimize_momentum.py` | **Primary optimizer** — grid search + walk-forward | **Produces `best_momentum.json`** |
| `strategy_optimizer.py` | Multi-strategy optimization engine | Framework, not the one that generated claims |
| `scripts/research_bear_futures_backtester.py` | Futures short backtester | Current bear backtest |
| `scripts/research_sideways_chop_backtester.py` | Sideways mean-reversion backtester | Current sideways research |
| `backtest_results.json` | Optimization results (10 configs) | Contains train/OOS data |
| `best_momentum.json` | Winning parameters + full 6-month run | **Source of +79% claim** |
| `bear_futures_backtest.json` | Latest bear futures run output | Empty (0 trades, 0 candles) |
| `research/JOURNAL.md` | Research journal sessions 001-002 | Notes suspicions |
| `research/BACKLOG.md` | Improvement backlog | Tracks known issues |

---

## 2. Methodology Assessment

### 2.1 Walk-Forward Validation: PARTIALLY CORRECT, BUT FLAWED

The optimizer (`optimize_momentum.py`, lines 432–580) implements a walk-forward split:
- **Train period:** 120 days (oldest 120 days of the 180-day dataset)
- **Test/OOS period:** 60 days (most recent 60 days)
- **Grid search:** 10,500 combinations sampled down to 500

**What's correct:**
- Parameters are optimized on train, validated on test — no parameter fitting on OOS data directly.
- Window start/end pricing is handled correctly (`run_momrot` sizes position at `start_ts` price, values at `end_ts` price). This was verified by `tests/test_backtest_window_safety.py`.
- The `_price_at_or_before` helper prevents using future data for initial position sizing.

**Critical flaw — Inverted train/OOS relationship:**

From `backtest_results.json`, the top momentum configurations show:
```
train_pnl: -29.66%    →    oos_pnl: +65.31%
train_pnl: -29.66%    →    oos_pnl: +62.16%
train_pnl: -31.35%    →    oos_pnl: +61.99%
```

From `best_momentum.json` (the actual deployed config):
```
train_pnl: +30.37%    →    oos_pnl: +46.24%
full_6mo:  +79.04%
```

**There are two different result sets.** The `backtest_results.json` results (train -30%, OOS +65%) appear to be from a different optimization run than `best_momentum.json` (train +30%, OOS +46%). The journal conflates these. Regardless, both have problems:

1. **The `backtest_results.json` set shows the strategy LOST money in 4 of 5 momentum configs during the train period.** The fact that OOS was positive for those configs is a statistical artifact — the parameters were chosen because they happened to do well on OOS despite losing on train. This is curve-fitting to OOS data.

2. **The selection criterion is wrong.** Lines 486–527 of `optimize_momentum.py` rank by **train P&L** (`train_results.sort(key=lambda item: item["pnl"], reverse=True)`), then take the top-10 and validate on OOS. But if the train P&L is negative for most configs, the "best" configs are simply the "least bad" — they weren't genuinely profitable in-sample. Selecting the least-bad-train-that-happens-to-be-good-OOS is equivalent to pure OOS overfitting.

3. **Only one train/test split.** There is no rolling or k-fold validation. A single 120/60 split is insufficient to establish robustness, especially in crypto where regime changes are extreme.

### 2.2 Lookahead Bias: MODERATE RISK

Several potential lookahead issues exist:

**Issue A — Close-of-bar execution (CRITICAL):**
- `optimize_momentum.py` line 195: The main loop iterates over hourly timestamps and uses `idx.get(coin, {}).get(ts)` to get the **close price** for the current bar.
- Performance is computed using close-to-close: `perf = (current / previous - 1) * 100` (line 269, 301, 315).
- Trade execution at lines 331–347 uses the **same timestamp's close price** as both the signal and execution price.
- **This means the strategy makes decisions using the close of bar `ts` and executes at the close of bar `ts` — zero latency.** In reality, by the time the close is observed and the order is placed, the price has moved.

**Issue B — Full lookback data available at signal time:**
- Line 211: `ref = [row for row in data.get(REF_COIN, []) if row["ts"] <= ts][-60:]` — filters correctly up to `ts`, no future leak in regime detection.
- Lines 266–268: Performance lookback `ts_lb = ts - lookback * HOUR_MS` and then `idx.get(candidate, {}).get(ts_lb)` — uses close at `ts_lb` and close at `ts`. This is close-to-close and correct as long as `ts` is the latest **completed** bar.

**Issue C — RSI uses full candle history (MINOR):**
- Line 320–324: `candles = [row for row in data.get(candidate, []) if row["ts"] <= ts][-16:]` — correctly filters to `<= ts`. No leak.

**Issue D — Ratio EMA initialization (MINOR):**
- In `backtest_full.py`, ratio EMA starts from the first observation. There is no warm-up period exclusion, meaning early trades in the first 20 bars may be based on unstable baselines. However, the `RATIO_MIN_SAMPLES = 20` filter (line 107, 667) mitigates this.

### 2.3 Survivorship Bias: HIGH RISK

**The coin universe is fixed and hand-selected:**
```python
COINS = ["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
         "APT", "INJ", "TIA", "ENA", "PEPE", "JUP"]
```

All 15 coins are chosen **because they are currently listed and liquid on Binance**. Coins that were delisted, crashed to zero, or were removed from Binance during the 6-month backtest period are excluded. This creates a systematic upward bias — the backtest never "holds" a coin that went bankrupt or was delisted.

For a 6-month backtest, this is less severe than for multi-year studies, but still relevant. SUI, APT, JUP, PEPE, and ENA are all relatively new listings that were selected because they survived and became liquid.

### 2.4 Data Leakage Between Train and OOS: LOW RISK

The walk-forward split in `optimize_momentum.py` is clean:
- Train: `train_start` to `test_start`
- OOS: `test_start` to `end_ts`
- No overlap in timestamps
- State is reset between runs (via `run_momrot` re-initializing balance, positions, etc.)

However, there is a **regime detection state leak**: regime detection uses 60 bars of lookback. If the train period ends and OOS begins, the first 60 bars of OOS regime detection overlap with train-period data. This is minor since regime is recomputed from raw candles each time.

---

## 3. Cost Modeling Assessment

### 3.1 Trading Fees: CORRECTLY MODELED ✅

Both the optimizer and full backtest model taker fees at 0.075% per side:
- `optimize_momentum.py` line 167: `fee = p.get("fee_rate", 0.00075)`
- `backtest_full.py` line 58: `TAKER_FEE = 0.00075`
- Applied on both buy and sell sides.

Total round-trip cost per trade: 0.075% × 2 = **0.15%**. This matches Binance's taker fee with BNB discount.

### 3.2 Slippage: CORRECTLY MODELED ✅ (but likely understated)

- `optimize_momentum.py` line 168: `slip = p.get("slippage", 0.0005)` — 0.05% per side
- Applied as `coin_val * (fee + slip)` in trade execution.

**However, 0.05% slippage is optimistic for a $62 account trading altcoin/USDC pairs.** Binance USDC pairs for coins like PEPE, JUP, ENA can have spreads of 0.1-0.5%. The backtest assumes near-perfect fill quality. For low-liquidity pairs, actual slippage could be 2-5x higher.

### 3.3 Funding Rates: NOT MODELED ON SPOT ❌, MODELED ON FUTURES ✅

**Spot backtest:** Funding rates are irrelevant (spot has no funding). Correct.

**Futures backtest** (`scripts/research_bear_futures_backtester.py`):
- Line 115–116: `fetch_funding_rates()` is called and funding P&L is computed.
- Line 246–253: `_funding_pnl_between()` correctly calculates funding costs during position holding.
- Line 252: Convention is correct: `pnl += notional * _safe_float(row.get("fundingRate"))` — positive funding = longs pay shorts = shorts receive. This is correct for Binance.
- **Verdict: Funding IS modeled on futures. The journal's claim "no funding costs" (Finding B) refers to the spot backtest, where it's not applicable.**

### 3.4 Missing Cost Components

| Cost Component | Spot Backtest | Futures Backtest |
|---------------|:---:|:---:|
| Taker fees | ✅ 0.075%/side | ✅ 0.075%/side |
| Slippage | ⚠️ 0.05% (understated) | ✅ 0.05% |
| Funding rates | N/A | ✅ Correctly modeled |
| Maker rebate opportunity | ❌ Not modeled | ❌ Not modeled |
| Bid-ask spread on low-liquidity pairs | ❌ | ❌ |
| Impact of $62 order size | ❌ | ❌ |
| Liquidation risk | N/A | ⚠️ Computed as buffer, not simulated |

---

## 4. Live vs. Backtest Trade Frequency Discrepancy

### The Problem
- **Backtest:** 46 trades over 180 days = **0.26 trades/day** (`best_momentum.json`)
- **Live:** 18 trades over 30 hours = **14.4 trades/day**
- **Discrepancy:** **55x higher** than backtest prediction

### Root Cause Analysis

#### Primary Cause: SCOUT_SLEEP_TIME = 1 second vs. confirmation_cycles

The live config (`user.cfg`) sets `scout_sleep_time=1`, meaning the strategy's `scout()` method is called **every 1 second**. The confirmation delay requires `confirmation_cycles=3` consecutive identical signals.

The confirmation logic in `momentum_strategy.py` lines 840–883:
```python
if (pending and pending[0] == current_coin.symbol
        and pending[1] == best_coin.symbol):
    count = pending[3] + 1
    # ...
    if count < self._confirmation_cycles or elapsed < min_confirm_seconds:
        return  # Wait for more confirmation
```

**Critical issue:** `CONFIRMATION_TIME_ENABLED` defaults to `"no"` in `user.cfg` (not set) and `config.py` line 414–417. This means the time-based gate (`min_confirm_seconds`) returns 0, and **only the cycle count matters**.

With `SCOUT_SLEEP_TIME=1`, three confirmation cycles complete in **3 seconds**. The backtest assumes hourly granularity, so its "3 confirmation cycles" = **3 hours**.

**This is the smoking gun:** The live bot confirms a rotation signal in 3 seconds; the backtest effectively confirms in 3 hours (since it processes one bar per hour). The 55x frequency discrepancy is entirely explained by this mismatch.

#### Secondary Cause: Live price feed vs. hourly close

The live bot fetches current price via WebSocket/API tickers every second. Intradabar price movements create transient momentum edge signals that disappear by the hourly close. The backtest only sees hourly closes, so these transient signals are invisible to it. The live confirmation delay (3 seconds) is far too short to filter out intrabar noise on a 1-hour bar.

#### The fix was partially implemented but not enabled

The code in `config.py` lines 412–417 shows awareness of this issue:
```python
# Cycle count alone can confirm a multi-hour signal after only a few seconds
# when SCOUT_SLEEP_TIME=1.
self.CONFIRMATION_TIME_ENABLED = ...
```

But `confirmation_time_enabled` is **not set in `user.cfg`**, so it defaults to `"no"`. The time gate is disabled. This is a configuration error.

---

## 5. Sharpe Ratio Assessment

The claimed Sharpe ratio of 3.85–3.94 is calculated in `strategy_optimizer.py` lines 488–499:
```python
returns = []
for i in range(1, len(self.equity_curve)):
    prev = self.equity_curve[i - 1][1]
    curr = self.equity_curve[i][1]
    if prev > 0:
        returns.append((curr - prev) / prev)
# Annualized Sharpe (hourly returns * 24 * 365)
sharpe = (mean_r / std_r * math.sqrt(24 * 365))
```

**Problems:**

1. **No risk-free rate subtraction.** The Sharpe formula uses raw returns without subtracting a risk-free rate. For a 6-month period with ~4-5% risk-free rate annualized, this inflates Sharpe by ~0.3–0.5.

2. **Hourly return calculation overstates frequency.** When the strategy holds a position and doesn't trade, hourly returns reflect the underlying coin's volatility, not the strategy's active returns. The Sharpe is dominated by the volatility of the held coin, not by trading alpha.

3. **3.85 is implausible.** Professional quant funds target Sharpe 1.0–2.0. Renaissance Technologies' Medallion Fund operates at ~2.5 after fees. A 3.85 Sharpe from a simple momentum rotation on 15 altcoins would be extraordinary — it is almost certainly an artifact.

4. **Small sample.** 46 trades over 180 days is too few to reliably estimate Sharpe. The equity curve has ~4,320 hourly data points, but only 46 trading events. Most of the Sharpe comes from price drift of the held position.

---

## 6. Specific Code Locations with Issues

### Critical Issues

| # | Location | Issue | Severity |
|---|----------|-------|----------|
| 1 | `optimize_momentum.py:195,331-347` | Trade execution at same-bar close — zero execution latency assumption | **CRITICAL** |
| 2 | `optimize_momentum.py:486` | Ranking by train P&L when train is negative for most configs = OOS overfitting | **CRITICAL** |
| 3 | `config.py:414-417` + `user.cfg` (missing) | `CONFIRMATION_TIME_ENABLED` not enabled → live confirms in 3 sec, not 3 hours | **CRITICAL** |
| 4 | `user.cfg:25` | `scout_sleep_time=1` compounds confirmation timing mismatch | **HIGH** |

### Methodological Issues

| # | Location | Issue | Severity |
|---|----------|-------|----------|
| 5 | `optimize_momentum.py:33-36` | Fixed coin universe — survivorship bias | **HIGH** |
| 6 | `optimize_momentum.py:448-450` | Single train/test split (120d/60d), no rolling validation | **MEDIUM** |
| 7 | `strategy_optimizer.py:496-499` | Sharpe uses hourly equity curve, not trade returns; no risk-free rate | **MEDIUM** |
| 8 | `backtest_full.py:58-59` | Slippage 0.05% is optimistic for altcoin/USDC pairs at $62 | **MEDIUM** |
| 9 | `optimize_momentum.py:168` | Same understated slippage in optimizer | **MEDIUM** |
| 10 | `best_momentum.json` vs `backtest_results.json` | Two different result sets with different train P&L — inconsistent provenance | **MEDIUM** |

### Missing Safeguards

| # | Location | Issue | Severity |
|---|----------|-------|----------|
| 11 | `optimize_momentum.py` | No bid-ask spread modeling | **LOW** |
| 12 | `optimize_momentum.py` | No market impact modeling for small accounts | **LOW** |
| 13 | `optimize_momentum.py` | No regime-conditioned walk-forward (train/test both may be same regime) | **LOW** |
| 14 | `backtest_full.py:766-770` | Re-entry logic picks coin based on single-bar (1h) performance — too noisy | **LOW** |

---

## 7. Futures Backtest Specific Assessment

The bear futures backtester (`scripts/research_bear_futures_backtester.py`) is **substantially better designed** than the spot backtest:

**Strengths:**
- Walk-forward with point-in-time momentum signals (line 400: `window = candles[:idx+1]`)
- Funding rates correctly modeled (line 246–253)
- Slippage on both entry and exit (lines 278, 257)
- Intrabar high/low used for stop-loss and trailing stop checks (lines 314, 321–331)
- Cooldown between trades enforced (line 423–424)

**Weaknesses:**
- `bear_futures_backtest.json` shows **0 candles for all symbols and 0 trades** — the backtest was never successfully run (or the results were wiped). The journal (Session 002) claims 353 trades with 82% win rate, but this is not reflected in the JSON output file.
- The journal claims +9.5% compounded return but the JSON shows empty records.
- Single-symbol shorting (one short per symbol at a time) — no portfolio-level simulation where multiple shorts or spot+futures hedging is modeled.
- Initial balance is $1000 (line 48), not the actual $62 live balance — results don't reflect real account constraints.

---

## 8. Is the Claimed Edge Trustworthy?

### The +79% Claim: **NOT TRUSTWORTHY**

| Evidence | Assessment |
|----------|------------|
| Train P&L was negative for most configs in `backtest_results.json` | ❌ Strategy doesn't work in-sample |
| Best config's train P&L (+30%) → OOS (+46%) looks reasonable in `best_momentum.json` | ⚠️ But only one split, and different from `backtest_results.json` |
| Sharpe 3.85 is 2-4x higher than world-class hedge funds | ❌ Implausible without methodological error |
| 48% max drawdown on a $62 account = $30 loss | ❌ Catastrophic risk for retail capital |
| Fees consume 13.7% of initial capital | ❌ Most returns go to Binance |
| Zero execution latency assumed (same-bar close execution) | ❌ Overstates returns |
| Survivorship bias from fixed, curated coin list | ❌ Overstates returns |
| Live trade frequency is 55x higher than backtest | ❌ Backtest doesn't represent live behavior |
| Only 18 live trades over 30 hours | ❌ Statistically meaningless for validation |

### Realistic Assessment

After accounting for:
- Realistic slippage (0.1-0.2% per side for altcoin/USDC pairs)
- Execution latency (at minimum, next-bar open execution)
- The inverted train/OOS results
- The 13.7% fee drag
- Survivorship bias

**The strategy likely has a marginally positive to slightly negative expected return.** The momentum rotation concept (buying outperforming coins) has theoretical merit in trending crypto markets, but the current implementation's execution costs and signal noise likely erode any edge at the $62 account size.

### What Would Move This to "Trustworthy"

1. **Fix the confirmation timing mismatch** — enable `CONFIRMATION_TIME_ENABLED=yes` with `SIDEWAYS_CONFIRMATION_MIN_SECONDS=3600` (match the hourly bar)
2. **Re-run walk-forward with 5+ rolling windows** across different market regimes
3. **Use next-bar-open execution** instead of same-bar-close
4. **Increase slippage to 0.15% per side** for realistic altcoin fills
5. **Track 100+ live trades** with proper logging before drawing conclusions
6. **Add a random-coin-selection baseline** to verify the strategy beats random chance
7. **Test with a dynamic coin universe** (coins available at each point in time, not just current survivors)

---

## 9. Recommendations

### Immediate (P0)

1. **Enable time-based confirmation** in `user.cfg`:
   ```ini
   confirmation_time_enabled=yes
   sideways_confirmation_min_seconds=3600
   bull_confirmation_min_seconds=3600
   ```
   This aligns live confirmation delay with the hourly bar the backtest uses.

2. **Increase `SCOUT_SLEEP_TIME` to 60 seconds minimum** — checking every 1 second wastes API calls and creates noise.

3. **Do not trust the +79% figure** for position sizing or capital allocation decisions.

### Short-Term (P1)

4. **Implement next-bar-open execution** in `optimize_momentum.py`: signal on bar `ts` close, execute at bar `ts + 1h` open.

5. **Run multi-window walk-forward** with at least 5 non-overlapping 60-day OOS windows.

6. **Add a benchmark**: compare strategy returns against (a) holding TIA, (b) holding SOL, (c) equal-weight portfolio of all 15 coins, (d) random coin rotation.

7. **Increase slippage assumption to 0.15%** per side for realistic modeling.

### Medium-Term (P2)

8. **Build a dynamic coin universe** — at each timestamp, only include coins that were listed and above a minimum liquidity threshold.

9. **Add Monte Carlo permutation testing** — shuffle trade order and coin assignment 1000x to establish confidence intervals.

10. **Separate the two backtest result sets** — clarify which results in `backtest_results.json` vs `best_momentum.json` are current and authoritative.

---

## 10. Summary

| Dimension | Rating | Notes |
|-----------|--------|-------|
| **Methodology soundness** | ⚠️ Poor | Same-bar-close execution, single split, inverted train/OOS |
| **Bias control** | ❌ Inadequate | Survivorship bias, OOS overfitting via negative-train selection |
| **Cost modeling** | ⚠️ Partial | Fees correct, slippage understated, futures funding modeled |
| **Live fidelity** | ❌ Low | 55x trade frequency mismatch due to confirmation timing bug |
| **Statistical rigor** | ❌ Insufficient | 46 trades, one split, implausible Sharpe |
| **Overall trustworthiness** | ❌ **NOT TRUSTWORTHY** | Edge may exist but current backtest cannot establish it |

The backtest infrastructure shows genuine effort (walk-forward splits, window safety tests, funding rate modeling on futures). However, the combination of same-bar execution, survivorship bias, understated slippage, inverted train/OOS, and the live confirmation timing bug means the claimed +79% return and Sharpe 3.85 are **not reliable**. The strategy should be treated as unproven until properly validated with corrected methodology and sufficient live trade data.
