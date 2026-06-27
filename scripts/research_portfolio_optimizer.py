#!/usr/bin/env python3
"""Portfolio Optimizer — Capital Allocation + Exhaustive Combo Search.

Problem
-------
The best equal-weight portfolio so far (LINK3x trend + DOT funding-contrarian 1x,
monthly rebalanced) gives +89.1% ann / Sharpe 2.45 / MaxDD -11.9% — strong but
short of the aggressive alpha bar (ann > 100%, Sharpe > 1.5, MaxDD < 15%).

This script attacks the gap on three fronts:
  1. Capital allocation optimization (min-variance, risk-parity, max-Sharpe
     tangency) instead of naive equal-weight — tilt toward high-Sharpe legs.
  2. Exhaustive 2/3/4-leg combo search over all gate-passing candidates.
  3. Leverage on the uncorrelated funding-contrarian leg (DOT 2x/3x) to push
     portfolio return over 100% while diversification holds drawdown in check.

Method
------
Each candidate leg (7 trend + 3 funding) is simulated independently on the full
data window using the exact engine from research_portfolio_dd_trend.py. Per-leg
equity curves are aligned to the hourly reference grid. Portfolio P&L is built
via monthly rebalancing to the target weight vector (matches existing methodology).
Allocation weights are optimized from the covariance of per-leg daily returns:
  - Min-variance: minimize w'Σw s.t. sum(w)=1, 0≤w≤0.6 (convex QP via SLSQP)
  - Risk-parity:  w_i ∝ 1/σ_i (inverse-volatility), capped at 0.6
  - Tangency:     maximize (w'μ)/√(w'Σw) (max-Sharpe)

Every 2/3/4-leg subset of the 10 candidates is evaluated under all 4 allocation
schemes (min-var, RP, tangency, equal-weight). The best scheme per combo is
reported. Walk-forward (60/40) validates the top 3: weights frozen from train,
applied to test. True winners need OOS Sharpe > 1.0 AND OOS ann > 50%.

Costs: 0.04% taker + 0.03% slippage/side, 0.010% funding/8h (already in leg returns).
Data: Binance USDC-M perps, 1h trend (9024 bars ~376d), 8h funding (3392 periods).
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize as sp_minimize

# ─── Reuse existing engines ──────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402
import research_portfolio_dd_trend as pp  # noqa: E402

REPO_ROOT = HERE.parent
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_CAPITAL = pp.INITIAL_CAPITAL  # 10_000
DAY_MS = pp.DAY_MS
MAX_W = 0.60  # no single leg > 60%
WF_SPLIT = 0.60  # walk-forward train fraction

# ─── Candidate leg definitions ───────────────────────────────────────────────
# 7 gate-passing trend legs + DOT funding contrarian at 1x/2x/3x
CANDIDATES: list[dict] = []
for _key in pp.ALL_GATE_CONFIG_KEYS:
    CANDIDATES.append({"label": _key, "spec": pp.trend_leg(_key),
                       "kind": "trend"})
for _lev in [1, 2, 3]:
    CANDIDATES.append({
        "label": f"DOTUSDT_funding_contrarian_{_lev}x",
        "spec": pp.funding_leg("DOTUSDT", float(_lev)),
        "kind": "funding",
    })
CAND_LABELS = [c["label"] for c in CANDIDATES]


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


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-leg data extraction
# ═══════════════════════════════════════════════════════════════════════════════
def leg_daily_returns(eq_aligned: np.ndarray, ref_ts: np.ndarray) -> np.ndarray:
    """Daily-resampled returns from an hourly-aligned equity curve."""
    day_keys = (ref_ts // DAY_MS).astype(np.int64)
    last_per_day: dict[int, float] = {}
    for k, v in zip(day_keys.tolist(), eq_aligned.tolist()):
        last_per_day[k] = v
    days = sorted(last_per_day)
    day_eq = np.array([last_per_day[k] for k in days], dtype=float)
    dr = np.diff(day_eq) / np.maximum(day_eq[:-1], 1e-12)
    return dr


def precompute_legs(packs: dict, targets: dict,
                    funding_data: dict[str, pd.DataFrame],
                    ref_ts: np.ndarray) -> dict[str, dict]:
    """Run every candidate leg on the full window, store aligned equity + metrics."""
    results: dict[str, dict] = {}
    for cand in CANDIDATES:
        lr = pp.run_leg(cand["spec"], packs, targets, funding_data,
                        ref_ts, INITIAL_CAPITAL, 0, None)
        eq = lr["eq_aligned"]
        results[cand["label"]] = {
            "eq_aligned": eq,
            "daily_returns": leg_daily_returns(eq, ref_ts),
            "metrics": lr["metrics"],
            "n_trades": len(lr["trades"]),
            "kind": cand["kind"],
        }
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  Allocation optimizers
# ═══════════════════════════════════════════════════════════════════════════════
def alloc_min_variance(cov: np.ndarray, max_w: float = MAX_W) -> np.ndarray:
    """Min-variance weights via SLSQP: minimize w'Σw s.t. Σw=1, 0≤w≤max_w."""
    n = cov.shape[0]
    if n == 1:
        return np.array([1.0])

    def obj(w):
        return float(w @ cov @ w)

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(1e-8, max_w)] * n
    x0 = np.full(n, 1.0 / n)
    res = sp_minimize(obj, x0, method="SLSQP", bounds=bounds,
                      constraints=cons, options={"maxiter": 1000, "ftol": 1e-14})
    w = np.clip(res.x, 0, max_w)
    s = w.sum()
    return w / s if s > 0 else x0


def alloc_max_sharpe(mu: np.ndarray, cov: np.ndarray,
                     max_w: float = MAX_W) -> np.ndarray:
    """Max-Sharpe (tangency) weights: maximize (w'μ)/√(w'Σw) s.t. Σw=1, 0≤w≤max_w."""
    n = len(mu)
    if n == 1:
        return np.array([1.0])

    def neg_sharpe(w):
        r = float(w @ mu)
        v = float(np.sqrt(max(w @ cov @ w, 1e-24)))
        return -r / v

    cons = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    bounds = [(1e-8, max_w)] * n
    starts = [np.full(n, 1.0 / n)]
    # analytical tangency (unconstrained) as an informed starting point
    try:
        inv = np.linalg.solve(cov + 1e-10 * np.eye(n), mu)
        inv = np.clip(inv, 0, None)
        if inv.sum() > 0:
            starts.append(np.clip(inv / inv.sum(), 0, max_w))
    except np.linalg.LinAlgError:
        pass
    # tilt toward highest-Sharpe leg
    ind_sharpe = mu / np.sqrt(np.maximum(np.diag(cov), 1e-24))
    tilt = np.clip(ind_sharpe, 0, None)
    if tilt.sum() > 0:
        starts.append(np.clip(tilt / tilt.sum(), 0, max_w))

    best = None
    for x0 in starts:
        res = sp_minimize(neg_sharpe, x0, method="SLSQP", bounds=bounds,
                          constraints=cons, options={"maxiter": 1000, "ftol": 1e-14})
        if best is None or res.fun < best.fun:
            best = res
    w = np.clip(best.x, 0, max_w)
    s = w.sum()
    return w / s if s > 0 else np.full(n, 1.0 / n)


def alloc_risk_parity(std_devs: np.ndarray, max_w: float = MAX_W) -> np.ndarray:
    """Inverse-volatility risk-parity weights, capped at max_w."""
    inv_vol = 1.0 / np.maximum(std_devs, 1e-12)
    w = inv_vol / inv_vol.sum()
    # iterative cap-and-renormalize
    for _ in range(100):
        if w.max() <= max_w + 1e-10:
            break
        over = w > max_w
        excess = float(np.sum(w[over] - max_w))
        w[over] = max_w
        under = ~over
        if under.any():
            w[under] += excess / under.sum()
        else:
            break
        w = np.clip(w, 0, max_w)
    s = w.sum()
    return w / s if s > 0 else np.full(len(std_devs), 1.0 / len(std_devs))


def compute_alloc(scheme: str, mu: np.ndarray, cov: np.ndarray,
                  stds: np.ndarray) -> np.ndarray:
    if scheme == "min_var":
        return alloc_min_variance(cov)
    if scheme == "risk_parity":
        return alloc_risk_parity(stds)
    if scheme == "tangency":
        return alloc_max_sharpe(mu, cov)
    if scheme == "equal":
        return np.full(len(mu), 1.0 / len(mu))
    raise ValueError(scheme)


ALLOCATION_SCHEMES = ["min_var", "risk_parity", "tangency", "equal"]


# ═══════════════════════════════════════════════════════════════════════════════
#  Weighted portfolio P&L (monthly rebalance to target weights)
# ═══════════════════════════════════════════════════════════════════════════════
def weighted_monthly_rebal(equity_curves: list[np.ndarray], weights: np.ndarray,
                           ref_ts: np.ndarray,
                           init_capital: float = INITIAL_CAPITAL) -> np.ndarray:
    """Build a portfolio equity curve via monthly rebalancing to target weights.
    Uses per-bar growth rates from each leg's aligned equity curve (scale-
    invariant). At each month boundary, reset each leg's capital to
    weight_i * total_portfolio."""
    n_legs = len(equity_curves)
    eqs = np.array(equity_curves)  # (n_legs, T)
    T = eqs.shape[1]
    dt = pd.to_datetime(ref_ts, unit="ms", utc=True)
    month_id = (dt.year * 12 + dt.month).to_numpy()

    port_eq = np.empty(T)
    leg_cap = init_capital * np.array(weights, dtype=float)
    port_eq[0] = leg_cap.sum()
    for i in range(1, T):
        prev = eqs[:, i - 1]
        curr = eqs[:, i]
        growth = np.where(prev > 1e-12, curr / prev, 1.0)
        leg_cap = leg_cap * growth
        if month_id[i] != month_id[i - 1]:
            total = leg_cap.sum()
            leg_cap = total * np.array(weights, dtype=float)
        port_eq[i] = leg_cap.sum()
    return port_eq


def _moments_from_legs(leg_data: dict, labels: list[str]) -> tuple:
    """Extract aligned daily-return matrix, mean vector, covariance, std devs."""
    drs = [leg_data[l]["daily_returns"] for l in labels]
    min_len = min(len(d) for d in drs)
    R = np.array([d[:min_len] for d in drs])  # (n_legs, n_days)
    mu = R.mean(axis=1)
    cov = np.cov(R)
    stds = np.sqrt(np.maximum(np.diag(cov), 1e-24))
    return R, mu, cov, stds


def eval_combo(labels: list[str], leg_data: dict, ref_ts: np.ndarray,
               init_capital: float = INITIAL_CAPITAL) -> dict:
    """Evaluate a leg combination under all 4 allocation schemes."""
    R, mu, cov, stds = _moments_from_legs(leg_data, labels)
    eq_curves = [leg_data[l]["eq_aligned"] for l in labels]
    results: dict[str, dict] = {}
    for scheme in ALLOCATION_SCHEMES:
        w = compute_alloc(scheme, mu, cov, stds)
        port_eq = weighted_monthly_rebal(eq_curves, w, ref_ts, init_capital)
        m = pp.portfolio_metrics(port_eq, ref_ts)
        results[scheme] = {"weights": w, "metrics": m}
    return results


def best_scheme_for_combo(eval_results: dict) -> tuple[str, dict]:
    """Pick the allocation scheme with highest Sharpe; among schemes that keep
    MaxDD > -15%, prefer the one with highest Sharpe."""
    valid = [(s, r) for s, r in eval_results.items()
             if r["metrics"]["max_dd"] > -0.15]
    pool = valid if valid else list(eval_results.items())
    best_s, best_r = max(pool, key=lambda kv: kv[1]["metrics"]["sharpe"])
    return best_s, best_r


def clears_gate(m: dict) -> bool:
    return (m["sharpe"] > 1.5 and m["ann_return"] > 1.0 and m["max_dd"] > -0.15)


def gate_score(m: dict) -> int:
    s = 0
    if m["sharpe"] > 1.5:
        s += 1
    if m["ann_return"] > 1.0:
        s += 1
    if m["max_dd"] > -0.15:
        s += 1
    return s


# ═══════════════════════════════════════════════════════════════════════════════
#  Walk-forward
# ═══════════════════════════════════════════════════════════════════════════════
def walk_forward_combo(cands: list[dict], packs: dict, targets: dict,
                       funding_data: dict, scheme: str,
                       init_capital: float = INITIAL_CAPITAL) -> dict:
    """60/40 walk-forward: optimize weights on train (first 60%), apply frozen
    to test (last 40%)."""
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    split = int(n * WF_SPLIT)
    ref_ts_train = ref_pack.ts[:split].copy()
    ref_ts_test = ref_pack.ts[split:].copy()
    labels = [c["label"] for c in cands]
    specs = [c["spec"] for c in cands]

    # --- train: run legs, optimize weights ---
    train_leg: dict[str, dict] = {}
    for label, spec in zip(labels, specs):
        lr = pp.run_leg(spec, packs, targets, funding_data, ref_ts_train,
                        init_capital, 0, split)
        train_leg[label] = {
            "eq_aligned": lr["eq_aligned"],
            "daily_returns": leg_daily_returns(lr["eq_aligned"], ref_ts_train),
        }
    R, mu, cov, stds = _moments_from_legs(train_leg, labels)
    w = compute_alloc(scheme, mu, cov, stds)
    train_eq = weighted_monthly_rebal(
        [train_leg[l]["eq_aligned"] for l in labels], w, ref_ts_train, init_capital)
    train_m = pp.portfolio_metrics(train_eq, ref_ts_train)

    # --- test: run legs, apply frozen weights ---
    test_leg: dict[str, dict] = {}
    for label, spec in zip(labels, specs):
        lr = pp.run_leg(spec, packs, targets, funding_data, ref_ts_test,
                        init_capital, split, n)
        test_leg[label] = {"eq_aligned": lr["eq_aligned"]}
    test_eq = weighted_monthly_rebal(
        [test_leg[l]["eq_aligned"] for l in labels], w, ref_ts_test, init_capital)
    test_m = pp.portfolio_metrics(test_eq, ref_ts_test)

    # buy & hold LINK on test
    cseg = ref_pack.close[split:n]
    test_bh = float(cseg[-1] / cseg[0] - 1) if len(cseg) > 1 else 0.0

    return {"weights": w, "scheme": scheme,
            "train": train_m, "test": test_m, "test_bh_link": test_bh}


# ═══════════════════════════════════════════════════════════════════════════════
#  Report generation
# ═══════════════════════════════════════════════════════════════════════════════
def _short_label(label: str) -> str:
    return (label.replace("USDC_donchian_atr2_vf_cb_", "DON ")
            .replace("USDC_donchian_cbreaker_", "DONcb ")
            .replace("USDC_supertrend_cbreaker_", "ST ")
            .replace("USDT_funding_contrarian_", "FUND ")
            .replace("LINK", "LNK"))


def generate_report(leg_data: dict, leaderboard: list[dict],
                    top3_wf: list[dict], allocation_detail: dict,
                    data_meta: dict, sanity: dict) -> tuple[str, dict]:
    L: list[str] = []
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Header ──
    L.append("# Portfolio Optimizer — Capital Allocation & Combo Search")
    L.append("")
    L.append(f"**Generated:** {gen}")
    L.append(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']//24} days) trend, "
             f"Binance USDC-M perps. Funding leg uses 8h bars (~{data_meta.get('funding_periods','?')} periods).")
    L.append(f"**Window:** {data_meta['start']} → {data_meta['end']}")
    L.append(f"**Engine:** Reuses `scripts/research_portfolio_dd_trend.py` per-leg simulation "
             f"(Donchian/Supertrend signals, ATR/vol-filter/circuit-breaker overlays, funding-contrarian z-score).")
    L.append(f"**Method:** Per-leg equity curves aligned to hourly grid; portfolio P&L via **monthly rebalancing "
             f"to optimized weights**. Weights optimized from covariance of per-leg daily returns.")
    L.append(f"**Costs:** {eng.FEE_RATE*100:.2f}% taker + {eng.SLIPPAGE*100:.2f}% slippage/side, "
             f"{eng.FUNDING_RATE*100:.3f}% funding/8h (already embedded in leg returns).")
    L.append(f"**Initial capital:** ${INITIAL_CAPITAL:,.0f}")
    L.append(f"**Constraints:** weights ≥ 0, Σw = 1, no single leg > {MAX_W*100:.0f}%.")
    L.append(f"**Walk-forward:** {int(WF_SPLIT*100)}/{int((1-WF_SPLIT)*100)} train/test; weights frozen from train.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Goal Gate")
    L.append("")
    L.append("**Sharpe > 1.5 AND Annualized > 100% AND MaxDD < 15%**")
    L.append("")
    L.append("---")
    L.append("")

    # ── Sanity check ──
    L.append("## Sanity Check: Equal-Weight Reproduction")
    L.append("")
    L.append("Verifies the optimizer reproduces the known equal-weight result for "
             "LINK3x + DOT-funding-1x (expected ~89% ann, Sharpe ~2.45, MaxDD ~-12%).")
    L.append("")
    s = sanity
    L.append(f"| Scheme | Ann Ret | Sharpe | Max DD | Calmar |")
    L.append(f"|--------|---------|--------|--------|--------|")
    L.append(f"| equal-weight (sanity) | {_fmt_pct(s['ann'])} | {s['sharpe']:.2f} | "
             f"{_fmt_pct(s['max_dd'])} | {s['calmar']:.2f} |")
    L.append("")

    # ── Per-leg candidate summary ──
    L.append("## Candidate Legs (gate-passing set + leveraged funding)")
    L.append("")
    L.append("| Leg | Ann Ret | Sharpe | Max DD | Calmar | PF | Win% | Trades |")
    L.append("|-----|---------|--------|--------|--------|----|------|--------|")
    for cand in CANDIDATES:
        m = leg_data[cand["label"]]["metrics"]
        L.append(f"| {_short_label(cand['label'])} | {_fmt_pct(m['ann_return'])} | "
                 f"{m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | "
                 f"{m['profit_factor']:.2f} | {m['win_rate']*100:.0f}% | "
                 f"{leg_data[cand['label']]['n_trades']} |")
    L.append("")

    # ── Allocation comparison for best combo ──
    L.append("## Allocation Scheme Comparison (best combo)")
    L.append("")
    ad = allocation_detail
    L.append(f"**Combo:** {ad['combo_label']}")
    L.append("")
    L.append("| Scheme | Weights | Ann Ret | Sharpe | Max DD | Calmar |")
    L.append("|--------|---------|---------|--------|--------|--------|")
    for scheme, info in ad["schemes"].items():
        w_str = " / ".join(f"{x*100:.0f}%" for x in info["weights"])
        m = info["metrics"]
        L.append(f"| {scheme} | {w_str} | {_fmt_pct(m['ann_return'])} | "
                 f"{m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} |")
    L.append("")

    # ── Exhaustive leaderboard ──
    L.append("## Exhaustive Combo Leaderboard (top 20)")
    L.append("")
    L.append(f"Every 2/3/4-leg subset of {len(CANDIDATES)} candidates evaluated under "
             f"4 allocation schemes. Best scheme per combo shown, sorted by gate-clear "
             f"status then Sharpe. **B** = clears all three bars.")
    L.append("")
    L.append("| Rank | Combo | Legs | Scheme | Ann Ret | Sharpe | Max DD | Calmar | Gate |")
    L.append("|------|-------|------|--------|---------|--------|--------|--------|------|")
    for i, row in enumerate(leaderboard[:20], 1):
        stars = " ★**B**" if row["gate_pass"] else ""
        L.append(f"| {i} | {_short_label(row['label'])} | {row['n_legs']} | "
                 f"{row['best_scheme']} | {_fmt_pct(row['ann'])} | {row['sharpe']:.2f} | "
                 f"{_fmt_pct(row['max_dd'])} | {row['calmar']:.2f} | "
                 f"{row['gate_score']}/3{stars} |")
    L.append("")

    # ── Gate-passing combos ──
    gate_passers = [r for r in leaderboard if r["gate_pass"]]
    L.append("## Gate-Passing Portfolios")
    L.append("")
    if gate_passers:
        L.append(f"**{len(gate_passers)} portfolio(s) clear all three bars simultaneously.**")
        L.append("")
        L.append("| # | Combo | Scheme | Ann Ret | Sharpe | Max DD | Weights |")
        L.append("|---|-------|--------|---------|--------|--------|---------|")
        for i, row in enumerate(gate_passers, 1):
            w_str = " / ".join(f"{x*100:.0f}%" for x in row["weights"])
            L.append(f"| {i} | {_short_label(row['label'])} | {row['best_scheme']} | "
                     f"{_fmt_pct(row['ann'])} | {row['sharpe']:.2f} | "
                     f"{_fmt_pct(row['max_dd'])} | {w_str} |")
        L.append("")
        for row in gate_passers:
            L.append(f"### {_short_label(row['label'])} — {row['best_scheme']}")
            L.append("")
            L.append(f"- **Ann return:** {_fmt_pct(row['ann'])}")
            L.append(f"- **Sharpe:** {row['sharpe']:.2f}")
            L.append(f"- **Max DD:** {_fmt_pct(row['max_dd'])}")
            L.append(f"- **Calmar:** {row['calmar']:.2f}")
            L.append(f"- **Weights:** " + ", ".join(
                f"{lab}={w*100:.0f}%" for lab, w in zip(row["labels"], row["weights"])))
            L.append("")
    else:
        L.append("**No portfolio clears all three bars simultaneously.**")
        L.append("")
        # closest combos
        L.append("Closest portfolios (highest ann return among Sharpe>1.5 & MaxDD<15%):")
        L.append("")
        closest = [r for r in leaderboard
                   if r["sharpe"] > 1.5 and r["max_dd"] > -0.15]
        closest.sort(key=lambda r: -r["ann"])
        L.append("| # | Combo | Scheme | Ann Ret | Sharpe | Max DD | Gap to 100% |")
        L.append("|---|-------|--------|---------|--------|--------|-------------|")
        for i, row in enumerate(closest[:5], 1):
            gap = 1.0 - row["ann"]
            L.append(f"| {i} | {_short_label(row['label'])} | {row['best_scheme']} | "
                     f"{_fmt_pct(row['ann'])} | {row['sharpe']:.2f} | "
                     f"{_fmt_pct(row['max_dd'])} | +{gap*100:.1f}pp |")
        L.append("")
        if closest:
            best = closest[0]
            L.append(f"To clear the bar, the top candidate ({_short_label(best['label'])}, "
                     f"{_fmt_pct(best['ann'])}) needs **+{(1.0 - best['ann'])*100:.1f} percentage points** "
                     f"more annualized return while holding Sharpe>1.5 and MaxDD<15%.")
        L.append("")

    # ── Walk-forward ──
    L.append(f"## Walk-Forward Validation (top 3, {int(WF_SPLIT*100)}/{int((1-WF_SPLIT)*100)} split)")
    L.append("")
    L.append("Weights optimized on the first 60% (train), frozen and applied to the "
             "last 40% (test — a severe bear market where LINK buy&hold was -38.8%). "
             "True winner requires OOS Sharpe > 1.0 AND OOS ann > 50%.")
    L.append("")
    L.append("| # | Combo | Scheme | Train Ann | Train Shp | Train DD | "
             "Test Ann | Test Shp | Test DD | Test B&H | OOS Pass? |")
    L.append("|---|-------|--------|-----------|-----------|----------|"
             "----------|----------|---------|----------|-----------|")
    for i, wf in enumerate(top3_wf, 1):
        tr, te = wf["train"], wf["test"]
        oos_pass = te["sharpe"] > 1.0 and te["ann_return"] > 0.50
        L.append(f"| {i} | {_short_label(wf['label'])} | {wf['scheme']} | "
                 f"{_fmt_pct(tr['ann_return'])} | {tr['sharpe']:.2f} | {_fmt_pct(tr['max_dd'])} | "
                 f"{_fmt_pct(te['ann_return'])} | {te['sharpe']:.2f} | {_fmt_pct(te['max_dd'])} | "
                 f"{_fmt_pct(wf['test_bh_link'])} | {'✅ YES' if oos_pass else '❌ no'} |")
    L.append("")

    # ── Verdict ──
    L.append("## VERDICT")
    L.append("")
    any_gate = bool(gate_passers)
    any_oos = any(wf["test"]["sharpe"] > 1.0 and wf["test"]["ann_return"] > 0.50
                  for wf in top3_wf)
    if any_gate:
        gp = gate_passers[0]
        L.append(f"**PASS.** The portfolio **{_short_label(gp['label'])}** "
                 f"({gp['best_scheme']} allocation) clears the aggressive alpha bar: "
                 f"**{_fmt_pct(gp['ann'])} ann / Sharpe {gp['sharpe']:.2f} / "
                 f"MaxDD {_fmt_pct(gp['max_dd'])}**.")
        if any_oos:
            L.append("")
            L.append("Walk-forward confirms robustness: OOS Sharpe > 1.0 and OOS ann > 50%.")
        else:
            L.append("")
            L.append("⚠️ However, walk-forward OOS metrics do not both clear "
                     "(Sharpe > 1.0 AND ann > 50%) — see table above.")
    else:
        best = leaderboard[0]
        L.append(f"**NO PORTFOLIO CLEARS ALL THREE BARS.** The closest is "
                 f"**{_short_label(best['label'])}** ({best['best_scheme']} allocation): "
                 f"**{_fmt_pct(best['ann'])} ann / Sharpe {best['sharpe']:.2f} / "
                 f"MaxDD {_fmt_pct(best['max_dd'])}** (gate {best['gate_score']}/3).")
        if best["sharpe"] > 1.5 and best["max_dd"] > -0.15:
            L.append("")
            L.append(f"To clear the return bar, need **+{(1.0 - best['ann'])*100:.1f}pp** more "
                     f"annualized return while keeping Sharpe > 1.5 and MaxDD < 15%. "
                     f"This likely requires either higher leverage on the funding leg "
                     f"(tested up to 3x here) or a new high-Sharpe uncorrelated leg.")
        elif best["ann"] > 1.0 and best["sharpe"] > 1.5:
            L.append("")
            L.append(f"Bottleneck is drawdown (currently {_fmt_pct(best['max_dd'])}, need < -15%).")
        L.append("")
    L.append("")

    md = "\n".join(L)

    # ── JSON payload ──
    payload = {
        "generated": gen,
        "data_meta": data_meta,
        "costs": {
            "fee_side": eng.FEE_RATE, "slippage_side": eng.SLIPPAGE,
            "funding_8h": eng.FUNDING_RATE,
        },
        "initial_capital": INITIAL_CAPITAL,
        "max_weight": MAX_W,
        "walk_forward_split": WF_SPLIT,
        "goal_gate": {"sharpe_gt": 1.5, "ann_gt": 1.0, "maxdd_gt": -0.15},
        "oos_gate": {"sharpe_gt": 1.0, "ann_gt": 0.50},
        "candidate_legs": {
            cand["label"]: {
                "ann_return": leg_data[cand["label"]]["metrics"]["ann_return"],
                "sharpe": leg_data[cand["label"]]["metrics"]["sharpe"],
                "max_dd": leg_data[cand["label"]]["metrics"]["max_dd"],
                "calmar": leg_data[cand["label"]]["metrics"]["calmar"],
                "profit_factor": leg_data[cand["label"]]["metrics"]["profit_factor"],
                "win_rate": leg_data[cand["label"]]["metrics"]["win_rate"],
                "n_trades": leg_data[cand["label"]]["n_trades"],
            }
            for cand in CANDIDATES
        },
        "sanity_check": sanity,
        "allocation_comparison": {
            "combo_label": allocation_detail["combo_label"],
            "schemes": {
                s: {"weights": info["weights"].tolist(),
                    "ann": info["metrics"]["ann_return"],
                    "sharpe": info["metrics"]["sharpe"],
                    "max_dd": info["metrics"]["max_dd"],
                    "calmar": info["metrics"]["calmar"]}
                for s, info in allocation_detail["schemes"].items()
            },
        },
        "leaderboard": [
            {
                "rank": i + 1, "label": row["label"], "labels": row["labels"],
                "n_legs": row["n_legs"], "best_scheme": row["best_scheme"],
                "weights": row["weights"].tolist(),
                "ann": row["ann"], "sharpe": row["sharpe"],
                "max_dd": row["max_dd"], "calmar": row["calmar"],
                "gate_score": row["gate_score"], "gate_pass": row["gate_pass"],
            }
            for i, row in enumerate(leaderboard[:50])
        ],
        "gate_passers": [
            {
                "label": row["label"], "labels": row["labels"],
                "best_scheme": row["best_scheme"],
                "weights": row["weights"].tolist(),
                "ann": row["ann"], "sharpe": row["sharpe"],
                "max_dd": row["max_dd"], "calmar": row["calmar"],
            }
            for row in gate_passers
        ],
        "walk_forward": [
            {
                "label": wf["label"], "labels": wf["labels"],
                "scheme": wf["scheme"], "weights": wf["weights"].tolist(),
                "train": {
                    "ann": wf["train"]["ann_return"], "sharpe": wf["train"]["sharpe"],
                    "max_dd": wf["train"]["max_dd"],
                },
                "test": {
                    "ann": wf["test"]["ann_return"], "sharpe": wf["test"]["sharpe"],
                    "max_dd": wf["test"]["max_dd"],
                },
                "test_bh_link": wf["test_bh_link"],
                "oos_pass": bool(wf["test"]["sharpe"] > 1.0
                                 and wf["test"]["ann_return"] > 0.50),
            }
            for wf in top3_wf
        ],
        "verdict": {
            "any_gate_pass": any_gate,
            "any_oos_pass": any_oos,
        },
    }
    return md, payload


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 72)
    print(" PORTFOLIO OPTIMIZER — CAPITAL ALLOCATION & COMBO SEARCH")
    print("=" * 72)

    print("\n[1/6] Loading trend data...")
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
    data_meta = {
        "n_bars": int(len(ref_pack.close)),
        "start": str(pd.to_datetime(int(ref_pack.ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_pack.ts[-1]), unit="ms", utc=True)),
    }
    print(f"  Trend: {data_meta['n_bars']} bars, {data_meta['start']} -> {data_meta['end']}")

    print("\n[2/6] Loading funding data (DOTUSDT 8h)...")
    funding_data: dict[str, pd.DataFrame] = {}
    try:
        fdf = pp.load_funding_dataset("DOTUSDT")
        funding_data["DOTUSDT"] = fdf
        data_meta["funding_periods"] = int(len(fdf))
        print(f"  Funding: {len(fdf)} 8h bars")
    except FileNotFoundError as e:
        print(f"  [WARN] {e}")
        data_meta["funding_periods"] = 0

    print("\n[3/6] Precomputing all candidate legs...")
    leg_data = precompute_legs(packs, targets, funding_data, ref_ts)
    for cand in CANDIDATES:
        m = leg_data[cand["label"]]["metrics"]
        print(f"  {_short_label(cand['label']):<30s}  "
              f"ann {_fmt_pct(m['ann_return']):>8s}  "
              f"shp {m['sharpe']:.2f}  dd {_fmt_pct(m['max_dd']):>8s}  "
              f"trd {leg_data[cand['label']]['n_trades']}")

    # ── Sanity check: reproduce known equal-weight result ──
    print("\n     Sanity: LINK3x + DOT-fund-1x equal-weight...")
    sanity_labels = ["LINKUSDC_donchian_atr2_vf_cb_3x",
                     "DOTUSDT_funding_contrarian_1x"]
    sanity_eq = weighted_monthly_rebal(
        [leg_data[l]["eq_aligned"] for l in sanity_labels],
        np.array([0.5, 0.5]), ref_ts)
    sm = pp.portfolio_metrics(sanity_eq, ref_ts)
    print(f"     → ann {_fmt_pct(sm['ann_return'])}, shp {sm['sharpe']:.2f}, "
          f"dd {_fmt_pct(sm['max_dd'])}  (expected ~89%/2.45/-12%)")
    sanity = {"ann": sm["ann_return"], "sharpe": sm["sharpe"],
              "max_dd": sm["max_dd"], "calmar": sm["calmar"]}

    print("\n[4/6] Exhaustive combo search (2/3/4 legs × 4 schemes)...")
    leaderboard: list[dict] = []
    total_combos = 0
    for k in [2, 3, 4]:
        for combo_indices in itertools.combinations(range(len(CANDIDATES)), k):
            total_combos += 1
            cands = [CANDIDATES[i] for i in combo_indices]
            labels = [c["label"] for c in cands]
            eval_res = eval_combo(labels, leg_data, ref_ts)
            best_scheme, best_res = best_scheme_for_combo(eval_res)
            m = best_res["metrics"]
            gp = clears_gate(m)
            leaderboard.append({
                "label": " + ".join(labels),
                "labels": labels,
                "n_legs": k,
                "best_scheme": best_scheme,
                "weights": best_res["weights"],
                "ann": m["ann_return"],
                "sharpe": m["sharpe"],
                "max_dd": m["max_dd"],
                "calmar": m["calmar"],
                "sortino": m["sortino"],
                "gate_score": gate_score(m),
                "gate_pass": gp,
            })
    # sort: gate-passers first, then by Sharpe desc, then ann desc
    leaderboard.sort(key=lambda r: (-int(r["gate_pass"]), -r["sharpe"], -r["ann"]))
    print(f"  Evaluated {total_combos} combos.")
    n_gate = sum(1 for r in leaderboard if r["gate_pass"])
    print(f"  Gate-passing: {n_gate}")
    if n_gate:
        for r in leaderboard:
            if r["gate_pass"]:
                print(f"    ★ {_short_label(r['label'])} [{r['best_scheme']}]  "
                      f"ann {_fmt_pct(r['ann'])}, shp {r['sharpe']:.2f}, "
                      f"dd {_fmt_pct(r['max_dd'])}")
    else:
        print("  Top 5 closest:")
        for r in leaderboard[:5]:
            print(f"    {_short_label(r['label'])} [{r['best_scheme']}]  "
                  f"ann {_fmt_pct(r['ann'])}, shp {r['sharpe']:.2f}, "
                  f"dd {_fmt_pct(r['max_dd'])}, gate {r['gate_score']}/3")

    # ── Allocation detail for best combo ──
    best_row = leaderboard[0]
    allocation_detail = {
        "combo_label": best_row["label"],
        "schemes": eval_combo(best_row["labels"], leg_data, ref_ts),
    }

    print("\n[5/6] Walk-forward (top 3)...")
    top3_wf: list[dict] = []
    for row in leaderboard[:3]:
        cands = [next(c for c in CANDIDATES if c["label"] == lab)
                 for lab in row["labels"]]
        wf = walk_forward_combo(cands, packs, targets, funding_data,
                                row["best_scheme"])
        wf["label"] = row["label"]
        wf["labels"] = row["labels"]
        tr, te = wf["train"], wf["test"]
        oos = te["sharpe"] > 1.0 and te["ann_return"] > 0.50
        print(f"  {_short_label(row['label'])} [{row['best_scheme']}]: "
              f"train shp {tr['sharpe']:.2f}/ann {_fmt_pct(tr['ann_return'])}, "
              f"test shp {te['sharpe']:.2f}/ann {_fmt_pct(te['ann_return'])} "
              f"{'✅OOS' if oos else '❌OOS'}")
        top3_wf.append(wf)

    print("\n[6/6] Generating report...")
    md, payload = generate_report(leg_data, leaderboard, top3_wf,
                                  allocation_detail, data_meta, sanity)
    md_path = DOCS_DIR / "portfolio-optimizer-analysis.md"
    json_path = DOCS_DIR / "portfolio-optimizer-data.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=_json_default),
                         encoding="utf-8")
    print(f"\n  Wrote {md_path.relative_to(REPO_ROOT)}")
    print(f"  Wrote {json_path.relative_to(REPO_ROOT)}")

    # ── Summary ──
    print("\n" + "=" * 72)
    print(" SUMMARY")
    print("=" * 72)
    print(f"{'Rank':<5} {'Ann':>8} {'Sharpe':>7} {'MaxDD':>8} {'Gate':>5}  Combo")
    for i, row in enumerate(leaderboard[:10], 1):
        flag = "★" if row["gate_pass"] else " "
        print(f"{i:<5} {_fmt_pct(row['ann']):>8} {row['sharpe']:>7.2f} "
              f"{_fmt_pct(row['max_dd']):>8} {row['gate_score']}/3{flag}  "
              f"{_short_label(row['label'])} [{row['best_scheme']}]")
    print(f"\nGate-passing portfolios: {n_gate}")
    print("DONE.")


if __name__ == "__main__":
    main()
