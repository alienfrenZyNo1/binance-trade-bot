# Strategic Directive #001 — Initial Project Mobilization
**Issued by:** The Boss (Human Approval Authority)
**Date:** 2026-06-26
**Priority:** MAXIMUM
**Status:** ACTIVE

---

## SITUATION

The Binance trading bot is LIVE with ~$62 capital, trading spot momentum rotation and USDC-M futures shorts. The initial audit (CURRENT_STATE.md) has identified **critical safety gaps** that must be addressed before any strategy optimization.

## TOP 3 CRITICAL FINDINGS

1. **No daily loss limit is active** — circuit breaker code exists but is disabled
2. **Database is empty** — state persistence may be broken
3. **Test suite is broken** — cannot verify any code changes

## DIRECTIVE

### PHASE 0 — STABILIZE (Immediate, 0-24h)

**No strategy optimization work is authorized.** All effort goes to safety:

| Issue | Assigned To | Action |
|-------|-------------|--------|
| #88: Enable circuit breaker | risk-agent | Enable with 3% daily / 8% weekly limits |
| #89: Fix empty database | devops-monitoring + execution-agent | Diagnose and fix state persistence |
| #90: Fix test suite | qa-agent | Install deps, get tests passing |
| #91: Root user + logging | devops-monitoring | Create systemd service, add logging |

**Authority:** All Phase 0 work is LOW RISK (fixes that reduce risk). Auto-approved after QA.

### PHASE 1 — VALIDATE (24-72h)

| Issue | Assigned To | Action |
|-------|-------------|--------|
| #92: Backtest revalidation | backtest-agent | Walk-forward with fees/slippage/funding |
| Live trade frequency analysis | execution-agent | Root cause 18 trades/30h vs 0.25/day predicted |

**Authority:** Research and backtest only. No live changes.

### PHASE 2 — IMPROVE (Week 2+)

Only after Phase 0 and Phase 1 are complete:
- Strategy improvements (BTC confirmation, ADX filters, position sizing)
- Monitoring enhancements (alerts, dashboards)
- Documentation updates

### RULES OF ENGAGEMENT

1. **All agents work via GitHub issues and PRs** — no direct commits to master
2. **Branch naming:** `agent-name/issue-NNN-short-description`
3. **Every PR must state:** purpose, changed files, tests run, live-risk level, rollback plan
4. **HIGH RISK PRs** require risk-agent + final-reviewer approval
5. **No agent may change live trading behaviour** until Phase 0 is complete
6. **The Boss reserves the right to halt all trading** at any time

### ESCALATION RULES

Escalate to The Boss immediately if:
- Any loss exceeds 5% of capital in a single day
- API errors, order rejections, or unexpected exchange behaviour
- Risk controls would need to be weakened
- Any agent wants to enable new live features
- Futures, leverage, or margin changes are proposed
- final-reviewer and risk-agent disagree

---

## DECISION LOG

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-06-26 | Assume command as Human Approval Authority | Initial project mobilization |
| 2026-06-26 | Freeze all strategy optimization | Safety gaps must be fixed first |
| 2026-06-26 | Issue #88-93 created on GitHub | Track all critical work items |
| 2026-06-26 | Risk appetite set: 3% daily / 8% weekly / 10% total halt | Capital preservation priority |
| 2026-06-26 | Futures shorting grandfathered (already active) | Cannot disable without market impact; will monitor |

---

*This directive supersedes all prior plans and instructions until Phase 0 is complete.*
