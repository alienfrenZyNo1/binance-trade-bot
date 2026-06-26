# Recovery Runbook — Binance Trading Bot

**Maintained by:** devops-monitoring / bot-lead
**Last updated:** 2026-06-26

---

## Quick Reference

| What | How |
|------|-----|
| **Emergency stop** | Telegram: `/kill confirm` |
| **Check bot status** | `systemctl status binance-trader` (or `ps aux \| grep binance_trade_bot`) |
| **View logs** | `journalctl -u binance-trader -f` (systemd) or `tail -f logs/trading.log` |
| **Check positions** | Telegram: `/balance` or `/status` |
| **Database location** | `/data/binance-bot-data/crypto_trading.db` |
| **Config location** | `/data/binance-bot-data/config/user.cfg` |

---

## Emergency Procedures

### 1. Kill Switch (Immediate Stop)

**Via Telegram:**
1. Send `/kill` to the bot's Telegram chat
2. Review the summary (shows positions, balances)
3. Send `/kill confirm` to execute
4. Bot closes all futures positions, transfers USDC back to spot
5. ⚠️ **Bot will resume trading on next cycle unless process is killed**

**Via Process Kill:**
```bash
# Find the bot process
ps aux | grep "binance_trade_bot" | grep -v grep
# Kill it
kill -TERM <PID>
# Wait 5 seconds, verify it's gone
ps aux | grep "binance_trade_bot" | grep -v grep
```

**Via systemd (when service is installed):**
```bash
sudo systemctl stop binance-trader
```

### 2. Bot Crash Recovery

The bot has built-in recovery:
1. **Position reconciliation:** On startup, compares DB state with exchange state
2. **Futures recovery:** Reconstructs open positions from exchange, re-places server stops
3. **Orphan cleanup:** Cancels stale algo orders

**To restart after crash:**
```bash
# systemd (when installed)
sudo systemctl start binance-trader

# manual
cd /home/lunafox/binance-trade-bot
source .venv/bin/activate
python -m binance_trade_bot &
```

### 3. Database Corruption

```bash
# Stop bot first!
sudo systemctl stop binance-trader  # or kill process

# Backup corrupted DB
cp /data/binance-bot-data/crypto_trading.db /data/binance-bot-data/crypto_trading.db.corrupt.$(date +%s)

# Check integrity
sqlite3 /data/binance-bot-data/crypto_trading.db "PRAGMA integrity_check;"

# If corrupt, restore from backup (if available)
# The bot auto-backs up to /data/crypto_trading.db on startup
cp /data/crypto_trading.db /data/binance-bot-data/crypto_trading.db

# Restart
sudo systemctl start binance-trader
```

### 4. API Key Compromise

1. **Immediately:** Log into Binance → API Management → Delete the compromised key
2. Create new API key with same permissions (Spot trading + Futures trading, NO withdrawals)
3. Update environment variables:
   ```bash
   # If using systemd override:
   sudo systemctl edit binance-trader
   # Update Environment=API_KEY=... and Environment=API_SECRET_KEY=...
   sudo systemctl restart binance-trader
   ```
4. Check for unauthorized trades in account history

---

## Health Checks

### Daily Check
```bash
# 1. Is bot running?
systemctl is-active binance-trader

# 2. Recent log errors?
journalctl -u binance-trader --since "1 hour ago" | grep -i "error\|exception\|fail" | tail -20

# 3. Database healthy?
sqlite3 /data/binance-bot-data/crypto_trading.db "PRAGMA integrity_check;"

# 4. Check Telegram bot is responsive
# Send /status to Telegram bot
```

### Pre-Restart Checklist
- [ ] Verify no open futures positions (or they have server-side stops)
- [ ] Check DB integrity
- [ ] Verify config hasn't drifted
- [ ] Check API key validity
- [ ] Review recent error logs

---

## Config Management

**CRITICAL:** The live config is at `/data/binance-bot-data/config/user.cfg`, NOT the git `user.cfg`.

The git `user.cfg` is a template/reference. Live config takes precedence.

To change live config:
1. Edit `/data/binance-bot-data/config/user.cfg`
2. Restart bot: `sudo systemctl restart binance-trader`
3. Verify change took effect via Telegram `/status`

---

## Monitoring Endpoints

| Metric | How to Check |
|--------|-------------|
| Bot uptime | `systemctl status binance-trader` |
| Current position | Telegram `/status` |
| Account balance | Telegram `/balance` |
| Trade history | Telegram `/history` |
| Current regime | Telegram `/regime` |
| Circuit breaker | Check logs for "Circuit breaker" messages |
| Kill switch active | Check if process is running |

---

## Data Locations

| Item | Path |
|------|------|
| Bot code | `/home/lunafox/binance-trade-bot/` |
| Virtual env | `/home/lunafox/binance-trade-bot/.venv/` |
| Live config | `/data/binance-bot-data/config/user.cfg` |
| Database | `/data/binance-bot-data/crypto_trading.db` |
| Log files | `/home/lunafox/binance-trade-bot/logs/` |
| PID lock | `/tmp/binance_trade_bot.pid` |
| Telegram bot service | `/etc/systemd/system/telegram-bot.service` |
| Trading bot service | `/etc/systemd/system/binance-trader.service` (pending install) |

---

*This runbook should be updated whenever deployment configuration changes.*
