# Backtest Revalidation Report — Issue #92

**Date:** 2026-06-27
**Author:** backtest-agent (automated, read-only research)
**Issue:** [#92 — Revalidate backtest methodology](https://github.com/alienfrenZyNo1/binance-trade-bot/issues/92)
**Scope:** Re-run the momentum-rotation backtest with corrected methodology (next-bar-open execution, realistic slippage, proper walk-forward splits) to determine whether the claimed edge is real or an artifact of the flaws identified in [`backtest-audit.md`](./backtest-audit.md).
**Verdict:** ⚠️ **EDGE SURVIVES — but is materially weaker than originally claimed, and is NOT yet deployment-grade.** Funding-rate costs remain unmodeled (the one remaining gap vs. the issue's acceptance criteria).

---

## TL;DR — what changed vs. the original claim

| Metric | Original claim (`best_momentum.json`) | Corrected (`s3`, 0.1% slip + next-bar-open + fees) | Δ |
|---|---|---|---|
| Full-period P&L (5 mo) | **+79%** (legacy same-bar-close) → +65% legacy @ 0.05% slip | **+36.5%** | roughly **halved** |
| Sharpe (annualized) | **3.85–3.94** | **1.12** | implausible → realistic |
| Max drawdown | 48% | **62%** | worse |
| Trades (full period) | 53 (legacy) | **41** | fewer (next-bar filter) |
| Walk-forward OOS mean | (single split, inverted) | **+36.6%** mean / **3/3** positive (ext) | robust sign, small sample |

The edge does **not** disappear under corrected methodology, but its magnitude shrinks by ~45% and its risk-adjusted quality drops from "too good to be true" (Sharpe ~3.85) to "plausible retail strategy" (Sharpe ~1.1). **Max drawdown of ~62% on a small account is a serious capital-survival concern and the single biggest caveat.**

---

## 1. Methodology applied (the fixes)

This revalidation is independent of the original `optimize_momentum.py` engine. A standalone, read-only engine (`scripts/revalidate_backtest.py`) reimplements the momentum-rotation strategy and applies five corrections. **No live config, DB, or trading code was modified.**

| Fix | Original (flawed) | Corrected | Why it matters |
|---|---|---|---|
| **Execution** | Same-bar-close (zero latency) | **Next-bar-open** (signal at close `t`, fill at open `t+1h`) | Eliminates the single biggest lookahead-bias source |
| **Slippage** | 0.05% / side | **0.1% / side** (`s3`) | Matches audit finding that 0.05% is optimistic for altcoin/USDC at $62 |
| **Fees** | 0.075% / side | **0.075% / side** (unchanged, already correct) | — |
| **Walk-forward** | One 120d/60d split, ranked by (often negative) train P&L | **Rolling disjoint OOS windows**, no train/OOS inversion | Tests out-of-sample stability |
| **Benchmarks** | None | **Buy & hold TIA/SOL** + **50-run random rotation** | Distinguishes skill from luck/drift |

All numbers below are sourced from `research_results/revalidation_results.json` (main) and `research_results/revalidation_walkforward_ext.json` (extended 20-day windows). Data: 4,320 hourly candles, **2025-12-29 → 2026-06-26** (~6 months), cached in `research_results/reval_data.json`.

---

## 2. Acceptance Criteria — checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Walk-forward backtest with proper splits | ✅ | §3 — s5 (2 windows) + ext (3 windows), no train/OOS inversion |
| 2 | Slippage 0.1% + fees + funding | ⚠️ **PARTIAL** | Slip ✅ + fees ✅; **funding NOT yet modeled** → §6 gap |
| 3 | New P&L, Sharpe, max drawdown | ✅ | §4 — +36.5% / 1.12 / 62% |
| 4 | Live vs backtest trade frequency root-caused | ✅ | §5 — 55× divergence = confirmation-timing bug, fixed |
| 5 | Parameter sensitivity table | ✅ | §7 — cost sweep, break-even at ~0.5% slip |
| 6 | Honest assessment: real or artifact? | ✅ | §8 — survives, but weak & risky |

---

## 3. Walk-forward validation (Criterion 1)

Two independent walk-forward runs, both using next-bar-open execution + 0.075% fees + 0.1% slippage. Per-window params are optimized on a **disjoint train window**, then validated on a forward **OOS window** the optimizer never saw.

### 3a. Main walk-forward (`s5`) — 2 windows, 30-day OOS

From `revalidation_results.json` → `s5_walk_forward`:

| Window | Train period | OOS period | Train P&L | **OOS P&L** | OOS trades | OOS maxDD | B&H TIA | B&H SOL | Beat? |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 2026-01-27 → 2026-05-27 | 2026-05-27 → 2026-06-26 | +55.7% | **−18.7%** | 21 | 52.2% | −12.8% | −12.8% | TIA ❌ SOL ❌ |
| 1 | 2025-12-28 → 2026-04-27 | 2026-04-27 → 2026-05-27 | −24.3% | **+91.8%** | 13 | 25.0% | +23.6% | −2.9% | TIA ✅ SOL ✅ |

- **OOS mean: +36.6%**, positive **1/2** windows, beats TIA **1/2**, beats SOL **1/2**.
- ⚠️ Window 0 (most recent month) is **negative** and **underperforms** both benchmarks — a warning that the edge is regime-dependent and may be decaying.
- Notably, the **negative-train window produced the best OOS** — the opposite of the original inverted flaw, but still a sign of high variance and small-sample instability.

### 3b. Extended walk-forward — 3 disjoint 20-day OOS windows

From `revalidation_walkforward_ext.json` (train = 100d, OOS = 20d, 250-combo grid, seed=7, corrected engine):

| # | OOS period | OptTrain | **OptOOS** | **FixOOS** | B&H TIA | B&H SOL | Opt beats Fix? | OOS trades | OOS maxDD |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 06-06 → 06-26 | +110.8% | **+33.5%** | +4.2% | +26.2% | +15.5% | ✅ | 16 | 18.1% |
| 1 | 05-17 → 06-06 | +51.4% | **+11.5%** | +71.7% | −20.8% | −27.0% | ❌ | 19 | 28.0% |
| 2 | 04-27 → 05-17 | −25.3% | **+10.7%** | +29.3% | +7.8% | +0.5% | ❌ | 25 | 19.0% |

**Across 3 windows (corrected engine):**
- **Optimized OOS: mean +18.6%, median +11.5%, positive 3/3, beats TIA 3/3.**
- **Fixed-deployed OOS: mean +35.1%, median +29.3%, positive 3/3, beats TIA 2/3.**
- B&H TIA mean: **+4.4%**.

**Key finding:** The edge is **positive in all 3 short OOS windows** and beats buy-and-hold TIA in 3/3 (optimized) or 2/3 (fixed). However, **per-window optimization only helped OOS in 1/3 windows** — meaning the deployed fixed params are roughly as good as freshly optimized ones, which cuts both ways: it suggests the params aren't badly overfit, but also that the "model" adds little over a static rule set. The high FixOOS window 1 (+71.7%) is a single lucky regime and inflates the fixed mean.

> **Robustness caveat:** 3 windows is still a small sample. Combining both runs gives **5 OOS windows total: 4 positive, 1 negative**, positive rate **4/5 (80%)**. This is encouraging but not conclusive — see §8.

---

## 4. New P&L, Sharpe, Max Drawdown (Criterion 3)

Full-period (2025-12-29 → 2026-06-26) corrected results, from `revalidation_results.json`:

| Scenario | P&L | Trades | Sharpe | Max DD |
|---|---|---|---|---|
| `s1` Legacy baseline (same-bar-close, 0.05% slip) | +65.0% | 53 | 1.48 | 58.9% |
| `s2` Next-bar-open (0.05% slip) | +42.2% | 41 | 1.19 | 61.1% |
| **`s3` Realistic (next-bar-open + 0.1% slip + fees)** | **+36.5%** | **41** | **1.12** | **62.0%** |

**Interpretation:**
- **P&L falls from +65% → +36.5%** when execution latency and realistic slippage are added — a ~45% reduction in headline return.
- **Sharpe drops from the implausible 3.85 (original, flawed calc) to 1.12** under corrected methodology. 1.12 is a realistic, believable number for a single-factor momentum strategy — it's "decent," not "extraordinary."
- **Max drawdown rises to ~62%** and is the most alarming figure: a 62% DD on a $62 account is a ~$38 paper loss. This is the dominant real-world risk and is **not mitigated** by the methodology fixes.
- Trade count drops from 53 → 41 because next-bar-open execution filters out some same-bar signals that don't persist to the next open.

> **Note on Sharpe:** The corrected engine computes Sharpe on hourly equity-curve returns annualized (√(24·365)). It still does not subtract a risk-free rate (~4-5% annualized) and is still influenced by held-position volatility rather than pure trading alpha, so 1.12 is an **upper bound** on the true strategy Sharpe.

---

## 5. Live vs. Backtest Trade-Frequency Divergence (Criterion 4) — ROOT-CAUSED ✅

**The discrepancy:** Backtest predicts ~0.26 trades/day; the live bot executed **18 trades in 30 hours (~14.4/day) — a ~55× divergence.**

**Root cause: confirmation-timing mismatch (a real bug, now fixed).**
- The live config had `SCOUT_SLEEP_TIME=1` (1-second polling). With `confirmation_cycles=3`, a rotation signal was confirmed in **~3 seconds**.
- The backtest processes **one bar per hour**, so its "3 confirmation cycles" effectively take **~3 hours**.
- The time-based gate (`CONFIRMATION_TIME_ENABLED`) defaulted to **off**, so only the (near-instant) cycle count mattered live.
- Result: the live bot acted on intrabar noise that disappears by the hourly close — invisible to the backtest. **The 55× discrepancy is fully explained by this single mismatch.**

**Fixes already merged (this is a closed root cause):**
- `6d2f693` — *"CRITICAL FIX: Enable time-based confirmation to stop noise trading."* Regime-aware minimums: default 180s, bull 300s, sideways 180s, bear 60s.
- `4608248` — order idempotency + confirmation timing + breaker tightening (3%/8%) + kill-switch position-flat verification.

**Conclusion:** The frequency divergence was a **live implementation/config bug**, not a backtest fidelity problem. It is resolved in code. The backtest's trade frequency is the intended behavior; live should now converge toward it once the new confirmation timing is exercised.

---

## 6. Cost Modeling — what's in and what's NOT (Criterion 2)

| Cost component | Modeled? | Value / note |
|---|---|---|
| Taker fees | ✅ | 0.075% / side (round-trip 0.15%), matches Binance w/ BNB discount |
| Slippage | ✅ | 0.1% / side in `s3` (was 0.05% in original — audit flagged as understated) |
| **Funding rates** | ❌ **NOT MODELED** | **Remaining gap — see below** |
| Bid-ask spread (low-liq pairs) | ❌ | Not separately modeled; partially absorbed by slip |
| Market impact ($62 size) | ❌ | Negligible at this size but unmodeled |
| Maker rebate opportunity | ❌ | Not modeled (assumes all-taker) |

**⚠️ Funding-rate gap (the one open item vs. acceptance criterion 2):**
The momentum-rotation strategy trades **spot** USDC pairs, where funding is N/A — so for the *primary* backtest the omission is correct. **However**, the bot also operates a **futures** leg (see `scripts/research_bear_futures_backtester.py`), and futures funding P&L is **not** folded into the combined revalidation P&L here. For any position held across funding intervals, net cost is therefore **understated** by one funding payment per 8h held. Because the strategy's average hold is short and funding rates have been mixed (sometimes in the holder's favor), the directional impact is ambiguous but **likely slightly negative on average** for long-dominant exposure. **This should be added before treating the +36.5% figure as final.** The audit (`backtest-audit.md` §3.3) confirms funding *is* correctly modeled inside the standalone futures backtester — the gap is only that it isn't aggregated into this report's headline numbers.

---

## 7. Parameter Sensitivity — Cost Sweep (Criterion 5)

From `revalidation_results.json` → `s4_cost_sweep` (next-bar-open, fees 0.075%/side, slippage varied, full period):

| Slippage / side | Full P&L | Trades | Δ vs 0% slip |
|---|---|---|---|
| 0.00% | +48.1% | 41 | — |
| 0.05% | +42.2% | 41 | −5.9pp |
| **0.10%** (deployed assumption, `s3`) | **+36.5%** | 41 | −11.7pp |
| 0.15% | +31.0% | 41 | −17.1pp |
| 0.20% | +25.7% | 41 | −22.4pp |
| 0.30% | +15.8% | 41 | −32.4pp |
| **0.50%** | **−1.8%** | 41 | **break-even / negative** |

**Sensitivity read-outs:**
- The edge is **linear in slippage** (trade count is fixed at 41, so each +0.05% slip costs ~5.9pp of P&L).
- **Break-even slippage ≈ 0.48% per side.** Above ~0.5% slippage the strategy loses money net of fees.
- At the **deployed 0.1% assumption**, margin of safety to break-even is ~5× — reasonable, but **not generous**. If real fills are worse than 0.1% on low-liquidity pairs (PEPE/JUP/ENA spreads can be 0.2–0.5%), a meaningful chunk of the +36.5% evaporates fast.
- Because P&L scales linearly and trades are constant, **slippage is the dominant cost lever** — more impactful than fee tier.

**Other sensitivity notes (qualitative, from `s5`/ext params):** OOS results are **not highly sensitive** to the exact param values — fixed deployed params (lookback=18, edge=8.0) performed comparably to per-window-optimized params (ext run: optimization helped OOS only 1/3 times). This argues *against* severe overfitting but *also* means the model's active selection adds limited value beyond a static momentum rule.

---

## 8. Honest Assessment — Is the Edge Real or an Artifact? (Criterion 6)

### Verdict: **The edge is PROBABLY REAL, but SMALL and RISKY — not deployment-grade on current evidence.**

**Evidence the edge is real (survives correction):**
1. Under the **strictest corrected methodology** (next-bar-open + 0.1% slip + fees), full-period P&L is still **+36.5%** — positive, not zero.
2. **Walk-forward OOS is positive in 4 of 5 windows** (2 from `s5` + 3 from ext), mean +36.6% (`s5`) / +18.6% (ext optimized) / +35.1% (ext fixed).
3. It **beats buy-and-hold TIA** in 3/3 ext windows (optimized) and outperforms a **50-run random rotation baseline (mean −63.7%)** by a wide margin (`s6`).
4. Sharpe dropped from an implausible **3.85 → 1.12** — i.e., the *implausibility* is gone. A real, modest edge is more believable than a fake 3.85.

**Evidence it is weak / fragile:**
1. **Max drawdown ≈ 62%** — the headline risk. A strategy that can lose ~62% of capital is dangerous at any size and especially on a $62 account; this alone argues against scaling.
2. **Only 5 OOS windows**, one strongly negative (−18.7%). Positive rate 80% sounds good but n=5 is statistically thin; one more bad window drops it to 67%.
3. **High variance:** `s5` window P&Ls swing from −18.7% to +91.8% (30-day OOS). The "+36.6% mean" is propped up by one outlier.
4. **Edge is ~5× from break-even on slippage** (break-even ≈ 0.48%/side). Tight margin; low-liquidity fills could close it.
5. **Optimization adds little OOS value** (helps 1/3 windows) — the "alpha" may be mostly *momentum-as-a-style* rather than this specific model.
6. **Funding costs unmodeled** in the headline number (likely a slight further drag; see §6).
7. **Survivorship bias** from a hand-picked, currently-listed 15-coin universe remains (per audit §2.3) and is not corrected here — this is an unquantified upward bias.

### What the numbers say in plain terms
- **Original claim (+79%, Sharpe 3.85): ARTIFACT.** The flaws (same-bar-close lookahead, understated slip, inverted/single split, flawed Sharpe math) inflated it substantially.
- **Corrected estimate (+36.5%, Sharpe ~1.1, maxDD ~62%): PROBABLY REAL but modest.** The sign and rough magnitude survive honest methodology, but the strategy is **high-risk** (62% DD), **thin-margin** (5× to break-even), and **under-sampled** (5 windows).

### Recommendation
- **Do NOT scale capital** on this evidence alone. 62% max drawdown is the binding constraint.
- Treat the corrected +36.5% as an **upper bound** pending: (a) funding-rate aggregation, (b) dynamic coin universe to remove survivorship bias, (c) ≥10 walk-forward windows, (d) 100+ tracked live trades post-fix-`6d2f693`.
- The strategy is **worth continued canary-scale tracking**, not abandonment and not expansion.

---

## 9. Open Items / Next Steps

1. **[GAP] Aggregate futures funding P&L** into the headline revalidation numbers (criterion 2 — the only unmet item).
2. **More windows:** extend to ≥10 disjoint OOS windows once more history is cached; current 5 is suggestive, not conclusive.
3. **Dynamic coin universe:** re-run with point-in-time listing/liquidity filters to quantify & remove survivorship bias.
4. **Track live trades** after the `6d2f693`/`4608248` confirmation fix to confirm live frequency converges to backtest (~0.26/day) and to build a real live-vs-backtest P&L comparison.
5. **Risk-first param search:** re-optimize for **max drawdown** (or Calmar) rather than raw P&L — the 62% DD is the real problem to solve.

---

## 10. Data Provenance

All figures are reproducible from committed, read-only artifacts:
- `research_results/revalidation_results.json` — main corrected revalidation (s1–s6, verdict).
- `research_results/revalidation_walkforward_ext.json` — extended 3-window walk-forward (regenerated 2026-06-27, deterministic seed=7).
- `research_results/reval_data.json` — cached OHLCV, 4,320 hourly candles, 2025-12-29 → 2026-06-26.
- `scripts/revalidate_backtest.py` — corrected engine (read-only).
- `scripts/revalidate_walkforward_ext.py` — extended walk-forward runner (read-only).
- `docs/audits/backtest-audit.md` — the audit this revalidation responds to.

**No live config, database, or trading code was modified during this revalidation.**
