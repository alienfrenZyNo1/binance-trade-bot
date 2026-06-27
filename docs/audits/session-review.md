# Session Code Review — Issue #101 (Final Review)

**Reviewer:** final-reviewer (INDEPENDENT VETO authority — reports to The Boss, not bot-lead)
**Date:** 2026-06-27
**Diff range:** `70371bb..HEAD`
**Verdict:** ⛔ **REQUEST_CHANGES** — Two blocking live-trading safety defects identified. DO NOT push to master until fixed. (Git push → Coolify auto-deploy, so this gate matters.)

---

## Executive Summary

The session introduces two **independent** critical defects, either of which is sufficient to block deploy on its own:

1. **🔴 BLOCKER A — `-2010` error code is overloaded, misclassified as idempotency success.**
   `_is_duplicate_order_error()` and the futures `-2010` handler both treat Binance error `-2010` as "order already placed (duplicate clientOrderId)". But `-2010` is `NEW_ORDER_REJECTED` — a generic catch-all that **also covers genuine insufficient-balance rejections**. Verified by execution: a real insufficient-balance error returns `True` from `_is_duplicate_order_error`. Result: an order that failed for **lack of funds is reported as a successfully-placed order**, the retry loop breaks, and `get_order(origClientOrderId=...)` is then called on an order that does not exist.

2. **🔴 BLOCKER B — Incomplete idempotency coverage.** Only 1 of 6 `futures_create_order` calls (and 2 of 3 order-entry paths) got `newClientOrderId`. The remaining entry/stop/close paths have **no idempotency protection**, so a network timeout on retry can place a duplicate order. This is exactly the duplicate-order / money-loss scenario the session was meant to eliminate.

A third **non-blocking** issue (Werkzeug 3 / flask-socketio import breakage) affects the web UI but not the trading core.

---

## Per-File Verdicts

### 1. `binance_trade_bot/binance_api_manager.py` — ⛔ **REQUEST_CHANGES**

**What changed:** `_generate_client_order_id()`, `_is_duplicate_order_error()`, error-classification helpers (`NON_RETRYABLE_API_CODES`, `_classify_retry_error`), class constants, `retry()` rewrite with exponential backoff + 429 handling, and `newClientOrderId` + duplicate-handling added to `_buy_alt`/`_sell_alt`.

#### 🔴 BLOCKER A — `-2010` overload is a live-trading correctness bug

`_is_duplicate_order_error()`:
```python
def _is_duplicate_order_error(e: BinanceAPIException) -> bool:
    if hasattr(e, 'code') and e.code == -2010:
        return True
    msg = str(e).lower()
    return "duplicate order" in msg or "duplicate" in msg
```

Binance error code `-2010` is **`NEW_ORDER_REJECTED`**, not a duplicate-order-specific code. Its message strings include:
- `"Account has insufficient balance for requested action."`
- `"Margin is insufficient."`
- `"Order would immediately trigger."`

I verified this concretely against the installed `python-binance`:
```
>>> _is_duplicate_order_error(<-2010 insufficient balance>)
True
```

**Live impact:** When a buy/sell fails because the account genuinely lacks balance, the code now:
1. classifies it as "order already placed (duplicate clientOrderId)",
2. calls `self.binance_client.get_order(symbol=…, origClientOrderId=…)` for an order that **does not exist on the exchange**,
3. breaks the retry loop, and
4. proceeds as if the order succeeded.

The subsequent `get_order` will itself raise `-2013 NO_SUCH_ORDER`, which is now uncaught at that point (the `except BinanceAPIException` is exhausted), so the order path errors out — but the surrounding `retry(self._buy_alt, …)` wrapper will catch it and re-enter `_buy_alt`, regenerating balance/price and creating a **new** `client_order_id` each loop because the price/qty inputs differ. So the actual net behavior is "broken retry loop that never succeeds but also doesn't duplicate" — *unless* the price happens to be identical across retries, in which case the deterministic ID collides with the *original failed attempt's* ID on a future legitimate retry. The logic is incoherent and not safe.

**What must change (reviewer note — bot-lead to implement):**
- Do **not** treat bare `-2010` as a duplicate. The duplicate-clientOrderId rejection is signaled by a specific message. Gate on the message: `e.code == -2010 and "duplicate" in str(e).lower()`, or better, on Binance's documented duplicate string. A genuine insufficient-balance `-2010` must remain retryable-to-fail or non-retryable — it must **never** be treated as "order placed".
- `-2010` is also simultaneously in `NON_RETRYABLE_API_CODES` (correct for retry()), but the buy/sell inner loop intercepts it first and mishandles it. These two interpretations of the same code in the same file are contradictory.
- Add a test for the insufficient-balance `-2010` case asserting it is **not** treated as a duplicate.

#### 🟠 Non-blocking — idempotency not applied to maker-reprice path

The `_reprice_buy` / `_reprice_sell` callbacks (lines ~619–624 and ~733–738) call `order_limit_buy`/`order_limit_sell` **without** `newClientOrderId`. If a maker order is repriced, the reprice call is unprotected. Lower severity because reprice is a one-shot fallback, but it is an unprotected order-placement path.

#### 🟠 Non-blocking — `retry()` returns `None` for non-retryable, caller may not handle

`retry()` now returns `None` for non-retryable errors instead of looping 20× then returning `None`. The return type is unchanged (`None`), so existing callers that handle `None` are fine. But `sell_alt`/`buy_alt` callers that previously got `None` only after exhausting all 20 attempts now get it instantly. This is a behavior change that is mostly **better** (faster failure), but any caller that assumed "if it returned None, we already retried a lot" should be audited. No bug found in current callers, but flagging the contract change.

#### ✅ What's correct
- `_classify_retry_error` correctly handles 429 (longer backoff via `RATE_LIMIT_BACKOFF_MULTIPLIER`), network `TimeoutError`/`ConnectionError` (retryable), and hard HTTP statuses (non-retryable). Tested and passing.
- Exponential backoff capped at 60s with `min(2**attempt, MAX_BACKOFF_SECONDS)` — correct, no overflow, schedule verified by `test_retry_uses_exponential_backoff_schedule`.
- Class constants (`MAX_RETRY_ATTEMPTS=20`) preserve the prior attempt count.
- `_generate_client_order_id` is deterministic and truncates to 36 chars (Binance limit). Hash inputs are side/coin/price/qty — reasonable.

---

### 2. `binance_trade_bot/futures_manager.py` — ⛔ **REQUEST_CHANGES**

**What changed:** Added `newClientOrderId` to the short-entry order only; added `-2010` → `return 'opened'` in the entry exception handler.

#### 🔴 BLOCKER A (futures variant) — `-2010` returns `'opened'` with NO verification

```python
except BinanceAPIException as e:
    if hasattr(e, 'code') and e.code == -2010:
        self.logger.info(f"Futures short already placed …")
        return 'opened'
```

Same `-2010` overload problem as the spot path, but **worse**: on a genuine insufficient-margin `-2010`, this returns `'opened'` (success) **without** calling `self._open_position = FuturesPosition(…)` and **without** querying the exchange to confirm an order exists. The caller believes a short is open; `_open_position` is `None`; the protective stop was never placed. The bot is now in an inconsistent state where:
- the main loop thinks no position is open (so it may **enter another short** on the next cycle), and
- if a short *was* somehow placed, it has **no server-side stop protection**.

**Live impact:** Either (a) false-success suppresses a real entry (lost opportunity, benign), or (b) on a partial/timing-dependent condition the bot re-enters and stacks shorts. Either way the return contract is broken.

#### 🔴 BLOCKER B — 5 of 6 `futures_create_order` calls lack idempotency

There are **6** `futures_create_order` call sites; only **1** (the short entry at line 574) got `newClientOrderId`:

| Line | Purpose | Idempotent? |
|------|---------|-------------|
| 270  | Close orphan short during reconciliation | ❌ No |
| 574  | Open short entry | ✅ Yes |
| 604  | Emergency flatten after stop-placement failure | ❌ No |
| 646  | `_close_position` market close | ❌ No |
| 727  | `_place_server_stops` hard stop | ❌ No |

The session's stated goal was order idempotency. The two highest-stakes **unprotected** paths are:
- **Line 646 (`_close_position`)**: the position-exit order. A timeout+retry here can place a **duplicate market close**, and because `reduceOnly="true"` it would attempt to flip into a long — rejected, but still a duplicate request and noisy failure during a critical exit.
- **Line 727 (`_place_server_stops`)**: the protective stop. A duplicate here could create **two hard stops**, doubling effective stop quantity if `reduceOnly` semantics allow it.
- **Line 604 (emergency flatten)**: called in the failure path when stop placement already failed — a duplicate here in a degraded state is exactly when you can least afford it.

**What must change:** All six call sites need a deterministic `newClientOrderId` (or, for the reduce-only close/stop paths, an idempotency strategy appropriate to `STOP_MARKET` orders). At minimum, lines 646 and 727 must be protected before this is safe to deploy. Each `-2010`/duplicate handler must **verify via `futures_get_order`** that the order exists before treating it as success.

---

### 3. `scripts/telegram_bot.py` — ✅ **APPROVE** (with notes)

**What changed:** Kill switch now (a) records `closed_symbols`/`transferred_amount`, (b) re-queries positions after closing and reports a `VERIFICATION FAILED` block if any remain, and (c) logs the kill event to `bot_state` via `_log_kill_event`.

#### Race conditions — none material
- The verification re-query happens after `time.sleep(1)` following the close loop. The close loop itself iterates the position snapshot fetched **once** at the top (`positions = get_futures_positions()`). This is a TOCTOU window in the *snapshot* (a position could open between fetch and close), but the new verification step re-queries fresh, which **mitigates** the window. The 1s settle wait is reasonable for MARKET closes.
- `_log_kill_event` is best-effort with a bare `except` and uses a timestamp-unique key, so repeated kills never collide on PK. Verified the `bot_state` schema has `updated_at` (Column DateTime) — the INSERT is schema-safe.
- No DB lock contention risk: connection is opened, committed, closed within the function.

#### Notes (non-blocking)
- `conn.close()` is in the happy path; on exception the connection may leak. Minor (SQLite), and the `except` swallows it. A `with`/`finally` would be cleaner but is not a safety issue.
- `datetime.utcnow()` is used (deprecated in 3.12, not 3.11). Project is on 3.11 — fine.
- The verification correctly does **not** claim success when positions remain, and the final summary branches on `all_flat`. This is the desired behavior.

---

### 4. `binance_trade_bot/logger.py` — ✅ **APPROVE**

**What changed:** `FileHandler` → `RotatingFileHandler` (10MB × 5), honors `LOG_DIR` env var, `mkdir(parents=True, exist_ok=True)`, and gracefully degrades to console-only on `OSError`.

- ✅ Correct, idempotent dir creation; failure is non-fatal and logged.
- ✅ Rotation bounds total disk to ~50MB — sensible for a persistent systemd deployment.
- ✅ `encoding="utf-8"` explicit — good.
- No live-trading risk. Handler attachment order (file then console) is unchanged.

---

### 5. `tests/test_retry_backoff.py` — ✅ **APPROVE** (22/22 pass)

All 22 tests pass (`pytest tests/test_retry_backoff.py` → 22 passed in 0.99s). Coverage:
- ✅ Non-retryable API codes (`-1013, -1121, -2010, -2013, -2015`)
- ✅ Non-retryable HTTP status (`400, 401, 403, 404, 422`)
- ✅ 429 rate-limited → longer backoff (`RATE_LIMIT_BACKOFF_MULTIPLIER`)
- ✅ Transient 5xx → retryable
- ✅ Network `TimeoutError`/`ConnectionError` → retryable
- ✅ Exponential backoff schedule matches `min(2**attempt, 60)` exactly
- ✅ Non-retryable short-circuits with zero sleeps

**Gap (non-blocking):** No test asserts that a genuine insufficient-balance `-2010` is **not** treated as a duplicate order — which is precisely the bug in BLOCKER A. Once the `-2010` classification is fixed, add a test like `test_insufficient_balance_2010_is_not_duplicate`.

---

### 6. `requirements.txt` — 🟠 **APPROVE WITH CAVEAT** (web-UI breakage, not trading core)

**What changed:** Snyk-driven security pins — `gunicorn 20.1.0→22.0.0`, `flask-cors 3.0.10→4.0.1`, `eventlet >=0.35.2→>=0.37.0`, `Werkzeug==2.3.8→>=3.0.6`, plus transitive pins (`aiohttp`, `certifi`, `cryptography`, `jinja2`, `requests`, `urllib3`, `setuptools`, etc.).

#### CVE safety — ✅ no known CVEs introduced
The pins are all **above** the fixed versions for their respective Snyk advisories (urllib3 ≥2.2.2, requests ≥2.32.0, cryptography ≥42.0.2, aiohttp ≥3.10.11, certifi ≥2023.7.22, jinja2 ≥3.1.3, Werkzeug ≥3.0.6 all close known高危 vulns). Installed versions confirm resolution. This part is good.

#### 🟠 Compatibility break — flask-socketio 5.0.1 + Werkzeug 3.x fails to import
Verified by execution:
```
>>> import flask_socketio
ImportError: cannot import name 'run_with_reloader' from 'werkzeug.serving'
```
`run_with_reloader` was **removed in Werkzeug 3.0**. `flask-socketio==5.0.1` (pinned, not upgraded) imports it unconditionally. Result: `api_server.py` (line 8: `from flask_socketio import SocketIO, emit`) **crashes on import**. The web dashboard/auto-trader UI will not start under the current pin set.

**Why not a hard blocker for trading:** the trading core (`binance_api_manager`, `futures_manager`, telegram bot) does not import flask-socketio, so the bot itself runs. But if the deployment includes the API server / web UI (gunicorn entrypoint), it will fail.

**What must change:** Either bump `flask-socketio` to ≥5.4.0 (which dropped the `run_with_reloader` import) **or** pin `Werkzeug<3` (e.g. `Werkzeug==2.3.8`, reverting the security bump). The latter re-opens GHSA on Werkzeug, so bumping flask-socketio is preferred. Verify the full web stack boots before deploy.

---

## Consolidated Decision

| File | Verdict | Reason |
|------|---------|--------|
| `binance_api_manager.py` | ⛔ **REQUEST_CHANGES** | BLOCKER A: `-2010` misclassified as duplicate → insufficient balance treated as success |
| `futures_manager.py` | ⛔ **REQUEST_CHANGES** | BLOCKER A (returns `'opened'` unverified) **+** BLOCKER B (5/6 order paths lack idempotency) |
| `telegram_bot.py` | ✅ APPROVE | Kill-switch verification + DB logging correct; no material race |
| `logger.py` | ✅ APPROVE | Clean rotating-handler change, graceful degradation |
| `test_retry_backoff.py` | ✅ APPROVE | 22/22 pass, good coverage (add `-2010`-balance test after fix) |
| `requirements.txt` | 🟠 APPROVE w/ caveat | No CVEs, but flask-socketio/Werkzeug 3 import breakage breaks web UI |

## Required Changes Before Push to Master

1. **(Blocking)** Fix `-2010` handling in `_is_duplicate_order_error()` (binance_api_manager.py) — must not treat insufficient-balance as duplicate. Gate on the duplicate-specific message string.
2. **(Blocking)** Fix futures `-2010` handler (futures_manager.py) — must `futures_get_order` to confirm existence before `return 'opened'`; otherwise return `'idle'`.
3. **(Blocking)** Add `newClientOrderId` to the 5 remaining `futures_create_order` call sites, prioritizing `_close_position` (line 646) and `_place_server_stops` (line 727).
4. **(Web UI)** Bump `flask-socketio` to ≥5.4.0 (or pin Werkzeug<3) so the API server imports cleanly.

**The reviewer will not sign off on a deploy until items 1–3 are resolved.** Item 4 can ship separately if the web UI is not part of this deployment, but should be tracked.

— final-reviewer (independent)
