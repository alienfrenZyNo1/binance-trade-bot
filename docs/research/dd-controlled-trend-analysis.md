# Drawdown-Controlled Trend Following with Leverage

**Generated:** 2026-06-27 19:26 UTC
**Data:** 9024 hourly bars (~376 days) per symbol, Binance USDC-M perps public API
**Symbols:** BTCUSDC, ETHUSDC, SOLUSDC, XRPUSDC, DOGEUSDC, SUIUSDC, AVAXUSDC, LINKUSDC, ADAUSDC, NEARUSDC
**Window:** 2025-06-16 20:00:00+00:00 -> 2026-06-27 19:00:00+00:00 (last ~1/3 was a severe bear market)
**Base signals:** EMA(50/200), Supertrend(14,7), Donchian(20,10) — long AND short
**Leverage:** 1x, 2x, 3x
**Costs:** 0.04% taker + 0.03% slippage per side, 0.010% funding/8h
**Configs tested:** 1350 (3 signals x 15 overlays x 3 lev x 10 symbols)

---

## Drawdown-Control Overlays Tested

| Overlay | Mechanism |
|--------|-----------|
| **ATR position sizing** (`atr_r1`=1%, `atr_r2`=2%) | Size = risk%·equity / (risk_unit·ATR_frac), capped at leverage. Shrinks exposure when volatility spikes. |
| **Trailing stop** (`trail_1.5/2.0/3.0`) | Exit when price moves N·ATR against the best favorable price since entry. |
| **Volatility regime filter** (`volfilter`) | Skip new entries when ATR/price > rolling-720bar 75th percentile. |
| **Equity drawdown breaker** (`cbreaker`) | Halve size at −10% running DD; force flat + halt new entries at −15% until recovery to within −10%. |
| **Combinations** | `atr2_trail2`, `atr2_vf_cb`, `atr2_trail2_vf`, `atr2_trail2_cb`, `full` (atr2+trail2+vf+cb), `full_atr1`, `full_trail3` |

---

## Target Gate: Sharpe > 1.0 AND Ann > 50% AND MaxDD < 20%

**7** of 1350 configs meet all three targets.

| Rank | Symbol | Strategy | Overlay | Lev | Ann Ret | Sharpe | Sortino | Max DD | Calmar | Win% | PF | Trades |
|------|--------|----------|---------|-----|---------|--------|---------|--------|--------|------|----|--------|
| 1 | LINKUSDC | donchian | atr2_vf_cb | 3x | 128.3% | 2.09 | 2.04 | -17.3% | 7.42 | 54% | 3.20 | 28 |
| 2 | LINKUSDC | donchian | atr2_vf_cb | 2x | 123.3% | 2.06 | 1.99 | -17.3% | 7.13 | 54% | 3.17 | 28 |
| 3 | LINKUSDC | donchian | atr2_vf_cb | 1x | 69.3% | 1.89 | 1.61 | -15.2% | 4.56 | 45% | 2.57 | 38 |
| 4 | NEARUSDC | donchian | cbreaker | 1x | 69.3% | 1.95 | 1.47 | -15.4% | 4.49 | 48% | 1.76 | 60 |
| 5 | NEARUSDC | donchian | atr2_vf_cb | 1x | 64.2% | 1.94 | 1.40 | -15.6% | 4.11 | 46% | 1.83 | 52 |
| 6 | ETHUSDC | supertrend | cbreaker | 1x | 56.7% | 1.55 | 1.26 | -15.2% | 3.72 | 62% | 4.21 | 13 |
| 7 | DOGEUSDC | donchian | atr2_vf_cb | 1x | 54.4% | 1.71 | 1.06 | -15.3% | 3.54 | 54% | 2.11 | 35 |

## Top 10 by Risk-Adjusted Return (Total Return / |Max DD|)

Regardless of absolute thresholds — pure return-per-unit-drawdown efficiency.

| Rank | Symbol | Strategy | Overlay | Lev | Ann Ret | Sharpe | Max DD | Ret/|DD| | Calmar | Win% | PF | Trades |
|------|--------|----------|---------|-----|---------|--------|--------|-----------|--------|------|----|--------|
| 1 | LINKUSDC | donchian | atr2_vf_cb | 3x | 128.3% | 2.09 | -17.3% | 7.75 | 7.42 | 54% | 3.20 | 28 |
| 2 | LINKUSDC | donchian | atr2_vf_cb | 2x | 123.3% | 2.06 | -17.3% | 7.44 | 7.13 | 54% | 3.17 | 28 |
| 3 | SUIUSDC | supertrend | volfilter | 3x | 449.2% | 1.75 | -68.8% | 6.95 | 6.53 | 46% | 1.39 | 35 |
| 4 | SUIUSDC | supertrend | volfilter | 2x | 341.5% | 1.68 | -54.2% | 6.67 | 6.30 | 46% | 1.61 | 35 |
| 5 | ETHUSDC | donchian | atr_r1 | 3x | 213.8% | 1.74 | -37.6% | 5.98 | 5.68 | 36% | 1.35 | 236 |
| 6 | SUIUSDC | supertrend | volfilter | 1x | 163.1% | 1.57 | -34.1% | 5.01 | 4.78 | 46% | 1.95 | 35 |
| 7 | ETHUSDC | donchian | atr_r1 | 2x | 174.8% | 1.65 | -37.1% | 4.94 | 4.71 | 36% | 1.31 | 236 |
| 8 | LINKUSDC | donchian | atr2_vf_cb | 1x | 69.3% | 1.89 | -15.2% | 4.74 | 4.56 | 45% | 2.57 | 38 |
| 9 | NEARUSDC | donchian | cbreaker | 1x | 69.3% | 1.95 | -15.4% | 4.66 | 4.49 | 48% | 1.76 | 60 |
| 10 | ETHUSDC | donchian | atr_r2 | 3x | 254.2% | 1.57 | -59.0% | 4.54 | 4.31 | 36% | 1.20 | 236 |

## How Each Drawdown-Control Overlay Improves the Base Strategy

Average change vs the **base** (no-overlay) config, across every symbol x strategy x leverage.
Positive ΔSharpe and ΔAnn are good; ΔMaxDD **less negative** (closer to 0) is good.

| Overlay | ΔSharpe | ΔMax DD | ΔAnn Ret | ΔCalmar | Best Ann | Best Sharpe | Best(least-bad) MaxDD | #meet gate |
|---------|---------|---------|----------|---------|----------|-------------|-----------------------|------------|
| base | +0.00 | +0.0% | +0.0% | +0.00 | 215.7% | 1.50 | -34.7% | 0 |
| atr_r1 | +0.15 | +23.4% | +42.2% | +0.75 | 213.8% | 1.74 | -31.5% | 0 |
| atr_r2 | +0.08 | +7.0% | +28.6% | +0.38 | 254.2% | 1.57 | -34.7% | 0 |
| trail_1.5 | -3.56 | +23.9% | -32.4% | -0.83 | -2.5% | 0.06 | -15.3% | 0 |
| trail_2.0 | -2.96 | +18.5% | -36.3% | -0.81 | -0.0% | 0.14 | -17.6% | 0 |
| trail_3.0 | -2.11 | +10.1% | -38.4% | -0.71 | 14.9% | 0.59 | -20.0% | 0 |
| volfilter | -0.20 | +1.8% | +6.0% | +0.09 | 449.2% | 1.75 | -34.1% | 0 |
| cbreaker | -0.51 | +60.1% | +18.4% | +0.23 | 69.3% | 1.95 | -15.0% | 2 |
| atr2_trail2 | -3.16 | +38.4% | -18.2% | -0.82 | -10.0% | -0.98 | -14.2% | 0 |
| atr2_vf_cb | -0.46 | +60.1% | +20.6% | +0.35 | 128.3% | 2.09 | -15.0% | 5 |
| atr2_trail2_vf | -2.98 | +42.2% | -14.0% | -0.77 | -3.9% | -0.25 | -9.9% | 0 |
| atr2_trail2_cb | -2.04 | +61.0% | +3.6% | -0.77 | -2.5% | -0.08 | -12.8% | 0 |
| full | -2.05 | +61.5% | +4.3% | -0.74 | -2.9% | -0.18 | -9.9% | 0 |
| full_atr1 | -2.37 | +63.9% | +6.0% | -0.74 | -1.8% | -0.25 | -5.5% | 0 |
| full_trail3 | -1.02 | +61.7% | +11.7% | -0.21 | 28.7% | 1.62 | -6.7% | 0 |

### Key takeaway

- The **full stack** (`full`: ATR2 + trail2 + volfilter + cbreaker) moves average MaxDD by +61.5% and average Sharpe by -2.05 vs base.
- Circuit breaker alone shifts average MaxDD by +60.1% (it mechanically caps drawdown).
- ATR-2% sizing shifts MaxDD by +7.0% (de-risks in high-vol regimes).
- Trailing-stop 2.0 shifts MaxDD by +18.5%.

## Per-Strategy Summary

### ema
Configs: 450 | Meet gate: 0

Best risk-adjusted (any):
| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Ret/|DD| |
|--------|---------|-----|-----|--------|--------|-----------|
| ETHUSDC | atr2_vf_cb | 1x | 41.2% | 1.40 | -15.8% | 2.70 |
| SUIUSDC | atr_r1 | 2x | 118.5% | 1.30 | -46.1% | 2.68 |
| SUIUSDC | atr_r1 | 3x | 118.5% | 1.30 | -46.1% | 2.68 |

### supertrend
Configs: 450 | Meet gate: 1

Best meeting-gate configs:
| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Calmar |
|--------|---------|-----|-----|--------|--------|--------|
| ETHUSDC | cbreaker | 1x | 56.7% | 1.55 | -15.2% | 3.72 |

Best risk-adjusted (any):
| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Ret/|DD| |
|--------|---------|-----|-----|--------|--------|-----------|
| SUIUSDC | volfilter | 3x | 449.2% | 1.75 | -68.8% | 6.95 |
| SUIUSDC | volfilter | 2x | 341.5% | 1.68 | -54.2% | 6.67 |
| SUIUSDC | volfilter | 1x | 163.1% | 1.57 | -34.1% | 5.01 |

### donchian
Configs: 450 | Meet gate: 6

Best meeting-gate configs:
| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Calmar |
|--------|---------|-----|-----|--------|--------|--------|
| LINKUSDC | atr2_vf_cb | 3x | 128.3% | 2.09 | -17.3% | 7.42 |
| LINKUSDC | atr2_vf_cb | 2x | 123.3% | 2.06 | -17.3% | 7.13 |
| LINKUSDC | atr2_vf_cb | 1x | 69.3% | 1.89 | -15.2% | 4.56 |

Best risk-adjusted (any):
| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Ret/|DD| |
|--------|---------|-----|-----|--------|--------|-----------|
| LINKUSDC | atr2_vf_cb | 3x | 128.3% | 2.09 | -17.3% | 7.75 |
| LINKUSDC | atr2_vf_cb | 2x | 123.3% | 2.06 | -17.3% | 7.44 |
| ETHUSDC | atr_r1 | 3x | 213.8% | 1.74 | -37.6% | 5.98 |

## Per-Symbol Summary (vs Buy & Hold)

| Symbol | Buy&Hold Ann | Buy&Hold MaxDD | #meet gate | Best meeting config |
|--------|--------------|----------------|------------|---------------------|
| BTCUSDC | -43.7% | -53.8% | 0 | _(none meet gate)_ |
| ETHUSDC | -40.0% | -69.2% | 1 | supertrend/cbreaker/1x: Ann 56.7%, Shp 1.55, DD -15.2% |
| SOLUSDC | -53.8% | -75.8% | 0 | _(none meet gate)_ |
| XRPUSDC | -53.6% | -72.3% | 0 | _(none meet gate)_ |
| DOGEUSDC | -57.3% | -76.0% | 1 | donchian/atr2_vf_cb/1x: Ann 54.4%, Shp 1.71, DD -15.3% |
| SUIUSDC | -76.8% | -85.2% | 0 | _(none meet gate)_ |
| AVAXUSDC | -66.7% | -83.5% | 0 | _(none meet gate)_ |
| LINKUSDC | -47.3% | -74.4% | 3 | donchian/atr2_vf_cb/3x: Ann 128.3%, Shp 2.09, DD -17.3% |
| ADAUSDC | -76.8% | -86.4% | 0 | _(none meet gate)_ |
| NEARUSDC | -21.5% | -71.3% | 2 | donchian/cbreaker/1x: Ann 69.3%, Shp 1.95, DD -15.4% |

## Leverage Impact (averaged across all configs)

| Leverage | Avg Ann | Avg Sharpe | Avg Max DD | Avg Calmar | #meet gate |
|----------|---------|------------|------------|------------|------------|
| 1x | -9.2% | -1.16 | -32.5% | -0.29 | 5 |
| 2x | -16.5% | -1.17 | -42.0% | -0.41 | 1 |
| 3x | -21.9% | -1.15 | -46.3% | -0.44 | 1 |

## Walk-Forward Validation (train 2/3, test 1/3)

The last 1/3 of the window was a severe bear market, so out-of-sample is a hard test.

| Symbol | Strat | Overlay | Lev | Train Ann | Train Shp | Test Ann | Test Shp | Test MaxDD | Test B&H | Robust? |
|--------|-------|---------|-----|-----------|-----------|----------|----------|------------|----------|---------|
| LINKUSDC | donchian | atr2_vf_cb | 3x | 245.0% | 2.56 | 32.2% | 1.08 | -15.1% | -16.4% | YES |
| LINKUSDC | donchian | atr2_vf_cb | 2x | 233.6% | 2.52 | 32.2% | 1.08 | -15.1% | -16.4% | YES |
| LINKUSDC | donchian | atr2_vf_cb | 1x | 120.3% | 2.31 | 0.7% | 0.13 | -15.1% | -16.4% | YES |
| NEARUSDC | donchian | cbreaker | 1x | 120.2% | 2.39 | 10.5% | 0.45 | -18.4% | 82.1% | no |
| NEARUSDC | donchian | atr2_vf_cb | 1x | 110.4% | 2.38 | 24.8% | 0.74 | -15.5% | 82.1% | no |
| ETHUSDC | supertrend | cbreaker | 1x | 96.2% | 1.90 | -23.9% | -1.42 | -15.3% | -19.7% | no |
| DOGEUSDC | donchian | atr2_vf_cb | 1x | 91.8% | 2.09 | -10.8% | -0.63 | -15.1% | -22.6% | no |
| SUIUSDC | supertrend | volfilter | 3x | 2663.9% | 2.47 | -77.5% | -0.29 | -62.3% | -25.1% | no |
| SUIUSDC | supertrend | volfilter | 2x | 1346.5% | 2.38 | -57.9% | -0.31 | -47.1% | -25.1% | no |
| ETHUSDC | donchian | atr_r1 | 3x | 255.2% | 1.88 | 123.8% | 1.34 | -35.1% | -19.7% | YES |
| SUIUSDC | supertrend | volfilter | 1x | 415.0% | 2.24 | -30.5% | -0.36 | -26.9% | -25.1% | no |
| ETHUSDC | donchian | atr_r1 | 2x | 220.4% | 1.81 | 84.6% | 1.17 | -33.3% | -19.7% | YES |
| ETHUSDC | donchian | atr_r2 | 3x | 363.9% | 1.75 | 72.4% | 1.04 | -55.1% | -19.7% | YES |
