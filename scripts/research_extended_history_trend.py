#!/usr/bin/env python3
"""Extended-history trend backtest — LINK Donchian + ETH Supertrend over 2+ yrs.

Tests whether the two causal trend legs that showed Sharpe 1.7-2.2 OOS in the
bear-only window but FAILED rolling validation, become ROBUST when a
BULL/SIDEWAYS regime is included via 2+ years of data.

Reuses the EXACT engine from scripts/research_dd_controlled_trend.py:
  - Donchian(20,10) breakout with .shift(1)  (causal)
  - Supertrend(14,7) with close[i-1] band carry  (causal)
  - ATR2 vol-filter overlay + circuit breaker
  - Costs: 0.04% taker + 0.03% slip/side, 0.010% funding/8h

Legs under test (identical to scripts/research_two_leg_trend.py):
  Leg 1: LINKUSDT donchian atr2_vf_cb @ 3x
  Leg 2: ETHUSDT  supertrend cbreaker @ 1x

Validation:
  (a) Full-sample per-leg + 2-leg tangency portfolio metrics (in-sample ref)
  (b) OOS 60/40 with train-only tangency weights (honest headline)
  (c) Rolling expanding-window validation, 6 windows ~10% test each.
      Weights re-optimized on each expanding TRAIN, frozen for that TEST slice.
      BOTH per-leg AND 2-leg portfolio.
  (d) Calendar dates per window + regime classification (bull/bear/sideways)
  (e) VERDICT: ROBUST iff full gate (Sharpe>1 & Ann>50% & MaxDD<20%) passes
      on MAJORITY (>=3/6) of rolling windows.

Gate: Sharpe>1.0, Ann>50%, MaxDD<20%. Numbers, not adjectives.
Research only — does NOT modify any live config.
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402
import research_portfolio_optimizer as opo  # noqa: E402

REPO_ROOT = HERE.parent
CACHE_DIR = HERE / "_cache_klines_extended"
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_MD = DOCS_DIR / "extended-history-trend-backtest.md"
REPORT_JSON = DOCS_DIR / "extended-history-trend-backtest.json"

INITIAL_CAPITAL = eng.INITIAL_CAPITAL  # 10_000
DAY_MS = eng.DAY_MS

# The two legs under test (USDT substitution — see report header)
LEGS = [
    {"label": "LINK3x", "symbol": "LINKUSDT", "strategy": "donchian",
     "overlay": "atr2_vf_cb", "leverage": 3},
    {"label": "ETH1x", "symbol": "ETHUSDT", "strategy": "supertrend",
     "overlay": "cbreaker", "leverage": 1},
]
LEG_LABELS = [l["label"] for l in LEGS]

# Rolling expanding-window boundaries (6 windows, ~10% test each)
# train grows 40%->89%, test = next ~10% slice
ROLLING = [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70),
           (0.70, 0.80), (0.80, 0.89), (0.89, 0.99)]

# Also per-leg individual rolling windows (same boundaries)
REGIME_THRESH = 0.15  # |total return| > 15% over window => bull/bear; else sideways


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


def fmt_pct(x: float | None, dec: int = 1) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x * 100:.{dec}f}%"


# ─── Data load ────────────────────────────────────────────────────────────────
def load_packs() -> tuple[dict[str, eng.IndicatorPack], dict[str, int]]:
    """Load cached klines, build indicator packs. Align all symbols to the
    common earliest-start / latest-end overlap so all legs share a timeline."""
    raw: dict[str, pd.DataFrame] = {}
    spans: dict[str, int] = {}
    for leg in LEGS:
        sym = leg["symbol"]
        if sym in raw:
            continue
        f = CACHE_DIR / f"{sym}.pkl"
        if not f.exists():
            raise FileNotFoundError(f"Missing cache for {sym}: {f}")
        df = pd.read_pickle(f)
        raw[sym] = df
    # also load all 6 fetched symbols for regime/coverage context
    for f in sorted(CACHE_DIR.glob("*.pkl")):
        sym = f.stem
        if sym not in raw:
            raw[sym] = pd.read_pickle(f)

    # overlap window: max(start) -> min(end)
    start_max = max(int(df["ts"].iloc[0]) for df in raw.values())
    end_min = min(int(df["ts"].iloc[-1]) for df in raw.values())
    print(f"  Overlap window: {pd.to_datetime(start_max, unit='ms', utc=True)} "
          f"-> {pd.to_datetime(end_min, unit='ms', utc=True)}")

    packs: dict[str, eng.IndicatorPack] = {}
    for sym, df in raw.items():
        m = (df["ts"] >= start_max) & (df["ts"] <= end_min)
        dfc = df[m].reset_index(drop=True)
        packs[sym] = eng.build_pack(dfc)
        spans[sym] = len(dfc)
    return packs, spans


# ─── Precompute full-series packs + targets (indicator continuity) ────────────
# IMPORTANT: matches the established methodology in research_portfolio_dd_trend.py
# ::run_leg — the pack (ATR, vol_ok percentile) and targets (Donchian/Supertrend)
# are computed ONCE on the FULL series, then sliced per window. This gives
# indicator continuity across train/test boundaries (a live bot has full history)
# and avoids cold-start warmup noise in short rolling windows. The simulate()
# itself only runs on the window slice, so there is NO look-ahead leak: signals
# at bar t only use data at-or-before bar t.
_FULL_PACKS: dict[str, eng.IndicatorPack] = {}
_FULL_TARGETS: dict[tuple[str, str], np.ndarray] = {}


def precompute_full(packs: dict[str, eng.IndicatorPack]) -> None:
    """Precompute full-series targets per (symbol, strategy) for indicator
    continuity. Packs are already full-series from load_packs()."""
    global _FULL_PACKS, _FULL_TARGETS
    _FULL_PACKS = packs
    strats_needed = {leg["strategy"] for leg in LEGS}
    for sym, pack in packs.items():
        for strat in strats_needed:
            tgt, _warm = eng.gen_targets(strat, pack)
            _FULL_TARGETS[(sym, strat)] = tgt


# ─── Leg simulation on a window ───────────────────────────────────────────────
def run_leg_on_window(leg: dict, packs: dict[str, eng.IndicatorPack],
                      start: int, end: int | None) -> dict:
    """Run one trend leg on bars [start:end] using the engine's simulate().
    Uses the FULL precomputed pack + targets, sliced to the window — identical
    to research_portfolio_dd_trend.py::run_leg (indicator continuity, no leak)."""
    pack = _FULL_PACKS[leg["symbol"]]
    s, e = start, end if end is not None else len(pack.close)
    sub = eng.IndicatorPack(
        close=pack.close[s:e], high=pack.high[s:e], low=pack.low[s:e],
        atr=pack.atr[s:e], atr_frac=pack.atr_frac[s:e],
        vol_ok=pack.vol_ok[s:e], ts=pack.ts[s:e])
    tgt_full = _FULL_TARGETS[(leg["symbol"], leg["strategy"])]
    tgt = tgt_full[s:e]
    ov = eng._overlay_by_name(leg["overlay"])
    res = eng.simulate(tgt, sub, ov, float(leg["leverage"]), 0, None, INITIAL_CAPITAL)
    eq = res["equity_curve"]
    return {
        "eq_aligned": eq,
        "daily_returns": opo.leg_daily_returns(eq, sub.ts),
        "metrics": res["metrics"],
        "ts": sub.ts,
    }


def portfolio_at_weights(leg_data: dict[str, dict], labels: list[str],
                         weights: np.ndarray, ref_ts: np.ndarray) -> tuple[np.ndarray, dict]:
    eqs = [leg_data[l]["eq_aligned"] for l in labels]
    port_eq = opo.weighted_monthly_rebal(eqs, np.asarray(weights, float), ref_ts, INITIAL_CAPITAL)
    m = eng._metrics_from_curve(port_eq, ref_ts, [], INITIAL_CAPITAL, 0, len(port_eq))
    return port_eq, m


def optimize_tangency(leg_data: dict[str, dict], labels: list[str]) -> np.ndarray:
    R, mu, cov, stds = opo._moments_from_legs(leg_data, labels)
    return opo.compute_alloc("tangency", mu, cov, stds)


# ─── Regime classification ────────────────────────────────────────────────────
def classify_regime(price_seg: np.ndarray) -> str:
    """Classify a price segment as BULL/BEAR/SIDEWAYS by total return."""
    if len(price_seg) < 2:
        return "n/a"
    tot = float(price_seg[-1] / price_seg[0] - 1.0)
    if tot > REGIME_THRESH:
        return f"BULL (+{tot*100:.0f}%)"
    if tot < -REGIME_THRESH:
        return f"BEAR ({tot*100:.0f}%)"
    return f"SIDEWAYS ({tot*100:+.0f}%)"


def classify_regime_multi(prices: dict[str, np.ndarray]) -> str:
    """Aggregate regime label across multiple symbols (majority vote)."""
    labels = []
    for sym, seg in prices.items():
        labels.append(classify_regime(seg))
    # extract category words
    cats = []
    for lab in labels:
        if "BULL" in lab:
            cats.append("BULL")
        elif "BEAR" in lab:
            cats.append("BEAR")
        else:
            cats.append("SIDEWAYS")
    from collections import Counter
    c = Counter(cats)
    top, n = c.most_common(1)[0]
    return f"{top} (basket: {', '.join(labels)})"


# ─── Tests ────────────────────────────────────────────────────────────────────
def test_full_sample(packs, ref_ts) -> dict:
    """(a) Full-sample per-leg + 2-leg tangency (in-sample)."""
    leg = {}
    for l in LEGS:
        leg[l["label"]] = run_leg_on_window(l, packs, 0, None)
    w = optimize_tangency(leg, LEG_LABELS)
    _, m = portfolio_at_weights(leg, LEG_LABELS, w, ref_ts)
    leg_m = {lab: leg[lab]["metrics"] for lab in LEG_LABELS}
    print(f"  full: port ann {fmt_pct(m['ann_return'])}, shp {m['sharpe']:.2f}, "
          f"dd {fmt_pct(m['max_dd'])}, w={[f'{x:.2f}' for x in w]}")
    for lab in LEG_LABELS:
        mm = leg_m[lab]
        print(f"    {lab}: ann {fmt_pct(mm['ann_return'])}, shp {mm['sharpe']:.2f}, "
              f"dd {fmt_pct(mm['max_dd'])}, trades {mm['n_trades']}")
    return {"metrics": m, "weights": w.tolist(), "leg_metrics": leg_m}


def test_oos_6040(packs, ref_ts_full) -> dict:
    """(b) OOS 60/40 with train-only tangency weights."""
    n = len(ref_ts_full)
    split = int(n * 0.60)
    ref_ts_train = ref_ts_full[:split]
    ref_ts_test = ref_ts_full[split:]
    # train
    train_leg = {}
    for l in LEGS:
        train_leg[l["label"]] = run_leg_on_window(l, packs, 0, split)
    train_leg["ts_check"] = None
    w = optimize_tangency(train_leg, LEG_LABELS)
    _, train_m = portfolio_at_weights(train_leg, LEG_LABELS, w, ref_ts_train)
    # test: apply FROZEN train weights
    test_leg = {}
    for l in LEGS:
        test_leg[l["label"]] = run_leg_on_window(l, packs, split, n)
    _, test_m = portfolio_at_weights(test_leg, LEG_LABELS, w, ref_ts_test)
    print(f"  oos60/40: train shp {train_m['sharpe']:.2f}/ann {fmt_pct(train_m['ann_return'])}, "
          f"TEST shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/"
          f"dd {fmt_pct(test_m['max_dd'])}, w={[f'{x:.2f}' for x in w]}")
    # regime of test window
    test_prices = {sym: packs[sym].close[split:n] for sym in
                   [l["symbol"] for l in LEGS]}
    regime = classify_regime_multi(test_prices)
    cal = (str(pd.to_datetime(int(ref_ts_full[split]), unit="ms", utc=True).date()),
           str(pd.to_datetime(int(ref_ts_full[-1]), unit="ms", utc=True).date()))
    return {
        "weights": w.tolist(), "calendar": f"{cal[0]} -> {cal[1]}",
        "test_regime": regime,
        "train": {"ann": train_m["ann_return"], "sharpe": train_m["sharpe"],
                  "max_dd": train_m["max_dd"]},
        "test": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                 "max_dd": test_m["max_dd"], "total_return": test_m["total_return"]},
    }


def test_rolling(packs, ref_ts_full) -> dict:
    """(d) Rolling expanding-window: per-leg AND 2-leg portfolio, 6 windows."""
    n = len(ref_ts_full)
    rows = []
    for train_frac, test_frac in ROLLING:
        train_end = int(n * train_frac)
        test_end = int(n * test_frac)
        if test_end <= train_end:
            continue
        ref_ts_train = ref_ts_full[:train_end]
        ref_ts_test = ref_ts_full[train_end:test_end]
        # train legs + tangency
        train_leg = {}
        for l in LEGS:
            train_leg[l["label"]] = run_leg_on_window(l, packs, 0, train_end)
        w = optimize_tangency(train_leg, LEG_LABELS)
        # test legs (frozen weights)
        test_leg = {}
        for l in LEGS:
            test_leg[l["label"]] = run_leg_on_window(l, packs, train_end, test_end)
        _, test_m = portfolio_at_weights(test_leg, LEG_LABELS, w, ref_ts_test)
        # per-leg test metrics
        per_leg = {}
        for lab in LEG_LABELS:
            per_leg[lab] = test_leg[lab]["metrics"]
        # calendar + regime
        cal_start = str(pd.to_datetime(int(ref_ts_full[train_end]), unit="ms", utc=True).date())
        cal_end = str(pd.to_datetime(int(ref_ts_full[test_end - 1]), unit="ms", utc=True).date())
        # regime over test window using both leg symbols + BTC as basket proxy
        regime_syms = [l["symbol"] for l in LEGS]
        if "BTCUSDT" in packs:
            regime_syms = list(set(regime_syms + ["BTCUSDT"]))
        test_prices = {sym: packs[sym].close[train_end:test_end] for sym in regime_syms}
        regime = classify_regime_multi(test_prices)
        rows.append({
            "window": f"train 0-{int(train_frac*100)}% / test {int(train_frac*100)}-{int(test_frac*100)}%",
            "train_frac": train_frac, "test_frac": test_frac,
            "calendar": f"{cal_start} -> {cal_end}",
            "regime": regime,
            "n_test_bars": test_end - train_end,
            "weights": w.tolist(),
            "portfolio": {"ann": test_m["ann_return"], "sharpe": test_m["sharpe"],
                          "max_dd": test_m["max_dd"], "total_return": test_m["total_return"]},
            "per_leg": {lab: {"ann": per_leg[lab]["ann_return"],
                              "sharpe": per_leg[lab]["sharpe"],
                              "max_dd": per_leg[lab]["max_dd"],
                              "n_trades": per_leg[lab]["n_trades"]}
                        for lab in LEG_LABELS},
        })
        pl = rows[-1]["per_leg"]
        print(f"    {rows[-1]['window']} ({rows[-1]['calendar']}, {regime.split(' (')[0]}): "
              f"PORT shp {test_m['sharpe']:.2f}/ann {fmt_pct(test_m['ann_return'])}/dd {fmt_pct(test_m['max_dd'])} | "
              f"LINK shp {pl['LINK3x']['sharpe']:.2f} | ETH shp {pl['ETH1x']['sharpe']:.2f}")
    # portfolio-level rollup
    n_robust_port = sum(1 for r in rows
                        if r["portfolio"]["sharpe"] > 1.0
                        and r["portfolio"]["ann"] > 0.50
                        and r["portfolio"]["max_dd"] > -0.20)
    # per-leg robustness
    per_leg_robust = {}
    for lab in LEG_LABELS:
        n_r = sum(1 for r in rows
                  if r["per_leg"][lab]["sharpe"] > 1.0
                  and r["per_leg"][lab]["ann"] > 0.50
                  and r["per_leg"][lab]["max_dd"] > -0.20)
        per_leg_robust[lab] = n_r
    return {"rows": rows, "n_windows": len(rows),
            "n_robust_portfolio": n_robust_port,
            "per_leg_robust": per_leg_robust}


def compute_verdict(rolling, oos) -> dict:
    n_win = rolling["n_windows"]
    n_robust = rolling["n_robust_portfolio"]
    majority = n_robust >= math.ceil(n_win / 2) if n_win else False
    verdict = "ROBUST" if majority else "NOT ROBUST"
    oos_ok = (oos["test"]["sharpe"] > 1.0 and oos["test"]["ann"] > 0.50
              and oos["test"]["max_dd"] > -0.20)
    return {
        "verdict": verdict,
        "robust_majority_rolling": majority,
        "rolling_robust_pass": n_robust,
        "rolling_total": n_win,
        "rolling_majority_needed": math.ceil(n_win / 2) if n_win else 0,
        "oos_6040_passes_gate": oos_ok,
        "per_leg_robust": rolling["per_leg_robust"],
    }


# ─── Markdown ─────────────────────────────────────────────────────────────────
def generate_markdown(data_meta, full, oos, rolling, verdict) -> str:
    L: list[str] = []
    def w(s=""): L.append(s)
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    w("# Extended-History Trend Backtest — LINK Donchian + ETH Supertrend (2+ yrs)")
    w("")
    w(f"*Generated by `scripts/research_extended_history_trend.py` — {gen}. "
      "Numbers, not adjectives. Research only — no live config modified.*")
    w("")
    # ── Substitution note ──
    w("> **⚠ DATA SUBSTITUTION (USDC → USDT).** The task requested USDC-M "
      "perpetuals. Binance USDC-M perps only list from **2024-01-04** (~18 "
      "months) and **INJUSDC does not exist**. USDC-M alone is STILL almost "
      "entirely the bear regime this research is trying to escape. To obtain "
      "2+ years covering a bull market, this study uses **USDT-M perpetuals**, "
      "which list back to 2019-2022. USDC and USDT trade within <0.1% of each "
      "other 24/7 (both USD-pegged), so trend signals (Donchian breakout, "
      "Supertrend) are numerically equivalent. The 2 years of additional "
      "history — 2023 recovery bull + 2024 ATH bull + 2025 sideways — is the "
      "entire point of this exercise.")
    w("")
    w("## Candidate (identical to `research_two_leg_trend.py`)")
    w("")
    w("- **Leg 1:** LINKUSDT donchian atr2_vf_cb @ **3x** (Donchian(20,10) breakout, ATR2 vol-filter, circuit breaker)")
    w("- **Leg 2:** ETHUSDT  supertrend cbreaker @ **1x** (Supertrend(14,7), circuit breaker)")
    w("- Both audited **look-ahead-free** in `portfolio-stress-INDEPENDENT-VALIDATION.md`: "
      "Donchian uses `rolling().max().shift(1)`; Supertrend uses `close[i-1]` for band carry; "
      "ATR is Wilder-causal; circuit breaker uses present/past data only.")
    w("")
    w(f"**Data:** {data_meta['n_bars']} hourly bars per symbol (~{data_meta['n_bars']//24} days), "
      f"Binance USDT-M perps. Overlap window {data_meta['start']} → {data_meta['end']}.")
    per_sym = "; ".join(f"{s}: {d['n_bars']} bars ({d['span']})" for s, d in data_meta["symbols"].items())
    w(f"**Per-symbol raw coverage:** {per_sym}")
    w(f"**Costs:** {eng.FEE_RATE*100:.2f}% taker + {eng.SLIPPAGE*100:.2f}% slippage/side "
      f"= {(eng.FEE_RATE+eng.SLIPPAGE)*200:.2f}% round-trip, {eng.FUNDING_RATE*100:.3f}% funding/8h.")
    w("**Method:** per-leg equity curves via the exact `research_dd_controlled_trend.py::simulate`; "
      "portfolio P&L via **monthly rebalancing to tangency weights**. Tangency weights optimized "
      "from covariance of per-leg daily returns (SLSQP max-Sharpe, no leg >60%).")
    w("")
    w("**CRITICAL METHODOLOGY RULES (from SD-003):**")
    w("- **No look-ahead:** signals decided at bar t from info available at bar t's close; earn move from t+1 onward.")
    w("- **Train-only weights:** tangency optimized on TRAIN half ONLY, frozen for TEST half. "
      "Full-sample-fit weights NEVER applied to test data.")
    w(f"- **Gate:** Sharpe > 1.0, Ann > 50%, MaxDD < 20%.")
    w("- **VERDICT RULE:** ROBUST iff full gate passes on MAJORITY (≥3/6) of rolling windows.")
    w("")

    # ── Regime map ──
    w("## Regime Map of the Extended Window")
    w("")
    w("To interpret results, here is the BTC price journey across the study window "
      "(USDT-M 1h close), with regime labels:")
    w("")
    w("| Period | BTC Start → End | Regime |")
    w("|---|---|---|")
    for seg in data_meta["btc_regime_segments"]:
        w(f"| {seg['period']} | ${seg['start']:.0f} → ${seg['end']:.0f} ({seg['ret']:+.0%}) | {seg['label']} |")
    w("")
    w("The extended window contains **two bull legs (2023 recovery, 2024 ATH run)**, "
      "a sideways/choppy phase, and the **late-2025/2026 bear** that dominated the "
      "prior 180-376 day studies. This is the regime diversification the prior "
      "studies lacked.")
    w("")

    # ── VERDICT block at top ──
    v = verdict["verdict"]
    badge = {"ROBUST": "🟢 ROBUST — passes the majority of rolling windows",
             "NOT ROBUST": "🔴 NOT ROBUST — fails the majority of rolling windows"}[v]
    w(f"> ## VERDICT: {badge}")
    w(">")
    w(f"> **Portfolio** robustness gate (Sharpe>1 & Ann>50% & MaxDD<20%): "
      f"**{verdict['rolling_robust_pass']}/{verdict['rolling_total']}** rolling windows pass; "
      f"majority ({verdict['rolling_majority_needed']}) required → "
      f"**{'PASS' if verdict['robust_majority_rolling'] else 'FAIL'}**.")
    for lab in LEG_LABELS:
        w(f"> **{lab} (per-leg)** robust gate pass: "
          f"**{verdict['per_leg_robust'][lab]}/{verdict['rolling_total']}** windows.")
    w(f"> OOS 60/40 ({oos['calendar']}, {oos['test_regime']}): "
      f"Ann **{fmt_pct(oos['test']['ann'])}**, Sharpe **{oos['test']['sharpe']:.2f}**, "
      f"MaxDD **{fmt_pct(oos['test']['max_dd'])}** — "
      f"{'passes' if verdict['oos_6040_passes_gate'] else 'FAILS'} the gate.")
    w("")

    # ── (a) Full-sample ──
    w("## (a) Full-sample metrics (IN-SAMPLE reference)")
    w("")
    w("Tangency weights optimized on the **full sample** — in-sample upper bound, "
      "NOT an out-of-sample number. Reference only.")
    w("")
    w("| Leg | Ann | Sharpe | MaxDD | Calmar | Trades |")
    w("|---|---:|---:|---:|---:|---:|")
    for lab in LEG_LABELS:
        m = full["leg_metrics"][lab]
        w(f"| {lab} | {fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | "
          f"{fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {m['n_trades']} |")
    w("")
    fm = full["metrics"]
    ww = " / ".join(f"{s}={int(x*100)}%" for s, x in zip(LEG_LABELS, full["weights"]))
    w("| Portfolio (full, tangency) | Ann | Sharpe | MaxDD | Calmar | Weights |")
    w("|---|---:|---:|---:|---:|---|")
    w(f"| 2-leg | {fmt_pct(fm['ann_return'])} | {fm['sharpe']:.2f} | "
      f"{fmt_pct(fm['max_dd'])} | {fm['calmar']:.2f} | {ww} |")
    w("")

    # ── (b) OOS 60/40 ──
    w("## (b) OOS 60/40 with train-only weights (honest headline)")
    w("")
    w("Tangency weights optimized on **train (first 60%) only**, then **frozen** "
      "and applied to the test half (last 40%). The only honest single-split OOS number.")
    w("")
    ww_tr = " / ".join(f"{s}={int(x*100)}%" for s, x in zip(LEG_LABELS, oos["weights"]))
    w(f"**Frozen train-optimized weights:** {ww_tr}")
    w("")
    w("| Window | Calendar | Regime | Ann | Sharpe | MaxDD |")
    w("|---|---|---|---:|---:|---:|")
    w(f"| Train (0-60%) | — | — | {fmt_pct(oos['train']['ann'])} | {oos['train']['sharpe']:.2f} | "
      f"{fmt_pct(oos['train']['max_dd'])} |")
    w(f"| **Test (60-100%)** | {oos['calendar']} | {oos['test_regime']} | "
      f"**{fmt_pct(oos['test']['ann'])}** | **{oos['test']['sharpe']:.2f}** | "
      f"**{fmt_pct(oos['test']['max_dd'])}** |")
    w("")
    gate = (oos["test"]["sharpe"] > 1.0 and oos["test"]["ann"] > 0.50
            and oos["test"]["max_dd"] > -0.20)
    w(f"**Gate (Sharpe>1 & Ann>50% & MaxDD<20%): {'✅ PASS' if gate else '❌ FAIL'}.**")
    w("")

    # ── (c) Rolling ──
    w("## (c) Rolling expanding-window validation (6 windows)")
    w("")
    w("Train on an expanding window, test on the next ~10% slice. Weights "
      "re-optimized on each expanding TRAIN set, frozen for that window's TEST slice. "
      "Calendar dates + regime shown to map performance to market context. "
      "This is the **strictest overfit test** — a robust strategy passes the gate "
      "in the MAJORITY of consecutive periods regardless of regime.")
    w("")
    w("### Per-leg + Portfolio, by window")
    w("")
    w("| Window | Calendar | Regime | LINK Ann | LINK Shp | LINK DD | ETH Ann | ETH Shp | ETH DD | **Port Ann** | **Port Shp** | **Port DD** | Port gate? |")
    w("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|")
    for r in rolling["rows"]:
        pl = r["per_leg"]
        p = r["portfolio"]
        robust = p["sharpe"] > 1.0 and p["ann"] > 0.50 and p["max_dd"] > -0.20
        mark = "✅" if robust else "❌"
        reg_short = r["regime"].split(" (")[0]
        w(f"| {r['window']} | {r['calendar']} | {reg_short} | "
          f"{fmt_pct(pl['LINK3x']['ann'])} | {pl['LINK3x']['sharpe']:.2f} | {fmt_pct(pl['LINK3x']['max_dd'])} | "
          f"{fmt_pct(pl['ETH1x']['ann'])} | {pl['ETH1x']['sharpe']:.2f} | {fmt_pct(pl['ETH1x']['max_dd'])} | "
          f"**{fmt_pct(p['ann'])}** | **{p['sharpe']:.2f}** | **{fmt_pct(p['max_dd'])}** | {mark} |")
    w("")
    # per-leg robustness summary
    for lab in LEG_LABELS:
        n_r = rolling["per_leg_robust"][lab]
        w(f"- **{lab}** robust gate pass: **{n_r}/{rolling['n_windows']}** windows")
    w(f"- **Portfolio** robust gate pass: **{rolling['n_robust_portfolio']}/{rolling['n_windows']}** windows")
    w("")
    w(f"**Portfolio robust gate (Sharpe>1 & Ann>50% & MaxDD<20%): "
      f"{rolling['n_robust_portfolio']}/{rolling['n_windows']} windows** — "
      f"majority ({verdict['rolling_majority_needed']}) required → "
      f"{'PASS' if verdict['robust_majority_rolling'] else 'FAIL'}.")
    w("")

    # ── (d) Verdict ──
    w("---")
    w("")
    w("## (d) Verdict")
    w("")
    if verdict["verdict"] == "ROBUST":
        w(f"**The 2-leg LINK+ETH trend portfolio is ROBUST on the extended 2+ year "
          f"window.** It passes the full gate on **{verdict['rolling_robust_pass']}/"
          f"{verdict['rolling_total']}** rolling windows — a majority. The OOS 60/40 "
          f"headline {'passes' if verdict['oos_6040_passes_gate'] else 'fails'} the gate.")
    else:
        w(f"**The 2-leg LINK+ETH trend portfolio is NOT robust on the extended window.** "
          f"It passes the full gate on only **{verdict['rolling_robust_pass']}/"
          f"{verdict['rolling_total']}** rolling windows — short of the majority "
          f"({verdict['rolling_majority_needed']}) required.")
        w("")
        w("Specific per-leg pass counts (full gate, per window):")
        for lab in LEG_LABELS:
            n_r = rolling["per_leg_robust"][lab]
            verdict_lab = "ROBUST" if n_r >= math.ceil(rolling["n_windows"] / 2) else "NOT ROBUST"
            w(f"- **{lab}:** {n_r}/{rolling['n_windows']} windows pass → **{verdict_lab}** (majority needed: {math.ceil(rolling['n_windows']/2)})")
        if not verdict["oos_6040_passes_gate"]:
            w(f"- OOS 60/40 fails the gate (Ann {fmt_pct(oos['test']['ann'])}, "
              f"Sharpe {oos['test']['sharpe']:.2f}, MaxDD {fmt_pct(oos['test']['max_dd'])})")
    w("")
    w("**Verdict rule:** ROBUST iff the full gate (Sharpe>1 & Ann>50% & MaxDD<20%) "
      "passes on the MAJORITY (≥3/6) of rolling expanding windows.")
    w("")
    w("## Methodology")
    w("")
    w("- **Engine:** exact reuse of `research_dd_controlled_trend.py::simulate` + "
      "`research_portfolio_optimizer.py::weighted_monthly_rebal` / `compute_alloc`. "
      "Signals/overlays/costs identical to the original portfolio stress test.")
    w("- **Weights:** tangency (max-Sharpe) optimized from train-half per-leg daily "
      "returns; frozen and applied to test. Never full-sample-fit on test.")
    w("- **No look-ahead:** Donchian `rolling().max().shift(1)`; Supertrend `close[i-1]`; "
      "ATR Wilder-causal; circuit-breaker present/past only.")
    w("- **Data:** USDT-M perps (USDC-M unavailable pre-2024 / INJUSDC nonexistent). "
      "Cached hourly klines in `scripts/_cache_klines_extended/`.")
    w("")
    return "\n".join(L)


# ─── Main ─────────────────────────────────────────────────────────────────────
def build_btc_regime_segments(btc_pack: eng.IndicatorPack) -> list[dict]:
    """Split BTC price into named regime segments for the report map."""
    close = btc_pack.close
    ts = btc_pack.ts
    # sample quarterly boundaries
    dt = pd.to_datetime(ts, unit="ms", utc=True)
    ys = pd.DatetimeIndex(dt).year.to_numpy()
    ms = pd.DatetimeIndex(dt).month.to_numpy()
    qs = (ms - 1) // 3 + 1
    cur_y, cur_q = int(ys[0]), int(qs[0])
    segs = []
    idx_starts = [0]
    for i in range(1, len(ys)):
        y, q = int(ys[i]), int(qs[i])
        if (y, q) != (cur_y, cur_q):
            idx_starts.append(i)
            cur_y, cur_q = y, q
    idx_starts.append(len(ys) - 1)
    dti = pd.DatetimeIndex(dt)
    for j in range(len(idx_starts) - 1):
        s, e = idx_starts[j], idx_starts[j + 1] - 1
        if e <= s:
            continue
        ret = float(close[e] / close[s] - 1.0)
        if ret > 0.20:
            lab = "BULL"
        elif ret < -0.20:
            lab = "BEAR"
        else:
            lab = "SIDEWAYS/CHOP"
        segs.append({
            "period": f"{dti[s].strftime('%Y-%m')}..{dti[e].strftime('%Y-%m')}",
            "start": float(close[s]), "end": float(close[e]), "ret": ret, "label": lab,
        })
    return segs


def main() -> None:
    print("=" * 70)
    print(" Extended-History Trend Backtest (2+ years, USDT-M)")
    print("=" * 70)
    print("\n[1/4] Loading cached klines + building packs...")
    packs, spans = load_packs()
    # reference timeline = ETH (both legs share grid; ETH/BTC fully overlap)
    ref_pack = packs["ETHUSDT"]
    ref_ts = ref_pack.ts.copy()
    n = len(ref_ts)
    data_meta = {
        "n_bars": int(n),
        "start": str(pd.to_datetime(int(ref_ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_ts[-1]), unit="ms", utc=True)),
        "symbols": {sym: {"n_bars": int(spans[sym]),
                          "span": f"{pd.to_datetime(int(packs[sym].ts[0]), unit='ms', utc=True).date()}.."
                                  f"{pd.to_datetime(int(packs[sym].ts[-1]), unit='ms', utc=True).date()}"}
                    for sym in spans},
        "btc_regime_segments": build_btc_regime_segments(packs["BTCUSDT"]),
    }
    print(f"  {len(packs)} symbols, {n} bars ({n//24} days) on common timeline")
    print(f"  BTC regime segments: {len(data_meta['btc_regime_segments'])}")

    # Precompute full-series packs + targets for indicator continuity across
    # window slices (matches research_portfolio_dd_trend.py::run_leg exactly).
    print("  Precomputing full-series signals (Donchian/Supertrend)...")
    precompute_full(packs)

    print("\n[2/4] (a) Full-sample per-leg + portfolio...")
    t0 = time.time()
    full = test_full_sample(packs, ref_ts)
    print(f"    ({time.time()-t0:.1f}s)")

    print("\n[3/4] (b) OOS 60/40 with train-only weights...")
    t0 = time.time()
    oos = test_oos_6040(packs, ref_ts)
    print(f"    ({time.time()-t0:.1f}s)")

    print("\n[4/4] (c) Rolling expanding-window (6 windows, per-leg + portfolio)...")
    t0 = time.time()
    rolling = test_rolling(packs, ref_ts)
    print(f"    ({time.time()-t0:.1f}s)")

    verdict = compute_verdict(rolling, oos)
    print(f"\n  VERDICT: {verdict['verdict']}  "
          f"(portfolio {verdict['rolling_robust_pass']}/{verdict['rolling_total']} rolling windows)")
    for lab in LEG_LABELS:
        print(f"    {lab}: {verdict['per_leg_robust'][lab]}/{verdict['rolling_total']} windows")

    md = generate_markdown(data_meta, full, oos, rolling, verdict)
    REPORT_MD.write_text(md, encoding="utf-8")
    payload = {"meta": data_meta, "full_sample": full, "oos_6040": oos,
               "rolling": rolling, "verdict": verdict,
               "costs": {"fee_side": eng.FEE_RATE, "slippage_side": eng.SLIPPAGE,
                         "funding_8h": eng.FUNDING_RATE},
               "legs": [{"label": l["label"], "symbol": l["symbol"],
                         "strategy": l["strategy"], "overlay": l["overlay"],
                         "leverage": l["leverage"]} for l in LEGS]}
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    print(f"\n  Wrote {REPORT_MD.relative_to(REPO_ROOT)}")
    print(f"  Wrote {REPORT_JSON.relative_to(REPO_ROOT)}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
