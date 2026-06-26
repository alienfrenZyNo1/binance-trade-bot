# Bot-Lead Decision Log — Session 2026-06-26

## Session Summary

**Duration:** Single session
**Authority:** Bot-Lead under SD-001 and risk-appetite.yaml

---

## Decisions Made

### D-001: Snyk Branch Triage — REJECT ALL
**Decision:** Do NOT merge any of the 82 snyk-fix-* branches.
**Rationale:** All branches downgrade critical dependencies (python-binance 1.0.37→1.0.12, Flask 2.3→2.1, etc.) which would BREAK the live bot. Snyk's automated fixer chose older "safe" versions rather than latest patched versions.
**Impact:** Prevented a potential outage. Issue #93 updated with detailed analysis.

### D-002: Backtest Audit Integration — ACCEPT
**Decision:** Accept the backtest audit verdict ("edge NOT trustworthy") as the baseline for strategy work.
**Rationale:** Audit found same-bar execution, inverted train/OOS, survivorship bias, and 55x live/backtest frequency mismatch. The +79% claim is not reliable.
**Impact:** All strategy decisions will use corrected backtest methodology. Issue #92 assigned to backtest-agent.

### D-003: Execution Audit Integration — ACCEPT
**Decision:** Accept the execution audit as the baseline for execution-layer work.
**Rationale:** Audit found 2 HIGH (order idempotency) and 9 MEDIUM issues. Overall risk: MEDIUM at $62 scale.
**Impact:** Created issues #95 (order IDs) and #96 (retry backoff) for execution-agent.

### D-004: Confirmation Timing — ESCALATE
**Decision:** Escalate the confirmation timing fix to The Boss (not auto-approveable — changes live behavior).
**Rationale:** Fix reduces trade frequency from 14/day to ~0.5/day. Critical safety improvement but technically a live execution change.
**Impact:** Created issue #94, documented in escalation.

### D-005: Circuit Breaker Thresholds — ESCALATE
**Decision:** Escalate threshold tightening (5%→3% daily, 12%→8% weekly) to The Boss.
**Rationale:** Risk-appetite.yaml mandates 3%/8% but live config has 5%/12%. Changing risk limits requires Boss approval per the approval envelope.
**Impact:** Documented in escalation.

---

## Documents Created

| Document | Purpose | Path |
|----------|---------|------|
| 30-Day Roadmap | Execution plan | `docs/roadmap-30day.md` |
| Recovery Runbook | Emergency procedures | `docs/runbook.md` |
| Promotion Pipeline | Strategy change governance | `docs/promotion-pipeline.md` |
| Boss Escalation | 3 pending approval requests | `docs/escalations/boss-approval-2026-06-26.md` |
| Backtest Audit | Strategy validation findings | `docs/audits/backtest-audit.md` |
| Execution Audit | Execution layer findings | `docs/audits/execution-audit.md` |

## GitHub Issues Updated/Created

| Issue | Action | Assignee | Status |
|-------|--------|----------|--------|
| #88 | Updated with config drift analysis | risk-agent | Blocked on Boss approval |
| #91 | Updated — service file exists | devops-monitoring | Ready to execute |
| #92 | Updated with audit findings | backtest-agent | Ready to execute |
| #93 | CRITICAL: all branches dangerous | bot-lead + qa-agent | Blocked (won't fix) |
| #94 | NEW: Confirmation timing fix | execution-agent | Blocked on Boss approval |
| #95 | NEW: Order ID idempotency | execution-agent | Ready to execute |
| #96 | NEW: Retry backoff | execution-agent | Ready to execute |

## Open Items for Next Session

1. **Await Boss approval** for escalations 1-3
2. **Begin backtest revalidation** (#92) — auto-approved, no blockers
3. **Begin execution hardening** (#95, #96) — auto-approved, no blockers
4. **Clean up Snyk branches** — delete all 82, run pip-audit instead
5. **Install systemd service** (#91) — auto-approved, needs brief downtime coordination
