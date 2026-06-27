# 🟢 CANDIDATE FLAG: 4-Leg Multi-Strategy Portfolio — GREEN Stress-Verified

**Date:** 2026-06-27 20:15 UTC
**Flagged by:** BOT-LEAD (automated check-in cycle)
**Decision needed:** Boss review for live deployment authorization (canary scale)

---

## What Was Found

A 4-leg multi-strategy portfolio has passed ALL aggressive alpha targets AND survived full stress testing with a 🟢 GREEN verdict.

## Portfolio Composition

| Leg | Instrument | Strategy | Leverage | Weight |
|-----|-----------|----------|----------|--------|
| 1 | LINKUSDC | Donchian breakout (ATR filter + circuit breaker) | 3x | 28% |
| 2 | NEARUSDC | Donchian breakout (circuit breaker) | 1x | 28% |
| 3 | ETHUSDC | Supertrend (circuit breaker) | 1x | 25% |
| 4 | DOTUSDT | Funding-rate contrarian (z-score fade) | 3x | 19% |

**Allocation:** Tangency weights (max-Sharpe), monthly rebalanced.

## Performance (Full Sample, ~376 days)

| Metric | Result | Target | Pass? |
|--------|--------|--------|-------|
| Annualized Return | **103.3%** | > 100% | ✅ |
| Sharpe Ratio | **2.78** | > 1.5 | ✅ |
| Max Drawdown | **-9.9%** | < 15% | ✅ |
| Calmar Ratio | 10.41 | — | — |

## Out-of-Sample (60/40 Walk-Forward)

| Metric | Result |
|--------|--------|
| Annualized Return | 191.1% |
| Sharpe Ratio | 2.17 |
| Max Drawdown | -20.2% |

## Stress Test Verdict: 🟢 GREEN

| Gate | Threshold | Result | Pass? |
|------|-----------|--------|-------|
| Multi-split OOS (7 splits) | ≥5/7 keep Sharpe>1 & Ann>50% | **5/7** | ✅ |
| Monte Carlo P(Sharpe>1.0) | ≥60% | **83.0%** | ✅ |
| Slippage Sharpe @0.10%/side | ≥1.0 | **2.62** | ✅ |

### Monte Carlo Distribution (5000 resamples)
- P(Ann > 0) = 93.3%
- P(Ann > 50%) = 80.0%
- Median Ann = 179.9%, Median Sharpe = 2.27

### Slippage Sensitivity
| Slippage/side | Sharpe | Ann Return |
|--------------|--------|-----------|
| 0.03% (baseline) | 2.78 | 103.4% |
| 0.10% (3.3× stress) | 2.62 | 95.1% |
| 0.15% (5× stress) | 1.85 | 45.4% |

## ⚠️ Caveats for Boss Review

1. **Requires futures + leverage enablement.** Legs 1 (LINK 3x) and 4 (DOT 3x) use 3x leverage on perpetuals. Current risk-appetite.yaml caps leverage at 1x. Boss must explicitly authorize 3x leverage.
2. **Rolling-window weakness.** 2/6 expanding windows had Sharpe>1.0. The edge is not uniformly distributed across all time periods — it is concentrated in trending/volatile regimes.
3. **DOT funding contrarian leg.** This leg had OOS Sharpe 1.462 standalone. It depends on funding-rate structure remaining positive-biased (86% of historical periods). A regime shift in funding would degrade this leg.
4. **$500 canary capital.** At $500, futures fees and minimum order sizes may eat into returns more than modeled. The backtest assumes $10,000 capital; slippage at $500 may differ.
5. **Backtest period.** ~376 days. Does not include a full multi-year regime cycle (e.g., 2021 bull → 2022 bear transition).

## Pipeline Status

| Step | Status |
|------|--------|
| Backtest | ✅ PASS (103.3% ann, Sharpe 2.78) |
| Stress Test | ✅ GREEN (5/7 splits, MC 83%, slippage 2.62) |
| Kelly Position Sizing | ✅ COMPLETE — Quarter-Kelly capped at f=1.0 ($500 at base leverage) |
| Risk Review (Gordon) | ⏳ PENDING — escalate next |
| QA Review (Quinn) | ⏳ PENDING |
| Eleanor Final Review | ⏳ PENDING — REQUIRED before Boss can approve |
| **Boss Approval** | **⏳ BLOCKED on Eleanor** — cannot approve until full review trail complete |

## Recommended Next Steps

1. Complete Kelly sizing → determine conservative position size for canary
2. Gordon risk review: liquidation cascade modeling, correlation stress during flash crashes
3. Quinn QA: code review of strategy implementation, edge-case testing
4. Eleanor final review: compile deployment package
5. **Boss decision: authorize 3x leverage + futures for canary deployment at $500?**

## Research Artifacts

- Stress test: `docs/research/portfolio-stress-analysis.md`
- Optimizer: `docs/research/portfolio-optimizer-analysis.md`
- Portfolio DD trend: `docs/research/portfolio-dd-trend-analysis.md`
- Funding directional: `docs/research/funding-directional-analysis.md`
- Scripts: `scripts/research_portfolio_stress.py`, `scripts/research_portfolio_optimizer.py`

---

*This is a research candidate flag. No live deployment without Boss approval. Capital preservation is the first priority.*
