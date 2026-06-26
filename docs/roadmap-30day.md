# 30-Day Roadmap — Binance Trading Bot

**Author:** Bot-Lead (AI Coordinator)
**Date:** 2026-06-26
**Status:** ACTIVE
**Authority:** Operating under SD-001 and risk-appetite.yaml envelope

---

## Executive Summary

The bot is LIVE with ~$62 USDC, running momentum rotation + futures shorts. The Boss completed an initial audit and fixed the test suite (289/289 passing) and verified live config. Three audit reports are complete (backtest, execution). This roadmap converts audit findings into a prioritized 30-day execution plan.

**Core principle:** Safety first, validate the edge, then improve. No live strategy changes without full promotion pipeline + Boss approval.

---

## Phase 0: Operational Safety (Days 1-5) — IN PROGRESS

### 0.1 Config Drift Fix (#88) — `risk-agent` + `bot-lead`
**Status:** Config drift identified. Circuit breaker IS enabled in live config (5%/12%) but risk-appetite.yaml mandates 3%/8%.
- [x] Circuit breaker code exists and is verified working
- [x] Live config has breaker enabled (5% daily / 12% weekly)
- [ ] **ESCALATE TO BOSS:** Tighten thresholds from 5%/12% → 3%/8% per risk-appetite.yaml (requires Boss approval — changing risk limits)
- [ ] Sync git `user.cfg` to match live config (document live values, add `# LIVE` comments)
- [ ] Add test confirming breaker blocks new entries when triggered
- [ ] Add Telegram alert when breaker activates
- **Branch:** `risk-agent/issue-88-config-drift`
- **Blocked on:** Boss approval for threshold change

### 0.2 Deployment Hardening (#91) — `devops-monitoring`
**Status:** systemd service file EXISTS (`binance-trader.service`) but NOT installed. Bot runs as root.
- [x] Service file drafted (with security hardening, non-root user)
- [ ] Install and enable systemd service (replace root process)
- [ ] Migrate bot from root PID 3032124 → `lunafox` user via systemd
- [ ] Set up persistent file logging (rotating file handler in `logs/`)
- [ ] Create recovery runbook (`docs/runbook.md`)
- [ ] Verify data directory permissions
- **Branch:** `devops/issue-91-systemd-logging`
- **Note:** Telegram sidecar already runs as systemd (`telegram-bot.service`)

### 0.3 Confirmation Timing Fix (from backtest audit) — `execution-agent`
**Status:** Critical finding — live bot confirms signals in 3 seconds vs 3 hours in backtest (55x trade frequency mismatch).
- [ ] Enable `confirmation_time_enabled=yes` in live config
- [ ] Set `sideways_confirmation_min_seconds=3600` and `bull_confirmation_min_seconds=3600`
- [ ] Increase `scout_sleep_time` from 1 → 60 seconds
- **⚠️ ESCALATE TO BOSS:** This changes live trading behavior (trade frequency). Requires approval.
- **Branch:** `execution-agent/fix-confirmation-timing`

---

## Phase 1: Strategy Validation (Days 3-10)

### 1.1 Backtest Revalidation (#92) — `backtest-agent`
**Status:** Audit complete. Verdict: edge NOT trustworthy with current methodology.
- [ ] Implement next-bar-open execution (not same-bar-close)
- [ ] Add realistic slippage (0.15% per side for altcoin/USDC)
- [ ] Run multi-window walk-forward (5+ non-overlapping 60-day OOS windows)
- [ ] Add benchmarks: TIA hold, SOL hold, equal-weight basket, random rotation
- [ ] Compare live trade log vs backtest predictions (root cause divergence)
- [ ] Parameter sensitivity analysis
- [ ] Produce honest assessment: is the edge real or artifact?
- **Branch:** `backtest-agent/issue-92-revalidation`
- **Authority:** Research only — auto-approved

### 1.2 Execution Layer Fixes (from execution audit) — `execution-agent`
**Status:** Audit complete. Overall risk: MEDIUM. Two HIGH issues.
- [ ] **P0:** Add client order IDs to all spot + futures orders (idempotency) — issues A1, C1
- [ ] **P0:** Add exponential backoff to `retry()` + error classification — issues A2, A3
- [ ] **P1:** Add timeout to repriced order polling (A6)
- [ ] **P1:** Add max-attempts to `_fetch_pending_orders()` (B1)
- [ ] **P1:** Verify position flat in kill switch (E1)
- [ ] **P1:** Add `/pause` Telegram command for persistent futures disable (E3)
- **Branch:** `execution-agent/execution-hardening`

---

## Phase 2: Security & Infrastructure (Days 5-15)

### 2.1 Snyk Security Merges (#93) — `bot-lead` + `qa-agent`
**Status:** 82 unmerged snyk-fix-* branches (more than initially counted).
- [ ] Triage all 82 branches by severity (critical/high/medium/low)
- [ ] Batch-merge critical/high fixes with test runs
- [ ] Delete stale/duplicate branches
- [ ] Verify Snyk integration runs on push
- **Branch:** Merge directly to master via PRs
- **Authority:** Auto-approved (security fixes that reduce risk)

### 2.2 Monitoring & Alerting — `devops-monitoring`
- [ ] Set up log-based alerts: API failures, circuit breaker triggers, kill switch activations
- [ ] Add health check endpoint or heartbeat
- [ ] Create Grafana/dashboards if time permits
- [ ] Set up daily P&L summary via Telegram

---

## Phase 3: Strategy Research (Days 10-25)

### 3.1 Regime v2 Multi-Signal Model (#72) — `strategy-researcher` + `strategy-developer`
**Status:** Research already underway (30+ research branches exist). 45% disagreement with legacy classifier.
- [ ] Complete walk-forward evaluation of Regime v2 vs legacy SOL-only
- [ ] Include switching costs: fees, slippage, funding, missed exposure
- [ ] Run 90d+ smoke test on public data
- [ ] Shadow-mode observation (paper alongside live)
- [ ] **NO live integration** until separate promotion PR with full pipeline
- **Authority:** Research only — auto-approved

### 3.2 Continuous Improvement Pipeline Setup
Establish the formal loop:
```
IDEA → RESEARCHED → IMPLEMENTED → BACKTESTED → STRESS TESTED → QA → RISK REVIEW → FINAL REVIEW → PAPER → SMALL LIVE (Boss) → NORMAL LIVE (Boss)
```
- [ ] Create GitHub issue template for strategy candidates
- [ ] Create promotion checklist document
- [ ] Each candidate gets its own issue tracking pipeline stage

---

## Phase 4: Polish & Documentation (Days 20-30)

### 4.1 Documentation — `docs-journal-agent`
- [ ] Complete `docs/runbook.md` (recovery procedures)
- [ ] Update `docs/developer-guide.md` with current architecture
- [ ] Document the promotion pipeline formally
- [ ] Create decision log for all Phase 0-3 changes

### 4.2 Test Coverage — `qa-agent`
- [ ] Add tests for circuit breaker behavior
- [ ] Add tests for order idempotency
- [ ] Add tests for confirmation timing logic
- [ ] Stress test: simulated flash crash, API outage, partial fills

---

## Issue Priority Matrix

| Issue | Priority | Phase | Assignee | Status | Blocked On |
|-------|----------|-------|----------|--------|------------|
| #88 | P0 | 0 | risk-agent | Config drift identified | Boss approval (thresholds) |
| #91 | P0 | 0 | devops-monitoring | Service file exists, not deployed | — |
| Confirmation timing | P0 | 0 | execution-agent | Identified in audit | Boss approval (live behavior) |
| #92 | P1 | 1 | backtest-agent | Audit complete, implementation pending | — |
| Execution fixes | P1 | 1 | execution-agent | Audit complete | — |
| #93 | P2 | 2 | bot-lead + qa-agent | Not started | After Phase 0 |
| #72 | P2 | 3 | strategy-researcher | Research in progress | After #92 |

---

## Escalation Items for The Boss

1. **CIRCUIT BREAKER THRESHOLDS:** Risk-appetite.yaml mandates 3%/8% but live config has 5%/12%. Requesting approval to tighten. This reduces risk so should be fast-tracked.

2. **CONFIRMATION TIMING FIX:** Enabling `confirmation_time_enabled` will reduce trade frequency from ~14/day to ~0.5/day, aligning with backtest assumptions. This is a live behavior change but makes the bot safer and more aligned with validated parameters.

3. **SYSTEMD MIGRATION:** Migrating from root process to systemd-managed `lunafox` user requires a brief bot restart. Requesting scheduled maintenance window.

---

## Risk Envelope (Enforced)

| Parameter | Value | Source |
|-----------|-------|--------|
| Max daily loss | 3% (target) / 5% (current) | risk-appetite.yaml |
| Max total drawdown | 10% | risk-appetite.yaml |
| Leverage max | 1x | risk-appetite.yaml |
| Futures margin max | 50% | risk-appetite.yaml |
| Kill switch | `/kill confirm` | Always active |
| Canary mode | Enabled ($75 spot / $50 futures) | Live config |

---

## Governance Rules

1. **All changes via PR** — no direct commits to master
2. **Branch naming:** `agent-name/issue-NNN-description`
3. **Every PR must state:** purpose, files changed, tests run, live-risk level, rollback plan
4. **HIGH RISK PRs** need risk-agent + final-reviewer approval
5. **No live strategy changes** without full promotion pipeline + Boss approval
6. **The Boss can halt all trading at any time**

---

*Last updated: 2026-06-26 by Bot-Lead*
*Next review: When Phase 0 items complete or Boss directs*
