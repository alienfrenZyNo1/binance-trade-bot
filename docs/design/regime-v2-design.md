# Regime v2 — Evidence-Gated Multi-Signal Activation Model (Design)

**Issue:** [#72 — Regime v2: evidence-gated multi-signal activation model](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/72)
**Status:** DESIGN / RESEARCH — not a live change. This document proposes an architecture; it does **not** wire anything into live trading.
**Audience:** strategy-research, strategy-developer, risk-agent, the Boss
**Depends on / informed by:** `binance_trade_bot/regime_v2_signals.py` (#102 candidate), `scripts/research_regime_v2_evaluator.py`, `scripts/regime_v2_forward_replay.py`, `docs/research/regime-v2-scoping-note.md`, `docs/research/regime_v2_backtest_report.md`, `docs/research/regime-v2-risk-review-gate-definition.md`, `docs/promotion-pipeline.md`.

---

## TL;DR

The live regime classifier is a single-coin (SOL) ADX/EMA rule. It is structurally blind to market-wide risk-off turns and to derivatives stress, and it cannot fire the bot's own `STORMY` (crash) regime at all. A multi-month research track (#102 + the #72 comment thread) has already built a **candidate** multi-signal scorecard and validated it forward-only on public data. This design proposes the **architecture that should govern promotion** of that candidate: an **evidence-gated, multi-signal regime model** that (a) fuses independent signal families into a single confidence-weighted vote, (b) requires a minimum *agreement of evidence* before any capital-moving transition, and (c) is promoted through the existing pipeline with explicit gates — never automatically.

The design is deliberately **conservative and additive**: it preserves the existing 3-cycle hysteresis, server-side futures stops, the notification flood guard, and the `/shadow` audit layer. It introduces **no automatic live promotion** and adds explicit **evidence gates** that address every known failure mode the research surfaced (whipsaw, no directional edge, the vol-threshold unit bug, untested funding/OI signals, over-defensiveness in calm regimes, and the cache-stability artifact).

> ⚠️ **Non-goal.** This document does not authorize any live change. Per the promotion pipeline (`docs/promotion-pipeline.md`) and the risk-agent gate-definition review, live promotion requires a **separate explicit PR**, full Backtest/Stress/QA/Risk gates, **and Boss approval** — *plus* resolution of the three live-safety defects currently flagged on master (the `-2010` order-error misclassification, idempotency gaps, and the dormant circuit breaker). The live SOL-only classifier must remain untouched until then.

---

## 1. Problem with the Current Regime Detection

### 1.1 What the live classifier is

The live regime is decided in a single place: `strategies/momentum_strategy.py::_update_market_regime` (lines ~186–283). On each `REGIME_CHECK_INTERVAL` (default 300s = 5 min) it:

1. Picks one **reference coin** (SOL if present, else the first enabled coin).
2. Fetches one 1h kline series for that single coin.
3. Computes **ADX(14) / +DI / −DI** and **EMA20 / EMA50** on it.
4. Applies a single threshold rule:
   - `ADX ≥ ADX_TREND_THRESHOLD` (default 25) → trending; then EMA50 + ±DI direction decides `BULL` vs `BEAR`.
   - otherwise → `SIDEWAYS`.
5. Passes the raw candidate through `RegimeHysteresis` (default 3 consecutive confirmations) before it becomes the active regime.

The labels (`bull`/`bear`/`sideways`/`stormy`) then gate everything: per-regime momentum lookback/edge, futures shorting (BEAR entry/exit), spot rotations, and the circuit breaker. `STORMY` parameters exist in config but **the live path never produces STORMY** — `avg_volatility` is hardcoded to `0.0` and `btc_correlation` to `None` in the DB log, so the dead-coded branch can never trigger.

### 1.2 Concrete failure modes (evidence-backed)

The original issue #72 incident, the NO-GO backtest report (`docs/research/regime_v2_backtest_report.md`), and the scoping note document these with measurements:

| # | Failure mode | Root cause | Evidence |
|---|---|---|---|
| F1 | **Single-coin blindness to market-wide risk-off.** Live can read `SIDEWAYS` (ADX≈24) on SOL while a multi-coin/breadth/derivatives view says `BEAR` with 95% confidence. | ADX/EMA on one coin cannot see breadth collapse or BTC-led turns. | Issue incident: 45% raw / 50% smoothed disagreement vs multi-signal over 30d; 27 legacy flips vs 8 smoothed. |
| F2 | **`STORMY` never fires** — the bot's own crash-preservation regime is non-functional in production. | The live path hardcodes vol=0; the *candidate* also mis-calibrated vol thresholds (hourly stdev compared against daily-scale cut-points). | Backtest report §5: max 24h hourly stdev observed = 0.0131, below even `VOL_LOW_MAX=0.015`. Risk review confirms the unit bug. |
| F3 | **No derivatives information.** Funding, basis, OI, taker imbalance — none inform the regime. | The live classifier is purely price-based on one spot pair. | #72 feature list; funding signal designed but untested (no cached historical dataset; live constraint forbids enabling futures). |
| F4 | **Candidate whipsaw.** When the v2 scorecard *is* run naively, it flips ~14× more often than legacy (55 vs 4 flips over 95d; 19 rapid reversals vs 0). | Composite with no confidence floor / hysteresis reacts to noise. | Backtest report §3.3; each flip implies a spot↔USDC↔futures capital move at ~0.1–0.15%/round-trip ⇒ 5–8% friction not modeled. |
| F5 | **No directional edge on extra defensiveness.** The candidate's extra risk-off calls were ~53% accurate (coin-flip). | The 3 active signals (breadth+BTC+vol) are all price-family and correlated. | Backtest report §3.2/§3.4. |
| F6 | **Over-defensiveness in calm/up regimes** is unvalidated — all samples so far are down-trends. | SIDEWAYS risk noted in hypotheses doc (+204% opportunity cost if over-defensive in a rally). | Scoping note §5; no non-down-trend window tested. |
| F7 | **Fragile validation artifacts.** Forward-replay cache key omits a fetch timestamp; stale vs fresh snapshots produced contradictory gate verdicts. | Cache design. | Scoping note §3; risk review conditions. |

### 1.3 What "good" looks like (the objective function)

Per the issue, regime labels must be **strategy-utility-based, not cosmetic chart labels**:

- **BULL** = spot momentum is expected to beat cash / BTC baseline **after fees**.
- **BEAR** = cash / short framework expected to beat the spot basket **after fees + funding**.
- **SIDEWAYS** = no strong trend; only trade if a chop candidate beats cash after costs.
- **STORMY** = volatility / liquidity stress; **prioritize capital preservation**.

The decisive metric is not "label accuracy" — it is whether the regime model, end-to-end through the bot's capital routing, produces a **better risk-adjusted outcome** (capital preservation in crashes, captured participation in rallies, bounded whipsaw cost) than the legacy SOL-only rule. The research already shows the *scorecard* adds value; the *selector overlay* needs work (see §6).

---

## 2. Proposed Architecture — Evidence-Gated Multi-Signal Model

### 2.1 Design principles

1. **Defense-first, additive, never auto-promoting.** A wrong `BULL` (staying risk-on into a crash) is far more expensive than a wrong `BEAR` (sitting out a rally). The model errs toward capital preservation and is gated into live only by the promotion pipeline.
2. **Independence of evidence.** Deliberately fuse signals from **independent families** (cross-sectional breadth, trend, volatility, derivatives) so a single-asset or single-family failure cannot move capital alone.
3. **Evidence gating, not thresholding.** A regime label is *proposed* by a scorecard but only *activated* when **(a) the composite confidence clears a floor** and **(b) enough independent signal families agree**. This directly attacks F4 (whipsaw) and F5 (no edge).
4. **Pure, replayable, no-lookahead.** All detectors are pure functions over passed-in data (the candidate already does this). Every feature at time *t* uses only candles ≤ *t*; the evaluator enforces strict forward-only label construction.
5. **Preserve all live guardrails.** Hysteresis (3-cycle), server-side futures stops, notification flood guard, and `/shadow` audit are kept; the v2 model is an *input* to the same hysteresis, not a replacement for it.

### 2.2 The pipeline (layers)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 0 — DATA ACQUISITION (public Binance only)                        │
│  spot 1h klines (BTC/ETH/SOL + breadth universe) · public funding/OI     │
│  ── all cached with a fetch-timestamped key (fixes F7) ──                │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1 — SIGNAL FAMILY DETECTORS  (pure functions, [-1,+1] each)       │
│   • Cross-sectional breadth      (§3.1)   [F1, market-wide view]         │
│   • Trend (ADX/EMA + Hurst)      (§3.2)   [directional, regime strength] │
│   • Volatility regime            (§3.3)   [F2, re-enables STORMY]        │
│   • Derivatives stress           (§3.4)   [F3, non-price, independent]   │
│   • Market-relative strength     (§3.5)   [basket vs majors/stablecoin]  │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2 — EVIDENCE FUSION  (confidence-weighted scorecard + agreement)  │
│   weighted blend → composite score ∈ [-1,+1]  +  per-family vote tally   │
│   + momentum-exhaustion label guard (direction #3 — proven)             │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3 — EVIDENCE GATE  (the new contribution of this design)          │
│   requires: |score| ≥ min_confidence  AND  ≥ N agreeing families         │
│   AND (asymmetric) stronger evidence required to *enter* BULL than to    │
│   *enter* BEAR/STORMY (defense-first). Otherwise → SIDEWAYS (no action). │
└───────────────────────────┬─────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 4 — HYSTERESIS + TRANSITION PLANNER  (existing, unchanged)        │
│   3-cycle RegimeHysteresis  →  plan_regime_transition (spot/futures)     │
│   → server-side stops, flood guard, /shadow audit                        │
└─────────────────────────────────────────────────────────────────────────┘
```

Layers 0–2 are largely already built (the #102 candidate + evaluator). **Layer 3 — the evidence gate — is the core new architectural contribution** of this design, and it is what converts the whipsaw-prone scorecard into an evidence-gated activation model as the issue title demands.

### 2.3 Why layer the agreement as a gate, not just a weight?

The candidate already produces a weighted composite score. The problem is that a weighted average can be dominated by one loud family (e.g., a noisy breadth thrust) while the *other* families disagree — and that single-family signal then moves capital and whipsaws back (F4). Requiring **concurrent agreement across independent families** is the standard ensemble-gating pattern that converts a soft blend into a *qualified* decision: capital only moves when the evidence is both strong (|score| high) *and* broad (multiple families agree). This is the structural fix for whipsaw that a confidence floor alone does not provide.

---

## 3. Signals / Indicators to Combine — and Why

Five signal families, each normalized to a score in **[-1, +1]** (+ = risk-on / bullish, − = risk-off / bearish) and an explicit family vote. The candidate already implements 1, 2, 3, and part of 4; this design completes 4 and adds 5.

### 3.1 Cross-sectional market breadth (F1) — *keep, anchor*

- **What:** fraction of the enabled USDC universe above its own EMA20 and EMA50; advancer/decliner ratio; median return; cross-sectional dispersion.
- **Why:** participation narrows at tops and broadens at bottoms, so breadth turns *before* any single-coin ADX. This is the direct structural fix for F1 — it is what lets the model see a market-wide risk-off that SOL's ADX cannot.
- **Existing:** `regime_v2_signals.breadth_signal` (implemented, tested).
- **Evidence gate role:** primary. A BEAR call should require breadth to confirm (breadth collapse is the clearest market-wide signal).

### 3.2 Trend strength & persistence — ADX/EMA **+ Hurst exponent** (F1, F5) — *extend*

- **What:** multi-reference-coin (BTC/ETH/SOL) ADX(14) + EMA20/50 directional vote **plus a Hurst-exponent persistence estimate**.
- **Why:** ADX answers "is there a trend?" and ±DI answers "which way?". ADX alone gives no directional edge on *extra* calls (F5). The **Hurst exponent** (H) answers a complementary question ADX cannot: **"is the series trending (H>0.5) or mean-reverting (H<0.5)?"**. This is important because:
  - Mean-reverting regimes (H<0.5) are exactly where momentum rotation *loses* and where SIDEWAYS/chop behavior is appropriate — the bot should *not* chase breakouts there.
  - A high ADX into a low-H regime is a classic exhaustion/false-break pattern; gating on H suppresses the false BULLs that caused F4 whipsaw.
  - H is a different statistical object from ADX (persistence of increments vs. range expansion), so it adds genuinely independent information.
- **Implementation note:** use a rolling R/S-style or variance-ratio Hurst estimator over the same no-lookahead window. Keep it a pure function; calibrate H cut-points on cached history (not live). Hurst is a *diagnostic/tie-breaker* and is treated as a sub-component of the trend family vote, **not** a live controller on its own — consistent with the issue's "HMM/Markov as diagnostic, not direct live controller" constraint.

### 3.3 Volatility regime (F2) — *fix the unit bug, then it re-enables STORMY*

- **What:** 24h realized vol (stdev of hourly log-returns), ATR/range expansion, and a **downside-vol shock** detector (semi-deviation spike). Dailyized via √T scaling before comparing to thresholds.
- **Why:** vol spikes precede/accompany crashes. This is the **only** path that can produce `STORMY` (capital preservation), which is currently dead (F2). Once the unit bug is fixed (risk-agent ACCEPTed the √T dailyization), STORMY becomes functional.
- **Existing:** `regime_v2_signals.volatility_regime` (implemented; the √T fix is risk-approved but must land before this layer is trusted).
- **Evidence gate role:** **STORMY override.** Extreme vol → STORMY regardless of other signals (defense-first), exactly as the candidate's `_classify_composite` already intends. Downside-vol shock (not just total vol) sharpens crash vs. rally-vol distinction.

### 3.4 Derivatives stress (F3) — *complete this; it is the great untested unknown*

- **What (all public, non-live):** funding rate (overheated-long / bear-capitulation), mark/index basis, open-interest (OI) value change, global & top-trader long/short ratios, taker buy/sell imbalance.
- **Why:** these are the **only non-price, non-correlated inputs** — they reveal what leveraged traders are already *doing*, which price-only indicators structurally cannot see. The backtest report (§6) explicitly flags that the candidate was tested on 3 of 4 signals and that funding is the most plausible fix for the SIDEWAYS/BEAR resolution gap (F5). Until this family is built and validated on cached public history, no claim about the candidate's full value is justified.
- **Existing:** `regime_v2_signals.funding_rate_signal` (implemented, API-free) — but **untested** on real data and missing basis/OI/long-short/imbalance sub-signals.
- **Evidence gate role:** secondary-but-independent. It is the family most likely to break ties correctly because it is orthogonal to the price family.

### 3.5 Market-relative strength (F1, F6) — *add*

- **What:** the enabled basket's rolling return vs BTC, ETH, SOL, and a stablecoin (USDC) standby.
- **Why:** detects "everything down together" (true risk-off → BEAR/STORMY) vs. "rotation within a stable market" (risk-on reshuffle → keep trading). This sharpens the SIDEWAYS-vs-BEAR decision and helps control over-defensiveness in calm regimes (F6): if the basket is *relatively* strong even when flat in absolute terms, the model should not panic to BEAR.

### Signal-family independence matrix (why five, not more)

| Family | Information type | Correlated with ADX? | Primary failure it fixes |
|---|---|---|---|
| Breadth | cross-sectional price | low | F1 (market-wide view) |
| Trend (ADX/EMA+Hurst) | directional price | — (baseline) | F1, F5 (persistence edge) |
| Volatility | risk/vol price | low | F2 (re-enables STORMY) |
| Derivatives | positioning/leverage | **~none** | F3, F5 (orthogonal tie-break) |
| Relative strength | cross-asset price | medium | F1, F6 (avoid false BEAR) |

Adding more signals beyond these has diminishing returns and increases overfitting surface (the issue explicitly cautions against complex ML models unless they beat the interpretable scorecard OOS and remain explainable). **Five independent families is the deliberate ceiling.**

---

## 4. Gating / Activation Logic — When a Strategy Is Enabled/Disabled

This is the heart of the "evidence-gated" model. There are **three gates** a regime candidate must clear before it can move capital; failing any of them means **no action** (label stays at the current regime or falls back to SIDEWAYS).

### 4.1 Gate A — Confidence floor

- Compute the composite score `S ∈ [-1, +1]` (Layer 2).
- Require `|S| ≥ min_confidence` (default 0.35, tunable) to propose any non-SIDEWAYS label. Below it → SIDEWAYS (no action).
- This is the candidate's existing `SCORE_BULL_MIN`/`SCORE_BEAR_MAX`; it is necessary but **not sufficient**.

### 4.2 Gate B — Independent-family agreement (the whipsaw killer)

- Each family produces a discrete vote: `risk_on` (score > +ε), `risk_off` (score < −ε), or `neutral`.
- Require **at least `min_agreeing_families`** (default 2 of the active families, tunable) to vote in the *same direction* as the proposed label.
- **Asymmetric, defense-first:** entering `BULL` requires *strictly more* agreement (default 3 families) than entering `BEAR`/`STORMY` (default 2 families). Rationale: a false BULL (risk-on into a crash) is catastrophic; a false BEAR (sitting out a rally) is merely costly. This asymmetry is the structural expression of "defense-first."
- **STORMY override:** if the volatility family reads `extreme`, `STORMY` is proposed **regardless** of the other families (defense-first; a crash is the one event you must not stay risk-on through). This matches the candidate's existing `_classify_composite` intent, now made functional by the vol-unit fix.

### 4.3 Gate C — Momentum-exhaustion label guard (proven in research — direction #3)

- Even after A+B, post-process the label with the no-lookahead momentum-exhaustion guard from the research track:
  - **Block BULL** when the basket has decelerated/rolled-over after a genuine extension (`basket_deceleration = roc6h − roc12h ≤ cap` while `roc12h` was positive) → fall back to SIDEWAYS. This is the precise lever that fixed maxDD (18% → 6–13%) and flipped returns negative→positive in the forward replay.
  - **Conservatively block BEAR** into a mean-reverting / diverging-positive BTC (avoid false breakdowns).
- This attacks the **root cause** (model calling a turn wrong) rather than the symptom (lagging overlays like confirmation gates or recent-P&L stops, which the research proved insufficient).

### 4.4 Layer 4 — Then (and only then) the existing hysteresis + planner

The gate-cleared label is still a **raw candidate**. It feeds the **unchanged** `RegimeHysteresis` (3 consecutive confirmations) before becoming active, then `plan_regime_transition` decides the side effects:

| Active → Candidate | Strategy activation result |
|---|---|
| any → **STORMY** | **Defensive**: flatten spot to USDC, **no new risk**, futures shorts protected by server-side stops; prioritize capital preservation. (New capability — requires vol-unit fix.) |
| any → **BEAR** | Sell spot → USDC → transfer to futures → open 1x short on worst-performing eligible coin (existing `_handle_regime_transition`). |
| BEAR → **BULL/SIDEWAYS** | Close shorts → transfer USDC back to spot → resume spot momentum rotation (existing exit path). |
| SIDEWAYS/BEAR → **BULL** | Spot momentum rotation enabled with BULL per-regime params (longer lookback, standard edge). |
| **BULL** → SIDEWAYS | Tighter/standard params; no capital move unless momentum buys already blocked. |
| gate fails (A/B/C) | **No action.** Label stays SIDEWAYS or current — the whole point of "evidence-gated." |

### 4.5 `/shadow` and the promotion-readiness job (audit only)

The v2 model runs in **shadow** alongside the live classifier. The daily `regime_promotion_readiness.py` job compares shadow vs live labels and reports readiness — it **never** routes capital. This is the audit/reporting layer the issue mandates, preserved exactly.

---

## 5. Required Data & Parameters

### 5.1 Data (public/free Binance only)

- **Spot 1h klines:** BTC, ETH, SOL (references) + the enabled USDC breadth universe (≥8–12 coins for a meaningful breadth reading).
- **Public derivatives (non-live reads):** historical funding rates, OI history, long/short ratios, taker buy/sell — via `fapi` public endpoints into a **fetch-timestamped cache** (fixes F7). *No live futures enablement required to build this dataset.*
- **History depth:** ≥ 365d for calibration/threshold-setting; ≥ 90d for a canonical smoke artifact.

### 5.2 Parameters (all tunable, all module-level constants — never magic numbers in logic)

| Parameter | Default | Purpose | Gate |
|---|---|---|---|
| `REGIME_CHECK_INTERVAL` | 300s | how often the model re-evaluates (existing) | — |
| `min_confidence` | 0.35 | Gate A: composite score floor | A |
| `min_agreeing_families (BEAR/STORMY)` | 2 | Gate B: defense-side agreement | B |
| `min_agreeing_families (BULL)` | 3 | Gate B: risk-on-side agreement (stricter) | B |
| `REGIME_CONFIRMATION_CYCLES` | 3 | Layer 4 hysteresis (existing) | 4 |
| `bull_deceleration_cap` | −1.0 | Gate C: block BULL into rollover | C |
| Vol thresholds (dailyized) | 0.015/0.035/0.07 | §3.3 STORMY bands (√T-scaled) | override |
| Composite weights | breadth .40 / trend .30 / vol .15 / deriv .15 | Layer 2 blend (renormalized on missing) | 2 |
| Per-side slippage | ≥ 0.15% | switching-cost modeling (fixes gap) | validation |
| `max_window_drawdown_pct` | 15% | robustness gate cap (risk-approved) | validation |

Every one of these is sweepable by the forward-replay harness without touching detector logic.

---

## 6. Risks & How They Are Controlled

| Risk | Severity | Control in this design |
|---|---|---|
| **Whipsaw** (F4): frequent regime flips cost 5–8% friction. | High | Gate B (family agreement) + Gate C (momentum guard) + unchanged 3-cycle hysteresis. Research shows the guard alone cut maxDD 18%→6–13%. Track flip-rate / rapid-reversal count as a gate metric. |
| **STORMY mis-fires** (over-defensive): vol-unit fix could make STORMY fire on noise, trapping the bot in cash. | Medium | Calibrate dailyized thresholds on ≥365d history (extreme should fire ~0.1–0.2% of windows, per risk review). STORMY only *flattens*/preserves — it never opens leveraged risk, so worst case is opportunity cost, not loss. |
| **Over-defensiveness in calm/up regimes** (F6): bleeding return by sitting out rallies. | High (untested) | (1) Asymmetric Gate B (harder to call BULL → also harder to wrongly leave BULL, but BULL entry is gated). (2) **Require a non-down-trend validation window before promotion** — explicit acceptance criterion. (3) Relative-strength family (§3.5) avoids false BEARs when the basket is relatively strong. |
| **No directional edge on extra calls** (F5): the model is defensive but not predictive. | Medium | Derivatives family (§3.4) is the most plausible fix (orthogonal info); acceptance gate requires label-conditional forward-return separation, not just drawdown capture. |
| **Cache instability** (F7): stale vs fresh snapshots gave contradictory verdicts. | Medium | Add fetch timestamp to the forward-replay cache key; promote only from a **committed canonical smoke artifact**, not ad-hoc cache reads (risk-agent condition 4). |
| **Lookahead / overfitting.** | High | Pure functions; strict ≤*t* feature construction; walk-forward OOS only; tune on train, evaluate on disjoint test; keep the interpretable scorecard as the baseline (ML only if it beats it OOS *and* stays explainable). |
| **Premature live promotion.** | Critical | No automatic promotion. Separate explicit PR + full pipeline + Boss approval + live-defect resolution. Shadow-only until then. |
| **Compound with live defects** (-2010 misclassification, idempotency, dormant breaker). | Critical | **Promotion is blocked until those live issues are resolved** — do not layer a new regime model onto a bot with known order/idempotency/breaker defects. |
| **Funding/OI signal untested.** | Medium | Build cached public dataset; re-validate with all 5 families before any promotion claim. Do not assert value until tested. |

---

## 7. Phased Implementation Plan

All phases are **research/non-live** until Phase 6, which is the only phase that touches live code and which requires the full promotion pipeline + Boss approval.

### Phase 0 — Stabilize the research substrate *(non-live, unblocks everything)*
- [ ] **Fix the vol-threshold unit bug** in `regime_v2_signals.py` (√T dailyization — risk-agent ACCEPTed) and re-validate STORMY fires on the 2026-06 crash cluster. *(Resolves F2.)*
- [ ] **Stabilize the forward-replay cache**: add fetch timestamp to the cache key. *(Resolves F7.)*
- [ ] Add per-side slippage (≥0.15%) and model funding as an explicit BEAR-route switching cost in the route-return evaluator.

### Phase 1 — Complete the signal families *(non-live)*
- [ ] Build the **cached public funding/OI/long-short/taker dataset** (historical `fapi`, not live futures). *(Resolves F3.)*
- [ ] Implement the **Hurst-exponent** persistence sub-signal (pure function, calibrated on history) and the **downside-vol shock** and **market-relative-strength** sub-signals. *(Extends §3.2/§3.3/§3.5.)*
- [ ] Tests for every new feature's no-lookahead construction (extend `tests/test_regime_v2_signals.py`).

### Phase 2 — Implement the evidence gates *(non-live — the core of this design)*
- [ ] Add **Gate A** (confidence floor) — already partially present as score cut-points; make it explicit.
- [ ] Add **Gate B** (independent-family agreement, asymmetric defense-first) and the **STORMY override**.
- [ ] Wire **Gate C** (momentum-exhaustion guard — already proven in research; commit it).
- [ ] Tests: gate blocks single-family whipsaw; gate requires N agreeing families; STORMY override fires on extreme vol; asymmetric BULL/BEAR thresholds; no-lookahead.

### Phase 3 — Validate *(non-live)*
- [ ] Walk-forward OOS on **both a down-trend and a non-down-trend window** (resolves F6).
- [ ] Compare against legacy SOL-only, cash/USDC, BTC/ETH/SOL, and buy-and-hold.
- [ ] Objective metrics: market-relative return, drawdown, **whipsaw cost** (flip-rate, rapid-reversal count), missed-rally cost, avoided-crash benefit, **flip-rate / median dwell time**, label-conditional forward returns.
- [ ] Apply the **risk-approved `maxdd-only` robustness gate** (anti-cash backstop ON; cap 15%). Confirm it rejects legacy_sol (~45% DD) and cash, accepts the selector.
- [ ] Emit a **committed canonical smoke artifact** (manifest/records/leaderboard) into `research_outputs/`.

### Phase 4 — Risk & final review *(non-live)*
- [ ] risk-agent review of the gated model + the chosen gate definition (already ACCEPTed `maxdd-only` with conditions).
- [ ] Document the chosen robustness gate in `docs/promotion-pipeline.md`.
- [ ] **Confirm the three live-safety defects are resolved** before any Phase 6 work.

### Phase 5 — Shadow observation *(non-live, in-process audit)*
- [ ] Run the full gated model in `/shadow` alongside the live classifier for a sustained period; daily `regime_promotion_readiness.py` reports agreement/disagreement and readiness. No capital routing.

### Phase 6 — Promotion PR *(LIVE — separate, explicit, gated)*
- [ ] Separate explicit PR wiring the **validated, gated** composite into `momentum_strategy._update_market_regime` as the *candidate producer* for the unchanged 3-cycle hysteresis.
- [ ] Preserve server-side futures stops, notification flood guard, `/shadow`, and the transition planner exactly.
- [ ] Full Backtest → Stress → QA → Risk → Final review pipeline.
- [ ] **Boss approval for SMALL-LIVE** (canary), then **Boss approval for NORMAL-LIVE**.
- [ ] Kill switch and circuit breaker remain fully functional at every stage.

---

## 8. Acceptance Criteria (mapping to issue #72)

| Issue criterion | How this design meets it |
|---|---|
| Research script/artifact for feature extraction + strategy-utility labels | Phases 1–3; strategy-utility labels already in the evaluator. |
| Walk-forward evaluator vs legacy SOL-only | Phase 3; evaluator exists. |
| Switching costs (spot/futures fees, slippage, funding, missed exposure) | Phase 0 (slippage/funding cost) + route-return model. |
| Manifest/records/leaderboard artifacts | Phase 3 canonical committed artifact. |
| Tests (features, no-lookahead labels, hysteresis, acceptance gates) | Phases 1–2; 95+ tests already green. |
| Fresh 90d+ public-data smoke | Phase 3. |
| No automatic live promotion; separate explicit promotion PR | Phases 5–6; design principle #1. |
| Preserve hysteresis, server stops, flood guard, `/shadow` | Layer 4 unchanged; §4.5. |

---

## 9. Open Questions for Review

1. **Is the asymmetric Gate B (stricter BULL than BEAR) the right trade-off?** It encodes "defense-first," but it biases toward sitting out rallies. The non-down-trend validation window (Phase 3) is the empirical check.
2. **Hurst as a live sub-signal vs diagnostic-only?** This design uses it as a trend-family tie-breaker, not a standalone controller — consistent with the issue's HMM constraint. Reviewers may prefer it diagnostic-only initially.
3. **How many breadth coins are required for a trustworthy breadth reading?** Below ~8 the breadth signal is noisy; the coin manager should enforce a minimum.
4. **Should STORMY ever open shorts, or only preserve capital?** This design has STORMY *flatten and protect only* (no new leverage), which is the conservative choice. Opening shorts is reserved for a *confirmed sustained* BEAR per Gate B.

---

## 10. Provenance

- **Live classifier:** `binance_trade_bot/strategies/momentum_strategy.py:186-283`, `binance_trade_bot/regime_hysteresis.py`, `binance_trade_bot/regime_transition_planner.py`, `binance_trade_bot/indicators.py`.
- **Candidate detector:** `binance_trade_bot/regime_v2_signals.py` (#102, committed `dbe3bd6`).
- **Research track:** `scripts/research_regime_v2_evaluator.py`, `scripts/regime_v2_forward_replay.py`, `scripts/regime_v2_gate_ab_comparison.py`, `scripts/regime_promotion_readiness.py`.
- **Prior validation/reviews:** `docs/research/regime_v2_backtest_report.md` (NO-GO), `docs/research/regime-v2-scoping-note.md`, `docs/research/regime-v2-risk-review-gate-definition.md`, `docs/research/regime-v2-gate-risk-review.md`, `docs/promotion-pipeline.md`.
- **Issue:** #72 (17-comment research thread documenting 5 directions of attack and the gate-definition resolution).

*End of design document. Research/design only — no live code, config, strategy, risk params, DB, Docker, or orders were modified. Nothing promoted.*
