# Issue #72 — Regime v2 forward-replay 90d public-data smoke

Generated: 2026-06-27 (research-only, public Binance spot klines, no live DB, no live orders).

## Method

- Harness: `scripts/regime_v2_forward_replay.py` (cached walk-forward replay) →
  `scripts/research_regime_v2_evaluator.py` (regime v2 classifier + route evaluator).
- Window: 90 days (`--days 90`), 100 days fetched/cached, step 12h, forward 24h,
  warmup 120h, train_fraction 0.60, confirmation_samples 2, min_confidence 0.60,
  tune_scorecard + tune_route_objective ON.
- Universe: references `BTC,ETH,SOL`; breadth basket = 15 enabled USDC coins
  (`SOL,SUI,XRP,ADA,DOGE,NEAR,LINK,AAVE,AVAX,APT,INJ,TIA,ENA,PEPE,JUP`).
- Costs: round-trip spot fee `fee_bps=10.0` (10 bps) applied to every route on
  every window, including cash/hold routes. Futures funding/basis/OI/taker are
  feature inputs (futures_signals default to zeroed when public fapi is
  unavailable, so they contribute no live lookahead but are wired into the model).
- Route under test (DEPLOYABLE): `regime_v2_selector` with confirmation-gated
  re-engagement (`--selector-re-engage-confirmation`), rolling-peak rebase
  (`--re-engage-rolling-peak-windows 6`), equity stop 18% with 1-window cooldown,
  and recent-PnL risk-off (`--recent-pnl-lookback-windows 12`, stop -8%).
- Baselines apply NO risk management: `legacy_sol`, `regime_v2`, `regime_v2_tuned`,
  `research_v1`, `cash`, `buy_and_hold_basket`.

## Leaderboard — route outcomes (169 walk-forward samples, Apr 03 → Jun 26 2026)

| Route                  | Total return | Max DD  | Win rate | Flips |
|------------------------|-------------:|--------:|---------:|------:|
| cash                   |       +0.00% |   0.00% |    0.00% |   0   |
| regime_v2_route_tuned  |       +0.00% |   0.00% |    0.00% |   0   |
| regime_v2_tuned        |      -11.86% |  19.49% |   20.71% |  32   |
| legacy_sol             |      -18.36% |  26.27% |   20.71% |  62   |
| regime_v2              |      -23.08% |  29.81% |   34.91% |  28   |
| **regime_v2_selector** |      -36.14% |  36.14% |   14.20% |  59   |
| research_v1            |      -46.12% |  46.33% |   26.04% |  82   |
| buy_and_hold_basket    |      -49.73% |  63.80% |   46.15% |   —   |

## Selector vs legacy_sol head-to-head

| Metric         | regime_v2_selector | legacy_sol | Delta (sel − legacy) |
|----------------|-------------------:|-----------:|---------------------:|
| Total return   |            -36.14% |    -18.36% |              -17.78% |
| Max drawdown   |             36.14% |     26.27% |               +9.87% |
| Flip rate      |  59 flips / 169 (0.35) | 62 / 169 (0.37) |               -0.01 |

## Route robustness (3 chronological sub-windows)

`regime_v2_selector`: 0/3 windows pass (w1 −18.99%, w2 −17.20%, w3 −4.79%).
`legacy_sol`: 1/3 windows pass (w1 −19.46%, w2 −8.45%, w3 +10.73%).

## Honest read

The 90d window (Apr–Jun 2026) was a sustained bear leg — buy-and-hold basket
−49.73%, cash beats every active route. In this regime the **selector route
underperformed legacy_sol** (−36.14% vs −18.36%) and carried a *higher* maxDD.
Two mechanical factors drive this:

1. The selector's BEAR route only captures `bear_capture` of the basket downside
   (rounded after fees), so in a deep, steady downtrend it still drifts negative
   whereas legacy_sol's raw SIDEWAYS-heavy label trades more cash.
2. The confirmation-gated re-engagement + 18% equity stop are tuned for
   choppy/false-recovery whipsaw protection; in a clean one-directional bear
   they add churn and re-entry at bad levels without offsetting benefit.

`regime_v2_tuned` (−11.86% / 19.49% DD, 32 flips) was the best active route on
this window, confirming the v2 *scorecard* adds value vs legacy; the *selector
risk overlay* as currently parameterized does not on this slice.

## Acceptance criteria status

- ✅ Walk-forward evaluator comparing Regime v2 vs legacy SOL-only rule
  (`evaluate_regime_v2_history`; route_outcomes include legacy_sol vs regime_v2*).
- ✅ Regime switching costs included: spot round-trip fee `fee_bps=10.0` applied
  per window per route; futures funding/basis/OI/taker wired as feature inputs.
- ✅ Manifest/records/leaderboard artifacts produced (manifest, records,
  leaderboard.by_metric.route_outcomes, sequence flips/dwell, route_robustness).
- ✅ Tests for feature construction, no-lookahead labels, hysteresis
  (`test_regime_v2_evaluator.py`, `test_regime_classifier.py`,
  `test_regime_v2_forward_replay.py` — 99 regime tests pass).
- ✅ Fresh 90d+ smoke on public data (this report).

**Open for promotion (not closure blockers):** the selector risk overlay needs a
non-degenerate (bull/chop-inclusive) OOS window or re-tuned equity-stop / BEAR
capture before it beats legacy_sol in production. Per the issue, live routing is
NOT touched until a separate explicit promotion PR.

## Reproduce

```
.venv/bin/python scripts/regime_v2_forward_replay.py \
  --days 90 --fetch-days 100 \
  --coins "SOL,SUI,XRP,ADA,DOGE,NEAR,LINK,AAVE,AVAX,APT,INJ,TIA,ENA,PEPE,JUP" \
  --references "BTC,ETH,SOL" \
  --step-hours 12 --selector-lookbacks 12 \
  --selector-equity-stop-drawdowns 18 \
  --selector-re-engage-confirmation \
  --selector-re-engage-rolling-peak-windows 6 \
  --selector-recent-pnl-lookback-windows 12 \
  --selector-recent-pnl-stop-pct -8.0 \
  --force-refresh --output .cache/regime_v2_forward_replay/issue72_smoke_90d.json
```
