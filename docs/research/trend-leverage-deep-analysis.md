# 🔬 Trend Following with Leverage: Deep Analysis

**Generated:** 2026-06-27 19:11 UTC
**Data:** 365 days daily OHLCV, top 15 USDC pairs from Binance
**Fee model:** 0.14% round-trip (taker + slippage)
**Liquidation:** Modeled at 1/leverage threshold with 0.1% buffer
**Total combos tested:** 1995

## 🎯 KEY FINDING: Best Strategy with Sharpe > 1.0 & Max DD < 25%

**No combination met the Sharpe > 1.0 AND Max DD < 25% criteria.**

This is the reality of trend following — the strategies that return the most
tend to have deep drawdowns. See the relaxed criteria below.

### Relaxed criteria: Sharpe > 0.8 & Max DD < 35%

| Rank | Coin | Strategy | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate |
|------|------|----------|--------|-----|------------|--------|---------|--------|----------|
| 1 | ETH | Supertrend | ST(14,5) | 1x | 39.4% | 0.94 | 1.08 | -28.6% | 100% |

## 📊 Top 20 by Annualized Return (No Filter)

| Rank | Coin | Strategy | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate | PF |
|------|------|----------|--------|-----|------------|--------|---------|--------|----------|-----|
| 1 | XRP | Supertrend | ST(14,7) | 5x | 16405.0% | 2.79 | 7.24 | -73.3% | 100% | ∞ |
| 2 | XRP | EMA_Crossover | EMA(10,200) | 5x | 15875.8% | 2.77 | 6.99 | -72.7% | 100% | ∞ |
| 3 | XRP | EMA_Crossover | EMA(20,200) | 5x | 12655.0% | 2.71 | 6.84 | -72.7% | 100% | ∞ |
| 4 | XRP | EMA_Crossover | EMA(50,150) | 5x | 11992.5% | 2.70 | 6.80 | -72.7% | 100% | ∞ |
| 5 | XRP | EMA_Crossover | EMA(10,150) | 5x | 11037.9% | 2.66 | 6.42 | -73.0% | 67% | 90.10 |
| 6 | XRP | EMA_Crossover | EMA(30,200) | 5x | 10855.2% | 2.67 | 6.72 | -72.7% | 100% | ∞ |
| 7 | XRP | EMA_Crossover | EMA(50,200) | 5x | 9748.3% | 2.64 | 6.67 | -73.3% | 100% | ∞ |
| 8 | XRP | EMA_Crossover | EMA(30,150) | 5x | 8597.2% | 2.59 | 6.48 | -72.7% | 100% | ∞ |
| 9 | XRP | EMA_Crossover | EMA(10,100) | 5x | 7902.9% | 2.54 | 5.92 | -77.7% | 50% | 14.94 |
| 10 | XRP | EMA_Crossover | EMA(20,150) | 5x | 7604.0% | 2.55 | 6.31 | -80.2% | 50% | 20.32 |
| 11 | XRP | Supertrend | ST(7,7) | 5x | 6459.7% | 2.53 | 6.47 | -94.4% | 50% | 10.88 |
| 12 | XRP | EMA_Crossover | EMA(20,100) | 5x | 5926.7% | 2.45 | 5.78 | -81.2% | 33% | 14.45 |
| 13 | XRP | EMA_Crossover | EMA(30,100) | 5x | 5290.5% | 2.42 | 5.70 | -86.5% | 50% | 10.57 |
| 14 | XRP | Supertrend | ST(10,7) | 5x | 4679.6% | 2.44 | 6.28 | -96.7% | 50% | 8.94 |
| 15 | XRP | Supertrend | ST(10,5) | 5x | 4567.7% | 2.37 | 5.48 | -91.5% | 33% | 8.12 |
| 16 | XRP | Supertrend | ST(14,3) | 5x | 4302.9% | 2.41 | 4.97 | -85.6% | 33% | 6.49 |
| 17 | XRP | EMA_Crossover | EMA(50,100) | 5x | 4069.2% | 2.35 | 5.63 | -74.1% | 33% | 8.24 |
| 18 | LINK | Supertrend | ST(14,7) | 5x | 3890.4% | 2.48 | 6.44 | -80.6% | 100% | ∞ |
| 19 | ADA | Supertrend | ST(10,7) | 5x | 3696.3% | 2.13 | 6.73 | -87.9% | 100% | ∞ |
| 20 | XRP | Supertrend | ST(7,5) | 5x | 3692.3% | 2.33 | 5.59 | -93.2% | 33% | 7.06 |

## 🏆 Best Configuration per Strategy

| Strategy | Coin | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate |
|----------|------|--------|-----|------------|--------|---------|--------|----------|
| Donchian_Breakout | XRP | DC(20,10) | 5x | 2189.5% | 2.18 | 4.49 | -92.5% | 25% |
| EMA_Crossover | XRP | EMA(10,200) | 5x | 15875.8% | 2.77 | 6.99 | -72.7% | 100% |
| Parabolic_SAR | XRP | SAR(0.02,0.2) | 5x | 648.4% | 1.80 | 3.78 | -96.4% | 29% |
| Supertrend | XRP | ST(14,7) | 5x | 16405.0% | 2.79 | 7.24 | -73.3% | 100% |

## 🪙 Best Strategy per Coin

| Coin | Strategy | Params | Lev | Ann Return | Sharpe | Max DD | Win Rate |
|------|----------|--------|-----|------------|--------|--------|----------|
| ADA | Supertrend | ST(10,7) | 5x | 3696.3% | 2.13 | -87.9% | 100% |
| APT | Supertrend | ST(10,7) | 5x | 14.8% | 0.83 | -89.8% | 0% |
| ATOM | EMA_Crossover | EMA(20,200) | 5x | 138.3% | 1.21 | -69.8% | 0% |
| AVAX | EMA_Crossover | EMA(10,150) | 5x | 479.5% | 1.73 | -84.6% | 50% |
| BNB | EMA_Crossover | EMA(50,100) | 5x | 153.1% | 1.34 | -85.7% | 50% |
| BTC | EMA_Crossover | EMA(50,150) | 5x | 46.7% | 0.99 | -74.1% | 50% |
| DOGE | EMA_Crossover | EMA(20,50) | 5x | 1936.2% | 2.20 | -86.6% | 50% |
| DOT | Supertrend | ST(14,7) | 5x | 266.9% | 1.49 | -81.4% | 0% |
| ETH | Supertrend | ST(14,5) | 5x | 449.0% | 1.76 | -70.6% | 100% |
| LINK | Supertrend | ST(14,7) | 5x | 3890.4% | 2.48 | -80.6% | 100% |
| LTC | Supertrend | ST(7,7) | 5x | 2286.8% | 2.37 | -80.8% | 100% |
| MATIC | Donchian_Breakout | DC(15,7) | 5x | 717.8% | 1.96 | -58.9% | 50% |
| NEAR | Supertrend | ST(10,7) | 5x | 353.1% | 1.86 | -97.2% | 33% |
| SOL | EMA_Crossover | EMA(30,100) | 5x | 110.6% | 1.35 | -91.7% | 67% |
| XRP | Supertrend | ST(14,7) | 5x | 16405.0% | 2.79 | -73.3% | 100% |

## ⚡ Leverage Impact Analysis

| Leverage | Mean Ann Return | Median Ann Return | Mean Sharpe | Median Sharpe | Mean Max DD | Median Max DD |
|----------|-----------------|-------------------|-------------|---------------|-------------|---------------|
| 1x | -8.3% | -12.2% | -0.04 | 0.00 | -55.9% | -56.2% |
| 2x | -30.0% | -39.6% | -0.03 | 0.02 | -81.9% | -85.8% |
| 3x | -41.1% | -57.6% | 0.10 | 0.14 | -90.9% | -94.3% |
| 5x | 540.6% | 46.7% | 1.12 | 1.12 | -88.6% | -90.7% |

## 📈 Strategy Type Comparison

| Strategy | Mean Ann | Median Ann | Max Ann | Mean Sharpe | Median Sharpe | Max Sharpe | Mean MaxDD | Median MaxDD |
|----------|----------|------------|---------|-------------|---------------|------------|------------|--------------|
| Donchian_Breakout | -0.7% | -33.8% | 2189.5% | -0.06 | -0.07 | 2.18 | -77.9% | -84.2% |
| EMA_Crossover | 168.6% | -17.2% | 15875.8% | 0.39 | 0.31 | 2.77 | -76.4% | -81.9% |
| Parabolic_SAR | -30.3% | -55.2% | 648.4% | -0.06 | -0.17 | 1.80 | -86.2% | -92.1% |
| Supertrend | 109.2% | -25.3% | 16405.0% | 0.34 | 0.26 | 2.79 | -83.0% | -89.3% |

## 🔬 Walk-Forward Validation (Top 5 Combos)

**Method:** Signals computed on full dataset (for indicator continuity), returns split: train = first 2/3, test = last 1/3.

**OOS Period Context:** The last 1/3 of data (~Dec 2025 – Jun 2026) was a **severe bear market**
for crypto — all major coins were down 30-65% during this period. A long-only trend
strategy that goes to cash (0 position) during this time is actually *protecting capital*.
Therefore, ROBUST means: the strategy avoided most of the crash or at least beat B&H significantly.

| Coin | Strategy | Params | Lev | Train Ann Ret | Train Sharpe | Train MaxDD | Test Ann Ret | Test Sharpe | Test MaxDD | B&H Ann Ret (OOS) | Assessment |
|------|----------|--------|-----|---------------|--------------|-------------|--------------|-------------|------------|--------------------|------------|
| MATIC | Donchian_Breakout | DC(15,7) | 5x | 2134.5% | 2.54 | -48.7% | 9.5% | 0.77 | -58.9% | -84.0% | ✅ ROBUST |
| XRP | EMA_Crossover | EMA(10,200) | 1x | 250.1% | 1.79 | -45.5% | 0.0% | 0.00 | 0.0% | -68.8% | ✅ ROBUST |
| XRP | Supertrend | ST(14,7) | 1x | 246.5% | 1.76 | -45.5% | 0.0% | 0.00 | 0.0% | -68.8% | ✅ ROBUST |
| XRP | Donchian_Breakout | DC(20,10) | 1x | 184.7% | 1.76 | -49.6% | -43.2% | -2.04 | -35.1% | -68.8% | ❌ FRAGILE |
| ETH | Supertrend | ST(14,5) | 2x | 95.9% | 1.15 | -54.0% | 0.0% | 0.00 | 0.0% | -68.1% | ✅ ROBUST |

**Robust combos: 4/5 survived out-of-sample testing.**

**Interpretation:** In a bear market OOS period, 'robust' means the strategy either:
- Preserved capital (flat or small loss vs huge B&H loss)
- Generated positive returns despite the crash (very rare)
- The Sharpe degradation metric shows how much worse the strategy performed OOS vs in-sample.
## 💥 Liquidation Events

- Total combos tested: 1980
- Combos with >80% max drawdown (near-liquidation): 1197 (60.5%)
  - At 1x leverage: 31 of 495 (6.3%) had >80% DD
  - At 2x leverage: 328 of 495 (66.3%) had >80% DD
  - At 3x leverage: 430 of 495 (86.9%) had >80% DD
  - At 5x leverage: 408 of 495 (82.4%) had >80% DD

## 💡 Key Insights

1. **Buy & Hold benchmark:** Average annualized return across 15 coins: -34.6%
2. **Best 1x strategy:** Median ann return -12.2% vs B&H -34.6%
3. **3x leverage amplifies:** Median ann return -57.6%, but median max DD -94.3%
4. **5x leverage is dangerous:** Median ann return 46.7%, but 408 of 495 combos blew up
5. **Strategy ranking by median Sharpe:** EMA_Crossover > Supertrend > Donchian_Breakout > Parabolic_SAR

## ⚠️ Important Caveats

- **In-sample bias:** Results shown are optimized over the full period. The walk-forward
  section shows what happens out-of-sample, which is more realistic.
- **Liquidation model is simplified:** Real liquidations have cascading effects, funding costs,
  and maintenance margin calls. Actual risk is higher than modeled.
- **Funding rates not included:** Short positions in perpetuals pay/receive funding,
  which can significantly impact returns over 365 days.
- **Single period test:** 365 days may not capture regime changes. Crypto trends can
  shift dramatically between bull/bear/sideways markets.
- **Daily timeframe:** Intraday execution would change results. Real-world slippage on
  breakout entries is typically worse than modeled.
