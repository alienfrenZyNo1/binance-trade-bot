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

---

## 2. CURRENT TRADING MODE

### LIVE TRADING — NOT TESTNET
- **testnet = false** in user.cfg
- Bot running as **root** process (PID 3032124), started 2026-06-26 20:17 UTC
- **~$62 capital** (USDC bridge)
- Currently holding TIA as `current_coin`
- Futures shorting is **ENABLED** during bear regime

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

### Futures Short (Bear)
- 1x leverage only, CROSS margin
- Max margin: 50% of USDC
- Stop loss: 15% server-side STOP_MARKET
- Trailing stop: 10% after +3% profit
- Funding rate guard: Skip if funding > 0.01%

### Backtest Claims (SUSPECT)
- 6-month P&L: +79% — Train -30%, OOS +65% (suspicious inversion)
- Sharpe: 3.85 (unrealistically high)
- Max drawdown: 48% (UNACCEPTABLE)
- No slippage, no funding costs modeled

---

## 4. CURRENT RISK SETTINGS

| Safety Control | Status |
|---------------|--------|
| Stop loss (futures) | 15% server-side STOP_MARKET |
| Trailing stop (spot) | 15% from peak |
| Portfolio circuit breaker | **DISABLED — CRITICAL** |
| Daily max loss limit | **NONE ACTIVE — CRITICAL** |
| Weekly max drawdown | **NONE ACTIVE — CRITICAL** |
| Canary capital guard | Disabled |
| Kill switch (Telegram) | `/kill confirm` (futures only) |
| Leverage cap | 1x |
| CROSS margin | Forced (risks entire wallet) |
| Restart recovery | DB state (DB IS EMPTY) |

---

## 5. MISSING SAFETY CONTROLS

### P0 — CRITICAL
1. No daily max loss limit (circuit breaker disabled)
2. Database is empty (0 bytes) — state persistence broken
3. Test suite is broken (missing sqlalchemy)
4. No persistent log files

### P1 — HIGH
5. Backtest reliability suspect (train/OOS inversion)
6. 48% max drawdown unacceptable
7. Bot running as root
8. 42+ unmerged Snyk security branches

### P2 — MEDIUM
9. No automated alerting
10. Canary mode disabled
11. CROSS margin risks entire wallet

---

## 6. RECOMMENDED NEXT STEPS

### Phase 0: Stabilize (0-24h)
1. Enable portfolio circuit breaker (3% daily, 8% weekly)
2. Investigate empty database
3. Fix test suite
4. Enable persistent logging

### Phase 1: Validate (24-72h)
5. Revalidate backtest with fees/slippage/funding
6. Root cause excessive trade frequency
7. Merge critical Snyk fixes

### Phase 2: Improve (Week 2+)
8. Strategy improvements (BTC confirmation, ADX filters)
9. Monitoring and alerting
10. Documentation

---

**DECISION:** No strategy changes until P0 items resolved, tests pass, and audits complete.

*Signed, The Boss — Human Approval Authority*