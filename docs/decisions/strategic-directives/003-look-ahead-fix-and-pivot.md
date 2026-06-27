# SD-003: Fix Look-Ahead Bias + Research Pivot

**Date:** 2026-06-27 20:55 UTC
**Authority:** The Boss (Human Approval Authority)
**Status:** ACTIVE — supersedes any implied direction from the GREEN flag

---

## Directive 1 — Fix the DOT Funding Look-Ahead (Marcus + Vera)

`scripts/research_portfolio_dd_trend.py::simulate_funding_leg` has a 0-bar look-ahead: it decides `pos[t]` from `funding_rate[t]` (knowable only at bar `t`'s close) and trades bar `t`'s own close-open move. This inflates the DOT leg's full-sample Sharpe from 0.78 (honest) to 2.41 (biased).

**Action:** Shift the funding signal by +1 bar so the position decided from bar `t`'s funding data is executed from bar `t+1`. This is how a live bot actually works. Re-run the full portfolio stress suite after the fix.

**Why it matters:** With the fix, the tangency optimizer assigns DOT 0% weight. The 191.1% OOS return collapses to 55%. The candidate was partly an artifact.

## Directive 2 — Re-derive All OOS Numbers With Train-Only Weights (Vera)

The Kelly/escalation memo's "105%/2.42/-10.7% OOS" uses full-sample-optimized weights applied to the test half — a data-snooping leak. Every OOS number that goes into a deployment package must use weights optimized strictly on the train half and frozen for the test half.

## Directive 3 — Research Pivot: LINK+ETH Trend Legs + Drawdown-Controlled Grid (Maya)

Stop refining the 4-leg portfolio as-specified. The honest, causal edges from this cycle are:

1. **LINK + ETH trend legs (Donchian/Supertrend, causal).** Individually Sharpe ~1.7-2.2 OOS. These are the trustworthy core. Deep-test as a 2-leg portfolio with train-only weights and rolling-window validation. Require positive Sharpe across ≥4/6 rolling windows before flagging.

2. **Drawdown-controlled grid (combo_vt60, 3%/20/2x on INJ).** Sharpe 1.43, MaxDD -14.8%, ~45% annualized, robust across all walk-forward splits. The most honest config found. Re-test in a sideways/bull regime when one appears; in the current bear regime it's the best available but misses the 50% bar by a hair.

3. **Do NOT bring forward any candidate that:**
   - Has a look-ahead (0-bar signal-to-execution)
   - Cites OOS numbers from full-sample-fit weights
   - Fails rolling-window validation (≥3/6 negative windows)

## Directive 4 — Rolling-Window Validation Is Now Mandatory (Vera)

No candidate reaches Boss review without passing rolling expanding-window validation. A strategy that wins on multi-split but loses in 4/6 consecutive periods is regime-overfit and will not be approved. The rolling-window table must be in every stress report.

## Live Operational Fix — Bug #110 (Marcus)

Fix `initialize_current_coin()` in `momentum_strategy.py` to query the actual Binance balance before falling back to random/config choice. Add startup reconciliation logging. This must land before any strategy goes live. Currently the bot reports TIA but holds INJ — if a momentum signal fires, the wrong coin could be traded.

---

*The independent validation saved real money this cycle. Skeptical review is not optional — it is the gate.*
