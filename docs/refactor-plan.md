# Refactor Plan

## Scope and guardrails

This project is a live Binance trading bot. Refactoring must be incremental, tested, and production-safe. The goal is to make the codebase easier and safer to change without altering live trading behaviour unless a change is explicitly a bug fix.

Guardrails:
- Preserve public CLI/module entry points, Telegram commands, DB schema, env/config names, and Docker deployment flow unless a PR explicitly documents a migration.
- Keep `USDC` as the bridge currency and continue using `config.BRIDGE.symbol`; do not hardcode `USDT`.
- Use small PRs with regression tests before behaviour changes.
- Never log secrets or commit `.env` files.
- After bot code changes: run targeted tests, full pytest, compile checks, build Docker image, restart container, and inspect logs.

## Current project map

### Main entry points
- `binance_trade_bot/__main__.py` / `python -m binance_trade_bot`
- `binance_trade_bot/crypto_trading.py` — main live bot loop startup.
- `binance_trade_bot/api_server.py` — Flask/API dashboard surface.
- `scripts/telegram_bot.py` — Telegram companion bot run by systemd.
- `Dockerfile` + `docker-entrypoint.sh` — production container entry.
- Research/ops scripts under `scripts/`, plus root-level backtest/optimizer scripts.

### Core runtime modules
- `binance_trade_bot/config.py` — config/env loading.
- `binance_trade_bot/binance_api_manager.py` — spot API, stream/cache integration, trade execution helpers.
- `binance_trade_bot/futures_manager.py` — USDC-M futures short lifecycle, margin, stops, transfers.
- `binance_trade_bot/strategies/momentum_strategy.py` — adaptive live strategy and regime transitions.
- `binance_trade_bot/auto_trader.py` — shared trade loop scaffolding.
- `binance_trade_bot/database.py` — SQLAlchemy session management, persistence, bot state, Socket.IO update emission.
- `binance_trade_bot/models/` — SQLAlchemy models.
- `binance_trade_bot/indicators.py`, `accounting.py`, `regime_hysteresis.py`, `notifications.py` — reusable helpers.

### Tests and tooling
- `tests/` contains unit/regression tests for futures safety, regime logic, Telegram shadow output, optimizer/backtest outputs, and production hardening.
- Main verification command: `python3 -m pytest tests -q`.
- Compile check: `python3 -m py_compile <changed python files>`.
- No dedicated lint/type-check config was found during initial inspection; linting is mostly via tests and syntax checks.

## Structure problems found

1. **Large coupled runtime files**
   - `momentum_strategy.py`, `futures_manager.py`, `database.py`, and `scripts/telegram_bot.py` each mix orchestration, API calls, formatting, business rules, persistence, and error handling.
   - This makes targeted testing harder and increases the chance that small fixes accidentally change live behaviour.

2. **Runtime and research code are mixed loosely**
   - Research scripts live in `scripts/` and root files (`optimize_momentum.py`, `strategy_optimizer.py`) rather than a clear `research/` or `binance_trade_bot/research/` package.
   - Some research modules are import-safe now, but the boundaries are still unclear.

3. **External API transfer/account semantics are not centralized**
   - Futures wallet balance, transferable balance, and transfer execution are currently methods on `FuturesManager` with Binance quirks embedded inline.
   - The live `-5013 insufficient balance` incident shows this area needs a small, well-tested transfer policy layer.

4. **Configuration is mostly centralized but validation is weak**
   - `Config` reads many env/config values but lacks strong validation for types, ranges, and incompatible settings.
   - Persistent volume config can differ from repo `user.cfg`, so docs and runtime introspection matter.

5. **Error handling is inconsistent**
   - Some high-frequency paths log routine conditions at INFO/ERROR and can spam notifications.
   - Some API exceptions are swallowed with generic messages, making root cause diagnosis slower.

6. **Telegram formatting and data gathering are tightly coupled**
   - `scripts/telegram_bot.py` mixes command routing, Binance/DB queries, formatting, and safety annotations.
   - Formatting is HTML-specific and should stay so, but reusable helpers could live in a focused module.

7. **Database class has too many responsibilities**
   - Session factory, Socket.IO updates, current coin state, ratio stats, regime logs, deposits, and bot_state helpers are all in one file.
   - This is a candidate for repository-style extraction, but only after tests cover existing behaviours.

8. **Dead/legacy code needs careful audit**
   - Legacy strategies and root backtest scripts may still be useful for comparison; do not remove until usage is checked via imports, README references, tests, and git history.

## Suggested target structure

Do not force a large rewrite. Move toward this structure incrementally:

```text
binance_trade_bot/
  config.py
  runtime/
    trading_loop.py              # future extraction from crypto_trading/auto_trader
  services/
    spot_service.py              # future extraction from BinanceAPIManager
    futures_service.py           # future extraction from FuturesManager
    transfer_policy.py           # wallet transfer amount/error policy
    notification_service.py
  strategy/
    regime.py                    # regime detection/hysteresis facade
    momentum.py                  # live strategy rules
  repositories/
    bot_state_repository.py
    deposit_repository.py
    regime_repository.py
  formatting/
    telegram_html.py
  research/
    regime_classifier.py
    bull_momentum_optimizer.py
    bear_futures_backtester.py
    sideways_chop_backtester.py
```

This is a direction, not a one-shot migration. Keep compatibility shims during moves.

## Incremental refactor sequence

### Phase 1 — Safety seams first

1. Extract futures transfer amount/error policy into a small pure helper.
   - Candidate file: `binance_trade_bot/futures_transfer.py` or `binance_trade_bot/services/transfer_policy.py`.
   - Tests should cover: full wallet balance, `maxWithdrawAmount`, zero `availableBalance`, Binance `-5013`, minimum transfer threshold, and retry/downsize policy.
   - Status: implemented in `binance_trade_bot/futures_transfer_policy.py` with pure unit tests in `tests/test_futures_transfer_policy.py`.

2. Add typed result objects for futures transfer attempts.
   - Avoid returning only `bool`; include attempted amount, status, error code, and retryability.
   - Preserve public behaviour at call sites initially by adapting result to bool.
   - Status: implemented with `TransferAttemptResult` / `TransferStatus`; `transfer_to_spot()` remains bool-compatible and delegates to `transfer_to_spot_result()`.

3. Keep high-frequency failure logs deduped/non-spamming.
   - API errors in repeated scout loops should log once or at debug unless action is required.
   - Status: notification handler already enforces exact-message dedupe and global rate limit; futures transfer `-5013` paths log `notification=False`.

### Phase 2 — Runtime strategy boundaries

4. Extract regime transition side effects from `momentum_strategy.py`.
   - Separate pure decision logic from side effects: sell spot, transfer wallet, open/close futures.
   - Keep tests around BEAR entry/exit safety before moving code.
   - Status: pure transition planner extracted in `binance_trade_bot/regime_transition_planner.py`; `MomentumStrategy` still owns side effects, with regression tests covering BEAR entry/exit call order.

5. Extract regime detection input/output contract.
   - Keep ADX/EMA calculation separate from stateful hysteresis and DB logging.
   - Status: complete through `indicators.py` and `regime_hysteresis.py`; live strategy calls pure helpers and only applies side effects after confirmed transition.

### Phase 3 — Persistence boundaries

6. Split `database.py` into repositories behind the existing `Database` facade.
   - Start with `bot_state` and `deposits` because they already have tests.
   - Keep `Database` methods as delegating compatibility wrappers.
   - Status: `BotStateRepository`, `DepositRepository`, `CoinRepository`, and `RegimeRepository` added behind `Database`; `Deposit.id` model fixed to match live INTEGER schema.

7. Lazy-load optional runtime integrations.
   - Continue avoiding eager socketio/eventlet imports for pure helper imports.
   - Status: Database keeps Socket.IO client lazy; import-safety tests cover pure helper imports.

### Phase 4 — Telegram maintainability

8. Move Telegram table/card formatting to `binance_trade_bot/formatting/telegram_html.py`.
   - Keep `scripts/telegram_bot.py` command names and responses stable.
   - Run local smoke tests and actual HTML send/delete validation after changes.
   - Status: implemented via pure `binance_trade_bot/formatting/telegram_html.py`; `scripts/telegram_bot.py` imports helpers while command handlers remain in place.

9. Move Telegram data collection into small service functions.
   - Separate command rendering from Binance/DB calls so output can be tested with fixtures.

### Phase 5 — Research organization

10. Create a clear research package or docs index.
    - Do not move scripts until command compatibility and tests are in place.
    - Add wrapper scripts if paths change.
    - Status: `docs/research-index.md` added; script paths intentionally preserved.

### Phase 6 — Documentation and developer experience

11. Add `docs/developer-guide.md` covering:
    - local setup
    - Docker workflow
    - live deployment workflow
    - required env/config values
    - testing commands
    - Telegram smoke testing
    - Binance API quirks
    - Status: complete in `docs/developer-guide.md`.

12. Add config validation documentation and eventually startup validation.
    - Start with warnings for invalid ranges before hard failures.
    - Status: non-fatal startup validation added in `binance_trade_bot/config_validation.py`; startup logs warnings/errors with `notification=False` and does not abort.

## Current urgent bug-fix plan: futures `-5013` transfer failure

Observed production symptom:

```text
Transfer from futures failed: APIError(code=-5013): Asset transfer failed: insufficient balance
```

Relevant live state after BEAR→SIDEWAYS transition:
- No open futures positions.
- No open futures orders.
- USDC futures wallet balance around `54.868`.
- `maxWithdrawAmount` around `54.699`.
- `availableBalance` reported as `0.00000000`.

Likely root cause:
- The bot tries to transfer the full computed futures balance once.
- Binance can reject that exact amount even when wallet/max-withdraw fields suggest funds are present, due to transfer precision, reserved dust, or a transient post-regime/accounting state.
- The current code treats this as a hard ERROR and notifies Telegram, but does not retry with a slightly smaller safe amount or degrade to a non-spamming retry state.

Safe fix direction:
1. Add a regression test around `transfer_to_spot()` handling Binance error `-5013`.
2. Implement a conservative downsize/retry policy for futures→spot transfers only:
   - floor transfer amount to 2 decimals for USDC,
   - leave a small dust buffer in futures wallet,
   - on `-5013`, refresh `maxWithdrawAmount` and retry once with the lower of refreshed withdrawable and `amount - dust_buffer`,
   - if still failing, log without notification spam and leave funds in futures for the next cycle/manual inspection.
3. Ensure deposit suppression only happens after confirmed transfer success.
4. Verify with tests and live logs.

## Test strategy

Prioritize behaviour tests, not implementation tests:
- Transfer policy pure unit tests.
- Futures manager regression tests with fake Binance client exceptions.
- Existing full test suite after each step.
- Live deployment smoke: Docker logs and a read-only futures account probe.

## Remaining risks

- Binance futures account fields differ by account mode and region; transfer logic should be conservative and observable.
- Moving files without compatibility wrappers could break scripts/systemd/Coolify paths.
- Eventlet/socketio remains a runtime dependency; import safety is improved but runtime deprecation remains tracked separately.
- Persistent volume config can drift from repo defaults; docs must call out which config is live.
