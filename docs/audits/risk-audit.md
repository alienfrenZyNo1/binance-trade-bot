# Risk Audit #98 — Circuit Breaker Verification (v2)

**Issue:** [#98 [risk] Verify circuit breaker actually fires in production](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/98)
**Auditor:** risk-agent (independent veto authority on safety)
**Date:** 2026-06-27 (re-audit)
**Scope:** Read-only end-to-end trace of the circuit-breaker code path, live DB exposure audit, CROSS-margin risk assessment, and an independent test suite. **No production code, risk parameters, thresholds, or trading behavior were modified.**
**Files added by this audit:** `tests/test_risk_audit_98_breaker_fires.py` (13 tests), this report.

> **Supersedes** the v1 report at this path (2026-06-27 earlier). v1 concluded
> NEEDS_FIX and recommended eager-seeding — but the **local repo already
> contained that fix** on an unmerged branch. This v2 corrects the picture: the
> fix exists locally but **has not been deployed**, so the breaker is still
> dormant in production. The single most important fact in this report is the
> **local-vs-deployed version skew** documented in §3.

---

## VERDICT: ⚠️ NEEDS_FIX — breaker logic is sound, but it is DORMANT in production

The circuit-breaker **logic is correct and correctly integrated** — at >3%
daily drawdown it returns `block_new_risk=True`, every new-entry path consults
it before buying, and exits/stop-losses bypass it by design. The 13-test
independent suite proves all of this.

**However, the breaker is NOT protecting live capital right now.** It is enabled
in config, but its equity baselines have never been seeded in the production DB,
so it cannot compute drawdown and fails open on every cycle. The eager-seeding
fix that would close this gap **exists in the local repo but is NOT in the
deployed container image** — the container was built from `master` before the
fix landed, and the fix branch has not been merged or pushed. Until that fix
ships and the container is redeployed, the breaker is inert.

**Do I recommend halting the bot? No — conditionally.** The bot is in BULL
regime holding a single spot position (INJ), no futures exposure, well inside
the canary spot cap (~$52 balance vs $75 cap), with a 15% trailing stop active.
The realized ~16% drawdown occurred before the breaker was enabled and is
already locked in. The marginal risk of leaving it live for the hours needed to
merge+deploy the fix is low. **The condition is: do not open any NEW entry until
the fix is deployed and baselines are confirmed seeded in the DB.** If a
drawdown-accelerating event is expected first, halt the container.

| Check | Status |
|---|---|
| Breaker logic pure + correct (≥3% daily / ≥8% weekly trips) | ✅ PASS |
| All entry paths check breaker before buy (spot 3/3 + futures) | ✅ PASS |
| Exits / stop-losses bypass breaker (no exit-blocking field; trailing stop never calls it) | ✅ PASS |
| Fail-open on missing equity now escalates to visible Telegram alert | ✅ PASS in local repo / ❌ NOT deployed |
| Baselines seeded eagerly on startup | ✅ PASS in local repo / ❌ NOT deployed |
| **Breaker actually protecting live capital right now** | ❌ **FAIL — dormant (no baselines in DB)** |
| Breaker enabled in live config | ✅ PASS (`portfolio_circuit_breaker_enabled = yes`) |
| Canary caps enforced and active | ✅ PASS (spot $75 + futures $50/15%) |
| CROSS-margin liquidation risk today | ✅ LOW (single position, 1x, server stops) |
| CROSS-margin liquidation risk if multi-position added | ⚠️ HIGH (latent — must be addressed first) |

---

## 1. Code-path trace — the breaker IS checked before every new trade

### 1a. The pure helper (`binance_trade_bot/risk_circuit_breaker.py`)

`evaluate_circuit_breaker(current_equity, daily_start_equity, weekly_start_equity, config)`
returns a frozen `CircuitBreakerResult` (`block_new_risk`, `triggered`, `scope`,
`drawdown_pct`, `threshold_pct`, `reason`). Logic (lines 46–98):

```python
daily_dd  = _drawdown_pct(current_equity, daily_start_equity)
weekly_dd = _drawdown_pct(current_equity, weekly_start_equity)

if daily_dd is None and weekly_dd is None:
    return CircuitBreakerResult(False, False, "none", 0.0, 0.0, "equity baseline unavailable")

if daily_limit > 0 and daily_dd is not None and daily_dd >= daily_limit:
    return CircuitBreakerResult(True, True, "daily", daily_dd, daily_limit, ...)
if weekly_limit > 0 and weekly_dd is not None and weekly_dd >= weekly_limit:
    return CircuitBreakerResult(True, True, "weekly", weekly_dd, weekly_limit, ...)
```

`_drawdown_pct` returns `None` when the start baseline is `None`/`<=0`, which is
the fail-open path. The `>=` comparison means exactly 3.0% trips (verified by
test `test_breaker_blocks_at_exactly_3pct_boundary`).

### 1b. The integration chokepoint — `_new_spot_risk_blocked()`

`strategies/momentum_strategy.py:_new_spot_risk_blocked` (line ~720 locally,
line 657 in the deployed image) is the single gate. It seeds baselines, checks
cooldown, then evaluates. Every new-entry path calls it **before** the buy:

| Entry path | File:line (deployed) | Guard |
|---|---|---|
| Spot rotation | `momentum_strategy.py:885` | `if self._new_spot_risk_blocked(): ... return` |
| Bridge re-entry | `momentum_strategy.py:716` | `if self._new_spot_risk_blocked(): return` |
| Bridge scout | `momentum_strategy.py:946` | `if self._new_spot_risk_blocked(): return` |
| Futures short | `futures_manager.py:368` | `if callable(self.new_risk_blocked) and self.new_risk_blocked(): return 'idle'` |

`self.futures_manager.new_risk_blocked = self._new_spot_risk_blocked` is wired
during `initialize()` (line 141), so the futures path uses the same gate.

### 1c. Exits bypass the breaker BY CONSTRUCTION — proven three ways

1. **No exit-blocking field exists.** `CircuitBreakerResult` has only
   `block_new_risk`; there is no `block_exits`/`block_close` field
   (`test_circuit_breaker_result_has_no_exit_blocking_field`).
2. **The spot trailing stop never calls the breaker.** Structural inspection of
   `_check_trailing_stop` source confirms neither `_new_spot_risk_blocked` nor
   `evaluate_circuit_breaker` appears in it
   (`test_trailing_stop_method_does_not_consult_the_breaker`).
3. **Futures exit management runs BEFORE the gate.** In `manage_bear`,
   `_manage_open_position()` (stop-loss/trailing/funding exits) executes before
   the `new_risk_blocked()` check (`futures_manager.py:358-372`), so a blocked
   new-entry never strands an existing short without protection
   (`test_futures_exit_management_runs_before_breaker_gate`).

A live end-to-end drive of `_check_trailing_stop` with a simulated 16.7%
peak-to-trough drop confirms the stop fires and sells regardless of breaker
state (`test_trailing_stop_fires_regardless_of_breaker_state`).

---

## 2. Equity tracking & seeding — the breaker CANNOT fire in production today

### 2a. The seeding code exists and is correct

`_ensure_circuit_breaker_baselines()` (line 642 locally / 615 deployed) seeds
`portfolio_daily_start_equity` / `portfolio_weekly_start_equity` into `bot_state`
and rolls them over on UTC-day and ISO-week boundaries:

```python
if daily is None or daily <= 0:
    self.db.set_bot_state("portfolio_daily_start_equity", str(equity))
    ...
elif stored_daily_period != daily_period:
    self.db.set_bot_state("portfolio_daily_start_equity", str(equity))  # rollover
```

This is correct. The problem is **when** it runs.

### 2b. LIVE PRODUCTION DB STATE — baselines have NEVER been seeded

Direct read-only inspection of the live DB at `/data/binance-bot-data/crypto_trading.db`
(5.8 MB, actively written, last modified 2026-06-27 12:16 UTC during this audit):

```
portfolio_daily_start_equity            : *** MISSING ***
portfolio_weekly_start_equity           : *** MISSING ***
portfolio_daily_period                  : *** MISSING ***
portfolio_weekly_period                 : *** MISSING ***
portfolio_circuit_breaker_last_triggered: *** MISSING ***
```

The entire live log contains **zero** matches for "circuit", "breaker",
"baseline", "equity", "drawdown", or "cooldown" — the breaker has never logged
a single evaluation. Because both baselines are `None`, `evaluate_circuit_breaker`
hits the fail-open branch (`"equity baseline unavailable"`) and returns
`block_new_risk=False` on every cycle.

**The breaker is enabled but blind. It will not fire until baselines are seeded.**

---

## 3. ⚠️ ROOT CAUSE — the fix exists locally but is NOT deployed (version skew)

This is the central finding and the reason v1 of this report was misleading.

### What's deployed

The live process is `python -m binance_trade_bot` (host PID 3537111, root) inside
a **Coolify-managed Docker container** (`ig7sexqj6pnpnbtkn18odyfn`, image built
**2026-06-26 20:17 UTC**). I inspected the code baked into that running image:

```
$ docker exec <cid> grep -n "baselines seeded eagerly" .../momentum_strategy.py
EAGER SEEDING NOT FOUND IN IMAGE

$ docker exec <cid> grep -n "_alert_circuit_breaker_fail_open\|CIRCUIT BREAKER BLIND" .../momentum_strategy.py
FAIL-OPEN ALERT NOT FOUND IN IMAGE
```

The deployed image's `_new_spot_risk_blocked` is the **master** version:
- **Lazy seeding only** — baselines seed exclusively inside the new-risk gate,
  never on startup. Until the first new entry is attempted after the breaker was
  enabled, the DB has no baselines and the breaker is dormant.
- **Silent fail-open** — when equity is unavailable it logs a `notification=False`
  warning (no Telegram alert).

### What's in the local repo

The local working tree is on branch `fix/deploy-blockers-98-101`, **2 commits
ahead of master**, containing exactly the two fixes the deployed image lacks:

```
master:  ab980f4  (deployed — has v1 audit, NOT the fixes)
HEAD:    aac91e4  fix(#98 F3): escalate circuit breaker fail-open to visible Telegram alert
         6d69f6c  fix(#98,#101): block 3 production-trading defects flagged by reviewers
```

Local `initialize()` (lines 144–169) now seeds baselines **eagerly on startup**
when the breaker is enabled, and `_new_spot_risk_blocked` now routes the
fail-open path through `_alert_circuit_breaker_fail_open()` (rate-limited visible
Telegram alert). My test `test_strategy_seeds_baselines_eagerly_in_initialize`
passes against local source and documents this skew.

**But `git branch -r --contains HEAD` returns nothing — the fix branch is not
pushed to origin, so it cannot have been deployed.** The auto-deploy pipeline
builds from `master`.

### Why the gap exists

The breaker was enabled in the live config (`/data/binance-bot-data/config/user.cfg`)
at ~2026-06-26 23:10 UTC. The last trade was 2026-06-26 02:40 UTC — **before** the
breaker was enabled. Since then the bot has been in BULL regime holding INJ, with
no coin passing the momentum buy guard (all currently negative — see log), so no
new entry has been attempted and the lazy-seeding path has never executed.

### Consequence

Until the fix branch is merged to master and the container is redeployed:
- The breaker cannot fire (no baselines → fail-open).
- If `_estimate_spot_equity()` hiccups (`None`), the failure is **silent** in
  production (no operator alert).
- The only thing keeping the bot safe is the canary cap, the trailing stop, and
  the momentum filter currently blocking all entries.

### Recommendation (read-only; not applied)

**Merge `fix/deploy-blockers-98-101` to master and let Coolify auto-redeploy.**
After redeploy, on the next container start `initialize()` will eagerly seed
baselines and the breaker will go live. Verify by re-reading `bot_state` for the
`portfolio_daily_start_equity` key. This is a low-risk, high-value change and is
the unblocking action for issue #98.

---

## 4. Test evidence — independent suite (13/13 PASS)

New file: `tests/test_risk_audit_98_breaker_fires.py`. Written independently of
the two pre-existing suites; re-derives every assertion from first principles.

```
tests/test_risk_audit_98_breaker_fires.py::test_breaker_blocks_at_daily_drawdown_above_3pct PASSED
tests/test_risk_audit_98_breaker_fires.py::test_breaker_blocks_at_exactly_3pct_boundary PASSED
tests/test_risk_audit_98_breaker_fires.py::test_breaker_allows_just_below_3pct PASSED
tests/test_risk_audit_98_breaker_fires.py::test_breaker_allows_when_no_drawdown PASSED
tests/test_risk_audit_98_breaker_fires.py::test_weekly_8pct_trips_even_if_daily_within_limits PASSED
tests/test_risk_audit_98_breaker_fires.py::test_circuit_breaker_result_has_no_exit_blocking_field PASSED
tests/test_risk_audit_98_breaker_fires.py::test_trailing_stop_method_does_not_consult_the_breaker PASSED
tests/test_risk_audit_98_breaker_fires.py::test_futures_exit_management_runs_before_breaker_gate PASSED
tests/test_risk_audit_98_breaker_fires.py::test_trailing_stop_fires_regardless_of_breaker_state PASSED
tests/test_risk_audit_98_breaker_fires.py::test_evaluate_returns_failopen_when_no_baseline PASSED
tests/test_risk_audit_98_breaker_fires.py::test_strategy_seeds_baselines_eagerly_in_initialize PASSED
tests/test_risk_audit_98_breaker_fires.py::test_cooldown_blocks_for_full_24h_then_releases PASSED
tests/test_risk_audit_98_breaker_fires.py::test_status_summary_is_human_readable_in_all_states PASSED
=== 13 passed ===
```

The suite proves the required invariants:
- **>3% daily drawdown blocks new entries** (the headline requirement) —
  behavioral test against the real pure helper, plus the exact-boundary and
  just-below cases.
- **Exits/stop-losses still execute** — three independent proofs: (a) no
  exit-blocking field can exist on the result, (b) the trailing-stop source
  never references the breaker, (c) futures `_manage_open_position` precedes the
  gate, and (d) a live drive of `_check_trailing_stop` sells during a simulated
  16.7% drop regardless of breaker state.
- **The fail-open-on-missing-baseline path** is the exact state of production
  today (`test_evaluate_returns_failopen_when_no_baseline`).

Pre-existing suites also still green: `test_risk_circuit_breaker.py` (6) and
`test_circuit_breaker_integration.py` (9) — **28 breaker-related tests total pass.**

---

## 5. Live exposure audit — what the bot holds right now

**DB inspected:** `/data/binance-bot-data/crypto_trading.db` (read-only, live).

| Item | Value |
|---|---|
| Live process | `python -m binance_trade_bot`, host PID 3537111, in container `ig7sexqj6pnpnbtkn18odyfn`, up ~13h |
| Current regime | **BULL** (ADX ≈ 30.7–32.2, stable across last 12 regime logs; 1079 total logs) |
| Current holding | **INJ** (spot), since 2026-06-26 02:40 UTC |
| `awaiting_reentry` | `False` |
| Last trade | 2026-06-26 02:40 UTC (JUP → INJ rotation) |
| Futures positions | **None** (BULL → futures path inactive; no `_open_position`) |
| `last_usdc_balance` (DB) | `0.00765` USDC (dust — capital is in INJ, not bridge) |
| Initial deposit | $62.41 USDC (backfilled 2026-06-22) |

### Realized drawdown — already exceeds both thresholds

Trade-history cost basis (USDC `crypto_trade_amount` of buys):

```
2026-06-22 20:49  TIA buy  $61.13   ← peak
2026-06-23 11:35  JUP buy  $57.61
2026-06-24 11:53  AAVE buy $57.68
2026-06-25 03:01  AAVE buy $56.71
2026-06-25 12:27  JUP buy  $54.58
2026-06-26 02:39  INJ buy  $52.08   ← current cost basis
```

Realized drawdown since inception: **$62.41 → $52.08 = −16.6%**. This exceeds
both the 3% daily and 8% weekly thresholds. **It occurred entirely while the
breaker was disabled** (breaker enabled ~23:10 UTC on Jun 26; last trade 02:40
UTC the same day). Had the breaker been active and seeded, it would have halted
new entries well before this point — concrete evidence the breaker is
load-bearing safety equipment that was off during the loss period. This drawdown
is locked in; the breaker cannot retroactively recover it.

### Canary cap enforcement — ✅ wired and active

Live config (`/data/binance-bot-data/config/user.cfg`):
`canary_mode_enabled = yes`, `canary_max_spot_trade_usdc = 75`,
`canary_futures_max_margin_pct = 0.15`, `canary_max_futures_margin_usdc = 50`.

- **Spot cap** enforced in `binance_api_manager.py:555` inside the buy path via
  `cap_spot_trade_balance()` (pure helper in `canary_capital_guard.py:42`):
  `allowed = min(bridge_balance, max_trade)`.
- **Futures cap** enforced in `futures_manager.py:528` inside `_attempt_entry`
  via `cap_futures_margin()`: applies both the 15% pct cap and the $50 absolute
  cap (`min(allowed, absolute_cap)`).

With ~$52 in capital the $75 spot cap is not currently binding (balance < cap),
but it is correctly in place to prevent a sudden deposit from being fully
deployed. The futures caps ($50 abs / 15% pct) would bind in BEAR mode. **The
canary guard is the only capital-protection layer currently active**, since the
breaker is dormant.

---

## 6. CROSS-margin risk assessment (1x leverage)

**Configured:** `futures_leverage = 1`, `futures_margin_type = CROSS`. CROSS is
used because Binance rejects ISOLATED on this account (`futures_manager.py:9-10,
124-138`); if ISOLATED is configured but cannot be set, the short is **aborted**
rather than silently opening CROSS (`_ensure_margin_mode` returns False →
`_open_short` returns `'idle'`).

### Current liquidation risk: LOW

1. **Single-position invariant.** `manage_bear` returns early at line 358
   (`if self._open_position is not None: return self._manage_open_position()`)
   before `_attempt_entry` can run. The bot cannot stack shorts. Reconciliation
   (`_reconcile_positions`, line 279) also enforces this: if Binance ever holds
   multiple shorts, it keeps the largest and immediately closes the rest
   reduce-only, placing a hard stop on any that can't be closed.
2. **1x leverage.** At 1x a short's liquidation price is extremely far away
   (price would need to ~double against the position). With a 15% hard stop-loss
   (`futures_stop_loss_pct=15.0`) the position is closed long before any
   liquidation.
3. **Server-side stops survive crashes.** Every short gets a `STOP_MARKET`
   (hard stop, `workingType=MARK_PRICE`, `reduceOnly`) placed immediately on
   entry (`_place_server_stops`, line 791). If the stop can't be confirmed the
   short is **flattened immediately** (line 649-667) — no naked shorts.
4. **Bull regime now.** No futures path is active; the bot is spot-only.

### Latent risk if multi-position were ever introduced: HIGH

CROSS margin shares margin across **all** open positions. If a future change
allowed N concurrent shorts:

1. **Shared margin pool** — one adverse position draws down the shared wallet
   and can force liquidation of *all* positions, including profitable ones.
   Loss is no longer bounded per-position.
2. **Correlated liquidation cascade** — crypto alts are highly correlated. A
   market-wide rally (common at regime transitions) moves all shorts underwater
   simultaneously; CROSS margin turns this into a single liquidation event
   rather than N independent stop-outs.
3. **No per-position margin isolation** — unlike ISOLATED, there is no
   per-position margin circuit breaker; the only guards are the portfolio
   drawdown breaker (blocks *new* entries, not existing exposure) and the
   per-position server stops.
4. **Funding stacks** across N positions.

**Recommendation:** If multi-position is ever implemented, CROSS margin must be
revisited. Prefer ISOLATED per position, or add an explicit aggregate-notional
cap (sum of `|positionAmt|`) enforced before each new short, independent of the
portfolio drawdown breaker. The single-position invariant in `futures_manager.py`
should be captured as a structural test so a future refactor can't silently
remove it. **Do NOT extend the current single-position CROSS design to N
positions without this.**

---

## Summary of findings

| # | Finding | Severity |
|---|---|---|
| F1 | Breaker logic correct; ≥3% daily / ≥8% weekly trips; all entry paths gated; exits bypass by construction | ✅ Confirmed safe |
| F2 | **Fix exists locally but is NOT deployed** — container runs master (`ab980f4`), fix branch (`aac91e4`) unpushed/unmerged | 🔴 **NEEDS_FIX (deploy)** |
| F3 | Live DB has no seeded baselines → breaker is dormant (fail-open) in production right now | 🔴 **LIVE GAP** (consequence of F2) |
| F4 | Deployed image fails-open *silently* (no Telegram); local fix escalates but isn't deployed | ⚠️ Risk (consequence of F2) |
| F5 | ~16.6% realized drawdown occurred entirely while breaker was disabled | 📉 Context (locked in) |
| F6 | Canary caps correctly wired (spot $75 + futures $50/15%) and active — the *only* active capital guard | ✅ Safe |
| F7 | CROSS + 1x + single-position → low liquidation risk today; HIGH if multi-position added | ✅ now / ⚠️ latent |
| F8 | Repo `user.cfg` is a safe-default (breaker/canary OFF); live config is a separate mounted file with them ON | ℹ️ Note (deployment detail) |

### Required follow-ups (for bot-lead; not applied under read-only constraint)

1. **[F2/F3, blocking]** Merge `fix/deploy-blockers-98-101` → master so Coolify
   deploys the eager-seeding + fail-open-alert fixes. After redeploy, confirm
   `portfolio_daily_start_equity` appears in `bot_state`. **Until then, treat
   the breaker as off.**
2. **[F7]** Add a structural test asserting `_attempt_entry` is unreachable while
   `_open_position is not None`, and document the single-position invariant as a
   hard constraint in `futures_manager.py`.
3. **[F3, belt-and-suspenders]** Consider making the equity-unavailable path
   fail-closed (block new risk) rather than fail-open, since the only cost is a
   missed entry, not a stranded exit. (Local fix currently keeps fail-open but
   escalates visibility — acceptable, but fail-closed is safer for a guardrail.)

---

### On the "halt" question

My veto authority: I do **not** exercise a halt. The conditions that would force
one — unbounded risk path, naked exposure, or an actively-bleeding position — do
not hold. Current state is a single spot position with a 15% trailing stop, no
futures, inside the canary cap, and the momentum filter is blocking all new
entries anyway. The dormant breaker is a **latent** gap, not an active loss path.

**The one condition I attach:** do not approve a regime change to BEAR (which
would open futures shorts, the higher-risk path) until F2/F3 are deployed and
baselines are confirmed seeded. If the regime turns bearish first, halt the
container manually.

---

*Audit conducted read-only. The live DB was opened in `mode=ro` only; no writes.
Files added: `tests/test_risk_audit_98_breaker_fires.py`, this report. No
production code, risk parameters, thresholds, config, or trading behavior were
modified.*
