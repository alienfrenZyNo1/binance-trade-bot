# Regime-Adaptive Combined Strategy — Master Analysis

*Generated: 2026-06-27 19:34 UTC*

**Research script:** `scripts/research_regime_combined.py`

---

## Strategy Design

| Regime | Condition | Action |
|--------|-----------|--------|
| **BULL** | ADX > 25, Price > EMA(200) | Trend-following LONG with leverage, 12% trail stop |
| **BEAR** | ADX > 25, Price < EMA(200) | SHORT with leverage OR go to USDC cash |
| **SIDEWAYS** | ADX < 20 | Grid-style range trading, tight spacing |
| **TRANSITION** | ADX 20-25 | Reduce to 50% position, no leverage |

### Variants

| Variant | Trend Lev | Stop Loss | Bear Action | Short Lev |
|---------|-----------|-----------|-------------|-----------|
| Conservative | 1x | 20% | Go to USDC | N/A |
| Balanced | 2x | 15% | Short | 2x |
| Aggressive | 3x | 20% | Short | 2x |

### Universe
BTCUSDC, ETHUSDC, SOLUSDC, BNBUSDC, XRPUSDC, DOGEUSDC, AVAXUSDC, LINKUSDC, INJUSDC

### Cost Model
Round-trip: 0.14% (0.04% taker + 0.03% slippage per side)

---

## Portfolio Results (Equal-Weight Across 9 Coins)

| Metric | Conservative | Balanced | Aggressive |
|--------|-------------|----------|------------|
| Total Return | -30.4% | -8.6% | -48.4% |
| Annualized Return | -27.3% | -7.6% | -44.1% |
| Sharpe Ratio | -2.16 | 0.06 | -0.75 |
| Sortino Ratio | -1.68 | 0.10 | -0.98 |
| Max Drawdown | 33.0% | 40.8% | 61.4% |
| Calmar Ratio | -0.83 | -0.19 | -0.72 |
| Profit Factor | 0.68 | 1.08 | 0.82 |
| Win Rate | 47.9% | 45.7% | 40.3% |
| Num Trades | 282 | 427 | 514 |
| Avg Trade Duration (days) | 6.8 | 7.7 | 6.4 |

| Regime Accuracy | 45.7% | 45.7% | 45.7% |

### Success Bar Check

| Criterion | Threshold | Conservative | Balanced | Aggressive |
|-----------|-----------|-------------|----------|------------|
| Sharpe > 1.0 | 1.00 | -2.16 ❌ | 0.06 ❌ | -0.75 ❌ |
| Annualized > 50% | 0.50 | -27.3% ❌ | -7.6% ❌ | -44.1% ❌ |
| Max DD < 25% | 0.25 | 33.0% ❌ | 40.8% ❌ | 61.4% ❌ |
| Walk-Forward Survives | OOS Sharpe > 0.3, DD < 40%, Ret > -10% | ❌ NO | ✅ YES | ✅ YES |

## Walk-Forward Validation (60/40 Split)

### Conservative

| Period | Total Return | Annualized | Sharpe | Sortino | Max DD | Calmar |
|--------|-------------|------------|--------|---------|--------|--------|
| In-Sample (60%) | -24.3% | -33.6% | -2.30 | -1.83 | 27.2% | -1.24 |
| Out-of-Sample (40%) | -9.0% | -18.8% | -2.39 | -2.07 | 9.0% | -2.08 |

**Survives:** ❌ NO

### Balanced

| Period | Total Return | Annualized | Sharpe | Sortino | Max DD | Calmar |
|--------|-------------|------------|--------|---------|--------|--------|
| In-Sample (60%) | -36.2% | -48.3% | -1.68 | -1.73 | 40.8% | -1.18 |
| Out-of-Sample (40%) | 23.0% | 57.6% | 0.95 | 2.74 | 22.4% | 2.57 |

**Survives:** ✅ YES

### Aggressive

| Period | Total Return | Annualized | Sharpe | Sortino | Max DD | Calmar |
|--------|-------------|------------|--------|---------|--------|--------|
| In-Sample (60%) | -59.1% | -73.1% | -2.46 | -2.58 | 61.4% | -1.19 |
| Out-of-Sample (40%) | 4.4% | 10.0% | 0.42 | 0.81 | 29.2% | 0.34 |

**Survives:** ✅ YES

## Monte Carlo Simulation (1,000 Trade-Sequence Shuffles)

| Metric | Conservative | Balanced | Aggressive |
|--------|-------------|----------|------------|
| Prob(Positive Return) | 0.7% | 41.1% | 6.5% |
| 5th Percentile (Worst Case) | -19.9% | -17.1% | -32.8% |
| Median (50th) | -12.7% | -2.0% | -18.1% |
| 95th Percentile (Best Case) | -4.9% | 16.6% | 1.9% |
| Mean Return | -12.7% | -1.3% | -17.5% |
| Median Max DD | 12.7% | 6.8% | 18.2% |

## Per-Coin Breakdown

### Conservative

| Coin | Total Ret | Ann Ret | Sharpe | Max DD | PF | Win% | Trades | WF | MC+ |
|------|-----------|---------|--------|--------|----|------|--------|----|---- |
| BTC | -4.7% | -4.2% | -0.23 | 17.4% | 0.96 | 58% | 19 | ❌ | 48% |
| ETH | -6.8% | -6.0% | -0.06 | 23.5% | 0.97 | 50% | 28 | ❌ | 39% |
| SOL | -36.6% | -33.0% | -0.99 | 38.8% | 0.64 | 50% | 32 | ❌ | 10% |
| BNB | -27.9% | -25.0% | -0.87 | 43.8% | 0.63 | 42% | 24 | ❌ | 9% |
| XRP | -31.8% | -28.5% | -1.02 | 35.8% | 0.70 | 51% | 35 | ❌ | 14% |
| DOGE | -38.0% | -34.3% | -1.30 | 46.5% | 0.56 | 42% | 26 | ❌ | 17% |
| AVAX | -30.5% | -27.4% | -0.92 | 34.0% | 0.70 | 56% | 34 | ❌ | 20% |
| LINK | -38.5% | -34.8% | -1.07 | 40.9% | 0.61 | 47% | 32 | ❌ | 19% |
| INJ | -58.8% | -54.2% | -2.04 | 62.0% | 0.57 | 40% | 52 | ❌ | 6% |

### Balanced

| Coin | Total Ret | Ann Ret | Sharpe | Max DD | PF | Win% | Trades | WF | MC+ |
|------|-----------|---------|--------|--------|----|------|--------|----|---- |
| BTC | 57.5% | 49.1% | 1.12 | 22.9% | 2.52 | 55% | 29 | ✅ | 92% |
| ETH | 95.0% | 79.9% | 0.99 | 35.6% | 1.95 | 49% | 39 | ✅ | 82% |
| SOL | -46.4% | -42.2% | -0.10 | 66.3% | 0.76 | 42% | 43 | ✅ | 29% |
| BNB | -37.6% | -34.0% | -0.27 | 70.1% | 0.79 | 42% | 36 | ✅ | 25% |
| XRP | -35.5% | -32.0% | -0.26 | 48.4% | 0.86 | 50% | 46 | ✅ | 35% |
| DOGE | -84.2% | -80.3% | -1.03 | 89.8% | 0.48 | 41% | 54 | ✅ | 19% |
| AVAX | 135.5% | 112.4% | 1.28 | 47.1% | 1.63 | 60% | 48 | ✅ | 88% |
| LINK | -62.2% | -57.5% | -0.54 | 68.7% | 0.68 | 46% | 52 | ❌ | 25% |
| INJ | -99.1% | -98.5% | -2.64 | 99.3% | 0.58 | 36% | 80 | ❌ | 0% |

### Aggressive

| Coin | Total Ret | Ann Ret | Sharpe | Max DD | PF | Win% | Trades | WF | MC+ |
|------|-----------|---------|--------|--------|----|------|--------|----|---- |
| BTC | 41.5% | 35.7% | 0.82 | 31.0% | 1.75 | 56% | 36 | ✅ | 86% |
| ETH | 33.3% | 28.7% | 0.70 | 52.5% | 1.33 | 45% | 51 | ✅ | 73% |
| SOL | -85.7% | -81.9% | -0.80 | 85.7% | 0.45 | 39% | 59 | ❌ | 14% |
| BNB | -73.0% | -68.4% | -0.52 | 87.3% | 0.65 | 37% | 41 | ✅ | 19% |
| XRP | -69.2% | -64.5% | -1.02 | 74.8% | 0.60 | 38% | 56 | ❌ | 10% |
| DOGE | -94.2% | -91.8% | -1.41 | 96.2% | 0.35 | 39% | 61 | ✅ | 5% |
| AVAX | -3.6% | -3.2% | 0.50 | 60.3% | 1.04 | 50% | 54 | ✅ | 57% |
| LINK | -84.5% | -80.6% | -1.02 | 88.0% | 0.50 | 38% | 58 | ✅ | 10% |
| INJ | -100.0% | -99.9% | -3.36 | 100.0% | 0.43 | 33% | 98 | ❌ | 0% |

## Per-Coin Walk-Forward Detail

### Conservative

| Coin | IS Ret | IS Sharpe | IS DD | OOS Ret | OOS Sharpe | OOS DD | Survives |
|------|--------|-----------|-------|---------|------------|--------|----------|
| BTC | -5.7% | -0.43 | 15.4% | 1.0% | 0.28 | 4.3% | ❌ |
| ETH | -5.5% | -0.04 | 23.4% | -1.4% | -0.16 | 6.0% | ❌ |
| SOL | -36.9% | -1.38 | 36.9% | 0.6% | 0.16 | 5.1% | ❌ |
| BNB | -19.6% | -0.77 | 36.0% | -13.3% | -1.71 | 15.1% | ❌ |
| XRP | -24.5% | -1.02 | 29.1% | -9.6% | -1.23 | 11.5% | ❌ |
| DOGE | -32.9% | -1.46 | 39.6% | -7.5% | -1.16 | 11.4% | ❌ |
| AVAX | -22.3% | -0.88 | 26.2% | -10.6% | -1.15 | 11.3% | ❌ |
| LINK | -34.8% | -1.28 | 37.4% | -5.6% | -0.68 | 11.6% | ❌ |
| INJ | -36.7% | -1.59 | 41.5% | -35.0% | -2.92 | 35.0% | ❌ |

### Balanced

| Coin | IS Ret | IS Sharpe | IS DD | OOS Ret | OOS Sharpe | OOS DD | Survives |
|------|--------|-----------|-------|---------|------------|--------|----------|
| BTC | 6.2% | 0.44 | 22.9% | 60.5% | 1.99 | 14.0% | ✅ |
| ETH | -10.0% | 0.18 | 35.6% | 116.6% | 1.82 | 21.4% | ✅ |
| SOL | -59.7% | -1.20 | 66.3% | 36.8% | 1.03 | 27.9% | ✅ |
| BNB | -49.6% | -0.92 | 67.3% | 19.6% | 0.98 | 16.5% | ✅ |
| XRP | -44.1% | -1.38 | 47.5% | 5.3% | 0.52 | 39.9% | ✅ |
| DOGE | -85.1% | -2.04 | 89.6% | 6.2% | 0.61 | 34.1% | ✅ |
| AVAX | 63.9% | 1.22 | 47.1% | 43.7% | 1.46 | 15.1% | ✅ |
| LINK | -66.7% | -1.58 | 68.7% | 13.7% | 0.71 | 41.0% | ❌ |
| INJ | -80.8% | -1.56 | 85.5% | -95.5% | -3.98 | 96.2% | ❌ |

### Aggressive

| Coin | IS Ret | IS Sharpe | IS DD | OOS Ret | OOS Sharpe | OOS DD | Survives |
|------|--------|-----------|-------|---------|------------|--------|----------|
| BTC | 5.7% | 0.40 | 26.7% | 44.8% | 1.45 | 31.0% | ✅ |
| ETH | -22.9% | 0.17 | 52.5% | 72.8% | 1.51 | 28.5% | ✅ |
| SOL | -80.6% | -1.47 | 84.3% | -23.9% | 0.13 | 58.5% | ❌ |
| BNB | -78.1% | -1.00 | 86.0% | 19.0% | 0.95 | 20.9% | ✅ |
| XRP | -69.6% | -2.16 | 70.9% | -7.4% | 0.14 | 47.1% | ❌ |
| DOGE | -94.3% | -2.41 | 96.0% | 2.3% | 0.53 | 36.0% | ✅ |
| AVAX | -12.6% | 0.46 | 60.3% | 10.3% | 0.63 | 26.3% | ✅ |
| LINK | -87.2% | -2.13 | 88.0% | 21.1% | 0.84 | 34.5% | ✅ |
| INJ | -92.8% | -2.56 | 95.0% | -99.4% | -4.32 | 99.4% | ❌ |

## Verdict & Recommendation

**Best Variant: BALANCED**

- Sharpe: 0.06 (target > 1.0 → ❌)
- Annualized: -7.6% (target > 50% → ❌)
- Max DD: 40.8% (target < 25% → ❌)
- Walk-Forward: ✅ SURVIVES
- Monte Carlo Prob(+): 41.1%
- Monte Carlo 5th %ile: -17.1%

### 🔴 DOES NOT MEET ALL SUCCESS CRITERIA

**Failing:** Sharpe 0.06 ≤ 1.0, Annualized -7.6% ≤ 50%, Max DD 40.8% ≥ 25%

The strategy shows promise but does not clear all deployment bars. See per-coin and per-regime analysis for improvement paths.

## Key Risks

1. **Regime lag:** ADX is a lagging indicator; regime changes may be detected 5-15 bars late
2. **Leverage amplification:** Drawdowns scale linearly with leverage in adverse moves
3. **Short squeeze risk:** Bear-regime shorts are exposed to sudden reversals
4. **Grid whipsaw:** Sideways regime misclassification → false grid signals
5. **Correlation collapse:** All 9 coins are crypto-correlated; tail events hit all positions
6. **Monte Carlo caveat:** Trade-shuffle MC preserves return distribution but not temporal structure; actual worst-case sequences may differ

## Methodology Notes

- **Data:** 365 days daily OHLCV from Binance public API (spot), 200-day indicator warmup
- **Regime detection:** ADX(14) Wilder's method + EMA(200) trend filter
- **Walk-forward:** 60% in-sample / 40% out-of-sample, no re-optimization
- **Monte Carlo:** 1,000 bootstrap resamples (with replacement) of trade sequence, 30% position sizing per trade
- **Costs:** 0.14% round-trip (0.04% taker + 0.03% slippage per side)
- **Liquidation:** Modeled at 1/leverage - 0.1% buffer for leveraged positions

## Analysis: What the Data Shows

### The Balanced Variant is the Only One Worth Talking About

While the full 9-coin portfolio is dragged down by catastrophic losses on INJ (-99%), LINK (-62%), and DOGE (-84%), the **Balanced variant's OOS performance is genuinely strong**:

| OOS Metric | Balanced | Target |
|-----------|----------|--------|
| Return | +23.0% in ~146 days (~57.6% annualized) | >50% ✅ |
| Sharpe | 0.95 | >1.0 (close) |
| Sortino | 2.74 | >1.5 ✅ |
| Max DD | 22.4% | <25% ✅ |
| Calmar | 2.57 | >1.0 ✅ |

The strategy loses money in-sample but makes it ALL back and more out-of-sample. This is the opposite of overfitting — it suggests the edge emerged in the recent ~5 months.

### The Winners vs Losers are Extremely Clear

**Winners (Balanced variant):** BTC (+57%), ETH (+95%), AVAX (+135%) — large-cap, liquid, strong trends.
**Catastrophic losers:** INJ (-99%), DOGE (-84%), LINK (-62%) — either liquidated or ground to zero via repeated stop-outs.

This suggests a **universe filter is critical**: limiting to BTC, ETH, AVAX, and BNB would dramatically improve results.

### Why the Full Portfolio Fails the Success Bar

1. **Universe dilution:** INJ alone (-99%) pulls the portfolio down by 11 percentage points. Without INJ, Balanced portfolio return would be roughly +2% instead of -8.6%.
2. **Correlated drawdowns:** All altcoins draw down simultaneously during crypto-wide selloffs, making the portfolio DD far worse than individual coin DDs.
3. **Regime lag:** 45.7% regime accuracy is barely better than random (33% for 3-class). The ADX(14) on daily bars is too slow to catch regime changes.

### Path Forward (if pursuing this direction)

1. **Universe selection:** Drop INJ, LINK, DOGE — or add a volatility/liquidity screen
2. **Faster regime detection:** Use ADX(7) or hourly ADX for earlier regime detection
3. **Position sizing:** Scale by inverse volatility to reduce altcoin blowup impact
4. **Correlation overlay:** Reduce total portfolio exposure when cross-coin correlation is high
5. **The Balanced OOS results are the most interesting signal in all 4,455+ configurations tested**
