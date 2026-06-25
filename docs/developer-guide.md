# Developer Guide

This guide documents the current Binance trade bot architecture and safe workflows for future developers/agents.

## Project purpose

This repository runs an automated Binance trading bot using **USDC** as the bridge currency. It rotates spot positions in BULL/SIDEWAYS regimes and can open protected USDC-M futures shorts in BEAR regimes.

Production safety matters more than refactor purity. Prefer small PRs, tests first, and boring compatibility wrappers.

## Main entry points

| Entry point | Purpose |
|---|---|
| `binance_trade_bot/crypto_trading.py` | Live main bot startup inside Docker |
| `binance_trade_bot/__main__.py` | `python -m binance_trade_bot` entry |
| `binance_trade_bot/api_server.py` | Flask/API dashboard surface |
| `scripts/telegram_bot.py` | Telegram companion bot run by systemd |
| `Dockerfile`, `docker-entrypoint.sh` | Production container build/startup |
| `strategy_optimizer.py`, `optimize_momentum.py`, `scripts/research_*.py` | Research/backtest tools |

## Current structure

| Area | Files |
|---|---|
| Runtime config | `binance_trade_bot/config.py`, `user.cfg` |
| Spot API/trading | `binance_trade_bot/binance_api_manager.py` |
| Futures shorts | `binance_trade_bot/futures_manager.py` |
| Futures transfer policy | `binance_trade_bot/futures_transfer_policy.py` |
| Strategy logic | `binance_trade_bot/strategies/momentum_strategy.py` |
| Pure strategy helpers | `binance_trade_bot/indicators.py`, `binance_trade_bot/regime_hysteresis.py`, `binance_trade_bot/accounting.py` |
| Persistence facade | `binance_trade_bot/database.py` |
| Repository seams | `binance_trade_bot/repositories.py` |
| SQLAlchemy models | `binance_trade_bot/models/` |
| Notifications | `binance_trade_bot/notifications.py`, `binance_trade_bot/logger.py` |
| Tests | `tests/` |
| Docs | `docs/`, skill references under Hermes |

## Local setup

Use Python 3.11. This environment is PEP 668-managed, so prefer a virtualenv.

```bash
cd /home/lunafox/binance-trade-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r dev-requirements.txt
```

Do **not** commit `.env` files, API keys, Telegram bot tokens, or live config secrets.

## Required runtime configuration

Production reads config from the persistent Docker volume, not just the repo sample:

```text
/data/binance-bot-data/config/user.cfg
/data/coolify/applications/ig7sexqj6pnpnbtkn18odyfn/.env
/data/binance-bot-data/apprise.yml
```

Important values/quirks:

| Setting | Notes |
|---|---|
| bridge currency | USDC only; do not hardcode USDT |
| API secret env | `API_SECRET_KEY`, not `API_SECRET` |
| futures margin | `FUTURES_MARGIN_TYPE=CROSS` by default |
| regime confirmation | `REGIME_CONFIRMATION_CYCLES=3` default |
| notification guard | duplicate dedupe + 12/minute cap |
| Socket.IO dashboard updates | `SOCKETIO_UPDATES_ENABLED=no` by default; enable only when the legacy API dashboard sidecar is actually running |

## Testing

Run targeted tests first, then the full suite:

```bash
python3 -m py_compile <changed-files.py>
python3 -m pytest tests/<specific_test>.py -q
python3 -m pytest tests -q
git diff --check
```

Critical test groups:

| Area | Tests |
|---|---|
| Futures transfer/margin | `test_futures_transfer*.py`, `test_futures_margin_mode.py` |
| Regime safety | `test_regime_hysteresis.py`, `test_indicator_adx.py` |
| Deposit accounting | `test_deposit_accounting.py`, `test_database_repositories.py` |
| Research/backtests | `test_*optimizer*.py`, `test_*backtester*.py`, `test_strategy_acceptance_gates.py` |
| Telegram/shadow | `test_telegram_shadow.py`, `scripts/smoke_telegram_commands.py` |
| Import safety | `test_import_safety.py` |

After Telegram command output changes, run:

```bash
python3 scripts/smoke_telegram_commands.py
python3 scripts/smoke_telegram_commands.py --send --delete-delay 0.05
```

The send/delete smoke test validates actual Telegram HTML while avoiding chat spam.

## Docker deployment workflow

Coolify auto-build is currently unreliable, so deploy manually after runtime code changes:

```bash
sudo docker build --no-cache -t ig7sexqj6pnpnbtkn18odyfn:latest .
sudo docker stop ig7sexqj6pnpnbtkn18odyfn
sudo docker rm ig7sexqj6pnpnbtkn18odyfn
sudo docker run -d --name ig7sexqj6pnpnbtkn18odyfn \
  --restart unless-stopped \
  -v /data/binance-bot-data:/app/data \
  --env-file /data/coolify/applications/ig7sexqj6pnpnbtkn18odyfn/.env \
  ig7sexqj6pnpnbtkn18odyfn:latest
```

Verify immediately:

```bash
sudo docker ps --filter name=ig7sexqj6pnpnbtkn18odyfn
sudo docker logs --tail 120 ig7sexqj6pnpnbtkn18odyfn
sudo docker logs --since 5m ig7sexqj6pnpnbtkn18odyfn 2>&1 | grep -E 'ERROR|Traceback|Transfer from futures failed|-5013' || true
```

Expected healthy startup includes:

```text
FuturesManager initialized | Leverage: 1x | Margin mode: CROSS | ... | Open positions: 0
Strategy 'momentum' active
```

## Telegram companion bot workflow

The companion bot is a systemd service:

```bash
sudo systemctl status telegram-bot
sudo systemctl restart telegram-bot
```

Do not start it with `nohup` or a shell background process. Its env comes from `.env.telegram`, and DB path should point to `/data/binance-bot-data/crypto_trading.db`.

## Important architecture decisions

1. **USDC bridge only** — Binance EU does not support USDT for this setup.
2. **Research does not directly affect live trading** — research scripts emit artifacts and acceptance gates first.
3. **Futures transfer policy is pure** — use `futures_transfer_policy.py` for dust/rounding/retry decisions.
4. **Live callers remain stable** — preserve public method signatures unless a migration is explicitly planned.
5. **Database facade remains** — new repositories should sit behind `Database` compatibility methods first.
6. **Telegram uses HTML** — escape user/exchange-provided values and prefer compact `<pre>` tables/cards.

## Common safe refactor pattern

1. Inspect current callers and tests.
2. Add a narrow failing test for the seam you want.
3. Extract pure logic or a small repository/service behind the existing API.
4. Keep old call sites working.
5. Run targeted tests and full suite.
6. Merge a small PR.
7. Deploy if runtime code changed.
8. Verify logs.

## Current refactor status

See `docs/refactor-plan.md` for the active plan and status by phase.
