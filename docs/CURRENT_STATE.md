# CURRENT_STATE.md — Binance Trading Bot Project Audit
**Produced by:** The Boss (Human Approval Authority)
**Date:** 2026-06-26
**Status:** INITIAL AUDIT COMPLETE — CRITICAL SAFETY GAPS IDENTIFIED

---

## 1. ARCHITECTURE SUMMARY

| Component | Technology | Status |
|-----------|-----------|--------|
| Framework | Forked edeng23/binance-trade-bot (custom Python, python-binance) | Active |
| Strategy Engine | Momentum rotation (bull) + Futures short (bear) | Active |
| Exchange API | python-binance==1.0.37 (spot + USDC-M futures) | Active |
| Database | SQLite via SQLAlchemy 1.4 | **EMPTY (0 bytes)** |
| Deployment | Bare metal (root process), systemd for Telegram sidecar | Active |
| CI/CD | GitHub Actions (lint + DockerHub push) | Configured |
| Monitoring | Telegram bot companion (@binance1986_bot) | Active |
| Regime Detection | ADX(14) + EMA(12/26) on SOL/USDC 1h klines | Active |
| Futures | USDC-M perpetuals, 1x leverage, CROSS margin | **ACTIVE — LIVE** |

**Source lines of code:** ~17,600 total (1,656 strategy/logic, 2,639 Telegram bot, rest tests/scripts)

---

## 2. CURRENT TRADING MODE

### 🚨 LIVE TRADING — NOT TESTNET
- **testnet = false** in user.cfg
- Bot running as **root** process (PID 3032124), started 2026-06-26 20:17 UTC
- **~$62 capital** (USDC bridge)
- Currently holding TIA as `current_coin`
- Futures shorting is **ENABLED** during bear regime

### Regime States
The bot classifies markets into 4 regimes:
- **BULL** → Spot momentum rotation (buy outperformers)
- **SIDEWAYS** → Reduced position spot rotation
- **BEAR** → Sell spot, transfer USDC to futures, short worst performers
- **STORMY** → Defensive (high volatility, minimal exposure)

---

## 3. CURRENT STRATEGY

### Spot Momentum Rotation (Bull/Sideways)
- Lookback: 18h performance measurement
- Min edge: 8% outperformance to trigger rotation
- Confirmation: 3 consecutive scout cycles
- Anti-churn: 24h block on re-buying sold coins
- Cooldown: 2h between trades
- Trailing stop: 15% from peak
- RSI filter: Skip if RSI > 68
- Maker orders: Enabled (0.025% vs 0.075% taker)
- Dynamic sizing: 70% in bear, 90% in sideways

### Futures Short (Bear)
- 1x leverage only
- CROSS margin (Binance rejects ISOLATED on this account)
- Max margin: 50% of USDC
- Stop loss: 15% hard stop (server-side STOP_MARKET)
- Trailing stop: 10% after +3% profit
- Funding rate guard: Skip if funding > 0.01%
- Short excludes: NEAR, TIA (poor backtested performance)

### Backtest Claims
- 6-month P&L: +79% (vs TIA buy & hold: -14%)
- Sharpe: 3.85 (suspiciously high)
- Trades: ~0.25/day (selective)
- **Max drawdown: 48%** ← UNACCEPTABLE

---

## 4. CURRENT RISK SETTINGS

| Safety Control | Configured | Enabled | Assessment |
|---------------|-----------|---------|------------|
| Stop loss (futures) | 15% server-side STOP_MARKET | ✅ Yes | Adequate |
| Trailing stop (spot) | 15% from peak | ✅ Yes | Adequate |
| Trailing stop (futures) | 10% after +3% profit | ✅ Yes | Adequate |
| Funding rate guard | 0.01% max | ✅ Yes | Adequate |
| Portfolio circuit breaker | Code exists | ❌ **DISABLED** | **CRITICAL** |
| Daily max loss limit | Code exists | ❌ **DISABLED** | **CRITICAL** |
| Weekly max drawdown | Code exists | ❌ **DISABLED** | **CRITICAL** |
| Canary capital guard | Code exists | ❌ **DISABLED** | **HIGH RISK** |
| Kill switch (Telegram) | `/kill confirm` | ✅ Yes | Manual only |
| Emergency stop | `/kill confirm` | ✅ Yes | Futures only |
| Max open positions | 1 (single-position model) | ✅ Yes | Architectural |
| Leverage cap | 1x | ✅ Yes | Within policy |
| Margin type | CROSS | ⚠️ Forced | **Risks entire wallet** |
| Anti-churn filter | 24h | ✅ Yes | Adequate |
| Trade cooldown | 2h | ✅ Yes | Adequate |
| Position reconciliation | On startup | ✅ Yes | Adequate |
| Restart recovery | DB state persistence | ⚠️ **DB EMPTY** | **BROKEN** |
| Singleton lock (flock) | bot.pid | ✅ Yes | Adequate |

---

## 5. CURRENT DEPLOYMENT SETUP

- **Host:** Linux 7.0.0-22-generic (87.106.150.252)
- **Bot process:** `python -m binance_trade_bot` running as **root** (security concern)
- **Telegram bot:** systemd service `telegram-bot.service` running as lunafox
- **Database:** `/home/lunafox/binance-trade-bot/data/crypto_trading.db` — **0 bytes, empty**
- **Docker:** docker-compose.yml exists but **not used** — bot runs bare-metal
- **No Coolify deployment** for the trading bot itself
- **API keys:** Loaded from environment variables (not in user.cfg, not in .env file)
- **No log files** present in logs/ directory

---

## 6. CURRENT TEST COVERAGE

### Test Suite: 32 test files exist

| Category | Files | Status |
|----------|-------|--------|
| Regime detection | 8 | Import errors (missing sqlalchemy) |
| Strategy/optimization | 6 | Import errors |
| Futures/execution | 5 | Import errors |
| Risk/circuit breaker | 2 | Import errors |
| Telegram/UI | 3 | Likely passing |
| Database/persistence | 2 | Collection error |
| Config/validation | 3 | Likely passing |
| Indicators/filters | 3 | Likely passing |

### 🚨 TEST SUITE IS BROKEN
- **5 collection errors** due to `ModuleNotFoundError: No module named 'sqlalchemy'`
- Tests cannot run because the venv is missing a core dependency
- Only pure-Python tests (indicators, circuit breaker helpers) can execute
- **No integration tests can verify live behaviour**

---

## 7. CURRENT KNOWN ISSUES

### From Research Journal (Session 001, 2026-06-23)

| ID | Issue | Severity | Status |
|----|-------|----------|--------|
| E001 | Trade frequency 100x higher than backtest prediction (18 trades/30h vs 0.25/day) | **CRITICAL** | Observed, not fixed |
| E002 | Backtest train -30%, OOS +65% — suspicious inversion | **CRITICAL** | Not fixed |
| E003 | CROSS margin risks entire futures wallet | **HIGH** | Accepted (no alternative) |
| BL-001 | Excessive trade frequency destroying capital via fees | **CRITICAL** | Not investigated |
| BL-002 | Backtest methodology needs revalidation | **CRITICAL** | Not done |
| BL-003 | Regime uses SOL only, no BTC confirmation | **MEDIUM** | Config exists, unclear if active |
| BL-004 | No volatility-scaled position sizing | **MEDIUM** | Not done |
| BL-005 | Volatility metric always 0.0 | **LOW** | Not fixed |

---

## 8. MISSING SAFETY CONTROLS — PRIORITY RANKED

### 🔴 P0 — CRITICAL (Fix Before Any Further Trading)

1. **No daily max loss limit** — The portfolio circuit breaker exists in code but is DISABLED. Without this, the bot can lose unlimited capital in a single day. **This must be enabled immediately.**

2. **Database is empty (0 bytes)** — The bot is running but its state database is empty. This means trade state, regime history, scout history, and position tracking may not be persisting. **Restart recovery is compromised.**

3. **No runtime equity monitoring** — Even with the circuit breaker code present, there is no evidence the bot tracks daily/weekly starting equity to feed into the breaker logic.

4. **Test suite is broken** — Cannot verify any code changes are safe. The venv is missing sqlalchemy. **All future development is blocked until tests pass.**

### 🟡 P1 — HIGH (Fix Within 48h)

5. **Backtest reliability is suspect** — Train/OOS inversion suggests lookahead bias or overfitting. The +79% return and 3.85 Sharpe are not trustworthy until revalidated.

6. **48% max drawdown** — Even if the backtest is correct, a 48% drawdown on live capital is unacceptable. This needs to be reduced dramatically.

7. **Bot running as root** — Security risk. API keys accessible to any process on the server.

8. **No log files** — The logs/ directory is empty. No persistent logging of trades, errors, or decisions.

9. **42+ unmerged Snyk security branches** — Vulnerability fixes sitting in branches, never merged into master.

### 🟢 P2 — MEDIUM (Fix Within 1 Week)

10. **No alerting on abnormal events** — Telegram bot exists but no automated alerts for errors, API failures, or risk threshold approaches.

11. **Canary mode disabled** — No capital caps for experiments. The bot trades with 100% of the ~$62.

12. **CROSS margin** — Entire futures wallet is at risk on any short position. ISOLATED margin is rejected by Binance for this account type, but this constraint needs documentation and mitigation.

13. **No max drawdown auto-halt** — The bot should automatically stop trading if drawdown exceeds a threshold.

---

## 9. RECOMMENDED NEXT STEPS (PRIORITY ORDER)

### Phase 0: Stabilize (0-24h) — NO STRATEGY CHANGES
1. **Enable portfolio circuit breaker** with conservative thresholds (3% daily, 8% weekly)
2. **Investigate empty database** — determine if bot is writing to a different DB path
3. **Fix test suite** — install missing sqlalchemy, get all tests passing
4. **Enable logging** — ensure logs go to persistent files
5. **Document API key permissions** — confirm no withdrawal permission

### Phase 1: Safety Hardening (24-72h)
6. **Revalidate backtest** — walk-forward with proper splits, fees, slippage, funding
7. **Investigate excessive trade frequency** — root cause of 18 trades/30h
8. **Merge critical Snyk security fixes**
9. **Set up monitoring alerts** — Telegram notifications for errors, API failures
10. **Move bot off root** — run as dedicated user

### Phase 2: Strategy Improvement (Week 2+)
11. **Add BTC trend confirmation** to regime detection
12. **Reduce drawdown** — tighter stops, volatility-scaled sizing
13. **Improve sideways market handling**
14. **Add fee-aware trade decision logic**
15. **Research ADX/chop/squeeze filters**

### Phase 3: Expansion (Week 4+, only after Phase 0-2 complete)
16. **Explore additional strategies** (mean reversion, grid)
17. **Multi-position portfolio** (beyond single-coin model)
18. **Testnet futures validation** (if futures expansion approved)

---

## 10. RISK APPETITE STATEMENT (INITIAL)

```yaml
# config/risk-appetite.yaml
risk_appetite:
  max_daily_loss_pct: 3.0        # 3% of total capital per day
  max_total_drawdown_pct: 10.0   # Full halt at 10% drawdown from peak
  max_position_size_pct: 100.0   # Single-position model (architectural)
  max_concurrent_positions: 1    # Architectural constraint
  max_correlation_between_strategies: N/A  # Single strategy
  allowed_instruments:
    spot: true
    futures: true                # ALREADY ACTIVE — grandfathered
    margin: false
    leverage_max: 1              # 1x only
  futures_max_margin_pct: 0.50   # 50% max into futures
  kill_switch: "/kill confirm"   # Telegram command
  daily_loss_circuit_breaker: REQUIRED (currently DISABLED)
```

---

## 11. TEAM AUDIT ASSIGNMENTS

The following audits are required before any strategy optimization work begins:

| Agent | Assignment | Priority | Deadline |
|-------|-----------|----------|----------|
| **risk-agent** | Audit all live risk controls, verify circuit breaker, assess CROSS margin exposure | P0 | Immediate |
| **execution-agent** | Audit Binance API integration, order management, error handling, restart recovery | P0 | Immediate |
| **qa-agent** | Fix broken test suite, establish baseline coverage, identify test gaps | P0 | Immediate |
| **backtest-agent** | Revalidate backtest methodology, check for lookahead bias, add fees/slippage | P1 | 48h |
| **devops-monitoring** | Audit deployment, logging, alerting, backup, uptime, emergency procedures | P0 | Immediate |
| **docs-journal-agent** | Create/update project journal with this audit, establish decision log | P1 | 48h |
| **final-reviewer** | Review this current-state report after all audits complete | P1 | After audits |
| **bot-lead** | Create 30-day roadmap from this report | P1 | 48h |

---

**DECISION:** No strategy changes, no new live deployments, and no risk parameter changes until:
1. P0 items are resolved
2. Test suite passes
3. Risk-agent and execution-agent complete their audits
4. This report is reviewed by final-reviewer

*Signed,*
*The Boss — Human Approval Authority*
