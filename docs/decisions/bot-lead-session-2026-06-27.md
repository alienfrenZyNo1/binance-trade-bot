# Bot-Lead Session Log — 2026-06-27 (cron check-in)

## Bot Health
- **Process:** UP (PID 3537111, `python -m binance_trade_bot`), running since Jun 26.
- **Docker:** Coolify container `ig7sexqj6pnpnbtkn18odyfn` — cannot query socket from this shell (permission denied), but process is live.
- **Holding:** INJ (spot), since 2026-06-26 02:40 UTC. `awaiting_reentry = False`.
- **Regime:** BULL. No futures positions open.
- **Balance:** ~$52.08 USDC equivalent (last trade crypto_trade_amount). Realized drawdown since inception ~15% ($61.46 → $52.08), all accrued while breaker was disabled.

## 🚨 ESCALATION TO THE BOSS — Rejected code is live on master

### What happened
Two independent veto authorities completed audits this session:
- **risk-agent (#98)** → verdict **NEEDS_FIX** (`docs/audits/risk-audit.md`)
- **final-reviewer (#101)** → verdict **REQUEST_CHANGES / DEPLOY BLOCK** (`docs/audits/session-review.md`)

Both identified **blocking live-trading safety defects**. **However, the code carrying these defects was already merged to `master` and auto-deployed** via Coolify (commits `4608248`, `84bdc5e`). The most recent push (`ab980f4`, docs-only, 11:57 UTC today) re-triggered a deploy of the same defective binary. The defects are therefore **live in production right now**.

### The three blocking defects (all live)

| ID | Defect | Source | Live impact |
|----|--------|--------|-------------|
| **BLOCKER A** | `-2010` (NEW_ORDER_REJECTED, a catch-all incl. insufficient balance) is misclassified as "duplicate order already placed" in `_is_duplicate_order_error()` and the futures `-2010` handler | final-reviewer #101 | A buy/sell/short that fails for **insufficient balance is reported as success**. Futures variant returns `'opened'` with no exchange verification and no stop placed → inconsistent state, possible stacked shorts. |
| **BLOCKER B** | Only 1 of 6 `futures_create_order` call sites has `newClientOrderId`. `_close_position` (exit) and `_place_server_stops` (protective stop) are unprotected | final-reviewer #101 | A timeout+retry on the position-exit or stop-placement path can place a **duplicate order** — the exact money-loss scenario the idempotency work was meant to prevent. |
| **F2** | Circuit-breaker equity baselines are seeded lazily (only on first new entry) and were never seeded in the live DB; breaker also silently fails-open if equity estimate is `None` | risk-agent #98 | **Breaker is dormant right now.** Confirmed by direct DB read: no `portfolio_daily_start_equity` / `weekly` / `last_triggered` keys exist. A drawdown spiral would not be halted until the next entry attempt self-seeds. |

### Current exposure
- **Latent, not actively bleeding.** Bot holds a spot position in BULL regime; no order is in flight. The `-2010` and idempotency bugs trigger only on the next order-placement/retry cycle. The dormant breaker matters only if drawdown deepens.
- **But the safety net has known holes on live capital.** The next rotation, re-entry, or regime change to BEAR (which would exercise the unprotected futures paths) would expose the defects.

### Bot-lead assessment & recommendation to The Boss
1. **Do NOT place new trades until BLOCKER A, BLOCKER B, and F2 are fixed and re-reviewed.** The lowest-disruption way to hold this is to set `awaiting_reentry`/pause new entries — but that changes live trading behavior and **requires Boss approval** per my constraints. I am flagging this for a decision rather than acting unilaterally.
2. **Fixes are being dispatched now** to strategy-developer (BLOCKER A + B + F2) on a feature branch — they will go through final-reviewer and risk-agent again before any merge. No merge to master without Boss sign-off given the live-safety context.
3. **No issue will be closed** until the fixes land and pass re-review.

### Decision requested from The Boss
- [ ] Authorize a **temporary halt on new entries** (not a liquidation of the existing INJ spot position) until the three blockers are fixed and re-reviewed? (Recommended: YES)
- [ ] Or accept the latent risk and proceed with fixes on the normal pipeline?

---

## Actions taken this run
- Dispatched **strategy-developer** to fix BLOCKER A + BLOCKER B + F2 on a branch (delegate_task).
- Recorded this escalation. No live trading behavior changed, no risk parameters touched, no issues closed.

## Open issues at run end
- #102 [development] Regime v2 multi-signal detector (candidate module)
- #101 [final-review] → audit DONE, verdict REQUEST_CHANGES; **fix work dispatched, not closeable yet**
- #100 [docs] Document session
- #98 [risk] → audit DONE, verdict NEEDS_FIX; **fix work dispatched, not closeable yet**
- #97 [research] Identify genuine edge sources
- #72 Regime v2 evidence-gated activation model
