# Strategy Promotion Pipeline

**Authority:** Bot-Lead under risk-appetite.yaml
**Rule:** No skipping stages. No live changes without Boss approval.

---

## Pipeline Stages

```
IDEA → RESEARCHED → IMPLEMENTED → BACKTESTED → STRESS TESTED → QA → RISK REVIEW → FINAL REVIEW → PAPER/TESTNET → SMALL LIVE → NORMAL LIVE
                                                                                              ↑ Boss approval    ↑ Boss approval
```

### Stage Definitions

| Stage | Owner | Description | Approval |
|-------|-------|-------------|----------|
| **IDEA** | Anyone | GitHub issue created with hypothesis | Auto (bot-lead) |
| **RESEARCHED** | strategy-researcher | Literature review, data exploration, feasibility | Auto |
| **IMPLEMENTED** | strategy-developer | Code written, unit tests, on a feature branch | Auto |
| **BACKTESTED** | backtest-agent | Walk-forward backtest with fees/slippage/funding, benchmarks | Auto |
| **STRESS TESTED** | backtest-agent | Monte Carlo, regime stress, parameter sensitivity | Auto |
| **QA** | qa-agent | Integration tests, edge cases, breaking attempts | Auto |
| **RISK REVIEW** | risk-agent | Capital preservation review, risk envelope check | Auto |
| **FINAL REVIEW** | final-reviewer | Independent code + risk review | Auto |
| **PAPER/TESTNET** | execution-agent | Deploy to testnet or paper trading | Auto |
| **SMALL LIVE** | The Boss | Canary-mode live deployment | **BOSS APPROVAL REQUIRED** |
| **NORMAL LIVE** | The Boss | Full deployment | **BOSS APPROVAL REQUIRED** |

---

## Acceptance Gates

### Backtest Gate
- [ ] Walk-forward with 5+ non-overlapping OOS windows
- [ ] Slippage ≥ 0.15% per side
- [ ] Taker fees 0.075% per side
- [ ] Funding rates modeled (futures)
- [ ] Next-bar-open execution (no same-bar)
- [ ] Beats all benchmarks (TIA hold, SOL hold, equal-weight, random)
- [ ] Max drawdown < 20%
- [ ] Sharpe > 0.5 (realistic threshold)

### Stress Test Gate
- [ ] Monte Carlo permutation test (1000+ shuffles) — p < 0.05
- [ ] Parameter sensitivity — edge persists with ±20% parameter changes
- [ ] Regime-conditioned performance — positive in target regime
- [ ] Flash crash simulation — no catastrophic loss

### QA Gate
- [ ] All existing tests pass (289+)
- [ ] New tests for strategy logic pass
- [ ] No new linting errors
- [ ] Manual review of order flow

### Risk Review Gate
- [ ] Strategy stays within risk-appetite.yaml envelope
- [ ] No increase in max daily loss exposure
- [ ] No leverage increase
- [ ] Kill switch still works
- [ ] Circuit breaker still works
- [ ] Worst-case loss scenario documented

---

## Issue Template

When creating a new strategy candidate issue:

```markdown
## Strategy Candidate: [Name]

**Hypothesis:** [What edge are we testing?]
**Regime target:** [Bull/Bear/Sideways/All]
**Expected improvement:** [What metric improves?]

### Pipeline Status
- [ ] IDEA
- [ ] RESEARCHED
- [ ] IMPLEMENTED
- [ ] BACKTESTED
- [ ] STRESS TESTED
- [ ] QA
- [ ] RISK REVIEW
- [ ] FINAL REVIEW
- [ ] PAPER/TESTNET
- [ ] SMALL LIVE (Boss approval)
- [ ] NORMAL LIVE (Boss approval)
```

---

## Continuous Improvement Loop

1. **Weekly:** strategy-researcher reviews market conditions, proposes candidates
2. **Weekly:** backtest-agent runs pending backtests, updates leaderboard
3. **Bi-weekly:** risk-agent reviews all promoted candidates
4. **Monthly:** Bot-lead produces pipeline status report
5. **As needed:** The Boss reviews and approves/denies live promotions

---

## Regime v2 Robustness Gate — chosen gate (FIXED, not re-picked per run)

**Status:** RESEARCH-TRACK ACCEPTANCE-CRITERIA DEFINITION ONLY.
**This entry records the chosen robustness gate for the Regime v2 evaluator. It does
NOT promote Regime v2 to live trading.** `momentum_strategy._update_market_regime`
remains the legacy SOL-only ADX/EMA classifier. Live promotion is a separate,
explicit, Boss-approved PR and is blocked on the three known live-safety defects
(see "Live promotion is blocked" below).

**Chosen gate mode: `maxdd-only`** — adopted as the default Regime v2 robustness
gate for the walk-forward evaluator (`scripts/research_regime_v2_evaluator.py`,
`build_route_robustness_gates()`).

**Binding approval:** risk-agent review
[`docs/reviews/risk-agent-gate-review-2026-06-27.md`](reviews/risk-agent-gate-review-2026-06-27.md)
(VERDICT: APPROVE `maxdd-only`, conditional, per §4).

### Rationale

The `maxdd-only` gate holds a drawdown-limited capital-protection strategy to exactly
the safety contract it should be held to:

- **Net-positive over the full span** — the route-level anti-cash backstop
  (`require_positive_total_return`, default **ON**) requires the full compound
  return to exceed `positive_total_return_floor_pct` (default `0.0`).
- **Bounded drawdown in every sub-window** — each contiguous chronological window
  must have `max_drawdown_pct <= max_window_drawdown_pct` (default `15.0`).
  A strategy that survives every segment without a deep hole passes; one that
  draws down hard in any segment fails.

This combination rejects every degenerate strategy the legacy gate's safety relied on:

| Strategy type | maxdd-only verdict | Why |
|---|---|---|
| **Pure cash** (0% every window) | ❌ REJECTED | route return `0%` does not exceed the net-positive floor (backstop) |
| **Net-negative but low maxDD** | ❌ REJECTED | all windows may pass the maxDD floor, but the route-level backstop catches a strategy that loses money overall |
| **High-drawdown `legacy_sol`** | ❌ REJECTED | the high-DD window fails the per-window maxDD floor |

### Why not the alternatives

- **`absolute` (the legacy 3/3 gate) — UNSATISFIABLE BY DESIGN.** It imposes a
  per-window absolute return floor. On crash-straddling segments even pure *cash*
  fails it (a window can have slightly negative return with tiny maxDD). A gate
  that cannot be satisfied by capital preservation on a crash segment is not a
  usable acceptance bar. (`docs/research/regime-v2-scoping-note.md` confirms this.)
- **`relative` — REJECTED AS DEGENERATE.** Without the backstop it passes
  `legacy_sol` at ~45% maxDD (it only asks "beat a crashing benchmark"); even with
  the backstop it conflates "beat a crashing benchmark" with "robust," which is
  exactly the cash-rewarding failure the backstop exists to paper over. Do not use.
- **`segment-aware` — a fully-approvable MORE-CONSERVATIVE ALTERNATIVE** (risk-agent
  §5), but it rejects on a 2/3 selector verdict vs `maxdd-only`'s 3/3 and adds a
  monotone-bleed-tail detector. It is more false-rejection-prone on small 3-window
  samples (one bad tail day can flip the verdict); its extra bleed signal is also
  caught downstream by the selector's own recent-P&L risk-off layer. Not chosen as
  the default for that reason, but remains available if greater end-of-window
  conservatism is later desired.

### Anti-cash backstop — non-default-disable (risk-agent condition 3)

The `require_positive_total_return` backstop **defaults to ON** for `maxdd-only`
(and `relative`). The only disable path is the explicitly-labeled **DIAGNOSTIC ONLY**
CLI flag `--window-gate-no-positive-backstop` (the harness docstring marks it as
diagnostic). There is no silent bypass. This is guarded by:

- `test_gate_modes_never_reward_degenerate_cash` — feeds pure cash through all four
  modes and asserts `passed=False` for each (do not weaken).
- `test_maxdd_only_defaults_require_positive_total_return_true` — asserts the default
  resolves to ON for `maxdd-only`, so the default cannot be silently flipped.

### Live promotion is BLOCKED

Recording this gate does **not** approve live Regime v2. Live promotion remains a
separate, explicit, Boss-approved PR and is blocked until the three known
live-safety defects are fixed (per risk-agent review §6, independent of gate choice):

- **BLOCKER A** — `-2010` API error misclassification in the live regime path.
- **BLOCKER B** — idempotency defect in the live regime write/commit path.
- **F2** — dormant circuit breaker (not yet wired into the live risk-off path).

Until those are resolved, `momentum_strategy.py` stays SOL-only and no live config,
DB, Docker, or order changes are made.

---

*This document is the source of truth for all strategy changes. No exceptions.*
