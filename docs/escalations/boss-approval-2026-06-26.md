# Escalation to The Boss — Pending Approvals

**From:** Bot-Lead
**Date:** 2026-06-26
**Status:** AWAITING DECISION

---

## Summary

Three items require your approval before execution. All are safety improvements that REDUCE risk, but they involve live trading behavior changes or risk parameter changes per the approval envelope in risk-appetite.yaml.

---

## ESCALATION 1: Circuit Breaker Thresholds (Issue #88)

**Current:** 5% daily / 12% weekly
**Requested:** 3% daily / 8% weekly (per risk-appetite.yaml)

**Justification:** Your risk-appetite.yaml explicitly mandates:
```yaml
daily_max_drawdown_pct: 3.0
weekly_max_drawdown_pct: 8.0
```

The live config at `/data/binance-bot-data/config/user.cfg` currently has 5%/12% — more permissive than your stated appetite. This is a config drift issue.

**Risk of approval:** Lower thresholds may trigger the breaker more often, pausing trading during volatile periods. This is the intended safety behavior.

**Risk of NOT approving:** The bot can lose up to 5% in a day before any automatic halt. With $62 capital, that's $3.10/day potential loss vs $1.86 at 3%.

**Implementation:** Change two values in `/data/binance-bot-data/config/user.cfg`, restart bot. Zero code changes.

**My recommendation:** APPROVE — aligns live config with your stated risk appetite.

---

## ESCALATION 2: Confirmation Timing Fix (Issue #94)

**Current:** Bot confirms rotation signals in 3 seconds
**Requested:** Confirm in 3600 seconds (1 hour) minimum

**Justification:** The backtest audit found a 55x trade frequency discrepancy:
- Backtest assumes hourly bars → 3 confirmation cycles = 3 hours
- Live bot with `scout_sleep_time=1` → 3 confirmation cycles = 3 seconds

This means the bot is trading 55x more often than the backtest validated. The code in `config.py:412-417` explicitly warns about this but the fix was never enabled in the live config.

**Also requested:** Increase `scout_sleep_time` from 1 → 60 seconds (reduce API calls from 3600/hour to 60/hour).

**Risk of approval:** Trade frequency will drop dramatically (~30x fewer trades). The bot will be less active but each trade will be more deliberate. This aligns with the backtest's assumptions.

**Risk of NOT approving:** The bot continues overtrading at 14+ trades/day, racking up fees on a $62 account where each trade costs ~$0.09 in fees (0.15% round-trip). At 14 trades/day, that's $1.26/day in fees alone — 2% daily erosion from fees.

**Implementation:** Set `confirmation_time_enabled=yes`, `sideways_confirmation_min_seconds=3600`, `bull_confirmation_min_seconds=3600`, `scout_sleep_time=60` in live config. Restart bot.

**My recommendation:** APPROVE — this is a critical fix. The current overtrading is destroying capital through fees.

---

## ESCALATION 3: Systemd Migration (Issue #91)

**Current:** Bot runs as root (PID 3032124), no process management
**Requested:** Migrate to systemd service as `lunafox` user

**Justification:** The bot running as root is a security vulnerability. The systemd service file already exists in the repo (`binance-trader.service`) with proper hardening.

**Risk of approval:** Requires a brief bot restart (~30 seconds of downtime). Position reconciliation handles open positions automatically on restart. Server-side futures stops remain active during downtime.

**Risk of NOT approving:** If the root process crashes, it won't auto-restart. No structured logging. Security exposure from running as root.

**Implementation:**
1. Install service file to `/etc/systemd/system/`
2. Create environment override with API keys
3. Stop root process
4. Start systemd service
5. Verify bot comes back up and reconciles positions

**My recommendation:** APPROVE — operational safety improvement. Minimal downtime risk.

---

## Additional Note: Snyk Branches (Issue #93)

**No approval needed** — but important to note: All 82 Snyk "security fix" branches are counterproductive. They DOWNGRADE dependencies (python-binance 1.0.37→1.0.12) which would BREAK the bot. I recommend closing all of them and running a proper `pip-audit` instead.

---

*Bot-Lead is ready to execute all three changes immediately upon approval.*
