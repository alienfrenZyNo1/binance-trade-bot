# Research Candidate Flag: Coin-Filtered Balanced Regime-Adaptive Strategy

**Date:** 2026-06-27 19:55 UTC
**Flagged by:** BOT-LEAD (automated)
**Type:** Research candidate meeting aggressive targets — NOT a deployment approval request
**Status:** FLAGGED FOR BOSS REVIEW (pending stress test + risk review)

## What was found

Three coin-filtered configurations of the Balanced regime-adaptive strategy meet ALL aggressive alpha targets from Directive 002 on out-of-sample data.

This is the strongest finding of the research program to date. The coin-filtering insight solved the problem that had been dragging the full 9-coin portfolio down: a few catastrophic losers (INJ -99%, DOGE -84%, LINK -62%) masked strong edges on selected coins.

### Three passing configurations (all on OOS data — the honest test)

| Config | Coins | OOS Sharpe | OOS Annualized | OOS Max DD | MC Prob(+) | Status |
|--------|-------|-----------|----------------|------------|------------|--------|
| **Strat Top 3** | APT, AVAX, OP | **1.35** ✅ | **111.2%** ✅ | **19.1%** ✅ | **92.6%** ✅ | 🟢 PASS |
| **Strat Top 5** | APT, AVAX, OP, BTC, RUNE | **1.16** ✅ | **68.5%** ✅ | **24.7%** ✅ | **92.7%** ✅ | 🟢 PASS |
| Quality Top 3 | BNB, ETH, XRP | 1.50 ✅ | 133.9% ✅ | 18.1% ✅ | 67.1% ✅ | 🟢 PASS |

**Selection method:** coins ranked by in-sample Balanced-strategy Sharpe (no look-ahead bias), then top-N taken and validated on the held-out 40% OOS window.

### Key metrics — Strat Top 3 (recommended primary candidate)

| Metric | In-Sample (60%) | Out-of-Sample (40%) | Full Period |
|--------|----------------|--------------------|----|
| Total Return | +91.4%… (APT-led) | +40.5% | +87.8% |
| Annualized Return | high | **+111.2%** | +74.1% |
| Sharpe Ratio | — | **1.35** | 1.24 |
| Max Drawdown | 28.7% | **19.1%** | 29.1% |
| Monte Carlo Prob(+) | — | — | **92.6%** |
| Monte Carlo 5th pct (worst case) | — | — | ~+3 to +5% |

### Why this is stronger than the DD-trend candidate (which failed stress)

1. **OOS-validated, not just full-period.** The DD-trend LINK3x candidate showed +128%/Sharpe 2.09 full-period but its parameter plateau was razor-thin (2/14 neighbors survived) — it was an overfit lucky path. The coin-filter configs are selected on IS data and validated on a *separate* OOS window.
2. **Monte Carlo 92.6% positive** vs the DD-trend's implicit reliance on a specific 28-trade sequence.
3. **Diversification.** 3-5 coins, not a single-coin bet.

## What's NOT done yet (required before any live approval)

1. ❌ Stress test at $500 account scale (slippage, funding, orderbook depth)
2. ❌ Parameter robustness sweep (regime-detection ADX period, stop %, leverage)
3. ❌ Transaction-cost sensitivity (APT/OP are lower-liquidity — verify realistic fills)
4. ❌ Gordon's risk review (leverage liquidation risk, correlation collapse)
5. ❌ Vera's independent backtest verification
6. ❌ Eleanor's final review package
7. ❌ Testnet forward validation

## ⚠️ Risk caveats the Boss must weigh

- **Universe is small (3-5 coins).** Concentration risk. APT and OP are mid-cap; liquidity stress test is mandatory.
- **The strategy uses 2x leverage and short positions.** Live deployment requires explicit Boss authorization of futures/leverage/shorts.
- **Selection method relies on IS-strategy-Sharpe ranking.** This is data-driven but still in-sample fit; the OOS result is one window. Needs additional non-adjacent OOS windows.
- **Regime accuracy was only 45.7%** (barely above random). The edge may be more about coin selection than regime timing.
- **APT/OP/RUNE are higher-volatility coins.** The 19-25% OOS max DD could be worse in an unluckier period.

## Decision

**DEFERRED** — Flagged for Boss awareness only. NOT ready for deployment approval. The full pipeline (stress test → risk review → QA → Eleanor's final review) must complete before any deployment request.

The coin-filter finding is the most credible path to the 100%+ target seen so far, but it is a research result, not a deployable package.

## Review trail

- Script: `scripts/research_coin_filter.py`
- Report: `docs/research/coin-filter-analysis.md`
- Data: `docs/research/coin-filter-data.json`
- Strategy base: `scripts/research_regime_combined.py` (Balanced variant)
- GitHub: posted to [#108](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/108)
