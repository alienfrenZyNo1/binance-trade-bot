# HA-004: REJECTION — 4-Leg LINK/NEAR/ETH/DOT Portfolio Candidate

**Date:** 2026-06-27 20:55 UTC
**Decision Authority:** The Boss (Human Approval Authority)
**Decision:** ❌ **REJECTED** — not approved for live deployment
**Requestor:** BOT-LEAD automated check-in (candidate flag `2026-06-27-green-4leg-portfolio-flag.md`)

---

## What Was Requested

Live canary deployment ($500) of a 4-leg multi-strategy portfolio:
- Leg 1: LINKUSDC Donchian (ATR + circuit breaker) @ 3x — 28% weight
- Leg 2: NEARUSDC Donchian (circuit breaker) @ 1x — 28% weight
- Leg 3: ETHUSDC Supertrend (circuit breaker) @ 1x — 25% weight
- Leg 4: DOTUSDT Funding contrarian (z-score fade) @ 3x — 19% weight

Headline claim: Ann 103.3%, Sharpe 2.78, MaxDD -9.9% (full sample); OOS 60/40: 191.1%/2.17/-20.2%.

## Review Trail

- Backtest: `docs/research/portfolio-optimizer-analysis.md`
- Stress test: `docs/research/portfolio-stress-analysis.md` (verdict: 🟢 GREEN)
- Kelly sizing: `docs/research/kelly-sizing-analysis.md`
- **Independent validation: `docs/research/portfolio-stress-INDEPENDENT-VALIDATION.md`** ← the document that changed the decision

## Decision: REJECTED

**The candidate is NOT approved for live deployment.** The GREEN verdict is not supported once methodology defects are corrected.

## Rationale — Three Material Defects Found

### Defect 1: Look-ahead bias in the DOT funding_contrarian leg (DISQUALIFYING)

`simulate_funding_leg` decides position `pos[t]` from funding z-score at bar `t`, then earns bar `t`'s own close-open return. The z-score at bar `t` uses `funding_rate[t]`, which is only knowable at bar `t`'s close. Trading it on bar `t`'s same-bar move is a **0-bar look-ahead**.

Quantified impact of the honest 1-bar shift:
| DOT leg window | As-coded (biased) | Corrected (honest) |
|---|---:|---:|
| Full sample Sharpe | 2.41 | **0.78** |
| OOS 60/40 Sharpe | 3.14 | 1.86 |
| Train 0-60% Sharpe | 1.75 | **-0.79** |

Most tellingly: **with the look-ahead fixed, the tangency optimizer assigns DOT 0% weight.** The "49% DOT" that drives the 191.1% OOS return is an artifact of the look-ahead making DOT look dominant on the train half. The trend legs (LINK/NEAR/ETH) were audited and are causal — the look-ahead is confined to DOT.

### Defect 2: OOS numbers cite two different "headline" figures; the favorable one uses data-snooping weights

The stress test's honest frozen-weights OOS is **191.1% / 2.17 / -20.2%** (MaxDD -20.2% **breaches** the <20% mandate). The Kelly/escalation memo cites **~105% / 2.42 / -10.7%** — but those use full-sample-optimized weights (28/28/25/19) applied to the test half, i.e. weights fit on data that includes the test period. That is a data-snooping leak. After correcting the DOT look-ahead, the honest OOS becomes **55% / 1.67 / -10.4%**.

### Defect 3: Rolling-window validation fails 4/6 consecutive periods including the most recent month

| Test window | Calendar | Ann | Sharpe |
|---|---|---:|---:|
| 89-99% | 2026-05-17 → 06-24 | **-50.1%** | -2.29 |
| 80-89% | 2026-04-13 → 05-21 | -25.8% | -2.29 |
| 50-60% | 2025-12-21 → 01-28 | -35.2% | -3.25 |

The edge is concentrated in late-January → mid-April 2026 (~2.5 months). The strategy loses in every other period tested, including the most recent month at -50% annualized. The multi-split "5/7 pass" is itself partly an artifact of the strong regime landing in the larger test windows. This is a regime-specific, not structural, edge.

## Risk Parameters Confirmed (unchanged)

- Max daily loss: 3% of capital
- Max total drawdown: 10% → full halt
- Leverage cap: 1x (3x would require separate explicit authorization — NOT granted here)
- Futures: enabled but under 1x cap and 50% margin limit
- Kill switch: permanent, untouched

## Conditions to Reconsider This Candidate

1. **Fix the DOT funding look-ahead** (shift signal +1 bar) and re-run the ENTIRE stress suite. With the fix, even in-sample Full Sharpe is ~2.29 and the optimizer drops DOT.
2. **Re-derive all OOS numbers using only train-fit weights** on the test half. No full-sample-fit weights on test data.
3. **Acknowledge and address the rolling-window failure.** A strategy that loses in 4/6 consecutive periods is not deployment-ready regardless of any single-split Sharpe.
4. If proceeding at all, treat only the **LINK + ETH trend legs** (causal, individually Sharpe ~1.7-2.2 OOS) as the candidate. Drop DOT and NEAR until they prove themselves out-of-sample.
5. Require ≥3 more months of true OOS data before sizing up.

## Strategic Directive Issued

Research pivot: stop refining the 4-leg portfolio as-specified. Focus the next research cycle on:
- Re-validate the LINK + ETH causal trend legs standalone (drop DOT, drop NEAR pending OOS proof)
- Deep-test grid trading with the drawdown-control stack (combo_vt60) which showed Sharpe ~1.43, MaxDD -14.8%, ~45% annualized robustly across walk-forward — the most honest config found this cycle
- Do NOT bring any candidate forward until the look-ahead is fixed and rolling-window performance is positive across the majority of periods

---

## Live Operational Note: Bug #110 (state reconciliation)

Separately from this research decision, the live bot has a state mismatch: after restart it reports `[TIA][USDC]` in scout logs while actually holding INJ on Binance. Root cause is `initialize_current_coin()` random choice ignoring actual balance. This is HIGH priority — if a momentum signal fires while `current_coin` is wrong, the rotation logic could sell the wrong coin or buy a duplicate. Assigned to Marcus (strategy-developer). **Must be fixed before any new strategy goes live.**

---

*Capital preservation is the first priority. When in doubt, reject. The pipeline is mandatory; Eleanor's skeptical review (here performed as independent validation) is required and caught a defect that would have lost real money.*
