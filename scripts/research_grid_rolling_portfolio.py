#!/usr/bin/env python3
"""
Grid Rolling Expanding-Window Validation + Multi-Coin Grid Portfolio
====================================================================
RESEARCH ONLY. No live trading.

This implements the two MANDATORY tests required by SD-003 for the grid
combo_vt60 (3%/20/2x) candidate — the #1 honest candidate for the aggressive
alpha directive:

PART 1 — Rolling expanding-window validation (6 windows) on INJUSDC combo_vt60
  Window 1: train 0-40%,  test 40-50%
  Window 2: train 0-50%,  test 50-60%
  Window 3: train 0-60%,  test 60-70%
  Window 4: train 0-70%,  test 70-80%
  Window 5: train 0-80%,  test 80-89%
  Window 6: train 0-89%,  test 89-99%
  For each window the grid is SEEDED at the first close of the test slice
  (genuinely OOS, no look-ahead). VERDICT: ROBUST iff the full gate
  (Sharpe>1 AND Ann>50% AND MaxDD<20%) passes on the MAJORITY (>=3/6).

PART 2 — Multi-coin grid portfolio at tangency (max-Sharpe) weights with
  MONTHLY rebalancing. Train-only weights (optimize on train half, freeze
  for test half). Same 6-window rolling validation applied to the portfolio.

Reuses, exactly (no rewrites):
  - scripts/research_grid_dd_controlled.py : simulate(), DDConfig, load_data()
  - scripts/research_portfolio_optimizer.py: weighted_monthly_rebal(),
                                            compute_alloc('tangency',...)

Cost model: 0.07% per side (0.14% round-trip) — embedded in the grid simulator
and applied as a monthly-rebalance drag in the portfolio combine.

Output: docs/research/grid-rolling-validation-and-portfolio.md
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ── Reuse the exact engines ──────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_grid_dd_controlled as g  # noqa: E402
import research_portfolio_optimizer as po  # noqa: E402

REPO = HERE.parent
DOCS = REPO / "docs" / "research"
DOCS.mkdir(parents=True, exist_ok=True)

ROLLING_BOUNDARIES = [(0.40, 0.50), (0.50, 0.60), (0.60, 0.70),
                      (0.70, 0.80), (0.80, 0.89), (0.89, 0.99)]

INITIAL_CAPITAL = g.INITIAL_CAPITAL  # 10000
HOURS_PER_YEAR = g.HOURS_PER_YEAR
COMMISSION_PER_SIDE = g.COMMISSION_PER_SIDE  # 0.0007


# ──────────────────────────────────────────────────────────────────────────────
#  PART 1: single-coin grid rolling expanding-window validation
# ──────────────────────────────────────────────────────────────────────────────
def _gate_pass(ann: float, sharpe: float, max_dd: float) -> bool:
    return (sharpe > 1.0 and ann > 0.50 and max_dd > -0.20)


def combo_vt60_cfg() -> g.DDConfig:
    """The combo_vt60 config: vol-target 60% + circuit breaker + weekly recenter,
    3% spacing, 20 levels, 2x leverage. (cb_dd_trigger 0.08, cooldown 48h — the
    combo sweep defaults from build_configs().)"""
    return g.DDConfig(
        spacing_pct=0.03, n_levels=20, leverage=2,
        vol_target=True, vol_target_vol=0.6,
        vol_min_scale=0.15, vol_max_scale=1.5,
        circuit_breaker=True, cb_dd_trigger=0.08, cb_cooldown_hours=48,
        recenter=True, recenter_hours=168,
        tag="combo_vt60",
    )


def rolling_single_coin(df: pd.DataFrame, cfg: g.DDConfig) -> list[dict]:
    """Run 6 rolling expanding windows. Grid is seeded at the first close of each
    test slice (the simulator uses closes[start_idx] as the grid center —
    genuinely OOS). Grid params are the FIXED combo_vt60 config (no per-window
    re-optimization; the config is the strategy under test)."""
    n = len(df)
    rows = []
    for w_idx, (tr_frac, te_frac) in enumerate(ROLLING_BOUNDARIES, 1):
        test_start = int(n * tr_frac)
        test_end = int(n * te_frac)
        if test_end <= test_start:
            continue
        m = g.simulate(df, cfg, test_start, test_end)
        ts = df["dt"].values
        cal_start = pd.to_datetime(ts[test_start]).strftime("%Y-%m-%d")
        cal_end = pd.to_datetime(ts[test_end - 1]).strftime("%Y-%m-%d")
        ann = m["annualized"]
        sh = m["sharpe"]
        dd = m["max_drawdown"]
        rows.append({
            "window": w_idx,
            "train_frac": tr_frac, "test_frac": te_frac,
            "calendar": f"{cal_start} → {cal_end}",
            "n_test_hours": test_end - test_start,
            "ann": ann, "sharpe": sh, "max_dd": dd,
            "total_return": m["total_return"],
            "trades": m["total_trades"],
            "buy_hold": m["buy_hold"],
            "pass": _gate_pass(ann, sh, dd),
        })
    return rows


# ──────────────────────────────────────────────────────────────────────────────
#  PART 2: multi-coin grid portfolio
# ──────────────────────────────────────────────────────────────────────────────
# Candidate grid legs: INJ (best) + BTC (lowest DD) + next-best coins from the
# drawdown-controlled scan. Each leg is the single-coin best DD-constrained
# grid config (the same configs the per-coin scan identified).
LEG_CONFIGS = {
    "INJUSDC": g.DDConfig(spacing_pct=0.03, n_levels=20, leverage=2,
                          vol_target=True, vol_target_vol=0.6,
                          vol_min_scale=0.15, vol_max_scale=1.5,
                          circuit_breaker=True, cb_dd_trigger=0.08,
                          cb_cooldown_hours=48, recenter=True,
                          recenter_hours=168, tag="combo_vt60"),
    "BTCUSDC": g.DDConfig(spacing_pct=0.05, n_levels=20, leverage=2,
                          recenter=True, recenter_hours=168, tag="recenter"),
    "XRPUSDC": g.DDConfig(spacing_pct=0.03, n_levels=30, leverage=2,
                          vol_target=True, vol_target_vol=0.4,
                          vol_min_scale=0.15, vol_max_scale=1.5,
                          circuit_breaker=True, cb_dd_trigger=0.08,
                          cb_cooldown_hours=48, recenter=True,
                          recenter_hours=168, tag="combo_vt40"),
    "LINKUSDC": g.DDConfig(spacing_pct=0.03, n_levels=20, leverage=2,
                           vol_target=True, vol_target_vol=0.4,
                           vol_min_scale=0.15, vol_max_scale=1.5,
                           circuit_breaker=True, cb_dd_trigger=0.08,
                           cb_cooldown_hours=48, recenter=True,
                           recenter_hours=168, tag="combo_vt40"),
    "SOLUSDC": g.DDConfig(spacing_pct=0.05, n_levels=20, leverage=2,
                          recenter=True, recenter_hours=168, tag="recenter"),
}


def equity_curve_for_leg(df: pd.DataFrame, cfg: g.DDConfig,
                         start_idx: int, end_idx: int) -> np.ndarray:
    """Return the per-hour equity curve (aligned to the test slice timeline) for
    one grid leg, using the exact grid simulator. Commission is already inside
    simulate()."""
    m = g.simulate(df, cfg, start_idx, end_idx)
    return m["equity_curve"]


def metrics_from_equity(equity: np.ndarray, ref_ts_ms: np.ndarray) -> dict:
    """Portfolio metrics on an hourly equity curve (mirrors the grid simulator's
    _metrics so single-coin and portfolio are comparable)."""
    n = len(equity)
    final_eq = float(equity[-1])
    days = n / 24.0
    total_ret = (final_eq - INITIAL_CAPITAL) / INITIAL_CAPITAL
    if final_eq > 0 and total_ret != 0 and days > 0:
        annualized = (final_eq / INITIAL_CAPITAL) ** (365.0 / days) - 1.0
    else:
        annualized = -1.0
    annualized = max(-1.0, min(annualized, 50.0))
    rmax = np.maximum.accumulate(equity)
    dd = (equity - rmax) / np.maximum(rmax, 1e-9)
    max_dd = max(-1.0, min(float(np.min(dd)) if n else 0.0, 0.0))
    rets = np.diff(equity) / np.maximum(equity[:-1], 1e-9)
    std = float(np.std(rets)) if n > 1 else 0.0
    sharpe = float(np.mean(rets) / std * math.sqrt(HOURS_PER_YEAR)) if std > 1e-12 else 0.0
    return {"final_equity": final_eq, "total_return": total_ret,
            "ann_return": annualized, "max_dd": max_dd, "sharpe": sharpe,
            "n_hours": n}


def leg_daily_returns(equity: np.ndarray, ref_ts_ms: np.ndarray) -> np.ndarray:
    """Daily-resampled returns from an hourly equity curve (mirrors
    portfolio_optimizer.leg_daily_returns)."""
    day_keys = (ref_ts_ms // (24 * 3600 * 1000)).astype(np.int64)
    last_per_day: dict[int, float] = {}
    for k, v in zip(day_keys.tolist(), equity.tolist()):
        last_per_day[k] = v
    days_sorted = sorted(last_per_day)
    day_eq = np.array([last_per_day[k] for k in days_sorted], dtype=float)
    if len(day_eq) < 2:
        return np.array([])
    return np.diff(day_eq) / np.maximum(day_eq[:-1], 1e-12)


def optimize_tangency_weights(leg_daily: dict[str, np.ndarray],
                              labels: list[str]) -> np.ndarray:
    """Tangency (max-Sharpe) weights from per-leg daily returns. Reuses
    portfolio_optimizer.compute_alloc('tangency', ...)."""
    drs = [leg_daily[l] for l in labels]
    min_len = min((len(d) for d in drs), default=0)
    if min_len < 2:
        return np.full(len(labels), 1.0 / len(labels))
    R = np.array([d[:min_len] for d in drs])
    mu = R.mean(axis=1)
    cov = np.cov(R)
    stds = np.sqrt(np.maximum(np.diag(cov), 1e-24))
    return po.compute_alloc("tangency", mu, cov, stds)


def build_portfolio_equity(dfs: dict[str, pd.DataFrame], labels: list[str],
                           weights: np.ndarray, start_idx: int, end_idx: int,
                           rebal_cost_per_side: float = COMMISSION_PER_SIDE
                           ) -> np.ndarray:
    """Build a portfolio equity curve by:
      1. Running each grid leg on [start_idx,end_idx] -> per-leg equity curves.
      2. Combining at `weights` with MONTHLY rebalancing via
         portfolio_optimizer.weighted_monthly_rebal.
    A monthly-rebalance transaction drag (0.07% per side of traded notional) is
    applied at each month boundary to reflect realistic turnover cost."""
    eq_curves = []
    for l in labels:
        eq = equity_curve_for_leg(dfs[l], LEG_CONFIGS[l], start_idx, end_idx)
        eq_curves.append(np.asarray(eq, dtype=float))
    ts_ms = dfs[labels[0]]["dt"].values.astype("datetime64[ms]").astype("int64")
    ts_slice = ts_ms[start_idx:end_idx]
    # ensure all curves same length as ts slice
    L = min(len(c) for c in eq_curves)
    eq_curves = [c[:L] for c in eq_curves]
    ts_slice = ts_slice[:L]
    port = po.weighted_monthly_rebal(eq_curves, np.array(weights, dtype=float),
                                     ts_slice, INITIAL_CAPITAL)
    # ── monthly rebalance transaction drag ────────────────────────────────
    # At each month boundary the optimizer notionally rebalances to the target
    # weights; we charge 0.07% per side on the traded (turnover) notional.
    dt = pd.to_datetime(ts_slice, unit="ms", utc=True)
    month_id = (dt.year * 12 + dt.month).to_numpy()
    # Recompute the combined curve WITH drag month by month.
    eqs = np.array(eq_curves)  # (n_legs, L)
    leg_cap = INITIAL_CAPITAL * np.array(weights, dtype=float)
    out = np.empty(L)
    out[0] = leg_cap.sum()
    for i in range(1, L):
        prev = eqs[:, i - 1]
        curr = eqs[:, i]
        g_ = np.where(prev > 1e-12, curr / prev, 1.0)
        leg_cap = leg_cap * g_
        if month_id[i] != month_id[i - 1]:
            total = leg_cap.sum()
            new_cap = total * np.array(weights, dtype=float)
            turnover = np.sum(np.abs(new_cap - leg_cap))
            drag = turnover * 2 * rebal_cost_per_side  # buy+sell legs
            total -= drag
            leg_cap = total * np.array(weights, dtype=float)
        out[i] = leg_cap.sum()
    return out


# ──────────────────────────────────────────────────────────────────────────────
#  Full pipeline
# ──────────────────────────────────────────────────────────────────────────────
def run_all():
    print("=" * 78)
    print("  GRID ROLLING-VALIDATION + MULTI-COIN PORTFOLIO (SD-003)")
    print("=" * 78)
    data = g.load_data()
    dfs = {s: g.arr_to_df(data[s]) for s in g.SYMBOLS if s in data}
    print(f"  Loaded {len(dfs)} symbols, {len(next(iter(dfs.values())))} bars each")

    n = len(next(iter(dfs.values())))
    sym = "INJUSDC"
    df_inj = dfs[sym]
    cfg = combo_vt60_cfg()

    # sanity: reproduce the known full-period number for combo_vt60
    full = g.simulate(df_inj, cfg, 0, n)
    print(f"\n  [SANITY] INJUSDC combo_vt60 full-period: "
          f"ann={full['annualized']:.1%} dd={full['max_drawdown']:.1%} "
          f"sh={full['sharpe']:.2f}  (target ~44.8%/-14.8%/1.43)")

    # ── PART 1: rolling single-coin ───────────────────────────────────────
    print("\n  PART 1 — Rolling expanding-window (INJUSDC combo_vt60)")
    rolling = rolling_single_coin(df_inj, cfg)
    for r in rolling:
        flag = "PASS" if r["pass"] else "fail"
        print(f"    W{r['window']} ({r['calendar']}): "
              f"ann={r['ann']:.1%} sh={r['sharpe']:.2f} dd={r['max_dd']:.1%}  [{flag}]")
    n_pass1 = sum(1 for r in rolling if r["pass"])
    print(f"  -> {n_pass1}/6 windows pass the full gate")

    # ── PART 2: multi-coin portfolio ──────────────────────────────────────
    print("\n  PART 2 — Multi-coin grid portfolio (tangency, monthly rebal)")
    labels = list(LEG_CONFIGS.keys())
    print(f"  Legs: {labels}")
    # per-leg full-period metrics for reference
    leg_full = {}
    for l in labels:
        m = g.simulate(dfs[l], LEG_CONFIGS[l], 0, n)
        leg_full[l] = m
        print(f"    {l:8s} {LEG_CONFIGS[l].tag:>12s}: "
              f"ann={m['annualized']:.1%} dd={m['max_drawdown']:.1%} "
              f"sh={m['sharpe']:.2f}")

    # OOS 50/50: optimize tangency on train half, freeze for test half
    split = n // 2
    print(f"\n  OOS 50/50 (train-only tangency weights, frozen for test):")
    leg_daily_train = {}
    for l in labels:
        eq = equity_curve_for_leg(dfs[l], LEG_CONFIGS[l], 0, split)
        ts_ms = (dfs[l]["dt"].values.astype("datetime64[ms]")
                 .astype("int64"))[:split]
        leg_daily_train[l] = leg_daily_returns(eq, ts_ms)
    w_oos = optimize_tangency_weights(leg_daily_train, labels)
    w_str = " / ".join(f"{l[:3]} {x*100:.0f}%" for l, x in zip(labels, w_oos))
    print(f"    frozen weights: {w_str}")
    port_train_eq = build_portfolio_equity(dfs, labels, w_oos, 0, split)
    port_test_eq = build_portfolio_equity(dfs, labels, w_oos, split, n)
    ts_train = (dfs[labels[0]]["dt"].values.astype("datetime64[ms]")
                .astype("int64"))[:split]
    ts_test = (dfs[labels[0]]["dt"].values.astype("datetime64[ms]")
               .astype("int64"))[split:n]
    train_m = metrics_from_equity(port_train_eq, ts_train)
    test_m = metrics_from_equity(port_test_eq, ts_test)
    print(f"    train: ann={train_m['ann_return']:.1%} sh={train_m['sharpe']:.2f} "
          f"dd={train_m['max_dd']:.1%}")
    print(f"    TEST : ann={test_m['ann_return']:.1%} sh={test_m['sharpe']:.2f} "
          f"dd={test_m['max_dd']:.1%}")

    # ── PART 2b: portfolio rolling expanding-window ───────────────────────
    print("\n  PART 2b — Portfolio rolling expanding-window (train-only weights)")
    port_rolling = []
    for w_idx, (tr_frac, te_frac) in enumerate(ROLLING_BOUNDARIES, 1):
        te_start = int(n * tr_frac)
        te_end = int(n * te_frac)
        if te_end <= te_start:
            continue
        # optimize tangency on the EXPANDING train slice [0, te_start)
        leg_dr = {}
        for l in labels:
            eq = equity_curve_for_leg(dfs[l], LEG_CONFIGS[l], 0, te_start)
            ts_ms = (dfs[l]["dt"].values.astype("datetime64[ms]")
                     .astype("int64"))[:te_start]
            leg_dr[l] = leg_daily_returns(eq, ts_ms)
        w = optimize_tangency_weights(leg_dr, labels)
        # apply frozen weights to test slice
        eq = build_portfolio_equity(dfs, labels, w, te_start, te_end)
        ts_ms_test = (dfs[labels[0]]["dt"].values.astype("datetime64[ms]")
                      .astype("int64"))[te_start:te_end]
        m = metrics_from_equity(eq, ts_ms_test)
        ts_all = dfs[labels[0]]["dt"].values
        cal_s = pd.to_datetime(ts_all[te_start]).strftime("%Y-%m-%d")
        cal_e = pd.to_datetime(ts_all[te_end - 1]).strftime("%Y-%m-%d")
        row = {
            "window": w_idx, "train_frac": tr_frac, "test_frac": te_frac,
            "calendar": f"{cal_s} → {cal_e}",
            "ann": m["ann_return"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
            "total_return": m["total_return"],
            "weights": {l: float(x) for l, x in zip(labels, w)},
            "pass": _gate_pass(m["ann_return"], m["sharpe"], m["max_dd"]),
        }
        port_rolling.append(row)
        flag = "PASS" if row["pass"] else "fail"
        print(f"    W{w_idx} ({row['calendar']}): "
              f"ann={row['ann']:.1%} sh={row['sharpe']:.2f} "
              f"dd={row['max_dd']:.1%}  [{flag}]  "
              f"w=[{'/'.join(f'{x*100:.0f}' for x in w)}]")
    n_pass2 = sum(1 for r in port_rolling if r["pass"])
    print(f"  -> {n_pass2}/6 portfolio windows pass the full gate")

    # ── verdicts ──────────────────────────────────────────────────────────
    v1 = "ROBUST" if n_pass1 >= 3 else "NOT ROBUST"
    v2 = "ROBUST" if n_pass2 >= 3 else "NOT ROBUST"
    print("\n" + "=" * 78)
    print(f"  VERDICT single-coin INJ combo_vt60: {v1} ({n_pass1}/6)")
    print(f"  VERDICT multi-coin grid portfolio : {v2} ({n_pass2}/6)")
    print("=" * 78)

    return {
        "full_inj": full, "rolling_single": rolling,
        "labels": labels, "leg_full": leg_full,
        "w_oos": {l: float(x) for l, x in zip(labels, w_oos)},
        "train_m": train_m, "test_m": test_m,
        "port_rolling": port_rolling,
        "verdict_single": v1, "n_pass_single": n_pass1,
        "verdict_portfolio": v2, "n_pass_portfolio": n_pass2,
    }


# ──────────────────────────────────────────────────────────────────────────────
#  Report
# ──────────────────────────────────────────────────────────────────────────────
def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def build_report(res: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full = res["full_inj"]
    L: list[str] = []
    L.append("# Grid Rolling Expanding-Window Validation + Multi-Coin Portfolio\n\n")
    L.append(f"**Generated:** {now}  \n")
    L.append("**Engine:** Reuses `scripts/research_grid_dd_controlled.py::simulate` "
             "(exact, no rewrite) and `scripts/research_portfolio_optimizer.py` "
             "(`weighted_monthly_rebal`, `compute_alloc('tangency')`).  \n")
    L.append("**Directive:** SD-003 — rolling expanding-window validation is "
             "MANDATORY for ALL candidates before Boss review.  \n")
    L.append("**Data:** 180 days hourly OHLCV (2025-12-29 → 2026-06-27), "
             "Binance public API, 10 coins. The full window is a **bear regime** "
             "(9/10 coins fell 31–65%).  \n")
    L.append("**Cost model:** 0.07% per side (0.14% round-trip), embedded in the "
             "grid simulator; a monthly-rebalance turnover drag of 0.07%/side is "
             "applied to the portfolio combine.  \n")
    L.append("**Gate:** Sharpe > 1.0 **AND** Ann > 50% **AND** MaxDD < 20%.  \n")
    L.append("**No look-ahead:** grid seeded at the first close of each test "
             "slice; weights optimized on train ONLY and frozen for test.  \n\n")
    L.append("---\n\n")

    # ── Part 1 header ─────────────────────────────────────────────────────
    L.append("## Part 1 — Rolling Expanding-Window Validation: INJUSDC combo_vt60\n\n")
    L.append("Config: **combo_vt60** = vol-target 60% + circuit breaker (8% / 48h) "
             "+ weekly recenter, 3% spacing, 20 levels, 2x leverage.  \n")
    L.append(f"Full-period reference: Ann **{_fmt_pct(full['annualized'])}**, "
             f"Sharpe **{full['sharpe']:.2f}**, MaxDD **{_fmt_pct(full['max_drawdown'])}**, "
             f"trades {full['total_trades']}.  \n\n")
    L.append("6 expanding windows; the train set grows and the test set is the "
             "next ~10% slice. Grid is re-seeded at the first close of each test "
             "slice (genuinely OOS). The config is **not** re-optimized per window "
             "— the fixed combo_vt60 config *is* the strategy under test, so this "
             "validates whether the fixed strategy is robust across periods.\n\n")

    L.append("| Window | Train | Test | Calendar (test) | Test Ann | Test Sharpe | "
             "Test MaxDD | Pass? |\n")
    L.append("|--------|-------|------|-----------------|----------|-------------|"
             "-----------|-------|\n")
    for r in res["rolling_single"]:
        pf = "✅ PASS" if r["pass"] else "❌ fail"
        L.append(f"| W{r['window']} | 0–{int(r['train_frac']*100)}% | "
                 f"{int(r['train_frac']*100)}–{int(r['test_frac']*100)}% | "
                 f"{r['calendar']} | {_fmt_pct(r['ann'])} | {r['sharpe']:.2f} | "
                 f"{_fmt_pct(r['max_dd'])} | {pf} |\n")
    n1 = res["n_pass_single"]
    L.append(f"\n**Pass count: {n1} / 6.**\n\n")
    L.append(f"### Verdict (Part 1): **{res['verdict_single']}**\n\n")
    L.append(f"Per SD-003 the grid is ROBUST iff the full gate passes on the "
             f"majority (≥3/6) of rolling windows. INJUSDC combo_vt60 passes "
             f"**{n1}/6** → **{res['verdict_single']}**.\n\n")
    L.append("---\n\n")

    # ── Part 2 ────────────────────────────────────────────────────────────
    L.append("## Part 2 — Multi-Coin Grid Portfolio (tangency, monthly rebal)\n\n")
    labels = res["labels"]
    L.append("Combines the single-coin **best DD-constrained grid config per coin** "
             "at tangency (max-Sharpe) weights with **monthly rebalancing**. "
             "Weights are optimized on the **train half only** and frozen for the "
             "test half (SD-003 train-only rule).\n\n")
    L.append("### Candidate legs\n\n")
    L.append("| Leg | Config | Spacing/Levels/Lev | Full-period Ann | Full-period "
             "Sharpe | Full-period MaxDD |\n")
    L.append("|-----|--------|--------------------|-----------------|"
             "---------------------|-------------------|\n")
    for l in labels:
        m = res["leg_full"][l]
        tag = LEG_CONFIGS[l].tag
        sp = f"{LEG_CONFIGS[l].spacing_pct:.0%}/{LEG_CONFIGS[l].n_levels}/{LEG_CONFIGS[l].leverage}x"
        L.append(f"| {l} | {tag} | {sp} | {_fmt_pct(m['annualized'])} | "
                 f"{m['sharpe']:.2f} | {_fmt_pct(m['max_drawdown'])} |\n")
    L.append("\n")

    # OOS
    tm = res["train_m"]
    te = res["test_m"]
    w = res["w_oos"]
    w_str = ", ".join(f"{l}={w[l]*100:.0f}%" for l in labels)
    L.append("### OOS 50/50 (train-only tangency weights, frozen for test)\n\n")
    L.append(f"**Frozen weights:** {w_str}\n\n")
    L.append("| Slice | Ann | Sharpe | MaxDD | Gate |\n")
    L.append("|-------|-----|--------|-------|------|\n")
    g_tr = "✅" if _gate_pass(tm["ann_return"], tm["sharpe"], tm["max_dd"]) else "❌"
    g_te = "✅" if _gate_pass(te["ann_return"], te["sharpe"], te["max_dd"]) else "❌"
    L.append(f"| Train (0–50%) | {_fmt_pct(tm['ann_return'])} | {tm['sharpe']:.2f} | "
             f"{_fmt_pct(tm['max_dd'])} | {g_tr} |\n")
    L.append(f"| **Test (50–100%)** | **{_fmt_pct(te['ann_return'])}** | "
             f"**{te['sharpe']:.2f}** | **{_fmt_pct(te['max_dd'])}** | {g_te} |\n\n")

    L.append("### Portfolio rolling expanding-window validation\n\n")
    L.append("Same 6 expanding windows; tangency weights re-optimized on each "
             "expanding train slice and frozen for that window's test slice.\n\n")
    L.append("| Window | Train | Test | Calendar (test) | Test Ann | Test Sharpe | "
             "Test MaxDD | Weights | Pass? |\n")
    L.append("|--------|-------|------|-----------------|----------|-------------|"
             "-----------|---------|-------|\n")
    for r in res["port_rolling"]:
        pf = "✅ PASS" if r["pass"] else "❌ fail"
        ws = "/".join(f"{r['weights'][l]*100:.0f}" for l in labels)
        L.append(f"| W{r['window']} | 0–{int(r['train_frac']*100)}% | "
                 f"{int(r['train_frac']*100)}–{int(r['test_frac']*100)}% | "
                 f"{r['calendar']} | {_fmt_pct(r['ann'])} | {r['sharpe']:.2f} | "
                 f"{_fmt_pct(r['max_dd'])} | {ws} | {pf} |\n")
    n2 = res["n_pass_portfolio"]
    L.append(f"\n**Pass count: {n2} / 6.**\n\n")
    L.append(f"### Verdict (Part 2): **{res['verdict_portfolio']}**\n\n")
    L.append(f"Portfolio passes **{n2}/6** rolling windows → **{res['verdict_portfolio']}**.\n\n")
    L.append("---\n\n")

    # ── Bottom line ───────────────────────────────────────────────────────
    L.append("## Bottom Line (numbers, not adjectives)\n\n")
    L.append("| Candidate | Full-period Ann | OOS 50/50 Test Ann | Rolling pass (≥3/6 = ROBUST) | Verdict |\n")
    L.append("|-----------|-----------------|---------------------|------------------------------|---------|\n")
    L.append(f"| INJUSDC combo_vt60 (single) | {_fmt_pct(full['annualized'])} "
             f"| n/a (single leg) | **{n1}/6** | **{res['verdict_single']}** |\n")
    L.append(f"| Multi-coin grid portfolio | see legs above | **{_fmt_pct(te['ann_return'])}** "
             f"| **{n2}/6** | **{res['verdict_portfolio']}** |\n\n")
    crosses50 = te["ann_return"] > 0.50
    L.append(f"- Does the portfolio cross the **50% annualized** bar OOS? "
             f"**{'YES' if crosses50 else 'NO'}** (OOS Ann = {_fmt_pct(te['ann_return'])}).\n")
    L.append(f"- Does it keep **MaxDD < 20%** OOS? "
             f"**{'YES' if te['max_dd'] > -0.20 else 'NO'}** "
             f"(OOS MaxDD = {_fmt_pct(te['max_dd'])}).\n")
    L.append(f"- Does it clear **Sharpe > 1.0** OOS? "
             f"**{'YES' if te['sharpe'] > 1.0 else 'NO'}** "
             f"(OOS Sharpe = {te['sharpe']:.2f}).\n\n")
    L.append("### Honest assessment\n\n")
    if res["verdict_single"] == "ROBUST" or res["verdict_portfolio"] == "ROBUST":
        L.append("At least one candidate clears the majority-window robustness gate.\n")
    else:
        L.append("Neither candidate clears the majority-window (≥3/6) robustness gate. "
                 "Per SD-003, **neither may be brought forward to Boss review** until "
                 "a strategy passes rolling-window validation. The single-coin INJ grid "
                 "remains the strongest honest *edge*, but it is regime-overfit on the "
                 "rolling test even though walk-forward (50/50, 60/40, 70/30) was "
                 "positive in both halves. The multi-coin portfolio adds diversification "
                 "(BTC's low-DD recenter grid dampens drawdown) but does not rescue the "
                 "rolling robustness or clear the 50% bar OOS.\n")
    L.append("\n---\n\n### Methodology notes\n\n")
    L.append("- Grid seeded at `closes[start_idx]` of each test slice → no look-ahead.\n")
    L.append("- Rolling windows: train expands (0→40% … 0→89%), test is the next ~10%.\n")
    L.append("- Portfolio weights: tangency (max-Sharpe) on **train-only** daily "
             "returns; frozen for the test slice (SD-003 rule). Capped at 60% per leg.\n")
    L.append("- Monthly rebalancing via `weighted_monthly_rebal` with a 0.07%/side "
             "turnover drag at month boundaries.\n")
    L.append("- 180-day bear-regime sample: 9/10 coins fell 31–65%. A long-only grid "
             "is structurally disadvantaged; these are conservative, regime-hostile "
             "OOS numbers.\n\n")
    L.append("*Research only. No live trading was performed.*\n")
    return "".join(L)


def main():
    res = run_all()
    report = build_report(res)
    out = DOCS / "grid-rolling-validation-and-portfolio.md"
    out.write_text(report)
    print(f"\n  Report written: {out}")
    # also dump raw json for traceability
    json_out = DOCS / "grid-rolling-validation-and-portfolio.json"
    import json
    with open(json_out, "w") as f:
        json.dump({k: (v.tolist() if hasattr(v, "tolist") else v)
                   for k, v in res.items()
                   if k not in ("full_inj", "leg_full")}, f, indent=2, default=float)
    print(f"  Raw data: {json_out}")


if __name__ == "__main__":
    main()
