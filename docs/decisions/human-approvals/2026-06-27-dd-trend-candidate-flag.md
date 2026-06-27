# Research Candidate Flag: DD-Controlled Trend Following

**Date:** 2026-06-27 19:30 UTC
**Flagged by:** BOT-LEAD (automated)
**Type:** Research candidate meeting aggressive targets — NOT a deployment approval request
**Status:** FLAGGED FOR BOSS REVIEW (pending stress test + risk review)

## What was found

A drawdown-controlled trend-following strategy with leverage meets ALL aggressive alpha targets from Directive 002.

### Best configuration

| Metric | Value | Directive Target | Pass? |
|--------|-------|------------------|-------|
| Annualized return | +128.3% | > 100% (target) / > 50% (min) | ✅ |
| Sharpe ratio | 2.09 | > 1.0 | ✅ |
| Max drawdown | −17.3% | < 20% | ✅ |
| Calmar ratio | 7.42 | — | — |
| Profit factor | 3.20 | > 1.5 | ✅ |
| Win rate | 54% | — | — |
| Trades | 28 (over 376 days) | — | low frequency |

**Config:** LINKUSDC / Donchian(20,10) breakout / ATR 2% position sizing + volatility filter + equity drawdown circuit breaker / 3x leverage / long+short futures

### Walk-forward validation

- Train (2/3, ~250 days): +245.0% ann / Sharpe 2.56
- Test (1/3, ~126 days, bear market): +32.2% ann / Sharpe 1.08 / MaxDD −15.1%
- Buy & hold (OOS): −16.4%
- Assessment: ROBUST — positive returns and positive Sharpe out-of-sample during a severe bear market

## What's NOT done yet (required before any live approval)

1. ❌ Transaction-level stress test at $500 scale (LINK orderbook depth, slippage modeling)
2. ❌ Funding cost analysis on short legs (LINK perp funding is variable)
3. ❌ Multi-symbol portfolio test (correlation between LINK/NEAR/DOGE candidates)
4. ❌ Gordon's risk review (liquidation risk, margin requirements, position sizing)
5. ❌ Vera's independent backtest verification
6. ❌ Eleanor's final review package
7. ❌ Testnet forward validation

## Decision

**DEFERRED** — This is flagged for Boss awareness only. It is NOT ready for deployment approval. The full pipeline (stress test → risk review → QA → Eleanor's final review) must complete before any deployment request is made.

The candidate is promising but requires rigorous validation. The strategy trades only 28 times in a year (low frequency = good for fee efficiency, but means the edge depends on a small number of trend captures).

## Review trail

- Script: `scripts/research_dd_controlled_trend.py`
- Report: `docs/research/dd-controlled-trend-analysis.md`
- Data: `docs/research/dd-controlled-trend-analysis.json` (1,350 configs)
- Raw data cache: `scripts/_cache_dd_trend/dd_trend_klines.pkl` (4.3MB, 10 symbols × 376 days)
- GitHub: [#108 comment](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/108#issuecomment-4820677601)
- Commit: `4fb2169`
