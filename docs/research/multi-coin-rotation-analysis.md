# Multi-Coin Rotation Strategy Analysis

**Date:** 2026-06-27 19:13 UTC
**Data:** 180 days hourly data, 20 USDC pairs
**Pairs:** BTCUSDC, ETHUSDC, SOLUSDC, BNBUSDC, XRPUSDC, DOGEUSDC, ADAUSDC, AVAXUSDC, LINKUSDC, DOTUSDC, MATICUSDC, UNIUSDC, ATOMUSDC, NEARUSDC, APTUSDC, FILUSDC, INJUSDC, SUIUSDC, SEIUSDC, TIAUSDC
**Fee model:** 0.1% per trade (Binance spot taker)
**Risk-free rate:** 4.0% (T-bill)

---

## Executive Summary

This study tests five rotation strategies across 20 liquid USDC pairs over 180 days
of hourly data to determine whether any rotation logic generates genuine alpha after fees.

### Key Findings

1. **Best Sharpe:** RS 30d top1 (Sharpe 0.073)
2. **Best Return:** RS 30d top1 (-15.4%)
3. **Best Risk-Adj:** RS 30d top1 (Max DD 39.4%)

**Verdict:** No strategy achieves meaningful risk-adjusted edge after fees.

## Detailed Results

| Strategy | Total Ret | Annual Ret | Sharpe | Max DD | Profit Factor | Turnover | Fees Paid | Fee Impact |
|----------|----------|------------|--------|--------|---------------|----------|-----------|------------|
| RS 7d top1 | -73.4% | -96.0% | -2.791 | 76.1% | 0.92 | 100% | 1.08% | 1% |
| RS 7d top3 | -47.9% | -79.6% | -1.919 | 51.1% | 0.94 | 90% | 1.20% | 3% |
| RS 7d top5 | -44.0% | -75.6% | -1.939 | 48.1% | 0.94 | 84% | 1.05% | 2% |
| RS 14d top1 | -69.8% | -94.6% | -2.625 | 72.8% | 0.92 | 100% | 0.98% | 1% |
| RS 14d top3 | -50.5% | -82.0% | -2.043 | 53.3% | 0.94 | 72% | 0.83% | 2% |
| RS 14d top5 | -50.2% | -81.7% | -2.259 | 53.1% | 0.93 | 67% | 0.73% | 1% |
| RS 30d top1 | -15.4% | -33.4% | 0.073 | 39.4% | 1.00 | 100% | 0.73% | 5% |
| RS 30d top3 | -30.4% | -58.6% | -0.875 | 40.3% | 0.97 | 64% | 0.73% | 2% |
| RS 30d top5 | -34.8% | -64.7% | -1.292 | 39.9% | 0.96 | 54% | 0.66% | 2% |
| MomVol top1 | -59.0% | -88.6% | -1.682 | 63.1% | 0.95 | 100% | 1.17% | 2% |
| MomVol top3 | -45.2% | -76.8% | -1.688 | 48.6% | 0.95 | 84% | 1.03% | 2% |
| MomVol top5 | -43.2% | -74.8% | -1.827 | 47.4% | 0.95 | 83% | 1.05% | 2% |
| ADX>20 top3 | -47.1% | -78.8% | -1.870 | 50.5% | 0.95 | 89% | 1.28% | 3% |
| ADX>20 top5 | -40.8% | -72.1% | -1.642 | 44.2% | 0.95 | 84% | 1.20% | 3% |
| ADX>25 top3 | -53.6% | -84.6% | -2.325 | 56.7% | 0.93 | 88% | 1.20% | 2% |
| ADX>25 top5 | -47.5% | -79.2% | -1.966 | 52.0% | 0.94 | 83% | 1.21% | 3% |
| ADX>30 top3 | -48.0% | -79.6% | -1.692 | 52.0% | 0.95 | 94% | 1.41% | 3% |
| ADX>30 top5 | -44.4% | -76.0% | -1.533 | 48.7% | 0.95 | 94% | 1.48% | 3% |
| MeanRev top1 | -69.2% | -94.3% | -2.746 | 69.7% | 0.90 | 100% | 5.89% | 9% |
| MeanRev top3 | -65.8% | -92.7% | -3.235 | 66.7% | 0.90 | 92% | 6.04% | 10% |
| MeanRev top5 | -63.8% | -91.6% | -3.100 | 64.8% | 0.90 | 91% | 6.73% | 12% |
| MultiSignal top1 | -38.2% | -68.9% | -0.596 | 56.5% | 0.98 | 100% | 1.37% | 4% |
| MultiSignal top3 | -37.6% | -68.3% | -1.145 | 43.0% | 0.97 | 80% | 1.09% | 3% |
| MultiSignal top5 | -42.8% | -74.4% | -1.686 | 46.6% | 0.95 | 82% | 1.07% | 3% |
| Equal Weight All | -37.3% | -67.9% | -1.614 | 41.0% | 0.95 | 0% | 0.00% | 0% |

## Strategy Breakdown

### 1. Relative Strength Rotation

Ranks all coins by trailing return (7d/14d/30d lookback). Holds top N. Rebalances weekly.

**Results by lookback & portfolio size:**

| Config | Return | Sharpe | Max DD | Turnover |
|--------|--------|--------|--------|----------|
| RS 14d top1 | -69.8% | -2.625 | 72.8% | 100% |
| RS 14d top3 | -50.5% | -2.043 | 53.3% | 72% |
| RS 14d top5 | -50.2% | -2.259 | 53.1% | 67% |
| RS 30d top1 | -15.4% | 0.073 | 39.4% | 100% |
| RS 30d top3 | -30.4% | -0.875 | 40.3% | 64% |
| RS 30d top5 | -34.8% | -1.292 | 39.9% | 54% |
| RS 7d top1 | -73.4% | -2.791 | 76.1% | 100% |
| RS 7d top3 | -47.9% | -1.919 | 51.1% | 90% |
| RS 7d top5 | -44.0% | -1.939 | 48.1% | 84% |

### 2. Momentum + Volume Rotation

Ranks by momentum score (price change × volume surge). This is what the current bot tries to do.

| Config | Return | Sharpe | Max DD | Turnover |
|--------|--------|--------|--------|----------|
| MomVol top1 | -59.0% | -1.682 | 63.1% | 100% |
| MomVol top3 | -45.2% | -1.688 | 48.6% | 84% |
| MomVol top5 | -43.2% | -1.827 | 47.4% | 83% |

### 3. Trend Strength (ADX) Rotation

Ranks by ADX. Only holds coins with strong trends (ADX > threshold).

| Config | Return | Sharpe | Max DD | Turnover |
|--------|--------|--------|--------|----------|
| ADX>20 top3 | -47.1% | -1.870 | 50.5% | 89% |
| ADX>20 top5 | -40.8% | -1.642 | 44.2% | 84% |
| ADX>25 top3 | -53.6% | -2.325 | 56.7% | 88% |
| ADX>25 top5 | -47.5% | -1.966 | 52.0% | 83% |
| ADX>30 top3 | -48.0% | -1.692 | 52.0% | 94% |
| ADX>30 top5 | -44.4% | -1.533 | 48.7% | 94% |

### 4. Mean Reversion Rotation

Ranks by oversold condition (RSI < 40 or z-score < -1). Buys most oversold, exits on recovery.

| Config | Return | Sharpe | Max DD | Turnover |
|--------|--------|--------|--------|----------|
| MeanRev top1 | -69.2% | -2.746 | 69.7% | 100% |
| MeanRev top3 | -65.8% | -3.235 | 66.7% | 92% |
| MeanRev top5 | -63.8% | -3.100 | 64.8% | 91% |

### 5. Multi-Signal Composite Rotation

Combines momentum (35%) + volume (20%) + trend (25%) + RSI (20%) into a composite score.

| Config | Return | Sharpe | Max DD | Turnover |
|--------|--------|--------|--------|----------|
| MultiSignal top1 | -38.2% | -0.596 | 56.5% | 100% |
| MultiSignal top3 | -37.6% | -1.145 | 43.0% | 80% |
| MultiSignal top5 | -42.8% | -1.686 | 46.6% | 82% |

### Baseline: Equal Weight (Buy & Hold All)

- Total Return: -37.3%
- Sharpe: -1.614
- Max Drawdown: 41.0%

## Stress Tests

### Flash Crash Simulation

Injects a synchronized price drop at the midpoint of the data period.

| Crash Size | Return Impact | Max DD |
|-----------|--------------|--------|
| 10% | -49.6% | 53.3% |
| 20% | -55.2% | 58.5% |
| 30% | -60.8% | 63.7% |

### Correlation Analysis

- **Average pairwise correlation:** 0.637
- **Max correlation:** 0.918
- **Min correlation:** 0.003
- **Effective independent bets:** 1.5 out of 20 coins
- **Diversification ratio:** 0.076 (1.0 = perfect diversification)

**Correlation = 1 scenario** (all coins move in lockstep, e.g. flash crash):

- Return: -32.04%
- Max DD: 34.81%
- Sharpe: -1.762

When correlation → 1, diversification benefit vanishes — holding 20 coins
is no better than holding 1. With average correlation of 0.637,
only ~1.5 independent bets exist. Rotation cannot protect against systemic sell-offs.

### Optimal Portfolio Sizing

How many coins should you hold for optimal diversification?

| Coins Held | Return | Sharpe | Max DD |
|-----------|--------|--------|--------|
| 1 | -73.1% | -2.760 | 76.1% |
| 3 | -47.8% | -1.907 | 51.1% |
| 5 | -44.0% | -1.934 | 48.1% |
| 10 | -42.5% | -1.862 | 45.4% |
| 15 | -40.5% | -1.760 | 43.6% |
| 20 | -37.4% | -1.621 | 41.0% |

## Conclusions

- **0/25** strategy configurations are profitable (absolute return > 0)
- **3/25** beat the equal-weight benchmark (-37.3%)
- **0/25** achieve Sharpe > 0.5

### Alpha vs Benchmark

The equal-weight hold-all benchmark returned **-37.3%** (Max DD 41.0%).

Best alpha vs benchmark: **RS 30d top1** at +21.9% excess return.
This means rotation added 21.9% over buy-and-hold-all, but both are still deeply negative.

**No configuration meets Sharpe > 0.5 threshold.**

- **Average fee impact:** 3.4% of gross returns consumed by fees
- **Average turnover per rebalance:** 83% of holdings changed

### Does combining signals beat single-signal rotation?

**NO** — Best single-signal (RS 30d top1, Sharpe 0.073) beats multi-signal (Sharpe -0.596).

Adding more signals introduces noise without improving ranking quality. Simplicity wins.

### Strategy Rankings (by Sharpe)

| Rank | Strategy | Return | Sharpe | Max DD | vs Benchmark |
|------|----------|--------|--------|--------|-------------|
| 1 | RS 30d top1 | -15.4% | 0.073 | 39.4% | +21.9% |
| 2 | MultiSignal top1 | -38.2% | -0.596 | 56.5% | -0.9% |
| 3 | RS 30d top3 | -30.4% | -0.875 | 40.3% | +6.9% |
| 4 | MultiSignal top3 | -37.6% | -1.145 | 43.0% | -0.3% |
| 5 | RS 30d top5 | -34.8% | -1.292 | 39.9% | +2.5% |
| 6 | ADX>30 top5 | -44.4% | -1.533 | 48.7% | -7.1% |
| 7 | Equal Weight All | -37.3% | -1.614 | 41.0% | +0.0% |
| 8 | ADX>20 top5 | -40.8% | -1.642 | 44.2% | -3.5% |
| 9 | MomVol top1 | -59.0% | -1.682 | 63.1% | -21.7% |
| 10 | MultiSignal top5 | -42.8% | -1.686 | 46.6% | -5.5% |

### Recommendations

1. **The market environment matters enormously.** Over this 180-day window, all 20 coins
   declined an average of 37%. No rotation strategy can overcome a systemic bear market.
   Rotation strategies work in bull/range markets and fail in correlated selloffs.

2. **Longer lookbacks reduce churn.** 30-day lookback had the lowest turnover and closest
   to benchmark returns. Short lookbacks (7d) generate excessive trading that bleeds fees.

3. **Mean reversion is the worst strategy** — it buys falling knives and pays 10-12% of
   returns in fees due to daily rebalancing. In crypto bear markets, oversold gets more oversold.

4. **Holding more coins reduces drawdown but not enough.** Going from 1 to 20 coins cut
   max DD from ~76% to ~41%, but average correlation of 0.64 means diversification is limited.

5. **For production:** Add a regime filter — only rotate when market trend is up.
   Hold cash/stablecoins during downtrends. Rotation alpha is ~0 in bear markets.

---

*Generated by `scripts/research_multi_coin_rotation.py` on 2026-06-27 19:13 UTC*
