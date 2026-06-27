# Risk Review: Regime-Adaptive Trend Strategy

**Reviewer:** GORDON (Risk Agent)  
**Date:** 2026-06-27  
**Artifact:** `binance_trade_bot/strategies/regime_trend_strategy.py` (1,113 lines, 73 tests)  
**Config:** `/data/binance-bot-data/config/user.cfg`  
**Research:** `docs/research/regime-combined-analysis.md`, `docs/research/coin-filter-analysis.md`  
**Pipeline Stage:** Risk Review → QA → Final Review → Boss Approval

---

## Executive Summary

The strategy code is well-structured, modular, and includes paper mode, hysteresis, and futures server-side stops. However, it carries **critical risk integration gaps** — most notably the circuit breaker is configured but **not wired into this strategy** — and the default 2× leverage with 15% stops produces 30% equity loss per stopped trade, which exceeds the circuit breaker's own weekly 8% limit in a single event.

**Verdict: APPROVED WITH CONDITIONS** — safe for paper trading immediately; live deployment requires 7 mandatory fixes (Section 6).

---

## 1. Position Sizing: Max Loss Per Trade at 2× with 15% Stop

### Configuration
| Parameter | Value | Source |
|-----------|-------|--------|
| Trend leverage | 2.0× | `TREND_LEVERAGE_DEFAULT` (line 73) |
| Hard stop loss | 15% | `STOP_LOSS_DEFAULT` (line 74) |
| Trailing stop | 12% | `TRAIL_STOP_DEFAULT` (line 75) |
| BULL position fraction | 100% of equity | `compute_position_size()` (line 234) |
| BEAR position fraction | 100% of equity | `compute_position_size()` (line 236) |
| Max futures margin | 50% of USDC | `FUTURES_MAX_MARGIN_PCT` default 0.5 |

### Loss Analysis (BULL regime, spot)

At 2× leverage with 100% equity deployment, the notional exposure is 2× equity. The stops fire as follows:

| Stop Type | Price Move | Equity Impact |
|-----------|-----------|---------------|
| Trailing stop (12% from peak) | -12% from peak | **-24% of equity** (2× × 12%) |
| Hard stop (15% from entry) | -15% from entry | **-30% of equity** (2× × 15%) |

**⚠️ CRITICAL:** A single stopped trade at the hard stop (-30% equity) exceeds the **weekly** circuit breaker threshold (8%) by 3.75× and the daily threshold (3%) by 10×. One bad trade can trigger every circuit breaker simultaneously.

### Liquidation Threshold at 2×

- **BULL (spot leveraged via futures):** At 2× leverage, liquidation occurs at approximately **-50% adverse move** (minus maintenance margin of ~0.5–1%). The 15% hard stop should trigger well before liquidation under normal conditions.
- **BEAR (futures short at 2×):** Same ~50% adverse move threshold. The 15% futures stop-loss (`FUTURES_STOP_LOSS_PCT = 15.0`) triggers at +15% against the short, well inside the liquidation zone.

**Risk:** The gap between the 15% stop and 50% liquidation is comfortable (35%), but a **flash crash or exchange outage** that bypasses the client-side stop could reach liquidation. Server-side STOP_MARKET orders exist for futures (good), but **no server-side stop exists for spot positions**.

### Note: `compute_position_size()` Is Never Called

The pure function `compute_position_size()` (line 218) returns leverage and position fraction per regime, but **it is never invoked** in the `scout()` loop or any execution path. Position sizing is implicit — the strategy deploys 100% of available bridge balance on every buy. The function exists for testing/documentation but does not enforce risk limits in live execution.

---

## 2. Drawdown Interaction with Circuit Breaker (3% / 8% / 10%)

### Circuit Breaker Configuration

| Threshold | Value | Scope |
|-----------|-------|-------|
| Daily max drawdown | 3.0% | `portfolio_daily_max_drawdown_pct` |
| Weekly max drawdown | 8.0% | `portfolio_weekly_max_drawdown_pct` |
| Cooldown | 24h | `portfolio_circuit_breaker_cooldown_hours` |
| (10% halt) | Not explicitly configured | Implied by weekly 8% + additional buffer |

### 🔴 CRITICAL: Circuit Breaker Is NOT Wired Into This Strategy

The `regime_trend_strategy.py` module contains **zero references** to:
- `risk_circuit_breaker` (the module)
- `evaluate_circuit_breaker()` (the function)
- `new_risk_blocked` (the callback)
- `_estimate_spot_equity()` or any equity estimation

By contrast, `momentum_strategy.py` (the current production strategy) fully integrates the circuit breaker:
- Sets `futures_manager.new_risk_blocked = self._new_spot_risk_blocked` (line 141)
- Seeds equity baselines on startup (line 144-158)
- Checks `_new_spot_risk_blocked()` before every new buy (lines 780, 949)

**The regime trend strategy creates a FuturesManager but never assigns `new_risk_blocked`, meaning futures entries are not gated by the circuit breaker. Spot entries also have no circuit breaker check anywhere in the scout loop.**

### Drawdown Cascade Scenario

1. Trade enters BULL at 2× leverage on a volatile coin (e.g., APT)
2. Price drops 12% → trailing stop triggers → realized loss: **-24% equity**
3. This single event exceeds:
   - Daily 3% threshold → 8× over
   - Weekly 8% threshold → 3× over
4. Circuit breaker activates — but only if wired in. With the current code, **it does nothing**.
5. Re-entry logic (`_reenter_from_bridge()`) would immediately look for a new buy with no drawdown check.

### Weekly vs. Trade-Level Mismatch

The circuit breaker is designed for a low-leverage momentum strategy where daily swings are 1-3%. At 2× leverage with 12-15% stops, a single stopped trade produces drawdown that dwarfs the weekly limit. The circuit breaker architecture is sound but the **leverage level is incompatible with the drawdown thresholds**.

---

## 3. Kill Switch Gaps

### 3.1 Weekend / Flash Crash Protection

Crypto trades 24/7/365 — there are no traditional "weekend gaps," but equivalent risk exists during:
- Exchange maintenance windows (Binance periodically pauses)
- Flash crashes (e.g., March 2020 COVID crash, May 2021, Nov 2022 FTX collapse)
- Stablecoin depeg events (USDC briefly depegged in March 2023)

**Futures positions:** ✅ Protected. Server-side `STOP_MARKET` orders with `workingType=MARK_PRICE` remain live on Binance even if the bot is offline.

**Spot positions:** 🔴 **UNPROTECTED.** The trailing stop (`_check_trailing_stop()`, line 656) and hard stop (`_check_hard_stop()`, line 695) are **client-side only** — they only execute when the bot's `scout()` loop runs. If the bot crashes, loses connectivity, or the VPS goes down, spot positions have zero protection.

### 3.2 API Failure Mid-Position

| Scenario | Futures | Spot |
|----------|---------|------|
| Bot crash | ✅ Server stops live | 🔴 No protection |
| API rate limit | ✅ Server stops live | 🔴 Stops not checked |
| Network outage (VPS) | ✅ Server stops live | 🔴 Stops not checked |
| Binance API outage | ✅ Server stops on Binance side | 🔴 No fallback |
| Partial fill on entry | ⚠️ Handled via reconciliation | 🔴 No handling |

The `_reconcile_positions()` method in FuturesManager (line 259) is excellent — it detects orphaned shorts on restart, keeps the largest, and flattens the rest. **No equivalent reconciliation exists for spot positions.**

### 3.3 Grid Mode Risk

The SIDEWAYS grid (`_scout_sideways()`, line 840) places buy/sell ladders with **no individual stop-loss per level**. If the market trends hard while classified as sideways (regime lag is documented at 45.7% accuracy), multiple grid buy levels fill, accumulating a large position with no exit until the trailing stop finally triggers — potentially at -24% equity.

### 3.4 Missing Kill Switches

- No **max open position time limit** — positions can be held indefinitely
- No **max concurrent positions** across spot + futures
- No **emergency flatten-all** command
- No **API heartbeat / health check** that auto-flattens on connectivity loss
- No **max daily trade count** — only a per-trade cooldown (`trade_cooldown_seconds`)

---

## 4. Futures Liquidation Risk at 2× CROSS on 50% Gap Move

### Scenario Analysis

**Setup:** Bear regime, 2× CROSS margin, 50% of USDC deployed as margin.

At 2× leverage:
- Position notional = margin × 2
- Liquidation price ≈ entry ± 50% (minus maintenance margin buffer of ~0.5-1%)
- Hard stop at 15% should trigger well before liquidation

**50% Gap Move (extreme scenario):**

| Factor | Impact |
|--------|--------|
| Margin deployed | 50% of futures USDC |
| Position notional | 100% of futures USDC (2× × 50%) |
| 50% adverse gap | Position loses 100% of margin → **liquidation** |
| CROSS margin | **Entire futures wallet** is collateral, not just the position's margin |
| Server STOP_MARKET | Fires at +15%, but MARKET order may slip significantly in a crash |
| Worst-case slippage | If price gaps past the stop, the STOP_MARKET becomes a market order at whatever price is available |

**Risk Assessment:**

1. **Under CROSS margin, the entire futures USDC balance is at risk** — not just the 50% allocated to this position. A sustained adverse move that overwhelms the stop could drain the entire futures wallet.
2. The server-side stop at 15% should contain losses to ~15% of position notional (≈7.5% of total futures USDC) under normal conditions.
3. **Catastrophic scenario:** A flash crash that gaps 50%+ in a single tick (e.g., exchange glitch, oracle failure) could blow through the stop and liquidate the entire futures wallet. This has happened on Binance (e.g., XRP flash crash Dec 2020, ADA flash crash 2021).
4. **Mitigating factor:** The canary guard caps futures margin to `$50 absolute` and `15% pct` (user.cfg lines 54-55), which limits absolute damage. This is appropriate for initial deployment.

**Recommendation:** The 2× CROSS configuration is acceptable under canary caps ($50 max). Before scaling, ISOLATED margin should be pursued (currently rejected by Binance for this account — documented in `futures_manager.py` line 129).

---

## 5. Coin Liquidity Risk for APT, OP, RUNE

### Background

The "Strat Top 5" universe (APT, AVAX, OP, BTC, RUNE) was selected by IS-period strategy Sharpe, not by liquidity. The coin filter analysis reveals these coins had **catastrophic buy-and-hold performance**:

| Coin | Net Return (Full Period) | Buy-Hold Sharpe | Trend Quality Rank | Concern |
|------|--------------------------|-----------------|--------------------|---------| 
| APT | **-89.4%** | -1.80 | 27/27 (worst) | Extreme volatility, newer listing |
| OP | **-86.9%** | -1.27 | 23/27 | L2 token, moderate liquidity |
| RUNE | **-73.6%** | -1.09 | 22/27 | THORChain, thinner order books |
| AVAX | -72.1% | -1.01 | 12/27 | Acceptable liquidity |
| BTC | -41.2% | -0.91 | 18/27 | Deep liquidity, no concern |

### Liquidity Assessment

| Coin | Typical Daily Volume | Bid-Ask Spread | Slippage Risk (canary size) | Slippage Risk (full size) |
|------|---------------------|----------------|---------------------------|--------------------------|
| BTC | $10B+ | <0.01% | Negligible | Low |
| AVAX | $500M+ | ~0.02% | Low | Moderate |
| APT | $200-400M | ~0.05% | Moderate | **High** |
| OP | $150-300M | ~0.05% | Moderate | **High** |
| RUNE | $50-150M | ~0.08-0.15% | Moderate | **High** |

### Specific Risks

1. **APT (-89.4% buy-and-hold, worst quality score):** This coin was selected purely because it trended strongly during the IS period, not because it's a quality asset. It's a newer listing with limited historical data. A 15% stop on APT could easily see 2-3% additional slippage in fast markets.

2. **OP (-86.9% buy-and-hold):** Layer 2 tokens are highly correlated to ETH gas prices and L2 adoption narratives. They can gap 20%+ on L2 news events. Moderate liquidity means wider spreads during volatility spikes.

3. **RUNE (-73.6% buy-and-hold):** THORChain has experienced protocol-level incidents (exploits in 2021-2022). RUNE liquidity is the thinnest of the five coins. The 2.5% grid spacing in SIDEWAYS mode could result in unfavorable fills.

4. **Correlation Risk:** All five coins are crypto-correlated. The research notes: "All altcoins draw down simultaneously during crypto-wide selloffs." A portfolio of APT+OP+RUNE+AVAX provides almost no diversification during a crypto crash — they will all drop together, and the 2× leverage amplifies the correlated drawdown.

5. **Selection Bias Warning:** These coins were selected because the Balanced strategy happened to perform well on them during the IS period (60%). The IS Sharpe ranking may be overfitted to a specific market regime. The research itself notes: "if a coin was profitable in the IS period with this specific strategy, it's likely to continue being profitable" — this is circular reasoning that ignores structural break risk.

---

## 6. Missing Risk Controls Needed Before Live

### 🔴 MANDATORY (Must fix before any live deployment)

| # | Gap | Severity | Fix |
|---|-----|----------|-----|
| 1 | **Circuit breaker not wired in** | CRITICAL | Import `risk_circuit_breaker`, assign `futures_manager.new_risk_blocked`, add `_new_spot_risk_blocked()` checks before spot buys/re-entries. Mirror `momentum_strategy.py` integration. |
| 2 | **No spot server-side stop** | CRITICAL | Use Binance OCO (One-Cancels-Other) or STOP_LOSS_LIMIT orders for spot positions. Client-side stops are insufficient for 24/7 markets. |
| 3 | **`compute_position_size()` never called** | HIGH | Either call it to enforce position sizing, or document that sizing is implicit (100% deployment). If implicit, add max-notional guard per trade. |
| 4 | **No spot position reconciliation on restart** | HIGH | Add `_reconcile_spot_position()` that checks actual holdings vs. expected on bot startup, similar to futures reconciliation. |
| 5 | **No max total exposure limit** | HIGH | Add a check that prevents opening new positions (spot or futures) when total exposure exceeds a configurable threshold (e.g., 1.5× equity). |

### 🟡 RECOMMENDED (Should fix before scaling beyond canary)

| # | Gap | Severity | Fix |
|---|-----|----------|-----|
| 6 | **No grid-level stop loss** | MEDIUM | Add per-level or aggregate stop for SIDEWAYS grid to prevent accumulation during regime misclassification. |
| 7 | **No volatility-scaled sizing** | MEDIUM | Scale position by inverse ATR or recent volatility. Currently deploys 100% regardless of coin volatility. |
| 8 | **No max position hold time** | MEDIUM | Force exit after N hours/days to prevent zombie positions. |
| 9 | **No emergency flatten-all command** | MEDIUM | Add a Telegram command or API endpoint to close everything immediately. |
| 10 | **No correlation-based exposure reduction** | LOW | Reduce total deployment when cross-coin correlation exceeds threshold. Config exists (`correlation_threshold = 0.85`) but is not used by this strategy. |

### 🟢 NICE TO HAVE

| # | Gap | Fix |
|---|-----|-----|
| 11 | ISOLATED margin for futures | Reattempt with Binance support; ISOLATED limits liquidation to position margin only. |
| 12 | Reduce leverage to 1.5× | Cuts per-trade max loss from 30% to 22.5%, bringing it closer to (still above) circuit breaker thresholds. |
| 13 | Add RUNE to `SHORT_EXCLUDE_COINS` or liquidity floor | RUNE's thin order book makes shorting dangerous during squeezes. |

---

## 7. VERDICT

### **APPROVED WITH CONDITIONS**

The strategy is approved for **paper trading** effective immediately. Live deployment with real capital is approved subject to the following mandatory conditions:

#### Conditions for Live Deployment

1. **Wire in the circuit breaker** — mirror the integration from `momentum_strategy.py` (Conditions #1 above). This is non-negotiable.
2. **Deploy only in canary mode** — the existing canary caps ($75 spot, $50 futures, 15% margin pct) must remain active. No increase until 30 days of live performance data.
3. **Add spot server-side stop orders** — or at minimum, a watchdog process that checks position health every 60 seconds independent of the main scout loop (Condition #2).
4. **30-day paper trading period** — run `regime_trend_paper = yes` for 30 days minimum with signal logging before any real orders.
5. **Start with Quality Top 3 (BNB, ETH, XRP)** not Strat Top 5 — the Quality Top 3 config has better OOS Sharpe (1.50 vs 1.16), lower drawdown (18.1% vs 24.7%), and uses only deep-liquidity large-cap coins. Switch to Strat Top 5 only after 30 days of profitable live operation.
6. **Weekly risk review** — re-evaluate drawdown, slippage, and regime accuracy every 7 days during the first 90 days.
7. **Kill switch documentation** — document the manual procedure for flattening all positions in case of emergency, accessible to all team members.

#### Explicitly NOT Approved

- ❌ No live deployment above canary caps ($75 spot / $50 futures)
- ❌ No use of 3× leverage (Aggressive variant)
- ❌ No deployment without circuit breaker integration
- ❌ No deployment of Strat Top 5 coin set without prior Quality Top 3 validation
- ❌ No deployment without spot server-side protection

---

## Appendix: Key Numbers Summary

| Metric | Value | Assessment |
|--------|-------|------------|
| Max loss per trade (2×, 15% stop) | -30% equity | 🔴 Too high vs. 3%/8% circuit breaker |
| Max loss per trade (2×, 12% trail) | -24% equity | 🔴 Too high vs. 3%/8% circuit breaker |
| Futures liquidation threshold (2×) | ~50% adverse move | 🟡 Adequate gap from 15% stop |
| Circuit breaker daily limit | 3% | 🔴 Exceeded by single trade |
| Circuit breaker weekly limit | 8% | 🔴 Exceeded by single trade |
| Regime accuracy | 45.7% | 🟡 Barely above random (33% for 3-class) |
| Best config OOS Sharpe | 1.50 (Quality Top 3) | 🟢 Passes >1.0 bar |
| Best config OOS Max DD | 18.1% | 🟢 Passes <25% bar |
| MC Prob(Positive) | 67.1-92.7% | 🟢 Passes >60% bar |
| Canapy spot cap | $75 | 🟢 Appropriate for initial deployment |
| Canary futures cap | $50 | 🟢 Appropriate for initial deployment |

---

*Review completed by GORDON Risk Agent. This review covers code-level risk analysis only and does not constitute investment advice.*
