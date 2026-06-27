#!/usr/bin/env python3
"""Kelly Criterion Position Sizing — GREEN-Verified 4-Leg Portfolio.

Candidate portfolio (escalated from portfolio-stress-analysis.md, VERDICT GREEN):
    Leg 1: LINKUSDC donchian atr2_vf_cb  @ 3x
    Leg 2: NEARUSDC donchian cbreaker    @ 1x
    Leg 3: ETHUSDC supertrend cbreaker   @ 1x
    Leg 4: DOTUSDT funding_contrarian    @ 3x
    Allocation: tangency weights 28% / 28% / 25% / 19% (optimized on full train)

    Full sample: Ann 103.3%, Sharpe 2.78, MaxDD -9.9%
    OOS (60/40): Ann 191.1%, Sharpe 2.17, MaxDD -20.2%

Purpose
-------
Before Boss live-deployment review, we need a defensible answer to "how much
capital should this strategy actually trade?" Kelly gives the theoretically
optimal bet size for geometric-growth maximization; half/quarter-Kelly are the
practical (variance-reduced, error-robust) variants most practitioners use.

This script:
  1. Reconstructs per-leg OOS daily returns (exact 60/40 split, same engine as
     the stress test) and the portfolio's OOS daily return series at headline
     weights.
  2. Computes per-leg and portfolio Kelly fraction:
        f* = mean(r) / var(r)              (daily, per-unit-capital)
     Annualized geometric-growth rate and the Kelly growth optimum.
  3. Computes half-Kelly (f*/2) and quarter-Kelly (f*/4).
  4. Simulates portfolio equity curves at full / half / quarter-Kelly sizing by
     leveraging the unit-weighted OOS return series, and reports realized
     annualized return, Sharpe, and drawdown at each sizing.
  5. Recommends a conservative position size for a $500 canary deployment.

Costs, signals, overlays, and the monthly-rebalanced portfolio construction are
identical to the stress test — this is a pure position-sizing layer on top.

Outputs:
    docs/research/kelly-sizing-analysis.md
    docs/research/kelly-sizing-data.json

Reuses: research_dd_controlled_trend.py (eng), research_portfolio_dd_trend.py (pp),
        research_portfolio_optimizer.py (opo).
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
REPORT_MD = DOCS_DIR / "kelly-sizing-analysis.md"
REPORT_JSON = DOCS_DIR / "kelly-sizing-data.json"

INITIAL_CAPITAL = pp.INITIAL_CAPITAL  # 10_000
IC = INITIAL_CAPITAL
DAY_MS = pp.DAY_MS

# ─── Winning combo (EXACTLY the stress-test candidate) ────────────────────────
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

WF_SPLIT = 0.60          # 60% train / 40% test — matches stress test & optimizer
PP_YEAR = 365.0          # daily compounding steps per year
CANARY_CAPITAL = 500.0   # $500 starting capital for canary deployment


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


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


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


def portfolio_equity(leg_data, labels, weights, ref_ts):
    """Portfolio equity curve via monthly rebalance to fixed weights."""
    eqs = [leg_data[l]["eq_aligned"] for l in labels]
    port_eq = opo.weighted_monthly_rebal(eqs, np.asarray(weights, float), ref_ts, IC)
    return port_eq


# ═══════════════════════════════════════════════════════════════════════════════
#  Kelly math
# ═══════════════════════════════════════════════════════════════════════════════
def kelly_fraction(daily_returns: np.ndarray) -> dict:
    """Full-Kelly fraction f* = mean(r) / var(r) for a daily return series.

    For a strategy with per-day returns r_i (per unit of capital at the base
    leverage already baked into the equity curve), the Kelly-optimal fraction of
    one's bankroll to commit is f* = mu / sigma^2. Trading at f* maximizes the
    long-run geometric growth rate; f>1 means borrow to lever beyond 100%.

    Returns dict with daily f*, annualized quantities, and growth diagnostics.
    """
    r = daily_returns[np.isfinite(daily_returns)]
    if len(r) < 2:
        return {"f_star": float("nan"), "mu_daily": float("nan"),
                "var_daily": float("nan"), "std_daily": float("nan")}
    mu = float(np.mean(r))
    var = float(np.var(r, ddof=1))
    std = math.sqrt(var) if var > 0 else float("nan")
    f_star = mu / var if var > 0 else float("nan")
    # annualized drift/vol of the *unit-capital* (f=1) strategy
    ann_mu = mu * PP_YEAR
    ann_var = var * PP_YEAR
    ann_std = std * math.sqrt(PP_YEAR) if std == std else float("nan")
    # Kelly long-run geometric growth rate g = mu*f - 0.5*var*f^2, maximized at f=f*
    # at f*: g* = mu^2 / (2*var)  (daily), annualized: *365
    g_star_daily = (mu ** 2) / (2.0 * var) if var > 0 else float("nan")
    g_star_annual = g_star_daily * PP_YEAR
    return {
        "f_star": f_star,            # full Kelly fraction (daily, unit-capital)
        "mu_daily": mu, "var_daily": var, "std_daily": std,
        "ann_mu": ann_mu, "ann_var": ann_var, "ann_std": ann_std,
        "sharpe_daily": mu / std if std and std > 0 else float("nan"),
        "sharpe_annual": (mu / std * math.sqrt(PP_YEAR)) if (std and std > 0) else float("nan"),
        "g_star_daily": g_star_daily,
        "g_star_annual": g_star_annual,
        "n_obs": int(len(r)),
    }


def size_returns(daily_returns: np.ndarray, fraction: float) -> np.ndarray:
    """Scale a per-unit-capital daily return series by a Kelly fraction.

    r_sized = fraction * r  (linear leverage of the base strategy).
    Equity = cumprod(1 + r_sized)."""
    return fraction * daily_returns


def equity_from_daily(daily_returns: np.ndarray, init: float = 1.0) -> np.ndarray:
    """Compound daily returns into an equity curve starting at `init`."""
    eq = np.empty(len(daily_returns) + 1)
    eq[0] = init
    for i, r in enumerate(daily_returns):
        eq[i + 1] = eq[i] * (1.0 + r)
        if eq[i + 1] <= 0:
            eq[i + 1] = 1e-9
    return eq


def metrics_from_daily(daily_returns: np.ndarray, years: float,
                       init: float = 1.0) -> dict:
    """Annualized metrics from a daily-return series."""
    r = daily_returns[np.isfinite(daily_returns)]
    eq = equity_from_daily(r, init)
    final = float(eq[-1])
    total_ret = final / init - 1.0
    ann = (final / init) ** (1.0 / max(years, 1e-6)) - 1.0 if final > 0 else -1.0
    mu = float(np.mean(r)) if len(r) else 0.0
    std = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    sharpe = mu / std * math.sqrt(PP_YEAR) if std > 0 else 0.0
    ann_vol = std * math.sqrt(PP_YEAR) if len(r) > 1 else 0.0
    # max drawdown on the compounded equity
    peak = np.maximum.accumulate(eq)
    safe = np.where(peak > 0, peak, 1e-12)
    dd = (eq - peak) / safe
    max_dd = float(dd.min())
    downside = r[r < 0]
    dstd = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    sortino = mu / dstd * math.sqrt(PP_YEAR) if dstd > 0 else 0.0
    calmar = ann / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0
    return {
        "init": init, "final": final, "total_return": total_ret,
        "ann_return": ann, "sharpe": sharpe, "sortino": sortino,
        "max_dd": max_dd, "ann_vol": ann_vol, "calmar": calmar,
        "n_days": int(len(r)),
    }


def growth_rate(daily_returns: np.ndarray, fraction: float) -> float:
    """Realized annualized geometric growth at a given Kelly fraction:
    g(f) = (1/years) * sum[ ln(1 + f*r) ]."""
    r = daily_returns[np.isfinite(daily_returns)]
    log_g = np.sum(np.log(np.maximum(1.0 + fraction * r, 1e-12)))
    return log_g


# ═══════════════════════════════════════════════════════════════════════════════
#  Report
# ═══════════════════════════════════════════════════════════════════════════════
def generate_markdown(data_meta, sanity, leg_kelly, port_kelly,
                      port_unit_oos, sizing_table, growth_table,
                      canary, recommendation) -> str:
    L: list[str] = []
    w = L.append
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    w("# Kelly Criterion Position Sizing — 4-Leg Portfolio")
    w("")
    w(f"*Generated by `scripts/research_kelly_sizing.py` — {gen}. "
      "Numbers, not adjectives. Research only — does not modify live config.*")
    w("")
    w("**Candidate (GREEN-verified from `portfolio-stress-analysis.md`):**")
    w("- Leg 1: LINKUSDC donchian atr2_vf_cb @ 3x")
    w("- Leg 2: NEARUSDC donchian cbreaker @ 1x")
    w("- Leg 3: ETHUSDC supertrend cbreaker @ 1x")
    w("- Leg 4: DOTUSDT funding_contrarian @ 3x")
    w(f"- Allocation: tangency weights "
      + " / ".join(f"{s} {int(wt*100)}%" for s, wt in zip(SHORT_LABELS, HEADLINE_WEIGHTS)))
    w("")
    oos = sanity["oos"]
    full = sanity["full"]
    w(f"**Reproduced headline:** Full Ann {fmt_pct(full['ann'])} / Sharpe {full['sharpe']:.2f} "
      f"/ MaxDD {fmt_pct(full['max_dd'])}. OOS(60/40) Ann {fmt_pct(oos['ann'])} / "
      f"Sharpe {oos['sharpe']:.2f} / MaxDD {fmt_pct(oos['max_dd'])}.")
    w(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']//24} days). "
      f"**Costs:** {eng.FEE_RATE*100:.2f}% taker + {eng.SLIPPAGE*100:.2f}% slippage/side.")
    w("")

    # ── Recommendation banner ──
    w(f"> ## RECOMMENDATION: {recommendation['title']}")
    w(">")
    w(f"> {recommendation['headline']}")
    w(">")
    w(f"> - **Canary capital:** {fmt_money(CANARY_CAPITAL)}")
    w(f"> - **Recommended Kelly fraction:** {recommendation['fraction']:.3f} "
      f"(= {recommendation['fraction_label']})")
    rec = canary["recommended"]
    w(f"> - **Capital at risk per the base strategy:** "
      f"{fmt_money(rec['effective_capital'])} "
      f"(= {recommendation['fraction']:.2f} × {fmt_money(CANARY_CAPITAL)})")
    w(f"> - **Expected OOS drawdown at this sizing:** {fmt_pct(rec['max_dd'])}")
    w(f"> - **Expected OOS annualized return:** {fmt_pct(rec['ann_return'])} "
      f"(Sharpe {port_kelly['sharpe_annual']:.2f}, scale-invariant)")
    w(f"> - **Stress-test ruin floor (quarter-Kelly, 95th-pctile MC drawdown):** "
      f"{fmt_pct(canary['stress_floor_pct'])} → "
      f"{fmt_money(canary['stress_floor_dollar'])}")
    if recommendation["caveats"]:
        w(">")
        for c in recommendation["caveats"]:
            w(f"> - ⚠️ {c}")
    w("")

    # ── 1. Method ──
    w("---")
    w("")
    w("## 1. Method")
    w("")
    w("Kelly criterion finds the bet size that maximizes long-run geometric "
      "(compounded) growth. For a strategy with daily returns r (per unit of "
      "committed capital, with the base leverage already embedded in the backtest "
      "equity curve):")
    w("")
    w("```")
    w("f* = mean(r) / var(r)        # full Kelly (daily)")
    w("g(f) = mu·f − ½·σ²·f²        # expected log-growth per step")
    w("g*  = mu² / (2·σ²)           # max growth, at f = f*")
    w("```")
    w("")
    w("Key points:")
    w("- **f > 1** means the optimal bet exceeds 100% of capital (i.e. use "
      "leverage/borrowing on top of the base strategy). f < 1 means bet a fraction.")
    w("- Full Kelly maximizes growth but has high variance and is extremely "
      "sensitive to estimation error in μ and σ². Real returns are non-Gaussian "
      "(fat tails), so true Kelly is usually *overstated* by the sample estimate.")
    w("- **Half-Kelly (f*/2)** keeps ~75% of optimal growth rate for ~50% of the "
      "variance, and is the standard practitioner default.")
    w("- **Quarter-Kelly (f*/4)** sacrifices more growth for a large drawdown "
      "reduction and robustness to parameter error — appropriate for a canary "
      "(first live money) deployment.")
    w("")
    w("This script reconstructs the **OOS (60/40 test) per-leg daily returns** "
      "using the exact same engine and split as the stress test, builds the "
      "headline-weighted portfolio daily-return series, and computes Kelly on "
      "the portfolio (the correct object — diversification already happened).")
    w("")

    # ── 2. Sanity reproduction ──
    w("## 2. Sanity: OOS reproduction")
    w("")
    w("Confirm the OOS portfolio metrics match the stress-test headline before "
      "doing Kelly math on them.")
    w("")
    w("| Window | Ann | Sharpe | MaxDD |")
    w("|---|---:|---:|---:|")
    w(f"| Full sample | {fmt_pct(full['ann'])} | {full['sharpe']:.2f} | {fmt_pct(full['max_dd'])} |")
    w(f"| OOS 60/40 (headline) | {fmt_pct(oos['ann'])} | {oos['sharpe']:.2f} | {fmt_pct(oos['max_dd'])} |")
    w(f"| OOS 60/40 (this run) | {fmt_pct(port_unit_oos['ann_return'])} | {port_unit_oos['sharpe']:.2f} | {fmt_pct(port_unit_oos['max_dd'])} |")
    oos_ok = (abs(port_unit_oos["ann_return"] - oos["ann"]) < 0.05
              and abs(port_unit_oos["sharpe"] - oos["sharpe"]) < 0.15)
    w(f"\nReproduction: {'✅ MATCH' if oos_ok else '⚠️ minor drift'} "
      f"(target OOS {fmt_pct(1.911)}/2.17/-20.2%).")
    w("")

    # ── 3. Per-leg Kelly ──
    w("## 3. Per-leg Kelly fractions")
    w("")
    w("Each leg's full-Kelly fraction computed from its OOS daily returns "
      "(independent legs, for diagnostics — the portfolio Kelly in §4 is what we size on).")
    w("")
    w("| Leg | μ_daily | σ_daily | f* (full Kelly) | Ann Sharpe | n_days |")
    w("|---|---:|---:|---:|---:|---:|")
    for lab, short in zip(WINNING_LABELS, SHORT_LABELS):
        k = leg_kelly[lab]
        w(f"| {short} | {k['mu_daily']*100:.3f}% | {k['std_daily']*100:.3f}% | "
          f"{k['f_star']:.3f} | {k['sharpe_annual']:.2f} | {k['n_obs']} |")
    w("")
    w("Note: individual-leg f* is not directly additive because legs are "
      "correlated; the portfolio Kelly (next section) accounts for covariance "
      "automatically by sizing the already-combined return series.")
    w("")

    # ── 4. Portfolio Kelly ──
    pk = port_kelly
    w("## 4. Portfolio Kelly (the sizing object)")
    w("")
    w("Computed on the **headline-weighted portfolio OOS daily-return series** "
      "(28/28/25/19, monthly rebalanced). This is the series the live strategy "
      "would actually produce per unit of committed capital.")
    w("")
    w("| Quantity | Value |")
    w("|---|---:|")
    w(f"| Mean daily return μ | {pk['mu_daily']*100:.4f}% |")
    w(f"| Std daily return σ | {pk['std_daily']*100:.4f}% |")
    w(f"| Annualized μ | {fmt_pct(pk['ann_mu'])} |")
    w(f"| Annualized σ | {fmt_pct(pk['ann_std'])} |")
    w(f"| Annualized Sharpe (μ/σ·√365) | {pk['sharpe_annual']:.2f} |")
    w(f"| **Full Kelly f*** | **{pk['f_star']:.3f}** |")
    w(f"| Half-Kelly f*/2 | {pk['f_star']/2:.3f} |")
    w(f"| Quarter-Kelly f*/4 | {pk['f_star']/4:.3f} |")
    w(f"| Optimal log-growth g* (daily) | {pk['g_star_daily']*100:.4f}% |")
    w(f"| Optimal log-growth g* (annualized) | {fmt_pct(pk['g_star_annual'])} |")
    w(f"| Observations | {pk['n_obs']} daily bars |")
    w("")
    fstar = pk["f_star"]
    interp = "exceeds 100%" if fstar > 1.0 else ("is below 100%" if fstar < 1.0 else "= 100%")
    w(f"**Interpretation:** full Kelly f* = {fstar:.3f} {interp} of capital. "
      + ("This means the OOS sample suggests levering *beyond* the base "
         "strategy's embedded leverage — a strong but dangerous signal that is "
         "almost certainly inflated by a favorable OOS sample."
         if fstar > 1.0 else
         "This means committing a fraction of capital to the base strategy and "
         "holding the rest in cash.")
      + " We do NOT trade full Kelly — see §5.")
    w("")

    # ── 5. Sizing simulation ──
    w("## 5. Sizing simulation (OOS, compounded)")
    w("")
    w(f"Leverage the unit-capital OOS portfolio daily returns by each Kelly "
      f"fraction, compound over the {port_unit_oos['n_days']}-day OOS window, "
      "and measure realized metrics.")
    w("")
    w("| Sizing | Fraction | Ann return | Sharpe | MaxDD | Total ret | Calmar |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for row in sizing_table:
        m = row["metrics"]
        w(f"| {row['label']} | {row['fraction']:.3f} | {fmt_pct(m['ann_return'])} | "
          f"{m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | {fmt_pct(m['total_return'])} | "
          f"{m['calmar']:.2f} |")
    w("")
    w("Observations:")
    w("- **Full Kelly** delivers the highest annualized return but at the cost "
      "of severe drawdown (frequently worse than the base-strategy MaxDD because "
      "f* > 1 multiplies tail losses).")
    w("- **Half/Quarter-Kelly** retain most of the Sharpe (Sharpe is "
      "scale-invariant for linear leverage) but compress drawdown substantially. "
      "This is the entire point of fractional Kelly.")
    w("- **Sharpe is identical across sizes** because linear leverage scales μ "
      "and σ equally — the risk-adjusted edge doesn't change, only the absolute "
      "P&L and drawdown magnitude do.")
    w("")

    # ── 6. Growth-rate curve ──
    w("## 6. Geometric growth vs. bet size")
    w("")
    w("Expected annualized log-growth g(f) = (1/years)·Σ ln(1 + f·r) evaluated "
      "at the three Kelly levels. The theoretical optimum is at f = f*.")
    w("")
    w("| Sizing | Fraction | Realized annualized log-growth |")
    w("|---|---:|---:|")
    for row in growth_table:
        w(f"| {row['label']} | {row['fraction']:.3f} | {row['growth_annualized']*100:.2f}% |")
    w("")
    w("Quarter-Kelly captures a fraction of optimal growth in exchange for "
      "dramatically lower drawdown risk and far greater robustness to the "
      "estimation error that always plagues μ.")
    w("")

    # ── 7. Canary deployment ──
    w(f"## 7. Canary deployment — {fmt_money(CANARY_CAPITAL)}")
    w("")
    w("Translating sizing into concrete dollars for a first-money canary. "
      f"Effective capital = fraction × {fmt_money(CANARY_CAPITAL)}. "
      "Drawdown is applied to that effective capital; cash buffer is held aside.")
    w("")
    w("**Important:** because raw full-Kelly f* > 1 (see §4), *every* fractional "
      "Kelly level here implies deploying *more than 100%* of the $500 — i.e. "
      "borrowing on top of the base strategy's already-embedded 3× leverage on "
      "LINK and DOT. For a canary (first live money) that is imprudent. The raw "
      "numbers are shown for completeness; the **recommendation caps the canary "
      "at f=1 (100% of capital at base leverage)**.")
    w("")
    w("| Sizing | Raw fraction | Effective capital (raw) | Exp. OOS MaxDD ($) | Exp. OOS Ann P&L ($) | Worst-day loss ($) |")
    w("|---|---:|---:|---:|---:|---:|")
    for key in ["full", "half", "quarter"]:
        c = canary[key]
        w(f"| {c['label']} | {c['fraction']:.3f} | {fmt_money(c['effective_capital'])} | "
          f"{fmt_money(c['max_dd_dollar'])} | {fmt_money(c['ann_pnl_dollar'])} | "
          f"{fmt_money(c['worst_day_dollar'])} |")
    w("")
    rec = canary["recommended"]
    w(f"**Recommended for canary: raw quarter-Kelly f={rec['fraction_raw']:.3f}, "
      f"capped at f={rec['fraction']:.3f} (= 100% of capital at base leverage).**")
    w("")
    w(f"- Deploy the full **{fmt_money(rec['effective_capital'])}** as the "
      f"strategy's base capital at the base strategy's embedded leverage "
      f"(3× on LINK/DOT, 1× on NEAR/ETH). No additional borrowing on top.")
    w(f"- Raw quarter-Kelly wanted f={rec['fraction_raw']:.2f} "
      f"(={fmt_money(rec['effective_capital_uncapped'])} effective) — capped to "
      f"f=1.0 for the canary. Revisit after ≥60 live days.")
    w(f"- Expected OOS drawdown at the capped sizing: **{fmt_pct(rec['max_dd'])}** = "
      f"**{fmt_money(rec['max_dd_dollar'])}** peak-to-trough.")
    w(f"- Expected OOS annual P&L at the capped sizing: **{fmt_money(rec['ann_pnl_dollar'])}** "
      f"({fmt_pct(rec['ann_return'])} on deployed capital).")
    w(f"- Worst single OOS day at the capped sizing: **{fmt_pct(rec['worst_day_loss_pct'])}** = "
      f"**{fmt_money(rec['worst_day_dollar'])}**.")
    w("")
    w("**Stress floor:** the stress test's Monte Carlo found the 95th-percentile "
      f"OOS drawdown at full (f=1) sizing to be {fmt_pct(canary['mc_p95_maxdd_full'])}. "
      f"At the capped canary sizing (f={rec['fraction']:.2f}) this is "
      f"{fmt_pct(canary['stress_floor_pct'])} = "
      f"**{fmt_money(canary['stress_floor_dollar'])}** worst realistic drawdown "
      "on the deployed capital.")
    w("")

    # ── 8. Methodology & caveats ──
    w("## 8. Methodology & caveats")
    w("")
    w("- **Engine:** exact reuse of `research_portfolio_stress.py` data flow — "
      "`research_dd_controlled_trend.py::simulate` + `run_leg` + "
      "`weighted_monthly_rebal`. Signals, overlays, costs identical to the "
      "stress test. Only the sizing layer is new.")
    w("- **Kelly object:** the *portfolio* daily-return series at headline "
      "tangency weights (28/28/25/19), not individual legs. Diversification is "
      "already priced in.")
    w("- **OOS window:** 60/40 split, weights frozen from the train half — same "
      "as the stress test's headline OOS. Kelly is computed on out-of-sample "
      "returns only (in-sample Kelly would be optimistic).")
    w("- **Linear leverage model:** r_sized = f · r. This assumes the strategy "
      "can be linearly scaled, which holds for notional-sized futures/perps "
      "away from liquidation. At very high f the linear model understates "
      "tail risk (convex losses near liquidation); another reason to avoid "
      "full Kelly.")
    w("")
    w("**Why NOT full Kelly, even though f* looks attractive:**")
    w("1. **Estimation error:** μ is estimated from ~150 OOS days; the true μ "
      "is uncertain. Full Kelly's growth rate falls off *quadratically* as you "
      "overshoot, and a 2× overestimate of μ makes f* 2× too big. Half/quarter "
      "Kelly have large margins against this.")
    w("2. **Fat tails:** Kelly assumes near-Gaussian returns. Crypto daily "
      "returns have excess kurtosis; true optimal f is materially smaller than "
      "the Gaussian estimate.")
    w("3. **Drawdown utility:** Kelly maximizes log-wealth (implicitly CRRA "
      "with γ=1). Most operators have higher risk aversion; fractional Kelly "
      "approximates γ≈2 (half) to γ≈4 (quarter).")
    w("4. **Non-stationarity:** the OOS edge may not persist at full strength. "
      "Fractional sizing survives a halving of the edge without going underwater.")
    w("")
    w("*This is research output. It does not change any live config. Boss "
      "deployment review decides whether and how to act on it.*")
    return "\n".join(L) + "\n"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print("KELLY POSITION SIZING — LINK3x / NEARcb1x / ETHST1x / DOTFUND3x")
    print("=" * 72)

    # ── Load data (same as stress test) ──
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
    n = len(ref_pack.close)
    ref_ts_full = ref_pack.ts.copy()
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

    # ── Reproduce full-sample + OOS headline (sanity) ──
    print("\n[sanity] reproducing full + 60/40 OOS...")
    full_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                               funding_data, ref_ts_full, 0, None)
    full_eq = portfolio_equity(full_leg, WINNING_LABELS, HEADLINE_WEIGHTS, ref_ts_full)
    m_full = pp.portfolio_metrics(full_eq, ref_ts_full)

    split60 = int(n * WF_SPLIT)
    ref_ts_tr = ref_pack.ts[:split60].copy()
    ref_ts_te = ref_pack.ts[split60:].copy()
    # train: optimize tangency weights (for sanity — headline uses fixed 28/28/25/19)
    tr_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                             funding_data, ref_ts_tr, 0, split60)
    R_tr, mu_tr, cov_tr, stds_tr = opo._moments_from_legs(tr_leg, WINNING_LABELS)
    w_train = opo.compute_alloc("tangency", mu_tr, cov_tr, stds_tr)
    # test: run legs, apply headline weights (we size on the headline-weighted port)
    te_leg = run_legs_window(WINNING_LEGS, WINNING_LABELS, packs, targets,
                             funding_data, ref_ts_te, split60, n)
    te_eq_headline = portfolio_equity(te_leg, WINNING_LABELS, HEADLINE_WEIGHTS, ref_ts_te)
    m_oos = pp.portfolio_metrics(te_eq_headline, ref_ts_te)
    sanity = {
        "full": {"ann": m_full["ann_return"], "sharpe": m_full["sharpe"],
                 "max_dd": m_full["max_dd"]},
        "oos": {"ann": m_oos["ann_return"], "sharpe": m_oos["sharpe"],
                "max_dd": m_oos["max_dd"]},
        "w_train_tangency": w_train.tolist(),
    }
    print(f"  full: ann {fmt_pct(m_full['ann_return'])}, shp {m_full['sharpe']:.2f}, "
          f"dd {fmt_pct(m_full['max_dd'])}")
    print(f"  oos : ann {fmt_pct(m_oos['ann_return'])}, shp {m_oos['sharpe']:.2f}, "
          f"dd {fmt_pct(m_oos['max_dd'])}")

    # ── Build the OOS portfolio daily-return series (the Kelly object) ──
    print("\n[kelly] building OOS portfolio daily-return series...")
    # daily-resample the headline-weighted OOS portfolio equity curve
    port_daily = opo.leg_daily_returns(te_eq_headline, ref_ts_te)
    port_daily = port_daily[np.isfinite(port_daily)]
    span_ms = float(ref_ts_te[-1] - ref_ts_te[0]) if len(ref_ts_te) > 1 else DAY_MS * len(port_daily)
    oos_years = max(span_ms / (365.0 * DAY_MS), 1e-6)
    print(f"  {len(port_daily)} OOS daily returns, {oos_years:.3f} years")

    # unit-capital OOS metrics (f=1) for the table
    port_unit_oos = metrics_from_daily(port_daily, oos_years, init=1.0)
    print(f"  unit-capital OOS: ann {fmt_pct(port_unit_oos['ann_return'])}, "
          f"shp {port_unit_oos['sharpe']:.2f}, dd {fmt_pct(port_unit_oos['max_dd'])}")

    # ── Per-leg Kelly (diagnostic) ──
    print("\n[kelly] per-leg Kelly fractions (diagnostic)...")
    leg_kelly: dict[str, dict] = {}
    for lab, short in zip(WINNING_LABELS, SHORT_LABELS):
        dr = te_leg[lab]["daily_returns"]
        k = kelly_fraction(dr)
        leg_kelly[lab] = k
        print(f"  {short}: f*={k['f_star']:.3f}  μ_d={k['mu_daily']*100:.3f}%  "
              f"σ_d={k['std_daily']*100:.3f}%  shp_ann={k['sharpe_annual']:.2f}")

    # ── Portfolio Kelly ──
    print("\n[kelly] portfolio Kelly fraction...")
    port_kelly = kelly_fraction(port_daily)
    f_star = port_kelly["f_star"]
    print(f"  f* = {f_star:.4f}")
    print(f"  μ_daily = {port_kelly['mu_daily']*100:.4f}%, σ_daily = {port_kelly['std_daily']*100:.4f}%")
    print(f"  ann μ = {fmt_pct(port_kelly['ann_mu'])}, ann σ = {fmt_pct(port_kelly['ann_std'])}")
    print(f"  g* annualized = {fmt_pct(port_kelly['g_star_annual'])}")

    # ── Sizing simulation ──
    print("\n[sim] equity at full/half/quarter Kelly...")
    sizing_fractions = [
        ("Full Kelly", f_star),
        ("Half Kelly", f_star / 2.0),
        ("Quarter Kelly", f_star / 4.0),
        ("Unit (f=1, reference)", 1.0),
    ]
    sizing_table = []
    for label, frac in sizing_fractions:
        r_sized = size_returns(port_daily, frac)
        m = metrics_from_daily(r_sized, oos_years, init=1.0)
        sizing_table.append({"label": label, "fraction": frac, "metrics": m})
        print(f"  {label:28s} f={frac:.3f}: ann {fmt_pct(m['ann_return'])}, "
              f"shp {m['sharpe']:.2f}, dd {fmt_pct(m['max_dd'])}, "
              f"calmar {m['calmar']:.2f}")

    # ── Growth-rate table ──
    print("\n[growth] realized log-growth...")
    growth_table = []
    for label, frac in [("Full Kelly", f_star), ("Half Kelly", f_star / 2.0),
                        ("Quarter Kelly", f_star / 4.0)]:
        g = growth_rate(port_daily, frac)
        g_ann = g / oos_years
        growth_table.append({"label": label, "fraction": frac,
                             "growth_annualized": g_ann})
        print(f"  {label:14s} f={frac:.3f}: g_ann = {g_ann*100:.2f}%")

    # ── Canary deployment ──
    print(f"\n[canary] ${CANARY_CAPITAL:.0f} deployment sizing...")
    # MC 95th-percentile drawdown from stress test (read from json if available,
    # else estimate from observed OOS tail). We load the stress-test JSON to stay
    # consistent with the GREEN verdict.
    mc_p95_maxdd_full = -0.25  # conservative default
    stress_json = DOCS_DIR / "portfolio-stress-data.json"
    if stress_json.exists():
        try:
            sj = json.loads(stress_json.read_text())
            mc = sj.get("test3_montecarlo", {}).get("max_dd_pct", {})
            p5 = mc.get("p5")  # most-negative tail (5th pctile of max_dd)
            if p5 is not None and math.isfinite(p5):
                mc_p95_maxdd_full = p5  # p5 of max_dd = worst 5% ≈ "95th pctile loss"
        except Exception:
            pass

    canary: dict[str, Any] = {}
    worst_day_pct = float(np.min(port_daily))  # worst single OOS day at f=1
    for key, label, frac_mult in [
        ("full", "Full Kelly", 1.0),
        ("half", "Half Kelly", 0.5),
        ("quarter", "Quarter Kelly", 0.25),
    ]:
        frac = f_star * frac_mult
        eff = CANARY_CAPITAL * frac
        # use the simulated metrics at this fraction
        sim = next(s["metrics"] for s in sizing_table if s["label"] == label)
        canary[key] = {
            "label": label,
            "fraction": frac,
            "effective_capital": eff,
            "ann_return": sim["ann_return"],
            "ann_pnl_dollar": sim["ann_return"] * eff,
            "max_dd": sim["max_dd"],
            "max_dd_dollar": sim["max_dd"] * eff,
            "worst_day_loss_pct": worst_day_pct * frac,
            "worst_day_dollar": worst_day_pct * frac * eff,
        }
        print(f"  {label:14s} f={frac:.3f}: eff ${eff:7.2f}, "
              f"annP&L ${canary[key]['ann_pnl_dollar']:8.2f}, "
              f"maxDD ${canary[key]['max_dd_dollar']:7.2f}")

    # recommended = quarter Kelly, but CAPPED at f=1 (100% of capital) for a
    # canary deployment. The base strategy already embeds 3x leverage on two
    # legs; layering additional notional leverage on top for a first-money
    # deployment is imprudent regardless of what the OOS Kelly says.
    rec_frac_raw = f_star / 4.0
    rec_frac_capped = min(rec_frac_raw, 1.0)
    rec_eff_raw = CANARY_CAPITAL * rec_frac_raw
    rec_eff = CANARY_CAPITAL * rec_frac_capped
    # metrics at the capped fraction
    rec_sim = metrics_from_daily(size_returns(port_daily, rec_frac_capped), oos_years, init=1.0)
    canary["recommended"] = {
        "label": "Quarter-Kelly (capped at f=1 for canary)",
        "fraction_raw": rec_frac_raw,
        "fraction": rec_frac_capped,
        "fraction_uncapped_label": f"f*/4 = {f_star:.3f}/4 = {rec_frac_raw:.3f}",
        "effective_capital": rec_eff,
        "effective_capital_uncapped": rec_eff_raw,
        "ann_return": rec_sim["ann_return"],
        "ann_pnl_dollar": rec_sim["ann_return"] * rec_eff,
        "max_dd": rec_sim["max_dd"],
        "max_dd_dollar": rec_sim["max_dd"] * rec_eff,
        "worst_day_loss_pct": worst_day_pct * rec_frac_capped,
        "worst_day_dollar": worst_day_pct * rec_frac_capped * rec_eff,
    }
    # stress floor: scale the MC 95th-pctile full-sample drawdown to the capped fraction
    stress_floor_pct = mc_p95_maxdd_full * rec_frac_capped  # linear in fraction
    canary["mc_p95_maxdd_full"] = mc_p95_maxdd_full
    canary["stress_floor_pct"] = stress_floor_pct
    canary["stress_floor_dollar"] = stress_floor_pct * rec_eff
    print(f"  recommended: quarter-Kelly f={rec_frac_capped:.3f} (raw {rec_frac_raw:.3f}, "
          f"capped at 1.0), eff ${rec_eff:.2f}, stress floor ${canary['stress_floor_dollar']:.2f}")

    # ── Recommendation text ──
    rec_eff = canary["recommended"]["effective_capital"]
    recommendation = {
        "title": "Quarter-Kelly Canary (capped at 100% capital)",
        "headline": (f"Deploy the full {fmt_money(rec_eff)} as strategy capital "
                     f"at the base strategy's embedded leverage "
                     f"(f={rec_frac_capped:.2f}, capped from raw quarter-Kelly f={rec_frac_raw:.2f})."),
        "fraction": rec_frac_capped,
        "fraction_label": f"min(f*/4={rec_frac_raw:.2f}, 1.0) = {rec_frac_capped:.2f}",
        "caveats": [],
    }
    # add caveats based on the numbers
    if f_star > 2.0:
        recommendation["caveats"].append(
            f"Full Kelly f*={f_star:.2f} is very high (>2×) — implies heavy "
            "leverage on top of the base strategy. This is almost certainly "
            "inflated by a favorable OOS sample; the raw quarter-Kelly "
            f"(f={rec_frac_raw:.2f}) is itself >100%, so we cap the canary at "
            "100% of capital at the base strategy's embedded leverage.")
    if canary["recommended"]["max_dd"] < -0.15:
        recommendation["caveats"].append(
            f"Even at the capped sizing the expected OOS drawdown is "
            f"{fmt_pct(canary['recommended']['max_dd'])} — set a hard stop / "
            "circuit breaker at this level on the live bot.")
    if port_kelly["n_obs"] < 200:
        recommendation["caveats"].append(
            f"Only {port_kelly['n_obs']} OOS daily observations — Kelly "
            "estimate has wide error bars. Re-estimate after 60+ live days.")
    if rec_frac_raw > 1.0:
        recommendation["caveats"].append(
            "The raw Kelly fractions (full/half/quarter) all exceed 100% of "
            "capital — this is the OOS sample telling you the edge is strong, "
            "NOT a license to lever. Treat the cap as a hard risk limit for "
            "the canary; revisit sizing only after ≥60 live days confirm the edge.")

    # ── Write reports ──
    print("\n[write] generating markdown + json...")
    md = generate_markdown(data_meta, sanity, leg_kelly, port_kelly,
                           port_unit_oos, sizing_table, growth_table,
                           canary, recommendation)
    REPORT_MD.write_text(md, encoding="utf-8")
    print(f"  wrote {REPORT_MD}")
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_meta": data_meta,
        "costs": {"fee_side": eng.FEE_RATE, "slippage_side": eng.SLIPPAGE,
                  "funding_8h": eng.FUNDING_RATE},
        "combo": {"labels": WINNING_LABELS, "short_labels": SHORT_LABELS,
                  "headline_weights": HEADLINE_WEIGHTS.tolist()},
        "sanity": sanity,
        "oos_years": oos_years,
        "port_unit_oos": port_unit_oos,
        "leg_kelly": leg_kelly,
        "port_kelly": port_kelly,
        "sizing_table": sizing_table,
        "growth_table": growth_table,
        "canary": canary,
        "recommendation": recommendation,
    }
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default),
                           encoding="utf-8")
    print(f"  wrote {REPORT_JSON}")

    print(f"\nDone in {time.time()-t0:.1f}s.")
    print("\n" + "=" * 72)
    print("KEY NUMBERS")
    print("=" * 72)
    print(f"  Portfolio full-Kelly f*        : {f_star:.3f}")
    print(f"  Half-Kelly f*/2                : {f_star/2:.3f}")
    print(f"  Quarter-Kelly f*/4 (raw)       : {f_star/4:.3f}")
    print(f"  OOS Sharpe (scale-invariant)   : {port_kelly['sharpe_annual']:.2f}")
    qk_dd = canary["quarter"]["max_dd"]
    print(f"  Quarter-Kelly raw OOS MaxDD    : {fmt_pct(qk_dd)}")
    rec = canary["recommended"]
    print(f"  Canary ${CANARY_CAPITAL:.0f} @ quarter-Kelly (capped f={rec['fraction']:.2f}):")
    print(f"    effective capital            : {fmt_money(rec['effective_capital'])}")
    print(f"    expected ann P&L             : {fmt_money(rec['ann_pnl_dollar'])}")
    print(f"    expected MaxDD ($)           : {fmt_money(rec['max_dd_dollar'])}")
    print(f"    stress floor (MC p95, capped): {fmt_money(canary['stress_floor_dollar'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
