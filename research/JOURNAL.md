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

---

## SESSION 002 — 2026-06-26 (Full Research Sweep: BEAR, SIDEWAYS, Regime v2)

### System Snapshot
- **Capital:** ~$75 spot (canary cap), ~$0.28 futures dust
- **Strategy:** Momentum rotation (BULL/SIDEWAYS) + futures shorts (BEAR)
- **Regime:** SIDEWAYS (ADX ~23, bot holding INJ/USDC)
- **All 15 coins healthy**, coin manager now regime-aware
- **3 audit bugs fixed** this session (#13, #29, #30)

---

### 1. BEAR FUTURES BACKTEST (was broken — now fixed and validated)

**Root cause of "0 candles" bug:** Symbol format mismatch — script expected `SOLUSDC` but CLI accepted bare `SOL`. Fixed with auto-append of bridge suffix.

**90-day backtest results (11 USDC perps, 2x leverage, 40% margin, 12% stop, 3%/1% trailing):**

| Metric | Value |
|---|---|
| Total trades | 353 |
| Win rate | 82% |
| Compounded return | +9.5% |
| Exit mix | 290 trailing exits, 61 stop-loss exits |
| Avg P&L/trade | +0.14% |

**Per-symbol short quality:**

| Tier | Symbols | Insight |
|---|---|---|
| ✅ Best targets | ADA (+25%), ENA (+24%), SUI (+17%), SOL (+16%) | Consistent downtrends, trailing stops capture profit |
| ✅ Good targets | AVAX (+12%), AAVE (+11%), XRP (+7%), DOGE (+3%) | Moderate edge |
| ❌ Bad targets | LINK (-4%), NEAR (-28%), TIA (-33%) | Too volatile — bounces trigger stop-outs repeatedly |

**Key finding:** NEAR and TIA should be excluded from short candidate selection. They produce 43 and 38 trades respectively but lose money overall because they bounce too hard against the short position.

**Action item:** Add a "short eligibility" filter to futures_manager that excludes known bad short targets (NEAR, TIA) even if they show the worst momentum.

---

### 2. SIDEWAYS REGIME: MOMENTUM ROTATION vs CASH

**The question:** Does the bot's momentum rotation strategy actually beat sitting in USDC during SIDEWAYS markets? This was previously untested.

**90-day study using same ADX(14) + EMA(12/26) regime classification on SOL/USDC:**

| Regime | Time | Momentum Return | Cash | Edge | Win Rate | Max DD |
|---|---|---:|---:|---:|---|---:|
| BULL | 21% (19d) | +21.3% | 0% | +21.3% | 50% | 35.5% |
| **SIDEWAYS** | **53% (48d)** | **+204.3%** | **0%** | **+204.3%** | **73%** | **11.7%** |
| BEAR | 25% (22d) | +27.2% | 0% | +27.2% | 59% | 21.0% |

**Key finding:** Momentum rotation during SIDEWAYS is massively profitable (+204.3%) with a 73% win rate and only 11.7% max drawdown. SIDEWAYS accounts for 53% of the time window — this is the bot's primary profit driver.

**Verdict:** Sitting in cash during SIDEWAYS would be a catastrophic mistake. The current strategy of running momentum rotation through all three spot regimes (BULL/SIDEWAYS/BEAR) is validated. No cash-only mode needed.

**Caveat:** The simulation uses simplified entry (no cooldown, no confirmation delay, no RSI filter). Live performance will be lower due to these safety filters, but the directional conclusion is clear.

---

### 3. REGIME v2 PROMOTION READINESS

**Status:** 🟡 Keep shadowing — NOT ready for live promotion

| Window | Best Route | Return | Max DD | Robust |
|---|---|---:|---:|---|
| 30d | legacy_sol | +18.6% | 6.8% | ❌ 2/3 |
| 60d | regime_v2_route_tuned | +25.0% | 6.1% | ❌ 2/3 |
| 90d | regime_v2 | +12.0% | 19.0% | ❌ 1/3 |

**Blockers:** Route robustness fails in all windows. 90d max drawdown exceeds 18% gate.

**Assessment:** Regime v2 does NOT need to be promoted to improve current performance. The existing single-regime-classifier + momentum rotation strategy already captures SIDEWAYS profits effectively (see finding above). Regime v2's multi-signal routing adds complexity without proven additional edge. Keep shadowing for data accumulation.

---

### 4. DO WE NEED MORE REGIMES/STRATEGIES?

**Answer: No.** The evidence says the current 3-regime system (BULL/SIDEWAYS/BEAR) with 2 strategies (momentum rotation + futures short) is well-calibrated:

1. **SIDEWAYS (53% of time):** Momentum rotation returns +204.3%. ✅ Working perfectly.
2. **BULL (21% of time):** Momentum rotation returns +21.3%. ✅ Working, high DD is acceptable for bull exposure.
3. **BEAR (25% of time):** Futures shorts return +9.5% with 82% win rate. ✅ Working, but NEAR/TIA should be excluded.
4. **STORMY:** Not tested separately because it's rare and the bot defaults to defensive (cash-like) behavior. No evidence it needs a dedicated strategy.

Adding more regimes would:
- Increase false classification risk (more boundaries = more wrong calls)
- Add complexity without proven edge
- Require new research/backtesting infrastructure

---

### 5. ACTIONABLE FINDINGS

| # | Finding | Priority | Effort |
|---|---|---|---|
| AF-1 | **Exclude NEAR, TIA from short candidate list** — they lose money as short targets | Medium | Low |
| AF-2 | **No code changes needed for SIDEWAYS** — momentum rotation already works here | None | None |
| AF-3 | **Regime v2 not needed now** — existing strategy captures SIDEWAYS profits | None | None |
| AF-4 | **BEAR backtester is now functional** — can be used for future futures optimization | Resolved | Done |
| AF-5 | **Canary cap review** — SIDEWAYS generates most profit, so canary caps can be raised once 24h review confirms clean operation | After 21:35 UTC | Low |
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

## SESSION 003 — 2026-06-27 (Hardening Sweep: Backtest Revalidation, Idempotency, Risk Audit, Regime v2 Candidate)

### System Snapshot
- **Capital:** ~$52 spot (single INJ position), ~$0.28 futures dust
- **Regime:** BULL (ADX ≈ 30–32, stable; SOL-only classifier)
- **Live code:** master @ `ab980f4` — **pre-fix** (see §6 below)
- **Trading mode:** **Canary / paper-scale** — `canary_mode_enabled = yes`, spot cap $75, futures caps $50 abs / 15% pct. No new entries; momentum filter currently blocking all candidates.

> ⚠️ **Important framing for this whole session:** several fixes were committed this
> session to branch `fix/deploy-blockers-98-101`, which **has NOT been merged to
> master and has NOT been deployed**. The live bot is still running the pre-fix
> container image (built from master `ab980f4` on 2026-06-26). Each section below
> marks clearly what is *committed-and-pending-review* vs *actually live*.

---

### 1. BACKTEST REVALIDATION — VERDICT: FAIL (original +79% was an artifact); corrected +36.5% is UNPROVEN

The original headline (+79% full-period, Sharpe 3.85) is **an artifact of methodological
flaws** (same-bar-close execution lookahead, understated 0.05% slippage, single inverted
train/OOS split, flawed Sharpe math). See `docs/audits/backtest-audit.md`.

A corrected, independent revalidation (`docs/audits/backtest-revalidation-report.md`,
issue #92) applied: next-bar-open execution, 0.1%/side slippage, 0.075%/side fees,
rolling disjoint OOS walk-forward windows, plus buy-&-hold and 50-run random-rotation
benchmarks.

| Metric | Original claim | Corrected (`s3`) |
|---|---|---|
| Full-period P&L (5 mo) | +79% | **+36.5%** |
| Sharpe (annualized) | 3.85 | **1.12** |
| Max drawdown | 48% | **62%** |
| Trades | 53 | 41 |

- The corrected +36.5% is **probably real but small, risky, and under-sampled** (5 OOS windows, 4/5 positive, high variance). **62% max drawdown is the binding constraint.**
- Funding-rate cost remains unmodeled in the headline number (the one open gap vs. acceptance criteria).
- Random-rotation baseline returned mean −63.7% (50 runs); B&H single-coin ≈ +4.4%.
- Break-even slippage ≈ 0.48%/side — only ~5× margin at the deployed 0.1% assumption.
- **Do not treat +36.5% as final or deployment-grade.** Treat it as an upper bound pending funding aggregation, a dynamic (survivorship-free) coin universe, ≥10 windows, and 100+ live trades post-fix.

---

### 2. CONFIRMATION-TIMING BUG — FOUND AND FIXED (live 3s → 3–5 min)

The live-vs-backtest trade-frequency divergence was **root-caused and fixed**
(revalidation report §5; backtest-audit §4). This was the real implementation bug behind
the "55× too many trades" finding from Session 002.

- **Root cause:** live config had `SCOUT_SLEEP_TIME=1` with `confirmation_cycles=3`, so a
  rotation signal was confirmed in ~3 seconds; the backtest processes one bar/hour, so its
  "3 cycles" ≈ 3 hours. The time-based gate (`CONFIRMATION_TIME_ENABLED`) defaulted to off,
  so the near-instant cycle count was all that mattered live.
- **Result:** the live bot acted on intrabar noise that disappears by the hourly close —
  invisible to the backtest. This single mismatch fully explains the 55× frequency gap.
- **Fix (committed):** regime-aware minimum confirmation seconds (default 180s, bull 300s,
  sideways 180s, bear 60s). Lives in the pending fix branch; **not yet deployed**.

---

### 3. ORDER IDEMPOTENCY — ADDED TO SPOT + FUTURES PATHS

To make order submission safe to retry without double-execution:
- Added `_generate_client_order_id()` / `_is_duplicate_order_error()` helpers and
  `newClientOrderId` on the spot buy/sell paths in `binance_api_manager.py`.
- Added `_generate_futures_client_order_id()` / `_verify_short_exists()` and
  `newClientOrderId` on **all 6** `futures_create_order` call sites in `futures_manager.py`
  (entry, close, emergency-flatten, reconciliation, hard stop, trailing algo).
- Also: `retry()` rewrite with exponential backoff + 429/`rate_limited` handling + error
  classification; kill-switch now re-queries positions and logs the kill event; rotating
  file logger.
- **Status:** committed on `fix/deploy-blockers-98-101`; **pending review — not deployed.**

---

### 4. RISK AUDIT (#98) — CIRCUIT BREAKER IS DORMANT IN PRODUCTION (F2/F3)

Independent read-only audit (`docs/audits/risk-audit.md`, issue #98):

- **The breaker logic is correct** (≥3% daily / ≥8% weekly trips; all entry paths gated;
  exits/stop-losses bypass by construction; 13-test independent suite + 15 existing = 28 green).
- **But it is DORMANT in production.** Its equity baselines were never seeded in the live DB
  (`portfolio_daily_start_equity` / `portfolio_weekly_start_equity` are MISSING), so
  `evaluate_circuit_breaker` hits the fail-open branch (`"equity baseline unavailable"`) on
  every cycle and returns `block_new_risk=False`. The breaker is enabled in config but blind.
- **Root cause = version skew (F2/F3):** the eager-seeding + visible-fail-open-alert fix
  exists **locally** on `fix/deploy-blockers-98-101` but the live container was built from
  master `ab980f4` *before* the fix. Until the branch is merged + redeployed, the breaker
  cannot fire and a fail-open is silent (no Telegram alert).
- ~16.6% realized drawdown occurred entirely *before* the breaker was enabled (locked in).
- The only active capital guard today is the **canary cap** (spot $75, futures $50/15%).
- Risk-agent did **not** exercise a halt (latent gap, not an active loss path). **Condition
  attached:** do not approve a regime change to BEAR (opens futures shorts) until F2/F3 are
  deployed and baselines confirmed seeded.

> **#98 veto review status: ⏳ PENDING.** The fix is committed on the branch but the
> risk-agent has not yet re-reviewed/signed off the merged+deployed state. Not merged, not deployed.

---

### 5. SESSION CODE REVIEW (#101) — TWO BLOCKERS FOUND AND FIXED (A/B); ROUND-2 APPROVE

Round-1 session review (`docs/audits/session-review.md`, issue #101) found two BLOCKERS:

- **BLOCKER A** — `-2010` misclassified as duplicate: an insufficient-balance `-2010` was
  treated as "duplicate order sent" and thus as a *successful* order. Fixed by gating on the
  duplicate-specific message string (`"duplicate order sent"`) instead of the bare code.
  Verified by execution against the installed `python-binance`.
- **BLOCKER B** — 5 of 6 `futures_create_order` calls lacked `newClientOrderId` (no
  idempotency). Fixed: all 6 sites now carry it; AST-based structural test guards regressions.

Round-2 review (same file, top) **APPROVED** both fixes as genuinely resolved. Non-blocking
follow-ups tracked: (F1, medium) entry-duplicate recovery leaves position untracked until
restart; (low) `flask-socketio`/Werkzeug-3 web-UI import breakage (trading core unaffected).

> **#101 final-review status: ⏳ PENDING.** Round-2 approved the code on the branch, but the
> branch has not been merged to master or deployed. The final reviewer has not signed off the
> deployed state.

---

### 6. DEPLOYMENT STATUS — LIVE IS PRE-FIX; FIXES ARE ON THE BRANCH, NOT DEPLOYED

| | Commit | Deployed? | Notes |
|---|---|---|---|
| **Live bot (now)** | master `ab980f4` | ✅ yes (container built 2026-06-26 20:17 UTC) | Pre-fix: dormant breaker, no idempotency, 3s confirmation |
| `6d69f6c` | fix(#98,#101): 3 defects | ❌ no | BLOCKER A/B + breaker tightening |
| `aac91e4` | fix(#98 F3): fail-open Telegram alert | ❌ no | Breaker fail-open visibility |
| `dbe3bd6` | feat(#102): regime v2 candidate | ❌ no | Non-live research module (see §7) |

**Unblocking action:** merge `fix/deploy-blockers-98-101` → master, let Coolify redeploy,
then confirm `portfolio_daily_start_equity` appears in `bot_state`. **Until then, treat the
circuit breaker as OFF.**

---

### 7. STRATEGY HYPOTHESES (#97) — COMPLETED: DEFENSIVE TIMING IS THE REAL EDGE

Research doc `research/strategy-hypotheses-2026-06.md` (issue #97) decomposed the corrected
+36.5% and concluded the edge is **defensive timing, not coin selection**:

- Momentum *selection* is weak: per-window optimization beat fixed deployed params in only
  1 of 3 OOS windows; random-coin-rotation baseline was −63.7% (50 runs).
- Value is concentrated in three defenses: the 15% trailing stop, the regime→USDC rotation
  in bears, and the 24h anti-churn filter.
- Futures exit mix is stop-dominated (290 trailing vs 61 hard-stop exits of 353 trades).
- Rough attribution: ~70–85% of the corrected spot edge is defensive; ≤15–30% selectional
  (statistically indistinguishable from zero at n=5).
- Five candidate hypotheses (H-A defensive overlay, H-B funding harvest, H-C vol-targeted
  sizing, H-D sideways mean-reversion) + recommended regime-detection signal additions
  (BTC confirmation, realized-vol, funding-rate, OI, market breadth). Highest-value next
  experiment: the H-A stop/regime/anti-churn ablation (does not yet exist in committed data).

---

### 8. REGIME v2 CANDIDATE MODULE (#102) — BUILT, NON-LIVE, 26 TESTS PASS

New non-live research module `binance_trade_bot/regime_v2_signals.py` (issue #102,
commit `dbe3bd6`) implements the five multi-signal detectors recommended in the hypotheses
doc §2.2: multi-coin breadth, BTC confirmation, realized-volatility regime, funding-rate
signal, and a weighted composite → BULL/SIDEWAYS/BEAR/STORMY.

- **Intentionally NOT wired into the live path** — every detector accepts data as params,
  no Binance API calls, fully offline-testable.
- Feeds the existing Regime v2 promotion pipeline (issue #72), which Session 002 judged
  🟡 *not ready for live promotion* (route robustness fails all windows; 90d DD > 18% gate).
- Tests: `tests/test_regime_v2_signals.py` — **26/26 pass.**
- **Status:** committed on the branch; **non-live; not deployed; not promoted.**

---

### 9. SESSION SUMMARY — WHAT IS DONE vs PENDING

| Work item | Issue | Done? | Live? |
|---|---|---|---|
| Backtest revalidation (corrected +36.5%, FAIL vs original +79%) | #92 | ✅ report | n/a (research) |
| Confirmation-timing bug fix (3s → 3–5min) | #92/#101 | ✅ committed | ❌ pending deploy |
| Order idempotency (spot + futures, all 6 sites) | #101 | ✅ committed | ❌ pending deploy |
| Risk audit: breaker dormant (F2/F3) | #98 | ✅ audit | ❌ fix pending deploy |
| Breaker eager-seed + fail-open alert | #98 | ✅ committed | ❌ pending deploy |
| Session code review (BLOCKER A/B fixed, round-2 approve) | #101 | ✅ review | ❌ pending deploy |
| Strategy hypotheses (defensive timing = edge) | #97 | ✅ doc | n/a (research) |
| Regime v2 candidate module (non-live, 26 tests) | #102 | ✅ committed | ❌ non-live by design |

**Two veto reviews remain PENDING (not merged, not deployed):**
1. **#98 — risk-agent** must re-review/sign off after the breaker fix is merged + deployed
   and baselines are confirmed seeded in the DB.
2. **#101 — final-reviewer** round-2 approved the code on the branch, but the branch is not
   merged to master or deployed; the deployed state has not been signed off.

**Net:** the live bot is running safe-default canary caps + trailing stop + momentum filter
blocking entries, but its circuit breaker is dormant and its fixes are on the branch. No
claim in this journal should be read as "the fixes are live."

---

