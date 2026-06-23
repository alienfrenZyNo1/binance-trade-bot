# Improvement Backlog
## Ranked by Expected Impact on Risk-Adjusted Returns

## P0 — Critical (Capital Bleeding Now)

### BL-001: Fix excessive trade frequency
- **Problem:** 18 trades in 30h (14/day). Backtest predicts 0.25/day. Fees = ~$8 = 13% of capital.
- **Root Cause Hypothesis:** 1h kline momentum edge triggers on intrabar noise; cache TTL too short; cooldown not enforced correctly
- **Test:** Log exact trigger conditions; compare to backtest logic
- **Target:** <1 trade/day average
- **Status:** TO INVESTIGATE

### BL-002: Revalidate backtest methodology
- **Problem:** Train P&L -30%, OOS +65% (inverted). Sharpe 3.9 unrealistic. No slippage. No funding costs.
- **Fix:** Walk-forward test with 60/20/20 train/validation/test. Add 0.1% slippage. Add funding rate costs.
- **Status:** TO DO

## P1 — High Impact

### BL-003: Add BTC trend confirmation to regime detection
- **Problem:** Regime uses SOL only. BTC drives crypto market; SOL can diverge.
- **Fix:** Require BTC EMA confirmation (BTC above/below 50EMA) before declaring bull/bear
- **Status:** TO DO

### BL-004: Volatility-scaled position sizing
- **Problem:** Bot is 100% in or 100% out. No risk adjustment for volatility.
- **Fix:** Use ATR or realized volatility to scale position size (Kelly-lite). In high vol, reduce to 50-70%.
- **Status:** TO DO

### BL-005: Compute volatility metric (currently 0.0)
- **Problem:** avg_volatility logged as 0.0 in all regime entries. Feature exists but unused.
- **Fix:** Compute 24h realized volatility (stdev of hourly returns) and store in regime log.
- **Status:** TO DO

## P2 — Medium Impact

### BL-006: Improve futures entry timing
- **Problem:** Shorts the worst performer with market order. No timing optimization.
- **Fix:** Add RSI overbought check (don't short if already oversold). Add distance-from-recent-high filter (only short after bounce from lows). Consider limit orders at resistance.
- **Status:** TO DO

### BL-007: Add max hold time on futures positions
- **Problem:** No time limit. Position could stay open indefinitely, accumulating funding costs.
- **Fix:** Close after 48-72h regardless of P&L if not hitting stop/target.
- **Status:** TO DO

### BL-008: Add breakeven stop on futures
- **Problem:** After profit, stop stays at -15%. Can give back all gains.
- **Fix:** Move stop to breakeven after +5% profit.
- **Status:** TO DO

## P3 — Lower Priority

### BL-009: Multi-coin composite regime detection
### BL-010: Dynamic cooldown scaling with volatility
### BL-011: Scale-out/partial profit taking on futures
### BL-012: Coin universe pruning (remove consistently underperforming coins)
### BL-013: Add crash protection (flash crash circuit breaker)
