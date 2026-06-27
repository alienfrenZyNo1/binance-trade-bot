# CURRENT_STATE.md — Live Bot Status (living doc)

> **Last updated:** 2026-06-27 (Session 003 / docs-journal-agent, issue #100)
> **Rule of thumb:** this file describes *what is actually running in production*.
> Anything that is only on a branch/PR is marked ❌ **not live**. When in doubt,
> trust `git` + the deployed container image, not a commit message.

---

## 1. What the live bot runs RIGHT NOW

| Field | Value |
|---|---|
| **Git ref** | `master` @ **`ab980f4`** (Coolify auto-deploys from master) |
| **Container** | Coolify-managed Docker image, built **2026-06-26 20:17 UTC** |
| **Trading mode** | 🐤 **Canary / paper-scale** — `canary_mode_enabled = yes` |
| **Spot cap** | $75 (`canary_max_spot_trade_usdc`) |
| **Futures caps** | $50 absolute / 15% margin pct (`canary_max_futures_margin_usdc` / `canary_futures_max_margin_pct`) |
| **Regime** | **BULL** (ADX ≈ 30–32; SOL/USDC 1h classifier) |
| **Holding** | Single spot position: **INJ** (since 2026-06-26 02:40 UTC), 15% trailing stop active |
| **Futures** | None (BULL → futures path inactive; no open short) |
| **Capital** | ~$52 in INJ, ~$0.28 USDC futures dust |
| **New entries** | None — momentum filter currently blocks all candidates (all negative 18h perf) |

### ⚠️ Active safety gaps in the *live* (master `ab980f4`) build
1. **Circuit breaker is DORMANT** (`#98` F2/F3). Enabled in config but its equity baselines were never seeded in the live DB → it fails open on every cycle. **Treat the breaker as OFF until the fix is deployed.** The only active capital guard is the canary cap + trailing stop.
2. **No order idempotency.** Spot and futures order paths lack `newClientOrderId` and the `-2010` duplicate check misclassified insufficient-balance as a duplicate success (`#101` BLOCKER A/B).
3. **3-second confirmation.** `SCOUT_SLEEP_TIME=1` + time gate off → live confirms rotation signals in ~3s vs the backtest's ~3h (the 55× trade-frequency root cause).
4. **Silent fail-open.** If equity is unavailable, the breaker logs a `notification=False` warning — no operator alert.

These are all **fixed on the branch `fix/deploy-blockers-98-101`** but that branch is **not merged and not deployed**.

---

## 2. Pending review on branch `fix/deploy-blockers-98-101`

The branch is **3 commits ahead of master**, pushed to origin, but **NOT merged / NOT deployed**:

| Commit | What | Issue | Veto review |
|---|---|---|---|
| `6d69f6c` | fix: 3 defects (BLOCKER A `-2010` misclass, BLOCKER B futures idempotency, breaker 5→3% / 12→8%) | #98 / #101 | ⏳ pending |
| `aac91e4` | fix: escalate circuit-breaker fail-open to visible Telegram alert | #98 F3 | ⏳ pending |
| `dbe3bd6` | feat: **regime v2 candidate multi-signal detector** (NON-LIVE research module, 26 tests pass) | #102 | n/a (non-live) |

### Two independent veto reviews must clear before merge/deploy
- **`#98` — risk-agent:** must re-review and sign off **after** the breaker fix is merged + deployed and baselines are confirmed seeded in `bot_state` (`portfolio_daily_start_equity` present). Risk-agent did **not** halt (latent gap, not active loss); **condition:** do not approve BEAR regime (opens futures shorts) until F2/F3 are deployed.
- **`#101` — final-reviewer:** Round-2 review **APPROVED** the code on the branch (BLOCKER A/B verified fixed by execution + AST test). Not yet merged to master / not deployed; deployed state not signed off. Non-blocking follow-ups tracked (F1 entry-duplicate recovery; `flask-socketio`/Werkzeug-3 web-UI breakage — trading core unaffected).

### Unblocking action
1. Merge `fix/deploy-blockers-98-101` → `master`.
2. Let Coolify auto-redeploy (container rebuild).
3. Confirm `portfolio_daily_start_equity` appears in `bot_state` → breaker goes live.
4. Re-run risk-agent `#98` re-review against the deployed image.

---

## 3. Open issues (as of this update)

| # | Title | Status |
|---|---|---|
| **#72** | Regime v2 promotion pipeline | 🟡 **Open** — shadowing; **not promotion-ready** (route robustness fails all windows; 90d DD > 18% gate). The new `regime_v2_signals.py` candidate (#102) feeds this but is non-live. |
| **#97** | Identify genuine edge sources | ✅ Research complete — doc `research/strategy-hypotheses-2026-06.md`. Verdict: **defensive timing is the edge, not coin selection.** |
| **#98** | [risk] Verify circuit breaker fires in prod | ⏳ **Pending** — audit done (dormant, F2/F3); fix on branch; awaiting risk-agent re-review post-deploy. |
| **#99** | Stress-test session changes | ✅ Closed — 74 new tests added. |
| **#101** | Session code review | ⏳ **Pending** — round-2 APPROVE on branch; not merged/deployed. |
| **#102** | Regime v2 candidate multi-signal detector | ✅ Built (non-live, 26 tests) — feeds #72. |
| **#92** | Backtest revalidation | ✅ Report complete — original +79%/Sharpe 3.85 = **artifact**; corrected +36.5%/Sharpe 1.12/**62% DD** = unproven, upper bound. |

---

## 4. Strategy / backtest status (do not cite the old numbers)

- **Original claim (+79%, Sharpe 3.85): ARTIFACT.** Corrected to **+36.5% / Sharpe ~1.1 / ~62% max DD** — probably real but small, risky, and under-sampled (5 OOS windows). **Not deployment-grade.**
- **Real edge = defensive timing** (trailing stop + regime→USDC + anti-churn), not coin selection (random rotation = −63.7%; optimization helped OOS in only 1/3 windows).
- **Binding constraint = 62% max drawdown.** Do not scale capital on current evidence.
- See `docs/audits/backtest-audit.md`, `docs/audits/backtest-revalidation-report.md`, `research/strategy-hypotheses-2026-06.md`.

---

## 5. How to verify any of this yourself

```bash
git fetch origin
git log --oneline master -1                 # deployed ref → expect ab980f4
git log --oneline origin/fix/deploy-blockers-98-101 -3   # pending fixes
# After redeploy, in the live container / on the host:
sqlite3 /data/binance-bot-data/crypto_trading.db \
  "SELECT key,value FROM bot_state WHERE key LIKE 'portfolio_%equity%' OR key LIKE 'portfolio_%period%';"
# Non-empty + positive values = breaker baselines seeded = breaker is LIVE.
```
