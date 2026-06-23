# Quantitative Research Journal
## Trading System: Binance Momentum Rotation + Futures Short

---

## SESSION 001 — 2026-06-23 (Initial Assessment)

### System Snapshot
- **Capital:** ~$62 total ($57.69 futures wallet, $28.24 margin in use, dust on spot)
- **Strategy:** Momentum rotation (bull) + USDC-M futures short (bear)
- **Regime:** BEAR (ADX ~50, EMA short < EMA long on SOL)
- **Live Position:** 339 ENA short @ $0.0852, mark $0.0863, P&L -1.2% (-$0.36)
- **Total Live Trades:** 18 (all within ~30 hours on June 22-23)
- **Coins Traded Live:** AAVE→TIA→APT→ENA→TIA→ENA→TIA→JUP

---

### 1. CURRENT ASSESSMENT

#### Strengths
1. **Regime detection works** — ADX + EMA on SOL correctly identifies bear market; bot is sitting in USDC and managing futures short
2. **Futures infrastructure operational** — short opened, managed, stop-loss active
3. **Momentum edge validated in backtest** — 8% min edge + 18h lookback reduces churning vs original mean-reversion
4. **Trailing stop protects profits** — 15% trailing on spot, 10% on futures
5. **Anti-churn filter prevents re-buying sold coins** — 24h block

#### Weaknesses (Critical)
1. **48% max drawdown in backtest** — unacceptable for any real strategy; implies catastrophic risk
2. **Fees destroy 11-14% of capital** — on $62, $8 in fees is catastrophic drag
3. **18 trades in 30 hours** = churning behavior (target should be ~0.25/day = 1 trade per 4 days)
4. **Backtest +79% is overstated** — train P&L was -30% (the strategy LOST money in-sample), OOS +65% is suspicious
5. **Single coin concentration** — entire portfolio in one position at all times
6. **No position sizing** — always 100% in or 100% out, no fractional exposure
7. **Regime detection uses only SOL** — one coin as proxy for entire market is fragile
8. **avg_volatility = 0.0 in all regime logs** — volatility metric is broken/not computed
9. **btc_correlation = None** — correlation metric not computed
10. **Futures position uses CROSS margin** — Binance rejected ISOLATED, entire wallet at risk
11. **Short selection is simplistic** — picks worst performer, no mean-reversion check, no RSI, no support/resistance

---

### 2. KEY FINDINGS

#### Finding A: Trade Frequency is 100x Too High
- Backtest target: 46 trades over 6 months = ~0.25/day
- Live result: 18 trades in 30 hours = ~14/day
- **Root cause:** The anti-churn and cooldown parameters may not be applied correctly, OR the momentum edge (8%) is being triggered by volatile intraday swings that the backtest doesn't capture (backtest uses daily/hourly close-to-close)

#### Finding B: Backtest Methodology is Flawed
- Train P&L = -30% but OOS = +65% — this is backwards from typical overfitting
- Likely cause: the "out-of-sample" period coincided with a massive bull run where ANY strategy profits
- Sharpe ratio of 3.9 is unrealistic (hedge funds aim for 1-2)
- No slippage modeling, no funding rate costs on futures, no look-ahead bias check

#### Finding C: Regime Detection Has Blind Spots
- Uses SOL 1h klines only — no BTC confirmation
- ADX threshold = 25 (default) but regime is "bear" at ADX 37-50 which is very strong trend
- avg_volatility and btc_correlation are logged as 0.0/None — these features exist in the schema but aren't computed
- No sideways/choppy detection beyond "ADX < threshold = sideways"

#### Finding D: Futures Short Strategy is Primitive
- Entry: picks worst-performing coin (most negative 18h momentum)
- No entry timing (just market order whenever)
- No support/resistance analysis
- No volume confirmation
- Exit: 15% hard stop, 10% trailing after 3% profit, funding rate kill
- Missing: max hold time, breakeven move, scale-in/scale-out

---

### 3. RESEARCH OPPORTUNITIES (Ranked by Expected Impact)

| # | Opportunity | Expected Impact | Effort | Priority |
|---|-----------|----------------|--------|----------|
| 1 | **Fix trade frequency** — investigate why live trades 100x more than backtest | CRITICAL — stops fee bleed | Low | P0 |
| 2 | **Add BTC trend confirmation** to regime detection | High — reduces false regime signals | Medium | P1 |
| 3 | **Volatility-scaled position sizing** — reduce exposure in high-vol periods | High — reduces drawdown | Medium | P1 |
| 4 | **Improve futures entry timing** — add RSI, distance from recent highs | High — improves short P&L | Medium | P2 |
| 5 | **Walk-forward revalidation** of backtest with proper OOS split | High — validates if edge is real | High | P2 |
| 6 | **Add max hold time** on futures positions | Medium — prevents stuck capital | Low | P2 |
| 7 | **Multi-coin regime detection** (BTC + ETH + SOL composite) | Medium — more robust regime | Medium | P3 |
| 8 | **Compute volatility metric** (currently logged as 0.0) | Medium — enables vol-scaled sizing | Low | P3 |
| 9 | **Dynamic cooldown** — scale cooldown with market volatility | Low — incremental improvement | Low | P4 |
| 10 | **Scale-out on futures** — take partial profits at defined levels | Low — nice to have | Medium | P4 |

---

### 4. RECOMMENDED EXPERIMENT (Highest Value)

**Hypothesis:** The live bot trades 100x more frequently than the backtest predicts because the 8% momentum edge is being measured on 1h klines but real-time price ticks create false signals that trigger rotation before the hourly bar closes.

**Experiment:** 
1. Add logging of exact trigger conditions at each trade (current perf, target perf, edge, RSI, time since last trade)
2. Compare live trade frequency vs backtest with identical parameters
3. If confirmed: add a confirmation delay (wait 2-3 scout cycles before executing rotation)

**Success Metric:** Reduce live trade frequency from ~14/day to <1/day without missing genuine momentum shifts.

---

### 5. CONFIDENCE RATING: **LOW**

**Reasoning:**
- Only 18 live trades over 30 hours — statistically meaningless
- Backtest is likely overstated (train/OOS inversion is a red flag)
- System has been live for <2 days
- Futures short has been open for hours, P&L noise
- Cannot draw conclusions about long-term viability yet

**What would move confidence to MEDIUM:**
- 100+ live trades with proper logging
- Properly validated backtest with realistic fees + slippage
- At least one complete regime transition (bear→bull or bull→bear) observed live
- 30 days of live operation without catastrophic bugs

---
