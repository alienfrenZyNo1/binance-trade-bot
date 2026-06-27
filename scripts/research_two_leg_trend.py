#!/usr/bin/env python3
"""2-Leg Trend Portfolio Re-validation — LINK + ETH, honest train-only weights.

Candidate under test (the *clean* subset flagged by the independent validation):
    Leg 1: LINKUSDC donchian atr2_vf_cb @ 3x   (causal, OOS Sharpe ~2.1)
    Leg 2: ETHUSDC  supertrend cbreaker   @ 1x (causal, OOS Sharpe ~1.5)

Both legs were audited as look-ahead-free in
docs/research/portfolio-stress-INDEPENDENT-VALIDATION.md. NEAR and DOT were
dropped (DOT has a 0-bar look-ahead; NEAR less robust). This script tests JUST
these two trend legs as a standalone portfolio, using ONLY train-half-optimized
tangency weights applied to the test half — never full-sample-fit weights on
test data.

Tests:
    (a) Full-sample 2-leg metrics (tangency on full sample; IN-SAMPLE only).
    (b) OOS 60/40 with train-only tangency weights (the headline honest number).
    (c) Multi-split walk-forward: 40/60, 50/50, 55/45, 60/40, 65/35, 70/30,
        75/25. For each: optimize tangency on TRAIN, freeze, apply to TEST.
    (d) Rolling expanding-window: train 0-40%→test 40-50%, ... 0-89%→89-99%
        (6 windows). Weights re-optimized on each expanding train, frozen for
        that window's test slice.
    (e) Verdict: robust iff Sharpe>1 & Ann>50% & MaxDD<20% on the MAJORITY of
        rolling windows.

Reuses the exact per-leg simulation engine from:
    scripts/research_dd_controlled_trend.py  (eng: signals, overlays, simulate)
    scripts/research_portfolio_dd_trend.py   (pp: run_leg, portfolio_metrics)
    scripts/research_portfolio_optimizer.py  (opo: weighted_monthly_rebal, alloc)

Costs: 0.04% taker + 0.03% slippage/side = 0.14% round-trip, 0.010% funding/8h.
LINK uses 3x leverage, ETH uses 1x. Monthly rebalanced portfolio.
This is research only — it does NOT modify any live config or trading code.

Outputs:
    docs/research/two-leg-trend-revalidation.md
    docs/research/two-leg-trend-revalidation-data.json
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
REPORT_MD = DOCS_DIR / "two-leg-trend-revalidation.md"
REPORT_JSON = DOCS_DIR / "two-leg-trend-revalidation-data.json"

INITIAL_CAPITAL = pp.INITIAL_CAPITAL  # 10_000
DAY_MS = pp.DAY_MS

# ─── The 2 trend legs under test ──────────────────────────────────────────────
TWO_LEG_LABELS = [
    "LINKUSDC_donchian_atr2_vf_cb_3x",
    "ETHUSDC_supertrend_cbreaker_1x",
]
TWO_LEGS = [pp.trend_leg(k) for k in TWO_LEG_LABELS]
SHORT_LABELS = ["LINK3x", "ETHST1x"]

SPLIT_FRACS = [0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

# Rolling expanding-window: train grows 40%->89%, test = next ~10% slice.
# Produces 6 windows: (40,50),(50,60),(60,70),(70,80),(80,89),(89,99).
ROLLING_BOUNDARIES = [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70),
                      (0.70, 0.80), (0.80, 0.89), (0.89, 0.99)]


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
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x * 100:.{dec}f}%"


# ═══════════════════════════════════════════════════════════════════════════════
#  Core: run the 2 legs on a windowed timeline, build portfolio at given weights
# ═══════════════════════════════════════════════════════════════════════════════
def run_legs_window(legs, labels, packs, targets, ref_ts, start, end):
    """Run each leg on native bars [start:end], aligned to ref_ts. Returns
    dict label -> {eq_aligned, daily_returns, trades, metrics}."""
    out = {}
    for label, spec in zip(labels, legs):
        lr = pp.run_leg(spec, packs, targets, {}, ref_ts, INITIAL_CAPITAL, start, end)
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
    port_eq = opo.weighted_monthly_rebal(eqs, np.asarray(weights, float), ref_ts, INITIAL_CAPITAL)
    return port_eq, pp.portfolio_metrics(port_eq, ref_ts)


def optimize_tangency(leg_data, labels):
    """Tangency weights from per-leg daily returns (mirrors optimizer)."""
    R, mu, cov, stds = opo._moments_from_legs(leg_data, labels)
    return opo.compute_alloc("tangency", mu, cov, stds)


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST (a): Full-sample 2-leg metrics (in-sample reference)
# ═══════════════════════════════════════════════════════════════════════════════
def test_full_sample(packs, targets) -> dict:
    ref_pack = packs["LINKUSDC"]
    ref_ts = ref_pack.ts.copy()
    leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets, ref_ts, 0, None)
    w = optimize_tangency(leg, TWO_LEG_LABELS)
    _, m = portfolio_at_weights(leg, TWO_LEG_LABELS, w, ref_ts)
    # per-leg full-sample metrics
    leg_m = {l: leg[l]["metrics"] for l in TWO_LEG_LABELS}
    print(f"  full: ann {fmt_pct(m['ann_return'])}, shp {m['sharpe']:.2f}, "
          f"dd {fmt_pct(m['max_dd'])}, w={[f'{x:.2f}' for x in w]}")
    return {"metrics": m, "weights": w.tolist(),
            "leg_metrics": {k: v for k, v in leg_m.items()}}


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST (b): OOS 60/40 with train-only weights (the honest headline)
# ═══════════════════════════════════════════════════════════════════════════════
def test_oos_6040(packs, targets) -> dict:
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    split = int(n * 0.60)
    ref_ts_train = ref_pack.ts[:split].copy()
    ref_ts_test = ref_pack.ts[split:].copy()
    # train: optimize tangency
    train_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                                ref_ts_train, 0, split)
    w = optimize_tangency(train_leg, TWO_LEG_LABELS)
    _, train_m = portfolio_at_weights(train_leg, TWO_LEG_LABELS, w, ref_ts_train)
    # test: apply FROZEN train weights
    test_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                               ref_ts_test, split, n)
    _, test_m = portfolio_at_weights(test_leg, TWO_LEG_LABELS, w, ref_ts_test)
    print(f"  oos60/40: train shp {train_m['sharpe']:.2f}/ann {fmt_pct(train_m['ann_return'])}, "
          f"TEST shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/"
          f"dd {fmt_pct(test_m['max_dd'])}, w={[f'{x:.2f}' for x in w]}")
    return {
        "weights": w.tolist(),
        "train": {"ann": train_m["ann_return"], "sharpe": train_m["sharpe"],
                  "max_dd": train_m["max_dd"]},
        "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                 "max_dd": test_m["max_dd"], "total_return": test_m["total_return"]},
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST (c): Multi-split walk-forward (7 splits, train-only weights)
# ═══════════════════════════════════════════════════════════════════════════════
def test_multisplit(packs, targets) -> dict:
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    rows = []
    for frac in SPLIT_FRACS:
        split = int(n * frac)
        ref_ts_train = ref_pack.ts[:split].copy()
        ref_ts_test = ref_pack.ts[split:].copy()
        train_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                                    ref_ts_train, 0, split)
        w = optimize_tangency(train_leg, TWO_LEG_LABELS)
        _, train_m = portfolio_at_weights(train_leg, TWO_LEG_LABELS, w, ref_ts_train)
        test_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                                   ref_ts_test, split, n)
        _, test_m = portfolio_at_weights(test_leg, TWO_LEG_LABELS, w, ref_ts_test)
        oos_pass = test_m["sharpe"] > 1.0 and test_m["ann_return"] > 0.50
        rows.append({
            "split": f"{round(frac*100)}/{round((1-frac)*100)}",
            "split_frac": frac,
            "n_train_bars": split, "n_test_bars": n - split,
            "weights": w.tolist(),
            "train": {"ann": train_m["ann_return"], "sharpe": train_m["sharpe"],
                      "max_dd": train_m["max_dd"]},
            "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                     "max_dd": test_m["max_dd"]},
            "oos_pass": bool(oos_pass),
        })
        print(f"    split {round(frac*100)}/{round((1-frac)*100)}: "
              f"train shp {train_m['sharpe']:.2f}/ann {fmt_pct(train_m['ann_return'])}, "
              f"test shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/"
              f"dd {fmt_pct(test_m['max_dd'])} {'✅' if oos_pass else '❌'}")
    n_pass = sum(1 for r in rows if r["oos_pass"])
    n_pass_dd = sum(1 for r in rows if r["test"]["max_dd"] > -0.20)
    return {"rows": rows, "n_pass": n_pass, "n_total": len(rows),
            "n_pass_dd_lt_20": n_pass_dd}


# ═══════════════════════════════════════════════════════════════════════════════
#  TEST (d): Rolling expanding-window validation (6 windows)
# ═══════════════════════════════════════════════════════════════════════════════
def test_rolling(packs, targets) -> dict:
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    rows = []
    for train_frac, test_frac in ROLLING_BOUNDARIES:
        train_end = int(n * train_frac)
        test_end = int(n * test_frac)
        if test_end <= train_end:
            continue
        ref_ts_train = ref_pack.ts[:train_end].copy()
        ref_ts_test = ref_pack.ts[train_end:test_end].copy()
        train_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                                    ref_ts_train, 0, train_end)
        w = optimize_tangency(train_leg, TWO_LEG_LABELS)
        test_leg = run_legs_window(TWO_LEGS, TWO_LEG_LABELS, packs, targets,
                                   ref_ts_test, train_end, test_end)
        _, test_m = portfolio_at_weights(test_leg, TWO_LEG_LABELS, w, ref_ts_test)
        # calendar dates for the test window
        cal_start = str(pd.to_datetime(int(ref_pack.ts[train_end]), unit="ms", utc=True).date())
        cal_end = str(pd.to_datetime(int(ref_pack.ts[test_end - 1]), unit="ms", utc=True).date())
        rows.append({
            "window": f"train 0-{int(train_frac*100)}% / test {int(train_frac*100)}-{int(test_frac*100)}%",
            "train_frac": train_frac, "test_frac": test_frac,
            "calendar": f"{cal_start} → {cal_end}",
            "n_test_bars": test_end - train_end,
            "weights": w.tolist(),
            "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                     "max_dd": test_m["max_dd"], "total_return": test_m["total_return"]},
        })
        print(f"    {rows[-1]['window']} ({rows[-1]['calendar']}): "
              f"shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/"
              f"dd {fmt_pct(test_m['max_dd'])}")
    n_pos = sum(1 for r in rows if r["test"]["ann"] > 0)
    n_sharpe = sum(1 for r in rows if r["test"]["sharpe"] > 1.0)
    # robustness gate: Sharpe>1 & Ann>50% & MaxDD<20%
    n_robust = sum(1 for r in rows
                   if r["test"]["sharpe"] > 1.0
                   and r["test"]["ann"] > 0.50
                   and r["test"]["max_dd"] > -0.20)
    return {"rows": rows, "n_windows": len(rows),
            "n_positive_ann": n_pos, "n_sharpe_gt_1": n_sharpe,
            "n_robust_pass": n_robust}


# ═══════════════════════════════════════════════════════════════════════════════
#  Verdict
# ═══════════════════════════════════════════════════════════════════════════════
def compute_verdict(multisplit, rolling, oos) -> dict:
    n_win = rolling["n_windows"]
    n_robust = rolling["n_robust_pass"]
    majority = n_robust >= math.ceil(n_win / 2) if n_win else False

    # secondary signals
    oos60_ok = (oos["test"]["sharpe"] > 1.0 and oos["test"]["ann"] > 0.50
                and oos["test"]["max_dd"] > -0.20)
    ms_majority = multisplit["n_pass"] >= math.ceil(multisplit["n_total"] / 2)

    if majority:
        verdict = "ROBUST"
    else:
        verdict = "NOT ROBUST"

    return {
        "verdict": verdict,
        "robust_majority_rolling": majority,
        "rolling_robust_pass": n_robust,
        "rolling_total": n_win,
        "rolling_majority_needed": math.ceil(n_win / 2) if n_win else 0,
        "oos_6040_passes_gate": oos60_ok,
        "multisplit_majority_pass": ms_majority,
        "multisplit_pass": multisplit["n_pass"],
        "multisplit_total": multisplit["n_total"],
        "rolling_n_positive_ann": rolling["n_positive_ann"],
        "rolling_n_sharpe_gt_1": rolling["n_sharpe_gt_1"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Markdown report
# ═══════════════════════════════════════════════════════════════════════════════
def generate_markdown(data_meta, full, oos, multisplit, rolling, verdict) -> str:
    L: list[str] = []

    def w(s=""):
        L.append(s)

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    w("# 2-Leg Trend Portfolio Re-validation — LINK + ETH (honest train-only weights)")
    w("")
    w(f"*Generated by `scripts/research_two_leg_trend.py` — {gen}. "
      "Numbers, not adjectives. Research only — no live config modified.*")
    w("")
    w("## Candidate")
    w("")
    w("- **Leg 1:** LINKUSDC donchian atr2_vf_cb @ **3x**")
    w("- **Leg 2:** ETHUSDC supertrend cbreaker @ **1x**")
    w("- Both legs audited as **look-ahead-free** in "
      "`portfolio-stress-INDEPENDENT-VALIDATION.md` (Donchian `.shift(1)`, "
      "Supertrend uses `close[i-1]`, ATR Wilder-causal, circuit-breaker uses "
      "present/past data only). NEAR and DOT dropped from this candidate.")
    w("")
    w(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']//24} days), "
      f"Binance USDC-M perps. Window {data_meta['start']} → {data_meta['end']}.")
    w(f"**Costs:** {eng.FEE_RATE*100:.2f}% taker + {eng.SLIPPAGE*100:.2f}% slippage/side "
      f"= {(eng.FEE_RATE+eng.SLIPPAGE)*200:.2f}% round-trip, {eng.FUNDING_RATE*100:.3f}% funding/8h.")
    w("**Method:** per-leg equity curves aligned to hourly grid; portfolio P&L via "
      "**monthly rebalancing to optimized weights**. Tangency weights optimized from "
      "the covariance of per-leg daily returns (SLSQP max-Sharpe, no leg >60%).")
    w("")
    w("**CRITICAL METHODOLOGY RULE:** weights are optimized on the TRAIN half ONLY, "
      "then frozen and applied to the TEST half. Full-sample-fit weights are NEVER "
      "applied to test data (the data-snooping leak flagged in the independent validation).")
    w("")

    # ── VERDICT block at top ──
    v = verdict["verdict"]
    badge = {"ROBUST": "🟢 ROBUST — passes the majority of rolling windows",
             "NOT ROBUST": "🔴 NOT ROBUST — fails the majority of rolling windows"}[v]
    w(f"> ## VERDICT: {badge}")
    w(">")
    w(f"> Robustness gate (Sharpe>1 & Ann>50% & MaxDD<20%): "
      f"**{verdict['rolling_robust_pass']}/{verdict['rolling_total']}** rolling windows pass; "
      f"majority ({verdict['rolling_majority_needed']}) required → "
      f"{'PASS' if verdict['robust_majority_rolling'] else 'FAIL'}.")
    w(f"> OOS 60/40 (train-only weights): "
      f"Ann **{fmt_pct(oos['test']['ann'])}**, Sharpe **{oos['test']['sharpe']:.2f}**, "
      f"MaxDD **{fmt_pct(oos['test']['max_dd'])}** — "
      f"{'passes' if verdict['oos_6040_passes_gate'] else 'FAILS'} the gate.")
    w(f"> Multi-split: {verdict['multisplit_pass']}/{verdict['multisplit_total']} "
      f"splits keep OOS Sharpe>1 & Ann>50%.")
    w("")

    # ── (a) Full-sample ──
    w("## (a) Full-sample 2-leg metrics (IN-SAMPLE reference)")
    w("")
    w("Tangency weights optimized on the **full sample**. This is an in-sample "
      "upper bound — NOT an out-of-sample number. Reported only as a reference for "
      "what the optimizer sees when it can see everything.")
    w("")
    w("| Leg | Ann | Sharpe | MaxDD | Trades |")
    w("|---|---:|---:|---:|---:|")
    for k in TWO_LEG_LABELS:
        m = full["leg_metrics"][k]
        w(f"| {k} | {fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | "
          f"{fmt_pct(m['max_dd'])} | {m['n_trades']} |")
    w("")
    fm = full["metrics"]
    ww = " / ".join(f"{s}={int(x*100)}%" for s, x in zip(SHORT_LABELS, full["weights"]))
    w("| Portfolio (full, tangency) | Ann | Sharpe | MaxDD | Calmar | Weights |")
    w("|---|---:|---:|---:|---:|---|")
    w(f"| 2-leg | {fmt_pct(fm['ann_return'])} | {fm['sharpe']:.2f} | "
      f"{fmt_pct(fm['max_dd'])} | {fm['calmar']:.2f} | {ww} |")
    w("")

    # ── (b) OOS 60/40 ──
    w("## (b) OOS 60/40 with train-only weights (the honest headline)")
    w("")
    w("Tangency weights optimized on the **train half (first 60%) only**, then "
      "**frozen** and applied to the test half (last 40%). This is the only honest "
      "single-split out-of-sample number.")
    w("")
    ww_tr = " / ".join(f"{s}={int(x*100)}%" for s, x in zip(SHORT_LABELS, oos["weights"]))
    w(f"**Frozen train-optimized weights:** {ww_tr}")
    w("")
    w("| Window | Ann | Sharpe | MaxDD |")
    w("|---|---:|---:|---:|")
    w(f"| Train (0-60%) | {fmt_pct(oos['train']['ann'])} | {oos['train']['sharpe']:.2f} | "
      f"{fmt_pct(oos['train']['max_dd'])} |")
    w(f"| **Test (60-100%)** | **{fmt_pct(oos['test']['ann'])}** | "
      f"**{oos['test']['sharpe']:.2f}** | **{fmt_pct(oos['test']['max_dd'])}** |")
    w("")
    gate = oos["test"]["sharpe"] > 1.0 and oos["test"]["ann"] > 0.50 and oos["test"]["max_dd"] > -0.20
    w(f"**Gate (Sharpe>1 & Ann>50% & MaxDD<20%): "
      f"{'✅ PASS' if gate else '❌ FAIL'}.**")
    w("")

    # ── (c) Multi-split ──
    w("## (c) Multi-split walk-forward (7 splits, train-only weights)")
    w("")
    w("For each of 7 train/test splits: optimize tangency on TRAIN, freeze, apply "
      "to TEST. A robust strategy stays positive across most splits; an overfit one "
      "flips sign with the split point.")
    w("")
    w("| Split | Train Ann | Train Shp | **Test Ann** | **Test Shp** | **Test MaxDD** | "
      "Sharpe>1 & Ann>50%? |")
    w("|---|---:|---:|---:|---:|---:|:---:|")
    for r in multisplit["rows"]:
        t, te = r["train"], r["test"]
        mark = "✅" if r["oos_pass"] else "❌"
        flag = " ← headline" if abs(r["split_frac"] - 0.60) < 1e-9 else ""
        w(f"| {r['split']}{flag} | {fmt_pct(t['ann'])} | {t['sharpe']:.2f} | "
          f"**{fmt_pct(te['ann'])}** | **{te['sharpe']:.2f}** | "
          f"**{fmt_pct(te['max_dd'])}** | {mark} |")
    w("")
    anns = [r["test"]["ann"] for r in multisplit["rows"]]
    shps = [r["test"]["sharpe"] for r in multisplit["rows"]]
    n_neg = sum(1 for a in anns if a < 0)
    w(f"**{multisplit['n_pass']}/{multisplit['n_total']}** splits keep OOS Sharpe>1.0 AND Ann>50%. "
      f"Test Ann range: {fmt_pct(min(anns))} … {fmt_pct(max(anns))}; "
      f"{n_neg}/{len(anns)} splits have negative OOS annualized return. "
      f"Test Sharpe range: {min(shps):.2f} … {max(shps):.2f}. "
      f"{multisplit['n_pass_dd_lt_20']}/{multisplit['n_total']} splits keep OOS MaxDD < 20%.")
    w("")

    # ── (d) Rolling ──
    w("## (d) Rolling expanding-window validation (6 windows)")
    w("")
    w("Train on an expanding window, test on the next ~10% slice. Weights "
      "re-optimized on each expanding train set, frozen for that window's test slice. "
      "This forces the strategy to perform in **every consecutive period** — the "
      "strictest overfit test. Calendar dates are shown to map performance to regimes.")
    w("")
    w("| Window | Calendar | Test Ann | Test Sharpe | Test MaxDD | Robust gate? |")
    w("|---|---|---:|---:|---:|:---:|")
    for r in rolling["rows"]:
        te = r["test"]
        robust = te["sharpe"] > 1.0 and te["ann"] > 0.50 and te["max_dd"] > -0.20
        mark = "✅" if robust else "❌"
        w(f"| {r['window']} | {r['calendar']} | {fmt_pct(te['ann'])} | "
          f"{te['sharpe']:.2f} | {fmt_pct(te['max_dd'])} | {mark} |")
    w("")
    w(f"- **Positive annualized return:** {rolling['n_positive_ann']}/{rolling['n_windows']} windows")
    w(f"- **Sharpe > 1.0:** {rolling['n_sharpe_gt_1']}/{rolling['n_windows']} windows")
    w(f"- **Full robust gate (Sharpe>1 & Ann>50% & MaxDD<20%): "
      f"{rolling['n_robust_pass']}/{rolling['n_windows']} windows** — "
      f"majority ({verdict['rolling_majority_needed']}) required → "
      f"{'PASS' if verdict['robust_majority_rolling'] else 'FAIL'}.")
    w("")

    # ── (e) Verdict ──
    w("---")
    w("")
    w("## (e) Verdict")
    w("")
    if verdict["verdict"] == "ROBUST":
        w(f"**The 2-leg LINK+ETH trend portfolio is ROBUST.** It passes the full "
          f"robustness gate (Sharpe>1 & Ann>50% & MaxDD<20%) on **{verdict['rolling_robust_pass']}/"
          f"{verdict['rolling_total']}** rolling windows — a majority. The OOS 60/40 "
          f"headline with train-only weights "
          f"({'passes' if verdict['oos_6040_passes_gate'] else 'fails'} the gate), and "
          f"{verdict['multisplit_pass']}/{verdict['multisplit_total']} multi-splits keep "
          f"OOS Sharpe>1 & Ann>50%.")
    else:
        w(f"**The 2-leg LINK+ETH trend portfolio is NOT robust.** It passes the full "
          f"robustness gate on only **{verdict['rolling_robust_pass']}/{verdict['rolling_total']}** "
          f"rolling windows — short of the majority ({verdict['rolling_majority_needed']}) required. "
          f"The edge does not persist across all consecutive periods.")
        w("")
        reasons = []
        if rolling["n_positive_ann"] < math.ceil(rolling["n_windows"] / 2):
            reasons.append(f"only {rolling['n_positive_ann']}/{rolling['n_windows']} rolling "
                           f"windows have positive annualized return")
        if rolling["n_sharpe_gt_1"] < math.ceil(rolling["n_windows"] / 2):
            reasons.append(f"only {rolling['n_sharpe_gt_1']}/{rolling['n_windows']} rolling "
                           f"windows have Sharpe>1.0")
        if not verdict["oos_6040_passes_gate"]:
            reasons.append(f"OOS 60/40 fails the gate (Ann {fmt_pct(oos['test']['ann'])}, "
                           f"Sharpe {oos['test']['sharpe']:.2f}, MaxDD {fmt_pct(oos['test']['max_dd'])})")
        if not verdict["multisplit_majority_pass"]:
            reasons.append(f"only {verdict['multisplit_pass']}/{verdict['multisplit_total']} "
                           f"multi-splits keep OOS Sharpe>1 & Ann>50%")
        if reasons:
            w("Specific failures:")
            for r in reasons:
                w(f"- {r}")
    w("")
    w("**Verdict rule:** ROBUST iff the full robustness gate "
      "(Sharpe>1 & Ann>50% & MaxDD<20%) passes on the MAJORITY of the 6 rolling "
      "expanding windows.")
    w("")
    w("## Methodology")
    w("")
    w("- **Engine:** exact reuse of `research_dd_controlled_trend.py::simulate` + "
      "`research_portfolio_dd_trend.py::run_leg` + "
      "`research_portfolio_optimizer.py::weighted_monthly_rebal` / `compute_alloc`. "
      "Signals/overlays/costs identical to the original portfolio stress test.")
    w("- **Weights:** tangency (max-Sharpe) optimized from train-half per-leg daily "
      "return covariance via SLSQP, no leg >60%, weights sum to 1. Weights are "
      "frozen from train and applied to test via a monthly-rebalanced portfolio. "
      "Full-sample-fit weights are NEVER applied to test data.")
    w("- **Trend indicators/signals** are sliced from full-series computation "
      "(causal, no look-ahead), matching the optimizer's walk-forward methodology.")
    w("- **Rolling:** expanding train (40%→89%), ~10% test slices, weights "
      "re-optimized per window.")
    w("")
    w("*Numbers, not adjectives. The Boss gets truth.*")
    return "\n".join(L) + "\n"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print("2-LEG TREND PORTFOLIO RE-VALIDATION — LINK3x / ETHST1x")
    print("  (honest train-only weights; no full-sample-fit weights on test)")
    print("=" * 72)

    print("\n[load] trend data...")
    raw = eng.load_all_symbols()
    packs: dict[str, eng.IndicatorPack] = {}
    for sym, df in raw.items():
        packs[sym] = eng.build_pack(df)
    targets: dict = {}
    for sym in ["LINKUSDC", "ETHUSDC"]:
        for strat in ["donchian", "supertrend"]:
            tgt, _warm = eng.gen_targets(strat, packs[sym])
            targets[(sym, strat)] = tgt
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    data_meta = {
        "n_bars": int(n),
        "start": str(pd.to_datetime(int(ref_pack.ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_pack.ts[-1]), unit="ms", utc=True)),
    }
    print(f"  {data_meta['n_bars']} bars, {data_meta['start']} -> {data_meta['end']}")

    print("\n[(a)] Full-sample 2-leg metrics (in-sample reference)...")
    full = test_full_sample(packs, targets)

    print("\n[(b)] OOS 60/40 with train-only weights...")
    oos = test_oos_6040(packs, targets)

    print("\n[(c)] Multi-split walk-forward (7 splits)...")
    multisplit = test_multisplit(packs, targets)
    print(f"  -> {multisplit['n_pass']}/{multisplit['n_total']} splits pass "
          f"(Sharpe>1 & Ann>50%)")

    print("\n[(d)] Rolling expanding-window validation (6 windows)...")
    rolling = test_rolling(packs, targets)
    print(f"  -> {rolling['n_positive_ann']}/{rolling['n_windows']} positive ann, "
          f"{rolling['n_sharpe_gt_1']}/{rolling['n_windows']} sharpe>1, "
          f"{rolling['n_robust_pass']}/{rolling['n_windows']} full robust gate")

    print("\n[(e)] Verdict...")
    verdict = compute_verdict(multisplit, rolling, oos)
    print(f"  VERDICT: {verdict['verdict']}")
    print(f"    rolling robust gate: {verdict['rolling_robust_pass']}/{verdict['rolling_total']} "
          f"(need {verdict['rolling_majority_needed']} for majority)")
    print(f"    oos 60/40 gate: {verdict['oos_6040_passes_gate']}")
    print(f"    multisplit: {verdict['multisplit_pass']}/{verdict['multisplit_total']}")

    print("\n[write] generating markdown + json...")
    md = generate_markdown(data_meta, full, oos, multisplit, rolling, verdict)
    REPORT_MD.write_text(md, encoding="utf-8")
    print(f"  wrote {REPORT_MD}")
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_meta": data_meta,
        "costs": {"fee_side": eng.FEE_RATE, "slippage_side": eng.SLIPPAGE,
                  "funding_8h": eng.FUNDING_RATE},
        "legs": {"labels": TWO_LEG_LABELS, "short_labels": SHORT_LABELS},
        "full_sample": full,
        "oos_6040": oos,
        "multisplit": multisplit,
        "rolling": rolling,
        "verdict": verdict,
    }
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default),
                           encoding="utf-8")
    print(f"  wrote {REPORT_JSON}")

    print(f"\nDone in {time.time()-t0:.1f}s. VERDICT: {verdict['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
