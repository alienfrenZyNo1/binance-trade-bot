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

*This document is the source of truth for all strategy changes. No exceptions.*
