# HUMAN APPROVAL REQUESTED — Deploy Circuit-Breaker Fix (#98/#101)

**Status:** ⏳ PENDING BOSS DECISION
**Requested by:** bot-lead (escalation from risk-agent #98 + final-reviewer #101)
**Timestamp:** 2026-06-27 (auto check-in)
**Priority:** 🔴 HIGH — active latent safety gap

---

## What is requested
Authorization to **push local master → origin/master**, which triggers Coolify
auto-deploy of the circuit-breaker arming fix. This changes live trading behavior
(arms a previously-dormant safety guard), so it requires Boss sign-off per
bot-lead operating constraints.

## Why it is needed (the gap)
The running container started **2026-06-26 23:32 UTC**. The circuit-breaker fix
commits are dated **2026-06-27 12:09–12:11 UTC** and sit **4 commits ahead of
`origin/master`** — they were never pushed, so Coolify never redeployed.

Consequence (from risk-audit F2/F3): the production DB has **no seeded equity
baselines**, so `portfolio_daily_start_equity` / `portfolio_weekly_start_equity`
are `None`, and the circuit breaker **cannot fire** in production today. It is
effectively OFF despite config reading 3% daily / 8% weekly.

The bot is not actively bleeding (single spot position, 15% trailing stop, inside
canary cap, momentum filter blocking new entries, no futures). The gap is
**latent**, not an active loss path — but a regime turn to BEAR (which would open
futures shorts) should NOT happen with the breaker dormant.

## Review trail (both independent authorities have signed off)
1. **risk-agent (#98):** verdict **CLEARED** — F2 (eager baseline seeding) and
   F3 (visible fail-open Telegram alert) both resolved; thresholds unchanged;
   verified by independent test run (16/16). *Conditional note:* breaker treated
   as off until deploy + baseline-seed confirmation.
2. **final-reviewer (#101):** verdict **APPROVE (Round 2)** — all 6 futures
   order-id sites AST-tested; idempotency + retry verified (52 in-scope tests);
   full suite 444 passed. "The code is safe to deploy for live trading."
3. **Tests:** local run of the 4 safety suites → **36/36 PASS**.

## What the deploy does (and does NOT do)
- ✅ **Adds:** eager equity-baseline seeding at startup (`initialize()`); visible
  high-severity Telegram alert when breaker is blind to equity data.
- ✅ **Does NOT change:** any threshold (still 3% daily / 8% weekly), any
  stop-loss, any kill switch, any canary cap, any API permission, any strategy
  logic. Purely additive safety instrumentation.
- ✅ **Fail-open retained** by design (blocking exits on missing data is worse);
  now it *shouts* instead of staying silent.

## Decision
- [ ] **APPROVED** — push to origin/master, let Coolify redeploy, then confirm
      `portfolio_daily_start_equity` is non-null in `bot_state` and breaker armed.
- [ ] **REJECTED** — keep dormant; (please specify mitigation).
- [ ] **DEFERRED** — need more info: __________

## Risk parameters (confirmed unchanged by this deploy)
| Parameter | Value | Touched? |
|---|---|---|
| Daily max drawdown | 3.0% | No |
| Weekly max drawdown | 8.0% | No |
| Spot canary cap | $75 | No |
| Futures canary margin | $50 / 15% | No |
| Futures/leverage | OFF | No |

## Post-deploy verification checklist (for execution-agent / bot-lead)
1. Confirm Coolify rebuilt container from new master SHA.
2. Confirm `portfolio_daily_start_equity` populated in DB shortly after boot.
3. Confirm a "breaker blind" Telegram alert does NOT fire under normal equity
   availability (only fires when equity API genuinely unavailable).
4. Leave regime at SIDEWAYS/BULL — do NOT approve BEAR (futures) until verified.
