# Deployment Runbook — Migrate the Binance Trading Bot to systemd

**Addresses:** GitHub issue #91 (P1-HIGH: Bot running as root + no persistent logging)
**Branch:** `fix/systemd-persistent-logging`
**Maintained by:** bot-lead / devops-monitoring
**Last updated:** 2026-06-26

---

## ⚠️ APPROVAL GATE — READ THIS FIRST

> **This migration touches a LIVE, real-money trading bot. It MUST NOT be
> executed without explicit, written approval from the Boss (human approval
> authority).**

Execution requires coordinated downtime: the running bot is stopped, its data
directory is re-permissioned, and ownership of live state files changes from
`root` to `lunafox`. Do this during a low-risk window (e.g. holding a stable
coin, no open futures positions). A pre-flight checklist is in §4.

Before starting, confirm in `docs/CURRENT_STATE.md` that the risk controls
(circuit breaker, kill switch, futures stops) are active and that no open
futures positions would be left unprotected during the cutover.

---

## 1. What this migration changes

| Aspect | Before | After |
|--------|--------|-------|
| Process owner | `root` (bare `python -m binance_trade_bot`) | `lunafox` (systemd unit) |
| Lifecycle | Manual / ad-hoc restarts | `systemctl start/stop/restart`, auto-restart on crash |
| Logging | `logs/crypto_trading.log` (plain `FileHandler`, single file) | Rotating files in `/data/binance-bot-data/logs/` (10 MB × 5) **+** journald |
| Data dir ownership | `root:root` | `lunafox:lunafox` |
| Secrets | root-owned env file | env file readable only by `lunafox` |

## 2. Files introduced by the change

| File | Purpose |
|------|---------|
| `deploy/binance-trade-bot.service` | systemd unit (runs as `lunafox`, `Restart=on-failure`) |
| `binance_trade_bot/logger.py` | Rotating file handler, `LOG_DIR`-configurable |
| `docs/deployment/systemd-migration.md` | This runbook |

## 3. Reference: key paths

| Item | Path |
|------|------|
| Bot code | `/home/lunafox/binance-trade-bot/` |
| Virtualenv | `/home/lunafox/binance-trade-bot/.venv/` |
| Live config | `/data/binance-bot-data/config/user.cfg` |
| Env / secrets | `/data/binance-bot-data/config/binance-trader.env` |
| Database | `/data/binance-bot-data/crypto_trading.db` |
| **New log dir** | `/data/binance-bot-data/logs/` |
| Unit install target | `/etc/systemd/system/binance-trade-bot.service` |

---

## 4. Pre-flight checklist (complete before cutover)

- [ ] **Boss approval recorded** (date / approval reference in the decision log).
- [ ] Reviewed and merged PR `fix/systemd-persistent-logging` to `master`.
- [ ] Working tree on the deploy host checked out at the merged commit.
- [ ] `sudo` access available (permission migration + unit install need root).
- [ ] Telegram kill switch reachable (`/status`, `/balance`) as a fallback stop.
- [ ] No open futures positions, **or** all open positions have server-side
      STOP_MARKET / trailing stops that remain active during downtime.
- [ ] Recorded current PID and a backup of the database:
      ```bash
      ps aux | grep binance_trade_bot | grep -v grep   # note the PID
      cp /data/binance-bot-data/crypto_trading.db \
         /data/binance-bot-data/crypto_trading.db.pre-systemd-$(date +%s)
      ```

---

## 5. Step-by-step migration

> All `sudo` commands require root. Non-`sudo` commands run as `lunafox`.
> Estimated total downtime: **2–5 minutes**.

### Step 1 — Stop the running bot

```bash
# Confirm the current root-owned process
ps aux | grep binance_trade_bot | grep -v grep

# Stop it. The bot traps SIGINT and unwinds cleanly.
sudo kill -INT <PID>

# Wait ~5s and confirm it is gone
ps aux | grep binance_trade_bot | grep -v grep
```

If a Docker container is also running, stop it too (`docker stop <id>`) to
avoid a singleton-lock (`bot.pid` / flock) conflict.

### Step 2 — Migrate data-directory ownership (root → lunafox)

The entire `/data/binance-bot-data` tree is currently `root`-owned. The systemd
unit runs as `lunafox`, so ownership **must** change or the bot will fail to
write its database, PID file, and logs.

```bash
sudo chown -R lunafox:lunafox /data/binance-bot-data

# Create and own the new persistent log directory
sudo mkdir -p /data/binance-bot-data/logs
sudo chown -R lunafox:lunafox /data/binance-bot-data/logs

# Lock down the secrets file (API keys) to lunafox only
sudo chown lunafox:lunafox /data/binance-bot-data/config/binance-trader.env
sudo chmod 600 /data/binance-bot-data/config/binance-trader.env

# Verify
ls -la /data/binance-bot-data/
ls -la /data/binance-bot-data/config/binance-trader.env
```

### Step 3 — Ensure the `data/` symlink and live config symlinks exist

The bot resolves `data/crypto_trading.db`, `user.cfg`, and
`supported_coin_list` relative to its working directory.

```bash
cd /home/lunafox/binance-trade-bot

# Point the relative "data/" dir at the live data dir
rm -rf data
ln -s /data/binance-bot-data data

# Live config overrides the git template
ln -sf /data/binance-bot-data/config/user.cfg user.cfg
ln -sf /data/binance-bot-data/config/supported_coin_list supported_coin_list

# Verify the symlinks resolve
ls -la data user.cfg supported_coin_list
```

### Step 4 — Install the systemd unit

```bash
sudo cp deploy/binance-trade-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
```

> **Note on the service name:** the unit is `binance-trade-bot.service`.
> The older `binance-trader.service` file at the repo root is superseded — do
> not install or enable both. If `binance-trader.service` was ever installed,
> disable it first:
> ```bash
> sudo systemctl disable --now binance-trader.service 2>/dev/null || true
> sudo rm -f /etc/systemd/system/binance-trader.service
> sudo systemctl daemon-reload
> ```

### Step 5 — Start the bot under systemd

```bash
sudo systemctl enable --now binance-trade-bot.service

# Confirm it is running as lunafox (NOT root)
systemctl status binance-trade-bot.service
ps -o user=,pid=,cmd= -p "$(systemctl show -p MainPID --value binance-trade-bot)"
```

The `ps` line must show `lunafox`, not `root`.

### Step 6 — Verify logging (both sinks)

```bash
# A) Journald (stdout/stderr)
journalctl -u binance-trade-bot -f

# B) Rotating file logs at the new persistent location
ls -la /data/binance-bot-data/logs/
tail -f /data/binance-bot-data/logs/crypto_trading.log
```

You should see a non-empty `crypto_trading.log` (and, over time, rotation to
`.1` … `.5` as it crosses 10 MB). If the file is absent or owned by root,
re-check Step 2.

### Step 7 — Confirm trading behaviour

- Send `/status` and `/balance` to the Telegram bot.
- Watch one full scout cycle in the logs for normal trade evaluation.
- Confirm position reconciliation ran on startup (look for
  `Reconciliation OK` or a mismatch warning).

---

## 6. Rollback

If the bot misbehaves under systemd:

```bash
# Stop the systemd-managed instance
sudo systemctl disable --now binance-trade-bot.service

# Restart manually as before (still as root for parity with the old setup)
cd /home/lunafox/binance-trade-bot
source .venv/bin/activate
nohup python -m binance_trade_bot >/tmp/bot.out 2>&1 &
```

> ⚠️ Rollback re-introduces the root-execution and no-persistent-log issues
> from #91. Use only as a temporary emergency measure; re-attempt the
> migration as soon as the root cause is fixed.

To revert the data-directory ownership (only if absolutely necessary):
```bash
sudo chown -R root:root /data/binance-bot-data
```

---

## 7. Post-migration (day-2)

- [ ] Add `journalctl -u binance-trade-bot` to the daily health check in
      `docs/runbook.md`.
- [ ] After ~24 h, confirm log rotation produced a `.1` backup at the expected
      size, or note that the bot is under the 10 MB threshold.
- [ ] Update `docs/CURRENT_STATE.md` to mark #91 as resolved and change the
      deployment row from "bare metal (root process)" to "systemd (lunafox)".
- [ ] Update `docs/runbook.md` quick-reference log path to
      `/data/binance-bot-data/logs/crypto_trading.log`.

---

## 8. Things the Boss must review before approval

1. **Running as `lunafox`, not root** — the unit sets `User=lunafox` /
   `Group=lunafox` and the hardening directives `NoNewPrivileges`,
   `ProtectSystem=full`, `ProtectHome=read-only`, `PrivateTmp`.
2. **Restart policy** — `Restart=on-failure` with `RestartSec=10` and a
   `StartLimitIntervalSec=300` / `StartLimitBurst=5` guard so a crash loop
   can't hammer the exchange API. (Note: this differs from the older
   `binance-trader.service` draft, which used `Restart=always`.)
3. **Persistent rotating logs** — `logger.py` now uses `RotatingFileHandler`
   (10 MB, 5 backups) writing to `LOG_DIR=/data/binance-bot-data/logs/` under
   systemd, with a safe fallback to `logs/` for local/dev runs. Both file and
   console (journald) handlers remain active.
4. **Permission migration** — Step 2 changes ownership of the entire live data
   tree from `root` to `lunafox`. This is required for the unprivileged unit to
   function; the database backup in §4 protects against mistakes.
5. **Downtime** — the bot is stopped for ~2–5 minutes during cutover. Confirm
   there are no open futures positions lacking server-side protection.
6. **Singleton lock** — only one instance runs at a time (flock on `bot.pid`).
   Any stray root process or Docker container must be stopped first (Step 1).

*Signed off required from: The Boss. Execution blocked until approved.*
