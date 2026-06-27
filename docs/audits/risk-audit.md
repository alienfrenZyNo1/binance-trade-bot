# Risk Audit #98 — Portfolio Circuit Breaker

**Issue:** [#98 [risk] Verify circuit breaker actually fires in production](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/98)
**Auditor:** risk-agent (independent veto authority on safety)
**Date:** 2026-06-27
**Scope:** Read-only audit of the portfolio circuit breaker code path, live DB exposure, and CROSS-margin risk. New tests added under `tests/`; no production code, thresholds, or trading behavior modified.

---

## VERDICT: ⚠️ NEEDS_FIX

The circuit breaker **logic is correct and correctly integrated** — every new-entry path checks the breaker before placing a trade, and exits/stop-losses bypass it by design. The 9-test integration suite proves this end-to-end.

**However, the breaker is NOT actually protecting live capital right now** because its equity baselines have never been seeded in the production DB. It will only begin protecting capital on the *next* new entry after it self-seeds, and it silently fails open (allows all trades) if equity estimation returns `None`. Combined with a realized ~15% drawdown that already occurred while the breaker was disabled, this is a live safety gap.

| Check | Status |
|---|---|
| Breaker logic pure + correct | ✅ PASS |
| All spot entry paths check breaker before buy | ✅ PASS (3/3) |
| Futures short entry checks breaker | ✅ PASS |
| Exits / stop-losses bypass breaker | ✅ PASS (verified by structure) |
| Baselines seeded in live DB | ❌ **FAIL — never seeded** |
| Breaker enabled in live config | ✅ PASS (`portfolio_circuit_breaker_enabled = yes`) |
| Canary caps enforced in code | ✅ PASS (spot + futures wired) |
| Silent fail-open on missing equity | ⚠️ RISK (by design, but undocumented) |
| CROSS-margin multi-position risk | ✅ Mitigated today (single-position enforced); latent if changed |

---

## 1. Code Path Trace — Breaker IS checked before every new trade

### The pure helper (`binance_trade_bot/risk_circuit_breaker.py`)

`evaluate_circuit_breaker(current_equity, daily_start_equity, weekly_start_equity, config)` returns a frozen `CircuitBreakerResult` with `block_new_risk: bool`. Key logic (lines 46–98):

```python
daily_dd = _drawdown_pct(current_equity, daily_start_equity)
weekly_dd = _drawdown_pct(current_equity, weekly_start_equity)

if daily_dd is None and weekly_dd is None:
    return CircuitBreakerResult(False, False, "none", 0.0, 0.0, "equity baseline unavailable")

if daily_limit > 0 and daily_dd is not None and daily_dd >= daily_limit:
    return CircuitBreakerResult(True, True, "daily", daily_dd, daily_limit, ...)
```

### Integration point — the live strategy gate (`strategies/momentum_strategy.py:657`)

`_new_spot_risk_blocked()` is the single chokepoint. It seeds baselines, checks cooldown, then evaluates:

```python
def _new_spot_risk_blocked(self):
    if not getattr(self.config, 'PORTFOLIO_CIRCUIT_BREAKER_ENABLED', False):
        return False
    equity = self._estimate_spot_equity()
    if equity is None:
        self.logger.warning("Circuit breaker enabled but equity estimate unavailable; allowing new risk")
        return False                                      # ← SILENT FAIL-OPEN
    ...
    daily, weekly = self._ensure_circuit_breaker_baselines(equity, now)
    result = evaluate_circuit_breaker(equity, daily, weekly, self.config)
    if result.block_new_risk:
        self.db.set_bot_state("portfolio_circuit_breaker_last_triggered", str(now))
        return True
    return False
```

### Every new-entry path calls the gate before the trade (quoted from production source)

**Spot rotation** (`scout`, after confirmation countdown):
```python
if self._new_spot_risk_blocked():        # momentum_strategy.py:885
    self._pending_rotation = None
    return
result = self.transaction_through_bridge_pair(current_coin, best_coin)   # :889
```

**Bridge re-entry** (`_reenter_from_bridge`):
```python
if self._new_spot_risk_blocked():        # momentum_strategy.py:716
    return
result = self.manager.buy_alt(best_coin, self.config.BRIDGE)             # :723
```

**Bridge scout** (`bridge_scout`):
```python
if self._new_spot_risk_blocked():        # momentum_strategy.py:946
    return
self.manager.buy_alt(best_coin, self.config.BRIDGE)                      # :953
```

**Futures short entry** (`futures_manager.py:323`, inside `manage_bear`):
```python
# Circuit breaker blocks new entries only. Existing shorts are managed
# above before this guard so stops/exits remain active.
if callable(self.new_risk_blocked) and self.new_risk_blocked():
    self.logger.warning("Futures circuit breaker active — blocking new short entry")
    return 'idle'
return self._attempt_entry(performance)
```

> `self.futures_manager.new_risk_blocked = self._new_spot_risk_blocked` is wired in `momentum_strategy.py:141` during `initialize()`.

---

## 2. Equity Baseline Seeding — ⚠️ LIVE GAP (the core finding)

### The seeding code exists and is correct (`momentum_strategy.py:615`)

`_ensure_circuit_breaker_baselines()` seeds `portfolio_daily_start_equity` / `portfolio_weekly_start_equity` into `bot_state`, and rolls them over on UTC day/ISO-week boundaries:

```python
if daily is None or daily <= 0:
    self.db.set_bot_state("portfolio_daily_start_equity", str(equity))
    self.db.set_bot_state("portfolio_daily_period", daily_period)
    ...
elif stored_daily_period != daily_period:
    self.db.set_bot_state("portfolio_daily_start_equity", str(equity))   # rollover
```

### BUT: the live production DB has NO baseline keys seeded

Inspection of the live DB at `/data/binance-bot-data/crypto_trading.db`:

```
portfolio_daily_start_equity:            *** MISSING ***
portfolio_weekly_start_equity:           *** MISSING ***
portfolio_daily_period:                  *** MISSING ***
portfolio_weekly_period:                 *** MISSING ***
portfolio_circuit_breaker_last_triggered:*** MISSING ***
```

**Why:** Baselines are seeded *lazily* — only from inside `_new_spot_risk_blocked()`, which only runs when a new entry is attempted. The breaker was enabled in live config at ~2026-06-26 23:12 UTC (`user.cfg.bak-tighten`), but the last trade was at 2026-06-26 02:40 UTC — **before** the breaker was enabled. Since then the bot has been in BULL regime holding INJ and no new entry has been attempted, so the seeding path has never executed.

**Consequence:** Until the next new entry is attempted, the breaker is dormant. On that next attempt it will self-seed and begin protecting capital. But there is a second, subtler fail-open: if `_estimate_spot_equity()` returns `None` (e.g. balance API hiccup), the breaker logs a warning and **allows the trade**. There is no `last_triggered` timestamp either, so cooldown has never engaged.

### Recommendation (not applied — read-only audit)

1. **Seed baselines eagerly on startup** when the breaker is enabled, rather than lazily on first entry. A one-line call to `_ensure_circuit_breaker_baselines()` from `initialize()` would close the window.
2. **Consider fail-closed** for the equity-unavailable case, or at minimum emit a high-severity Telegram alert (not just a `notification=False` warning) so an operator notices the breaker is blind.

---

## 3. Test Evidence — breaker blocks entries, exits still execute

New test file: `tests/test_circuit_breaker_integration.py` — **9/9 PASS**.

```
tests/test_circuit_breaker_integration.py::test_breaker_blocks_new_entries_at_3pct_daily_drawdown PASSED
tests/test_circuit_breaker_integration.py::test_breaker_just_below_threshold_allows_entries PASSED
tests/test_circuit_breaker_integration.py::test_breaker_blocks_at_exactly_threshold_boundary PASSED
tests/test_circuit_breaker_integration.py::test_breaker_result_has_no_exit_blocking_field PASSED
tests/test_circuit_breaker_integration.py::test_futures_manage_bear_checks_breaker_only_for_new_entries PASSED
tests/test_circuit_breaker_integration.py::test_spot_trailing_stop_does_not_call_breaker PASSED
tests/test_circuit_breaker_integration.py::test_all_spot_entry_paths_call_breaker_before_buy PASSED
tests/test_circuit_breaker_integration.py::test_cooldown_persists_block_for_24h_after_trigger PASSED
tests/test_circuit_breaker_integration.py::test_daily_baseline_reset_re_enables_trading_after_new_day PASSED
```

The tests prove the required invariants via two complementary techniques:

- **Behavioral** (using the real pure helper): at exactly 3.0% daily drawdown the breaker returns `block_new_risk=True`; at 2.9% it returns `False`; the 24h cooldown persists then releases; a daily rollover re-enables trading.
- **Structural** (introspecting live source with `inspect.getsource`): the `CircuitBreakerResult` dataclass has no exit-blocking field; `_check_trailing_stop` never calls the breaker; all three spot entry methods and `manage_bear` call the gate *before* the entry action; and in `manage_bear`, `_manage_open_position()` runs *before* the breaker gate so stop-loss/trailing/funding exits stay live while new entries are blocked. These structural assertions will break loudly if a future refactor reorders the blocks.

Pre-existing breaker tests (`tests/test_risk_circuit_breaker.py`, 6 tests) also still pass.

---

## 4. Live Exposure Audit

**DB inspected:** `/data/binance-bot-data/crypto_trading.db` (live, 5.8 MB, last modified 2026-06-27 11:40 UTC). The repo's `data/crypto_trading.db` is empty (0 bytes) — a stale artifact.

| Item | Value |
|---|---|
| Live process | `python -m binance_trade_bot` (PID 3537111), running |
| Current regime | **BULL** (ADX ≈ 31.6, confirmed across last 10 regime logs) |
| Current holding | **INJ** (spot), since 2026-06-26 02:40 UTC |
| `awaiting_reentry` | False |
| Last trade | 2026-06-26 02:40 UTC (JUP → INJ rotation) |
| Futures positions | **None** (no `_open_position`; BULL regime → futures path not active) |
| Initial deposit | $62.41 USDC (backfilled 2026-06-22) |
| Latest realized USDC balance | ~$52.08 (from `crypto_trade_amount` of last trade) |
| **Realized drawdown since inception** | **~15.3%** ($61.46 → $52.08) |

### Realized drawdown context

The ~15% drawdown occurred over Jun 22–26, **entirely while the circuit breaker was disabled** (it was only enabled at ~23:12 UTC on Jun 26). This drawdown exceeds *both* the 3% daily and 8% weekly thresholds. Had the breaker been active and seeded, it would have halted new entries well before this point. This is direct evidence that the breaker is load-bearing safety equipment that was not switched on during the loss period.

### Canary cap enforcement — ✅ wired and active

Live config: `canary_mode_enabled = yes`, `canary_max_spot_trade_usdc = 75`, `canary_futures_max_margin_pct = 0.15`, `canary_max_futures_margin_usdc = 50`.

- **Spot cap** enforced in `binance_api_manager.py:539` inside the buy path:
  ```python
  canary_cap = cap_spot_trade_balance(target_balance, self.config)
  if canary_cap.capped:
      self.logger.warning(f"{canary_cap.reason}: limiting spot buy ...")
  target_balance = canary_cap.allowed_balance
  ```
- **Futures cap** enforced in `futures_manager.py:483` inside `_attempt_entry`:
  ```python
  cap = cap_futures_margin(usdc_balance, self.max_margin_pct, self.config)
  margin = cap.allowed_margin
  ```

With a ~$52 balance the $75 spot cap is not currently binding (balance < cap), but the cap is correctly in place to prevent a sudden deposit from being fully deployed. The futures caps ($50 absolute / 15% pct) would bind if the bot entered BEAR mode.

---

## 5. CROSS Margin Risk Assessment (1x leverage)

**Configured:** `futures_leverage = 1`, `futures_margin_type = CROSS`. CROSS is used because *Binance rejects ISOLATED on this account* (documented in `futures_manager.py:9-10, 124-138`); if ISOLATED is configured but cannot be set, the short is **aborted** rather than silently opening CROSS.

### Current liquidation risk: LOW

- Only **one** futures short can be open at a time. `manage_bear` returns early at `futures_manager.py:313-314` (`if self._open_position is not None: return self._manage_open_position()`) before `_attempt_entry` can run. The bot cannot stack positions.
- At 1x leverage, a short's liquidation price is extremely far away (price would need to ~double against the position). With a 15% hard stop-loss (`futures_stop_loss_pct=15.0`) plus a 10% trailing stop plus server-side `STOP_MARKET` orders, the position would be closed long before any liquidation.
- Server-side stops survive bot crashes (`futures_manager.py:11-12`).

### Latent risk if multi-position were ever introduced: HIGH

CROSS margin shares margin across **all** open positions. If a future change allowed N concurrent shorts:
1. **Shared margin pool** — a single adverse position can draw down the shared wallet and force liquidation of *all* positions, including profitable ones. Loss is no longer bounded per-position.
2. **Correlated liquidation cascade** — crypto alts are highly correlated. A market-wide rally (common in regime transitions) could move all shorts underwater simultaneously, and CROSS margin would amplify this into a single liquidation event rather than N independent stop-outs.
3. **No per-position isolation** — unlike ISOLATED, there is no circuit breaker on margin itself; the only guards are the portfolio breaker (which blocks *new* entries, not existing exposure) and the per-position stops.
4. **Funding stacking** — adverse funding on N positions compounds.

**Recommendation:** If multi-position is ever implemented, CROSS margin must be revisited. Prefer ISOLATED per position, or add an explicit aggregate-notional cap (sum of |positionAmt|) enforced before each new short, independent of the portfolio drawdown breaker. Do NOT extend the current single-position CROSS design to N positions without this.

---

## Summary of Findings

| # | Finding | Severity |
|---|---|---|
| F1 | Breaker logic + integration correct; all entry paths gated; exits bypass by design | ✅ Confirmed safe |
| F2 | Live DB has never seeded breaker baselines — breaker is dormant until next entry | 🔴 **NEEDS_FIX** |
| F3 | Breaker silently fails open if `_estimate_spot_equity()` returns `None` | ⚠️ Risk |
| F4 | ~15% realized drawdown occurred entirely while breaker was disabled | 📉 Context |
| F5 | Canary caps correctly wired (spot + futures) and active in live config | ✅ Safe |
| F6 | CROSS + 1x + single-position → low liquidation risk today; HIGH if multi-position added | ⚠️ Latent |
| F7 | `.env.telegram` exists locally with live API keys (NOT tracked in git — confirmed via `git ls-files`) | ℹ️ Note |

### Required follow-ups (for bot-lead; not applied under read-only constraint)

1. **[F2, blocking]** Seed breaker baselines eagerly on startup (`initialize()`) when the breaker is enabled, so protection starts immediately rather than on the next lazy entry.
2. **[F3]** Reconsider fail-open for equity-unavailable; at minimum escalate to a visible Telegram alert.
3. **[F6]** Document the single-position invariant as a hard constraint in `futures_manager.py` and add a structural test asserting `_attempt_entry` is unreachable while `_open_position is not None`.

---

*Audit conducted read-only. Files added: `tests/test_circuit_breaker_integration.py`, `docs/audits/risk-audit.md`. No production code, risk parameters, thresholds, or trading behavior were modified.*
