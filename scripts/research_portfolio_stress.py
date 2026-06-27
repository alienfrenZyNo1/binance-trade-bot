#!/usr/bin/env python3
"""Portfolio Stress Test — Multi-Split Walk-Forward + Monte Carlo + Slippage.

Candidate under test (the optimizer's #1 gate-passing portfolio):
    Leg 1: LINKUSDC donchian atr2_vf_cb @ 3x
    Leg 2: NEARUSDC donchian cbreaker   @ 1x
    Leg 3: ETHUSDC supertrend cbreaker  @ 1x
    Leg 4: DOTUSDT funding_contrarian   @ 3x
    Allocation: tangency weights optimized on train (headline 28/28/25/19).

Headline (single 60/40 split, full sample):
    Full:   Ann 103.3%, Sharpe 2.78, MaxDD -9.9%
    OOS:    Ann 191.1%, Sharpe 2.17, MaxDD -20.2%

THE PROBLEM: a different combo (APT/AVAX/OP) showed catastrophic split-point
sensitivity (50/50: -4.6% ann; 60/40: +111%; 70/30: -65%). If this winner shows
the same pattern it is OVERFIT and must NOT be escalated.

TESTS:
    1. Multi-split walk-forward: 40/60, 50/50, 55/45, 60/40, 65/35, 70/30, 75/25.
       For each: optimize tangency weights on train, freeze, apply to test.
    2. Rolling expanding-window validation (train grows 40%->90%, test = next 10%).
    3. Monte Carlo bootstrap: 5000 resamples of per-leg OOS daily returns,
       compounded and combined at portfolio weights.
    4. Slippage sensitivity on full sample: 0.03%, 0.05%, 0.10%, 0.15% per side.

VERDICT:
    GREEN (escalate):  >=5/7 splits keep OOS Sharpe>1.0 AND OOS Ann>50%;
                       MC P(Sharpe>1.0) >= 60%; slippage Sharpe >= 1.0 @ 0.10%/side.
    YELLOW (more data): 3-4/7 splits pass; MC P(Sharpe>1.0) 40-60%.
    RED (overfit):      <=2/7 splits pass OR MC P(Sharpe>1.0) < 40%.

Reuses the exact per-leg simulation engine from:
    scripts/research_dd_controlled_trend.py  (eng: signals, overlays, simulate)
    scripts/research_portfolio_dd_trend.py   (pp: run_leg, portfolio_metrics, funding)
    scripts/research_portfolio_optimizer.py  (opo: weighted_monthly_rebal, alloc, moments)

Outputs:
    docs/research/portfolio-stress-analysis.md
    docs/research/portfolio-stress-data.json

Numbers, not adjectives. Honest verdict.
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ─── Reuse existing engines ──────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402
import research_portfolio_dd_trend as pp  # noqa: E402
import research_portfolio_optimizer as opo  # noqa: E402

REPO_ROOT = HERE.parent
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_MD = DOCS_DIR / "portfolio-stress-analysis.md"
REPORT_JSON = DOCS_DIR / "portfolio-stress-data.json"

INITIAL_CAPITAL = pp.INITIAL_CAPITAL  # 10_000
IC = INITIAL_CAPITAL
DAY_MS = pp.DAY_MS

# Baseline costs (saved so we can restore after slippage sweep)
BASE_FEE = eng.FEE_RATE        # 0.0004
BASE_SLIP = eng.SLIPPAGE       # 0.0003
BASE_COST_SIDE = eng.COST_SIDE  # 0.0007

# ─── Winning combo (must test EXACTLY this) ───────────────────────────────────
WINNING_LEGS = [
    pp.trend_leg("LINKUSDC_donchian_atr2_vf_cb_3x"),
    pp.trend_leg("NEARUSDC_donchian_cbreaker_1x"),
    pp.trend_leg("ETHUSDC_supertrend_cbreaker_1x"),
    pp.funding_leg("DOTUSDT", 3.0),
]
WINNING_LABELS = [
    "LINKUSDC_donchian_atr2_vf_cb_3x",
    "NEARUSDC_donchian_cbreaker_1x",
    "ETHUSDC_supertrend_cbreaker_1x",
    "DOTUSDT_funding_contrarian_3x",
]
SHORT_LABELS = ["LINK3x", "NEARcb1x", "ETHST1x", "DOTFUND3x"]
HEADLINE_WEIGHTS = np.array([0.28, 0.28, 0.25, 0.19])

SPLIT_FRACS = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
SLIPPAGE_LEVELS = [0.0003, 0.0005, 0.0010, 0.0015]
MC_NITER = 5000
MC_SEED = 7


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


def fmt_pct(x: float, dec: int = 1) -> str:
    return f"{x * 100:.{dec}f}%"


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: run the 4 legs on a windowed timeline, build portfolio at given weights
# ═══════════════════════════════════════════════════════════════════════════════
def run_legs_window(legs, labels, packs, targets, funding_data,
                    ref_ts, start, end):
    """Run each leg on native bars [start:end], aligned to ref_ts. Returns
    dict label -> {eq_aligned, daily_returns, trades, metrics}."""
    out = {}
    for label, spec in zip(labels, legs):
        lr = pp.run_leg(spec, packs, targets, funding_data, ref_ts, IC, start, end)
        eq = lr["eq_aligned"]
        out[label] = {
            "eq_aligned": eq,
            "daily_returns": opo.leg_daily_returns(eq, ref_ts),
            "trades": lr["trades"],
            "metrics": lr["metrics"],
        }
    return out


def portfolio_at_weights(leg_data, labels, weights, ref_ts):
    eqs = [leg_data[l]["eq_aligned"] for l in labels]
    port_eq = opo.weighted_monthly_rebal(eqs, np.asarray(weights, float), ref_ts, IC)
    return port_eq, pp.portfolio_metrics(port_eq, ref_ts)


def optimize_tangency(leg_data, labels):
    """Tangency weights from per-leg daily returns (mirrors optimizer)."""
    R, mu, cov, stds = opo._moments_from_legs(leg_data, labels)
    return opo.compute_alloc("tangency", mu, cov, stds)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST 1: Multi-split walk-forward
# ═══════════════════════════════════════════════════════════════════════════════
def test_multisplit(packs, targets, funding_data) -> dict:
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    rows = []
    for frac in SPLIT_FRACS:
        split = int(n * frac)
        ref_ts_train = ref_pack.ts[:split].copy()
        ref_ts_test = ref_pack.ts[split:].copy()

        # train: run legs, optimize tangency
        train_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                                    funding_data, ref_ts_train, 0, split)
        w = optimize_tangency(train_leg, WINNING_LABELS)
        _, train_m = portfolio_at_weights(train_leg, WINNING_LABELS, w, ref_ts_train)

        # test: run legs, apply frozen weights
        test_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                                   funding_data, ref_ts_test, split, n)
        test_eq, test_m = portfolio_at_weights(test_leg, WINNING_LABELS, w, ref_ts_test)

        oos_pass = test_m["sharpe"] > 1.0 and test_m["ann_return"] > 0.50
        rows.append({
            "split": f"{int(frac*100)}/{int((1-frac)*100)}",
            "split_frac": frac,
            "n_train_bars": split, "n_test_bars": n - split,
            "weights": w.tolist(),
            "train": {"ann": train_m["ann_return"], "sharpe": train_m["sharpe"],
                      "max_dd": train_m["max_dd"]},
            "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                     "max_dd": test_m["max_dd"]},
            "oos_pass": bool(oos_pass),
        })
        print(f"    split {int(frac*100)}/{int((1-frac)*100)}: "
              f"train shp {train_m['sharpe']:.2f}/ann {fmt_pct(train_m['ann_return'])}, "
              f"test shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/"
              f"dd {fmt_pct(test_m['max_dd'])} {'✅' if oos_pass else '❌'}")
    n_pass = sum(1 for r in rows if r["oos_pass"])
    return {"rows": rows, "n_pass": n_pass, "n_total": len(rows)}


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST 2: Rolling expanding-window validation
# ═══════════════════════════════════════════════════════════════════════════════
def test_rolling(packs, targets, funding_data) -> dict:
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    rows = []
    train_frac = 0.40
    step = 0.10
    while train_frac + step <= 1.0 + 1e-9:
        test_frac = min(train_frac + step, 1.0)
        train_end = int(n * train_frac)
        test_end = int(n * test_frac)
        if test_end <= train_end:
            break
        ref_ts_train = ref_pack.ts[:train_end].copy()
        ref_ts_test = ref_pack.ts[train_end:test_end].copy()

        train_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                                    funding_data, ref_ts_train, 0, train_end)
        w = optimize_tangency(train_leg, WINNING_LABELS)

        test_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                                   funding_data, ref_ts_test, train_end, test_end)
        _, test_m = portfolio_at_weights(test_leg, WINNING_LABELS, w, ref_ts_test)

        rows.append({
            "window": f"train 0-{int(train_frac*100)}% / test {int(train_frac*100)}-{int(test_frac*100)}%",
            "train_frac": train_frac, "test_frac": test_frac,
            "n_test_bars": test_end - train_end,
            "weights": w.tolist(),
            "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                     "max_dd": test_m["max_dd"], "total_return": test_m["total_return"]},
        })
        print(f"    {rows[-1]['window']}: shp {test_m['sharpe']:.2f}/"
              f"ann {fmt_pct(test_m['ann_return'])}/dd {fmt_pct(test_m['max_dd'])}")
        train_frac += step
    n_pos = sum(1 for r in rows if r["test"]["ann"] > 0)
    n_sharpe = sum(1 for r in rows if r["test"]["sharpe"] > 1.0)
    return {"rows": rows, "n_windows": len(rows),
            "n_positive_ann": n_pos, "n_sharpe_gt_1": n_sharpe}


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST 3: Monte Carlo bootstrap (per-leg OOS daily returns resampled)
# ═══════════════════════════════════════════════════════════════════════════════
def test_montecarlo(oos_leg_data, ref_ts_test, weights, n_iter=MC_NITER,
                    seed=MC_SEED) -> dict:
    """Bootstrap the OOS per-leg daily-return sequence with replacement.
    Each iteration: resample each leg's daily returns independently, compound
    from 1.0, combine at portfolio weights -> portfolio path -> metrics.

    This is a returns-sequencing robustness test (mirrors the coin-filter MC
    methodology, adapted to the bar-based equity engine). It destroys temporal
    order to ask: is the edge in the return *distribution*, or only in one
    lucky ordering?"""
    rng = np.random.default_rng(seed)
    w = np.asarray(weights, float)

    # per-leg daily returns on the OOS window (all aligned to same ref_ts => same length)
    leg_dr = []
    for lab in WINNING_LABELS:
        dr = np.asarray(oos_leg_data[lab]["daily_returns"], dtype=float)
        dr = dr[np.isfinite(dr)]
        leg_dr.append(dr)
    min_len = min(len(d) for d in leg_dr)
    leg_dr = [d[:min_len] for d in leg_dr]
    n_days = min_len

    # OOS time span in years (from the test ref_ts)
    span_ms = float(ref_ts_test[-1] - ref_ts_test[0]) if len(ref_ts_test) > 1 else float(DAY_MS * n_days)
    years = max(span_ms / (365.0 * DAY_MS), 1e-6)
    pp_year = 365.0  # daily steps

    ann_rets = np.empty(n_iter)
    max_dds = np.empty(n_iter)
    sharpes = np.empty(n_iter)
    finals = np.empty(n_iter)

    for b in range(n_iter):
        paths = np.empty((len(leg_dr), n_days + 1))
        for li, dr in enumerate(leg_dr):
            sampled = rng.choice(dr, size=n_days, replace=True)
            eqp = np.empty(n_days + 1)
            eqp[0] = 1.0
            for i in range(n_days):
                eqp[i + 1] = eqp[i] * (1.0 + sampled[i])
                if eqp[i + 1] <= 0:
                    eqp[i + 1] = 1e-9
            paths[li] = eqp
        # weighted combination at portfolio weights
        port_path = w @ paths  # (n_days+1,)
        fin = port_path[-1]
        finals[b] = fin
        ann_rets[b] = (fin) ** (1.0 / years) - 1.0 if fin > 0 else -1.0
        peak = np.maximum.accumulate(port_path)
        safe = np.where(peak > 0, peak, 1e-12)
        dd = (port_path - peak) / safe
        max_dds[b] = float(dd.min())
        step_rets = np.diff(port_path) / np.maximum(port_path[:-1], 1e-12)
        step_rets = step_rets[np.isfinite(step_rets)]
        if len(step_rets) > 1 and np.std(step_rets) > 0:
            sharpes[b] = np.mean(step_rets) / np.std(step_rets) * math.sqrt(pp_year)
        else:
            sharpes[b] = 0.0

    def pct(a, p):
        return float(np.percentile(a, p))

    return {
        "n_iter": n_iter, "seed": seed, "years": years, "n_days": n_days,
        "weights": w.tolist(),
        "ann_return_pct": {"p5": pct(ann_rets, 5), "p25": pct(ann_rets, 25),
                           "p50": pct(ann_rets, 50), "p75": pct(ann_rets, 75),
                           "p95": pct(ann_rets, 95), "mean": float(np.mean(ann_rets))},
        "max_dd_pct": {"p5": pct(max_dds, 5), "p50": pct(max_dds, 50),
                       "p95": pct(max_dds, 95)},
        "sharpe_pct": {"p5": pct(sharpes, 5), "p25": pct(sharpes, 25),
                       "p50": pct(sharpes, 50), "p75": pct(sharpes, 75),
                       "p95": pct(sharpes, 95)},
        "prob_sharpe_gt_1": float(np.mean(sharpes > 1.0)),
        "prob_ann_gt_50pct": float(np.mean(ann_rets > 0.50)),
        "prob_ann_positive": float(np.mean(ann_rets > 0.0)),
        "prob_maxdd_lt_20pct": float(np.mean(max_dds > -0.20)),
        "prob_maxdd_lt_25pct": float(np.mean(max_dds > -0.25)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST 4: Slippage sensitivity (full sample, frozen headline weights)
# ═══════════════════════════════════════════════════════════════════════════════
def _set_slippage(slip):
    eng.SLIPPAGE = slip
    eng.COST_SIDE = eng.FEE_RATE + slip
    pp.COST_SIDE = eng.FEE_RATE + slip


def _restore_costs():
    eng.SLIPPAGE = BASE_SLIP
    eng.COST_SIDE = BASE_COST_SIDE
    pp.COST_SIDE = BASE_COST_SIDE


def test_slippage(packs, targets, funding_data, ref_ts) -> dict:
    rows = []
    for slip in SLIPPAGE_LEVELS:
        _set_slippage(slip)
        leg_data = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                                   funding_data, ref_ts, 0, None)
        _, m = portfolio_at_weights(leg_data, WINNING_LABELS, HEADLINE_WEIGHTS, ref_ts)
        rows.append({
            "slippage_pct": slip * 100,
            "sharpe": m["sharpe"], "ann_return": m["ann_return"],
            "max_dd": m["max_dd"], "total_return": m["total_return"],
            "calmar": m["calmar"],
        })
        print(f"    slip {slip*100:.2f}%: shp {m['sharpe']:.2f}/"
              f"ann {fmt_pct(m['ann_return'])}/dd {fmt_pct(m['max_dd'])}")
    _restore_costs()
    sharpe_010 = next((r["sharpe"] for r in rows if abs(r["slippage_pct"] - 0.10) < 1e-9), None)
    return {"rows": rows, "sharpe_at_0p10pct": sharpe_010}


# ═══════════════════════════════════════════════════════════════════════════════
#  Verdict
# ═══════════════════════════════════════════════════════════════════════════════
def compute_verdict(multisplit, mc, slip) -> dict:
    n_pass = multisplit["n_pass"]
    n_total = multisplit["n_total"]
    p_sharpe = mc["prob_sharpe_gt_1"]
    slip_ok = slip["sharpe_at_0p10pct"] is not None and slip["sharpe_at_0p10pct"] >= 1.0

    if n_pass >= 5 and p_sharpe >= 0.60 and slip_ok:
        verdict = "GREEN"
    elif n_pass <= 2 or p_sharpe < 0.40:
        verdict = "RED"
    else:
        # 3-4 splits pass OR (mixed) -> yellow zone
        verdict = "YELLOW"

    gates = {
        "splits_pass_ge_5": n_pass >= 5,
        "mc_prob_sharpe_ge_60pct": p_sharpe >= 0.60,
        "slippage_sharpe_ge_1_at_0p10": slip_ok,
        "red_split_trigger": n_pass <= 2,
        "red_mc_trigger": p_sharpe < 0.40,
    }
    reasons = []
    if n_pass < 5:
        reasons.append(f"only {n_pass}/{n_total} splits keep OOS Sharpe>1.0 AND Ann>50% "
                       f"(need >=5 for GREEN)")
    if p_sharpe < 0.60:
        reasons.append(f"MC P(Sharpe>1.0) = {p_sharpe*100:.1f}% < 60%")
    if not slip_ok:
        reasons.append(f"slippage Sharpe @0.10%/side = "
                       f"{slip['sharpe_at_0p10pct']:.2f if slip['sharpe_at_0p10pct'] is not None else 'n/a'} < 1.0")
    return {"verdict": verdict, "gates": gates,
            "n_splits_pass": n_pass, "n_splits_total": n_total,
            "mc_prob_sharpe_gt_1": p_sharpe,
            "slippage_sharpe_010": slip["sharpe_at_0p10pct"],
            "reasons": reasons}


# ═══════════════════════════════════════════════════════════════════════════════
#  Markdown report
# ═══════════════════════════════════════════════════════════════════════════════
def generate_markdown(data_meta, sanity, multisplit, rolling, mc, slip,
                      verdict) -> str:
    L: list[str] = []

    def w(s=""):
        L.append(s)

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    w("# Portfolio Stress Test — Multi-Split Walk-Forward + Monte Carlo + Slippage")
    w("")
    w(f"*Generated by `scripts/research_portfolio_stress.py` — {gen}. Numbers, not adjectives.*")
    w("")
    w("**Candidate (optimizer #1 gate-passer):**")
    w("- Leg 1: LINKUSDC donchian atr2_vf_cb @ 3x")
    w("- Leg 2: NEARUSDC donchian cbreaker @ 1x")
    w("- Leg 3: ETHUSDC supertrend cbreaker @ 1x")
    w("- Leg 4: DOTUSDT funding_contrarian @ 3x")
    w(f"- Allocation: tangency weights (headline { ' / '.join(f'{s}={int(wt*100)}%' for s, wt in zip(SHORT_LABELS, HEADLINE_WEIGHTS)) })")
    w("")
    w(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']//24} days), "
      f"Binance USDC-M perps. Window {data_meta['start']} → {data_meta['end']}.")
    w(f"**Costs (baseline):** {BASE_FEE*100:.2f}% taker + {BASE_SLIP*100:.2f}% slippage/side "
      f"= {(BASE_FEE+BASE_SLIP)*200:.2f}% round-trip, {eng.FUNDING_RATE*100:.3f}% funding/8h.")
    w("**Engine:** exact per-leg simulation reused from "
      "`research_dd_controlled_trend.py` / `research_portfolio_dd_trend.py` / "
      "`research_portfolio_optimizer.py` (Donchian/Supertrend signals, ATR/vol-filter/"
      "circuit-breaker overlays, funding-contrarian z-score, monthly rebalanced portfolio).")
    w("")

    # ── VERDICT block at top ──
    v = verdict["verdict"]
    badge = {"GREEN": "🟢 GREEN — escalate to Boss",
             "YELLOW": "🟡 YELLOW — needs more data",
             "RED": "🔴 RED — overfit, do NOT escalate"}[v]
    w(f"> ## VERDICT: {badge}")
    w(">")
    sm = sanity["full"]
    oos = sanity["oos"]
    w(f"> Headline reproduced — Full: Ann **{fmt_pct(sm['ann'])}**, Sharpe **{sm['sharpe']:.2f}**, "
      f"MaxDD **{fmt_pct(sm['max_dd'])}**. OOS(60/40): Ann **{fmt_pct(oos['ann'])}**, "
      f"Sharpe **{oos['sharpe']:.2f}**, MaxDD **{fmt_pct(oos['max_dd'])}**.")
    w(">")
    w("> | Gate | Threshold | Result | Pass? |")
    w("> |---|---|---|---|")
    w(f"> | Multi-split OOS pass | >=5/7 keep Sharpe>1 & Ann>50% | "
      f"{verdict['n_splits_pass']}/{verdict['n_splits_total']} | "
      f"{'✅' if verdict['gates']['splits_pass_ge_5'] else '❌'} |")
    w(f"> | MC P(Sharpe>1.0) | >=60% for GREEN; <40% = RED | "
      f"{verdict['mc_prob_sharpe_gt_1']*100:.1f}% | "
      f"{'✅' if verdict['gates']['mc_prob_sharpe_ge_60pct'] else '❌'} |")
    s10 = verdict["slippage_sharpe_010"]
    s10s = f"{s10:.2f}" if s10 is not None else "n/a"
    w(f"> | Slippage Sharpe @0.10%/side | >=1.0 | {s10s} | "
      f"{'✅' if verdict['gates']['slippage_sharpe_ge_1_at_0p10'] else '❌'} |")
    if verdict["reasons"]:
        w(">")
        for r in verdict["reasons"]:
            w(f"> - {r}")
    w("")
    w("**Verdict rules:** GREEN = >=5/7 splits pass AND MC P(Sharpe>1.0)>=60% AND "
      "slippage Sharpe>=1.0@0.10%; YELLOW = 3-4/7 pass (or mixed); "
      "RED = <=2/7 pass OR MC P(Sharpe>1.0)<40%.")
    w("")
    w("---")
    w("")

    # ── The problem context ──
    w("## Why this test exists")
    w("")
    w("A *different* optimizer combo (APT/AVAX/OP balanced regime-adaptive) looked great on "
      "a single 60/40 split (+111.2% ann, Sharpe 1.35) but collapsed under split-point stress:")
    w("")
    w("| Split | Ann | Sharpe |")
    w("|---|---:|---:|")
    w("| 50/50 | -4.6% | 0.24 |")
    w("| 60/40 | +111.2% | 1.35 |")
    w("| 70/30 | -65.0% | -1.30 |")
    w("")
    w("That signature = **overfit to one split point**. This script checks whether the winning "
      "LINK/NEAR/ETH/DOT portfolio has the same disease. If it does, it must NOT be escalated.")
    w("")

    # ── Sanity / reproduction ──
    w("## 0. Headline reproduction (sanity)")
    w("")
    w("Confirm the engine reproduces the optimizer's reported numbers before stressing.")
    w("")
    w("| Window | Ann | Sharpe | MaxDD | Weights |")
    w("|---|---:|---:|---:|---|")
    ww = " / ".join(f"{int(x*100)}%" for x in sanity["full"]["weights"])
    w(f"| Full sample | {fmt_pct(sm['ann'])} | {sm['sharpe']:.2f} | {fmt_pct(sm['max_dd'])} | {ww} |")
    ww2 = " / ".join(f"{int(x*100)}%" for x in sanity["oos"]["weights"])
    w(f"| OOS (60/40 test) | {fmt_pct(oos['ann'])} | {oos['sharpe']:.2f} | {fmt_pct(oos['max_dd'])} | {ww2} |")
    w("")
    full_ok = (abs(sm["ann"] - 1.033) < 0.03 and abs(sm["sharpe"] - 2.78) < 0.10
               and abs(sm["max_dd"] - (-0.099)) < 0.02)
    oos_ok = (abs(oos["ann"] - 1.911) < 0.05 and abs(oos["sharpe"] - 2.17) < 0.10
              and abs(oos["max_dd"] - (-0.202)) < 0.03)
    w(f"Reproduction vs headline (Full 103.3%/2.78/-9.9%; OOS 191.1%/2.17/-20.2%): "
      f"{'✅ **MATCH**' if (full_ok and oos_ok) else '⚠️ minor drift — see numbers above'}.")
    w("")

    # ── TEST 1: Multi-split ──
    w("## 1. Multi-split walk-forward (the critical overfit test)")
    w("")
    w("For each of 7 train/test splits: **optimize tangency weights on train, freeze, "
      "apply to test.** A robust strategy stays positive (Sharpe>1.0 AND Ann>50%) across "
      "most splits. An overfit one flips sign with the split point.")
    w("")
    w("| Split | Train Ann | Train Shp | **Test Ann** | **Test Shp** | **Test MaxDD** | OOS Pass? |")
    w("|---|---:|---:|---:|---:|---:|:---:|")
    for r in multisplit["rows"]:
        t, te = r["train"], r["test"]
        mark = "✅" if r["oos_pass"] else "❌"
        flag = " ← headline" if abs(r["split_frac"] - 0.60) < 1e-9 else ""
        w(f"| {r['split']}{flag} | {fmt_pct(t['ann'])} | {t['sharpe']:.2f} | "
          f"**{fmt_pct(te['ann'])}** | **{te['sharpe']:.2f}** | **{fmt_pct(te['max_dd'])}** | {mark} |")
    w("")
    w(f"**Splits passing (OOS Sharpe>1.0 AND OOS Ann>50%): "
      f"{multisplit['n_pass']}/{multisplit['n_total']}.**")
    w("")
    # split-sensitivity signature check
    anns = [r["test"]["ann"] for r in multisplit["rows"]]
    shps = [r["test"]["sharpe"] for r in multisplit["rows"]]
    n_neg = sum(1 for a in anns if a < 0)
    w(f"Test Ann range: {fmt_pct(min(anns))} … {fmt_pct(max(anns))}; "
      f"{n_neg}/{len(anns)} splits have negative OOS annualized return. "
      f"Test Sharpe range: {min(shps):.2f} … {max(shps):.2f}.")
    w("")

    # ── TEST 2: Rolling ──
    w("## 2. Rolling expanding-window validation")
    w("")
    w("Train on an expanding window, test on the next 10% slice. Weights re-optimized on "
      "each expanding train set, frozen for that window's test slice. Shows period-by-period "
      "stability (short test windows are noisier — read the trend, not any single cell).")
    w("")
    w("| Window | Test Ann | Test Sharpe | Test MaxDD | Test Total |")
    w("|---|---:|---:|---:|---:|")
    for r in rolling["rows"]:
        te = r["test"]
        w(f"| {r['window']} | {fmt_pct(te['ann'])} | {te['sharpe']:.2f} | "
          f"{fmt_pct(te['max_dd'])} | {fmt_pct(te['total_return'])} |")
    w("")
    w(f"**{rolling['n_positive_ann']}/{rolling['n_windows']}** windows have positive annualized "
      f"return; **{rolling['n_sharpe_gt_1']}/{rolling['n_windows']}** have Sharpe>1.0.")
    w("")

    # ── TEST 3: Monte Carlo ──
    w(f"## 3. Monte Carlo bootstrap ({mc['n_iter']} resamples)")
    w("")
    w(f"Resampled each leg's OOS (60/40 test) daily-return sequence with replacement, "
      f"compounded, combined at portfolio weights ({ ' / '.join(f'{s} {int(wt*100)}%' for s, wt in zip(SHORT_LABELS, mc['weights'])) }). "
      f"OOS window ≈ {mc['years']:.2f} years ({mc['n_days']} daily obs/leg). "
      f"This tests whether the edge lives in the return *distribution* or only in one "
      f"favorable ordering.")
    w("")
    w("| Percentile | Ann return | Max DD | Sharpe |")
    w("|---|---:|---:|---:|")
    ar, md, sh = mc["ann_return_pct"], mc["max_dd_pct"], mc["sharpe_pct"]
    w(f"| 5th | {fmt_pct(ar['p5'])} | {fmt_pct(md['p5'])} | {sh['p5']:.2f} |")
    w(f"| 25th | {fmt_pct(ar['p25'])} | — | {sh['p25']:.2f} |")
    w(f"| **50th (median)** | **{fmt_pct(ar['p50'])}** | **{fmt_pct(md['p50'])}** | **{sh['p50']:.2f}** |")
    w(f"| 75th | {fmt_pct(ar['p75'])} | — | {sh['p75']:.2f} |")
    w(f"| 95th | {fmt_pct(ar['p95'])} | {fmt_pct(md['p95'])} | {sh['p95']:.2f} |")
    w("")
    w("| Probability question | Result |")
    w("|---|---:|")
    w(f"| **P(Sharpe > 1.0)** | **{fmt_pct(mc['prob_sharpe_gt_1'])}** |")
    w(f"| P(Ann > 50%) | {fmt_pct(mc['prob_ann_gt_50pct'])} |")
    w(f"| P(Ann > 0) | {fmt_pct(mc['prob_ann_positive'])} |")
    w(f"| P(MaxDD < 20%) | {fmt_pct(mc['prob_maxdd_lt_20pct'])} |")
    w(f"| P(MaxDD < 25%) | {fmt_pct(mc['prob_maxdd_lt_25pct'])} |")
    w("")
    pthr = ">=60% (GREEN)" if mc["prob_sharpe_gt_1"] >= 0.60 else (
        "40-60% (YELLOW)" if mc["prob_sharpe_gt_1"] >= 0.40 else "<40% (RED)")
    w(f"**MC gate: P(Sharpe>1.0) = {fmt_pct(mc['prob_sharpe_gt_1'])} → {pthr}.**")
    w("")

    # ── TEST 4: Slippage ──
    w("## 4. Slippage sensitivity (full sample, frozen headline weights)")
    w("")
    w("Per-side slippage swept; fee held at 0.04%/side. Weights frozen at headline "
      "28/28/25/19. Tests cost-model robustness on the full sample.")
    w("")
    w("| Slippage/side | Sharpe | Ann return | Max DD | Total Ret | Calmar |")
    w("|---:|---:|---:|---:|---:|---:|")
    for r in slip["rows"]:
        flag = " ← baseline" if abs(r["slippage_pct"] - 0.03) < 1e-9 else ""
        mark = " ⚠️<1.0" if r["sharpe"] < 1.0 else ""
        w(f"| {r['slippage_pct']:.2f}%{flag} | {r['sharpe']:.2f}{mark} | "
          f"{fmt_pct(r['ann_return'])} | {fmt_pct(r['max_dd'])} | "
          f"{fmt_pct(r['total_return'])} | {r['calmar']:.2f} |")
    w("")
    s10 = slip["sharpe_at_0p10pct"]
    s10s = f"{s10:.2f}" if s10 is not None else "n/a"
    w(f"**Slippage gate: Sharpe @0.10%/side = {s10s} → "
      f"{'PASS (>=1.0)' if (s10 is not None and s10 >= 1.0) else 'FAIL (<1.0)'}.**")
    w("")

    # ── Conclusion ──
    w("---")
    w("")
    w("## Conclusion")
    w("")
    if v == "GREEN":
        w("The winning portfolio is **robust** across split points, Monte Carlo sequencing, "
          "and cost stress. It does NOT exhibit the APT/AVAX/OP split-flip pathology.")
        w("")
        w(f"- **Multi-split:** {multisplit['n_pass']}/{multisplit['n_total']} splits keep "
          f"OOS Sharpe>1.0 AND Ann>50%.")
        w(f"- **Monte Carlo:** P(Sharpe>1.0)={fmt_pct(mc['prob_sharpe_gt_1'])}, "
          f"P(Ann>0)={fmt_pct(mc['prob_ann_positive'])}.")
        w(f"- **Slippage:** Sharpe {s10s} at 0.10%/side (3.3× baseline).")
        w("")
        w("**Recommendation: escalate to Boss for live deployment authorization** "
          "with conservative initial sizing.")
    elif v == "YELLOW":
        w("The winning portfolio shows **partial robustness** — it survives some but not all "
          "stress dimensions. Not a clean overfit, but not a clean pass either.")
        w("")
        for r in verdict["reasons"]:
            w(f"- {r}")
        w("")
        w("**Recommendation: gather more out-of-sample data before escalation.** "
          "Do NOT escalate yet.")
    else:  # RED
        w("The winning portfolio **fails the stress test.** Despite a strong single-split "
          "headline, it is not robust across split points and/or trade-order resampling. "
          "This is the same class of pathology that sank the APT/AVAX/OP combo.")
        w("")
        for r in verdict["reasons"]:
            w(f"- {r}")
        w("")
        w("**Recommendation: do NOT escalate to Boss.** The headline is overfit. "
          "Return to research: more data, simpler strategy, or different allocation.")
    w("")
    w("## Methodology")
    w("")
    w("- **Engine:** exact reuse of `research_dd_controlled_trend.py::simulate` + "
      "`research_portfolio_dd_trend.py::run_leg` + "
      "`research_portfolio_optimizer.py::weighted_monthly_rebal` / `compute_alloc`. "
      "Signals/overlays/costs identical to the optimizer run.")
    w("- **Multi-split:** for each split, tangency weights optimized on train daily-return "
      "covariance (SLSQP, max-Sharpe, no leg >60%), frozen, applied to test via monthly "
      "rebalanced portfolio. Trend indicators/signals sliced from full-series computation "
      "(causal, no look-ahead), matching the optimizer's walk-forward.")
    w("- **Rolling:** expanding train (40%→90%), 10% test slices, weights re-optimized per window.")
    w("- **Monte Carlo:** per leg, OOS daily returns resampled iid with replacement (5000x), "
      "compounded from 1.0, combined at portfolio weights. Sharpe on per-step portfolio "
      "returns annualized by √365. Note: iid resampling destroys volatility clustering — "
      "this is a sequencing-distribution test, conservative on the drawdown side.")
    w("- **Slippage:** module cost globals (`eng.COST_SIDE`/`pp.COST_SIDE`) patched per level, "
      "legs re-run on full sample, headline weights frozen.")
    w("")
    w("*Numbers, not adjectives. The Boss gets truth.*")
    return "\n".join(L) + "\n"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print("PORTFOLIO STRESS TEST — LINK3x / NEARcb1x / ETHST1x / DOTFUND3x")
    print("=" * 72)

    print("\n[load] trend data...")
    raw = eng.load_all_symbols()
    packs: dict[str, eng.IndicatorPack] = {}
    for sym, df in raw.items():
        packs[sym] = eng.build_pack(df)
    targets: dict = {}
    for sym in ["LINKUSDC", "NEARUSDC", "ETHUSDC", "DOGEUSDC"]:
        for strat in ["donchian", "supertrend"]:
            tgt, _warm = eng.gen_targets(strat, packs[sym])
            targets[(sym, strat)] = tgt
    ref_pack = packs["LINKUSDC"]
    ref_ts = ref_pack.ts.copy()
    n = len(ref_pack.close)
    data_meta = {
        "n_bars": int(n),
        "start": str(pd.to_datetime(int(ref_pack.ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_pack.ts[-1]), unit="ms", utc=True)),
    }
    print(f"  {data_meta['n_bars']} bars, {data_meta['start']} -> {data_meta['end']}")

    print("\n[load] funding data (DOTUSDT 8h)...")
    funding_data: dict[str, pd.DataFrame] = {}
    try:
        fdf = pp.load_funding_dataset("DOTUSDT")
        funding_data["DOTUSDT"] = fdf
        data_meta["funding_periods"] = int(len(fdf))
        print(f"  {len(fdf)} 8h bars")
    except FileNotFoundError as e:
        print(f"  [WARN] {e}")
        data_meta["funding_periods"] = 0

    # ── Sanity: reproduce headline (full sample + 60/40 OOS) ──
    print("\n[sanity] reproducing headline (full + 60/40 OOS)...")
    full_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                               funding_data, ref_ts, 0, None)
    w_full = optimize_tangency(full_leg, WINNING_LABELS)
    _, m_full = portfolio_at_weights(full_leg, WINNING_LABELS, w_full, ref_ts)
    print(f"  full: ann {fmt_pct(m_full['ann_return'])}, shp {m_full['sharpe']:.2f}, "
          f"dd {fmt_pct(m_full['max_dd'])}, w={[f'{x:.2f}' for x in w_full]}")

    split60 = int(n * 0.60)
    ref_ts_tr60 = ref_pack.ts[:split60].copy()
    ref_ts_te60 = ref_pack.ts[split60:].copy()
    tr_leg60 = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                               funding_data, ref_ts_tr60, 0, split60)
    w60 = optimize_tangency(tr_leg60, WINNING_LABELS)
    te_leg60 = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                               funding_data, ref_ts_te60, split60, n)
    _, m_oos60 = portfolio_at_weights(te_leg60, WINNING_LABELS, w60, ref_ts_te60)
    print(f"  oos60: ann {fmt_pct(m_oos60['ann_return'])}, shp {m_oos60['sharpe']:.2f}, "
          f"dd {fmt_pct(m_oos60['max_dd'])}, w={[f'{x:.2f}' for x in w60]}")
    sanity = {
        "full": {"ann": m_full["ann_return"], "sharpe": m_full["sharpe"],
                 "max_dd": m_full["max_dd"], "weights": w_full.tolist()},
        "oos": {"ann": m_oos60["ann_return"], "sharpe": m_oos60["sharpe"],
                "max_dd": m_oos60["max_dd"], "weights": w60.tolist()},
    }

    # ── TEST 1 ──
    print("\n[1/4] Multi-split walk-forward (7 splits)...")
    multisplit = test_multisplit(packs, targets, funding_data)
    print(f"  -> {multisplit['n_pass']}/{multisplit['n_total']} splits pass")

    # ── TEST 2 ──
    print("\n[2/4] Rolling expanding-window validation...")
    rolling = test_rolling(packs, targets, funding_data)
    print(f"  -> {rolling['n_positive_ann']}/{rolling['n_windows']} positive ann, "
          f"{rolling['n_sharpe_gt_1']}/{rolling['n_windows']} sharpe>1")

    # ── TEST 3 ──
    print(f"\n[3/4] Monte Carlo bootstrap ({MC_NITER})...")
    mc = test_montecarlo(te_leg60, ref_ts_te60, w60, n_iter=MC_NITER, seed=MC_SEED)
    print(f"  -> P(Sharpe>1)={fmt_pct(mc['prob_sharpe_gt_1'])}  "
          f"P(Ann>0)={fmt_pct(mc['prob_ann_positive'])}  "
          f"P(Ann>50%)={fmt_pct(mc['prob_ann_gt_50pct'])}")
    print(f"     Ann p5/p50/p95: {fmt_pct(mc['ann_return_pct']['p5'])} / "
          f"{fmt_pct(mc['ann_return_pct']['p50'])} / {fmt_pct(mc['ann_return_pct']['p95'])}")

    # ── TEST 4 ──
    print("\n[4/4] Slippage sensitivity (full sample)...")
    slip = test_slippage(packs, targets, funding_data, ref_ts)
    print(f"  -> Sharpe @0.10% = {slip['sharpe_at_0p10pct']:.2f}"
          if slip['sharpe_at_0p10pct'] is not None else "  -> n/a")

    # ── Verdict ──
    print("\n[verdict] computing...")
    verdict = compute_verdict(multisplit, mc, slip)
    print(f"  VERDICT: {verdict['verdict']}")
    for gk, gv in verdict["gates"].items():
        print(f"    {gk}: {gv}")
    for r in verdict["reasons"]:
        print(f"    - {r}")

    # ── Write reports ──
    print("\n[write] generating markdown + json...")
    md = generate_markdown(data_meta, sanity, multisplit, rolling, mc, slip, verdict)
    REPORT_MD.write_text(md, encoding="utf-8")
    print(f"  wrote {REPORT_MD}")
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_meta": data_meta,
        "costs": {"fee_side": BASE_FEE, "slippage_side": BASE_SLIP,
                  "funding_8h": eng.FUNDING_RATE},
        "combo": {"labels": WINNING_LABELS, "short_labels": SHORT_LABELS,
                  "headline_weights": HEADLINE_WEIGHTS.tolist()},
        "sanity": sanity,
        "test1_multisplit": multisplit,
        "test2_rolling": rolling,
        "test3_montecarlo": mc,
        "test4_slippage": slip,
        "verdict": verdict,
    }
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default),
                           encoding="utf-8")
    print(f"  wrote {REPORT_JSON}")

    print(f"\nDone in {time.time()-t0:.1f}s. VERDICT: {verdict['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
