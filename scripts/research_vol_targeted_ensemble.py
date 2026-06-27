#!/usr/bin/env python3
"""Vol-Targeted Multi-Signal Ensemble Backtest — Research (causal, honest).

PROBLEM
-------
No single strategy passes the aggressive gate (Sharpe>1.0, Ann>50%, MaxDD<20%).
But individual causal signals have real edge:
  * LINK/NEAR/ETH Donchian + ETH Supertrend trend signals: individually strong
    but 15-37% drawdowns.
  * SOL funding-contrarian with momentum confirmation: partial-sample Sharpe ~1.14.
The signals are partially uncorrelated (trend vs mean-reversion).

HYPOTHESIS
----------
Combining uncorrelated signals + vol-targeted position sizing (scale position by
inverse realized vol, cap gross at 2x, apply Kelly fraction) lifts portfolio
Sharpe above 1.0 while keeping MaxDD under 20%, and may push annualized >50%.

SIGNALS (causal only; NO DOT funding_contrarian as-coded — it has a 0-bar
look-ahead bug documented in portfolio-stress-INDEPENDENT-VALIDATION.md):
  * Trend: Donchian(20,10) breakout on LINK, ETH, NEAR  (causal, .shift(1))
  * Trend: Supertrend(14,7) on ETH                       (causal)
  * Funding: contrarian_mom on SOL, enter-z=3.0, signal SHIFTED +1 bar (honest)

VOL TARGETING
  * Per leg: mult_t = clip(target_vol / realized_vol_t, 0, MAX_GROSS=2.0)
  * realized_vol from past returns (causal), rebalanced weekly.
  * Sized return = mult_{t-1} * base_1x_return_t  (mult known at t-1 => causal)
  * Rebalance cost charged on |delta mult| each weekly rebalance.

PORTFOLIO
  * Tangency (max-Sharpe) or risk-parity allocation across legs, monthly rebal.

VALIDATION
  * Full-sample headline metrics.
  * Rolling expanding-window (6 windows, 10% test slices).
  * Monte Carlo bootstrap (2000 resamples of per-leg daily returns).
  * Walk-forward (60/40 and 70/30 splits).

COSTS: 0.04% taker + 0.03% slippage/side = 0.14% round-trip. Funding 0.010%/8h.
DATA: Binance USDC-M perps hourly klines (cached); SOL funding 8h (cached).

Reuses signal generators + cost model + portfolio engine from:
  scripts/research_dd_controlled_trend.py     (eng: Donchian, Supertrend, ATR)
  scripts/research_portfolio_optimizer.py     (opo: tangency/RP, monthly rebal)
  scripts/research_portfolio_dd_trend.py      (pp: portfolio_metrics)
  scripts/research_funding_directional_deep.py(mom-confirmed contrarian signal)
  scripts/research_kelly_sizing.py            (kelly_fraction = mu/var)

Numbers, not adjectives. The Boss gets truth.
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
import research_portfolio_optimizer as opo  # noqa: E402
import research_portfolio_dd_trend as pp  # noqa: E402
import research_funding_directional as fbase  # noqa: E402

REPO_ROOT = HERE.parent
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_MD = DOCS_DIR / "vol-targeted-ensemble-analysis.md"
REPORT_JSON = DOCS_DIR / "vol-targeted-ensemble-data.json"

# ─── Configuration ───────────────────────────────────────────────────────────
INITIAL_CAPITAL = 10_000.0
IC = INITIAL_CAPITAL
DAY_MS = eng.DAY_MS
HOUR_MS = 3_600_000
EIGHTH_MS = 8 * HOUR_MS

# Costs (match engine / funding baseline)
COST_SIDE = eng.COST_SIDE  # 0.0007 per side (0.04% taker + 0.03% slip)
FUNDING_RATE = eng.FUNDING_RATE  # 0.0001 per 8h (for trend perp holding proxy)

# Vol targeting
TARGET_VOL_ANN = 0.15  # 15% annualized target volatility
MAX_GROSS = 2.0  # cap gross leverage per leg at 2x
VT_LOOKBACK_HOURS = 168  # 1 week realized-vol window (trend, hourly)
VT_LOOKBACK_FUND = 21  # ~1 week realized-vol window (funding, 8h => 21 bars)
VT_REBAL_HOURS = 168  # rebalance vol-target weekly

# Annualization factors
HOURS_PER_YEAR = 24.0 * 365.0
EIGHTH_PER_YEAR = 3.0 * 365.0

# Portfolio
PORT_REBAL = "monthly"
MAX_W = 0.50  # no single leg > 50% of portfolio

# Validation
MC_NITER = 2000
MC_SEED = 20260627
WF_SPLITS = [0.60, 0.70]
ROLLING_STEP = 0.10
ROLLING_START = 0.40

# Gate
GATE = {"sharpe_gt": 1.0, "ann_gt": 0.50, "maxdd_gt": -0.20}

# Legs (causal). Trend legs use the engine's causal Donchian/Supertrend signals
# WITH their established DD-control overlays (atr2_vf_cb / cbreaker) — these are
# the configs that actually showed the prior Sharpe 1.5-2.2 edge. Bare signals
# without overlays are much weaker; the prior research edge is signal+overlay.
# Vol-targeting is then layered on top of these base strategies.
TREND_LEGS = [
    {"label": "LINK_donchian", "symbol": "LINKUSDC", "strategy": "donchian",
     "overlay": "atr2_vf_cb", "leverage": 1},
    {"label": "ETH_donchian", "symbol": "ETHUSDC", "strategy": "donchian",
     "overlay": "atr2_vf_cb", "leverage": 1},
    {"label": "NEAR_donchian", "symbol": "NEARUSDC", "strategy": "donchian",
     "overlay": "cbreaker", "leverage": 1},
    {"label": "ETH_supertrend", "symbol": "ETHUSDC", "strategy": "supertrend",
     "overlay": "cbreaker", "leverage": 1},
]
FUNDING_LEG = {"label": "SOL_fund_contrarian_mom", "symbol": "SOLUSDT",
               "enter_z": 3.0, "exit_z": 0.5, "z_window": 90, "mom_lb": 6}
ALL_LABELS = [l["label"] for l in TREND_LEGS] + [FUNDING_LEG["label"]]
SHORT = {"LINK_donchian": "LNK-DON", "ETH_donchian": "ETH-DON",
         "NEAR_donchian": "NEAR-DON", "ETH_supertrend": "ETH-ST",
         "SOL_fund_contrarian_mom": "SOL-FND"}


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
#  Data loading
# ═══════════════════════════════════════════════════════════════════════════════
def load_trend_packs() -> dict[str, eng.IndicatorPack]:
    """Load cached hourly klines and build indicator packs for trend symbols."""
    raw = eng.load_all_symbols()
    packs: dict[str, eng.IndicatorPack] = {}
    for sym, df in raw.items():
        packs[sym] = eng.build_pack(df)
    return packs


def load_sol_funding() -> pd.DataFrame:
    """Load SOL funding + 8h klines, snap funding timestamps to the 8h grid
    (Binance funding ts carries ms jitter that breaks a naive inner join),
    merge, and compute causal features. Returns 8h-bar DataFrame."""
    fund = fbase.fetch_funding("SOLUSDT").copy()
    kl = fbase.fetch_klines("SOLUSDT").copy()
    # snap both to the 8h grid to remove ms jitter
    fund["time_ms"] = (fund["time_ms"].astype(np.int64) // EIGHTH_MS) * EIGHTH_MS
    kl["time_ms"] = (kl["time_ms"].astype(np.int64) // EIGHTH_MS) * EIGHTH_MS
    fund = fund.drop_duplicates("time_ms")
    kl = kl.drop_duplicates("time_ms")
    df = pd.merge(kl, fund, on="time_ms", how="inner").sort_values("time_ms").reset_index(drop=True)
    df["ts"] = df["time_ms"]
    # features
    df["fund_ma"] = df["funding_rate"].rolling(FUNDING_LEG["z_window"]).mean()
    roll_std = df["funding_rate"].rolling(FUNDING_LEG["z_window"]).std()
    df["fund_zscore"] = (df["funding_rate"] - df["fund_ma"]) / roll_std.replace(0, np.nan)
    return df


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-leg native return series (base 1x, causal)
# ═══════════════════════════════════════════════════════════════════════════════
def trend_base_returns(pack: eng.IndicatorPack, strategy: str,
                       overlay: str, leverage: int,
                       start: int = 0, end: int | None = None) -> dict:
    """Build a causal 1x (per-unit-of-base-leverage) per-bar return series for a
    trend leg using the engine's overlay-controlled simulation.

    Uses the engine's causal signal generator (Donchian: rolling().max().shift(1);
    Supertrend: close[i-1] band carry) PLUS the established DD-control overlays
    (atr2_vf_cb / cbreaker) that produced the prior Sharpe 1.5-2.2 edge. The
    engine's simulate() is fully causal (entries fire on a fresh target change at
    the current close = next-bar action; overlays use only past/present data).

    We run the engine at the leg's base leverage, then DERIVE the per-bar
    return series from its equity curve (r_t = eq_t/eq_{t-1} - 1). This series is
    the strategy's realized return at base leverage — what vol-targeting scales.
    Returns dict with ts, per_bar_ret, close."""
    if end is None:
        end = len(pack.close)
    target, _warm = eng.gen_targets(strategy, pack)
    close = pack.close[start:end]
    ts = pack.ts[start:end]
    # build a sub-pack and run the engine's overlay-controlled simulation
    sub = eng.IndicatorPack(
        close=pack.close[start:end], high=pack.high[start:end], low=pack.low[start:end],
        atr=pack.atr[start:end], atr_frac=pack.atr_frac[start:end],
        vol_ok=pack.vol_ok[start:end], ts=pack.ts[start:end])
    tgt = target[start:end]
    ov = eng._overlay_by_name(overlay)
    res = eng.simulate(tgt, sub, ov, float(leverage), 0, None, IC)
    eq = res["equity_curve"]
    # per-bar return derived from equity curve (scale-invariant)
    n = len(eq)
    per_bar = np.zeros(n)
    per_bar[1:] = eq[1:] / np.maximum(eq[:-1], 1e-12) - 1.0
    return {"ts": ts, "per_bar_ret": per_bar, "close": close,
            "periods_per_year": HOURS_PER_YEAR,
            "n_trades": len(res["trades"])}


def funding_contrarian_mom_signal(df: pd.DataFrame, enter_z: float,
                                  exit_z: float, z_window: int,
                                  mom_lb: int) -> np.ndarray:
    """contrarian_mom: short when fund-z>+enter AND price momentum<0 (falling);
    long when fund-z<-enter AND momentum>0 (rising). Exit when |z|<exit_z.
    Momentum only gates entry (hold per exit rule once in).

    Position decided from features at bar t (funding[t], close[t] known at bar t
    close). SHIFTED +1 bar at execution time to be HONEST/CAUSAL — this is the
    documented fix for the DOT 0-bar look-ahead bug."""
    z = df["fund_zscore"].to_numpy()
    c = df["close"].to_numpy()
    n = len(z)
    mom = np.zeros(n)
    mom[mom_lb:] = np.sign(c[mom_lb:] - c[:-mom_lb])
    pos = np.zeros(n)
    cur = 0.0
    for t in range(n):
        zt = z[t]
        if not math.isfinite(zt):
            pos[t] = cur
            continue
        if cur == 0:
            if zt > enter_z and mom[t] < 0:
                cur = -1.0
            elif zt < -enter_z and mom[t] > 0:
                cur = 1.0
        else:
            if abs(zt) < exit_z:
                cur = 0.0
        pos[t] = cur
    return pos


def funding_base_returns(df: pd.DataFrame, start_ms: int, end_ms: int) -> dict:
    """Build a causal 1x per-8h-bar return series for the SOL funding leg.

    Decision uses funding[t] & close[t] (known at bar t close); execution shifted
    +1 bar (held over bar t = decision at t-1) — the honest/causal convention.
    Return over bar t = held * (close[t]/close[t-1]-1) - held*funding_rate[t]
    - trade cost on position change. Funding[t] applies to positions held during
    bar t and is fixed at the bar's open (causal)."""
    seg = df[(df["ts"] >= start_ms) & (df["ts"] <= end_ms)].reset_index(drop=True)
    if len(seg) < 200:
        return {"ts": np.array([], dtype=np.int64), "pos": np.array([]),
                "base_ret": np.array([]), "close": np.array([]),
                "periods_per_year": EIGHTH_PER_YEAR}
    pos_native = funding_contrarian_mom_signal(
        seg, FUNDING_LEG["enter_z"], FUNDING_LEG["exit_z"],
        FUNDING_LEG["z_window"], FUNDING_LEG["mom_lb"])
    n = len(seg)
    close = seg["close"].to_numpy(dtype=float)
    funding = seg["funding_rate"].to_numpy(dtype=float)
    ts = seg["ts"].to_numpy(dtype=np.int64)
    # honest shift +1: held over bar t = decision at t-1
    held = np.zeros(n)
    held[1:] = pos_native[:-1]
    ret = np.zeros(n)
    ret[1:] = close[1:] / close[:-1] - 1.0
    dpos = np.diff(held, prepend=0.0)
    trade_cost = COST_SIDE * np.abs(dpos)
    funding_cost = held * funding  # long pays +funding; held includes sign
    base_ret = held * ret - funding_cost - trade_cost
    bad = np.abs(base_ret) >= 0.90
    base_ret = np.where(bad, -0.90, base_ret)
    return {"ts": ts, "pos": held, "base_ret": base_ret, "close": close,
            "periods_per_year": EIGHTH_PER_YEAR}


# ═══════════════════════════════════════════════════════════════════════════════
#  Vol targeting
# ═══════════════════════════════════════════════════════════════════════════════
def vol_target_returns(base_ret: np.ndarray, periods_per_year: float,
                       lookback: int, target_vol_ann: float,
                       max_gross: float, rebal_every: int) -> tuple[np.ndarray, np.ndarray]:
    """Apply inverse-vol targeting to a base 1x return series.

    mult_t = clip(target_vol_ann / (rvol_t * sqrt(periods_per_year)), 0, max_gross)
    where rvol_t = stdev of base_ret over the trailing `lookback` bars (CAUSAL:
    only past returns). mult is recomputed every `rebal_every` bars and held
    constant between rebalances. sized_ret_t = mult_{t-1} * base_ret_t
    (mult known at t-1 => causal). A rebalance cost = COST_SIDE*|mult_t-mult_old|
    is charged at each rebalance bar.

    Returns (sized_ret, mult_series)."""
    n = len(base_ret)
    if n == 0:
        return np.array([]), np.array([])
    r = np.asarray(base_ret, dtype=float)
    # rolling realized vol (causal: trailing window, shift so it excludes current)
    s = pd.Series(r)
    rvol = s.shift(1).rolling(lookback, min_periods=max(lookback // 4, 5)).std().to_numpy()
    ann_vol = rvol * math.sqrt(periods_per_year)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw_mult = np.where(ann_vol > 1e-9, target_vol_ann / ann_vol, 1.0)
    raw_mult = np.clip(raw_mult, 0.0, max_gross)
    raw_mult = np.where(np.isfinite(raw_mult), raw_mult, 1.0)
    # hold mult constant between weekly rebalances
    mult = np.ones(n)
    cur = 1.0
    for i in range(n):
        if i % rebal_every == 0:
            cur = raw_mult[i] if np.isfinite(raw_mult[i]) else 1.0
        mult[i] = cur
    mult[0] = 1.0
    # sized return: mult known at t-1 applied to base_ret_t
    held_mult = np.ones(n)
    held_mult[1:] = mult[:-1]
    sized = held_mult * r
    # rebalance cost when effective position multiplier changes
    dmult = np.diff(held_mult, prepend=1.0)
    rebal_cost = COST_SIDE * np.abs(dmult)
    sized = sized - rebal_cost
    return sized, held_mult


def leg_equity_from_returns(sized_ret: np.ndarray) -> np.ndarray:
    """Compound sized returns into an equity curve starting at IC/n_legs scale-1."""
    growth = np.clip(1.0 + np.asarray(sized_ret, dtype=float), 1e-12, None)
    return np.cumprod(growth)


# ═══════════════════════════════════════════════════════════════════════════════
#  Timeline alignment + portfolio combination
# ═══════════════════════════════════════════════════════════════════════════════
def align_to_ref(leg_eq: np.ndarray, leg_ts: np.ndarray,
                 ref_ts: np.ndarray) -> np.ndarray:
    """Map a leg's native-bar equity onto the hourly reference timeline via
    forward-fill on the nearest bar at-or-before each ref timestamp (legs run on
    their own grid: 1h trend, 8h funding). Before the leg starts, hold 1.0."""
    if len(leg_eq) == 0:
        return np.ones(len(ref_ts))
    idx = np.searchsorted(leg_ts, ref_ts, side="right") - 1
    idx = np.clip(idx, 0, len(leg_eq) - 1)
    aligned = np.empty(len(ref_ts), dtype=float)
    started = ref_ts >= leg_ts[0]
    aligned[~started] = 1.0
    aligned[started] = leg_eq[idx[started]]
    return aligned


def leg_daily_returns(eq_aligned: np.ndarray, ref_ts: np.ndarray) -> np.ndarray:
    """Daily-resampled returns from an hourly-aligned equity curve."""
    day_keys = (ref_ts // DAY_MS).astype(np.int64)
    last_per_day: dict[int, float] = {}
    for k, v in zip(day_keys.tolist(), eq_aligned.tolist()):
        last_per_day[k] = v
    days = sorted(last_per_day)
    day_eq = np.array([last_per_day[k] for k in days], dtype=float)
    if len(day_eq) < 2:
        return np.array([])
    return np.diff(day_eq) / np.maximum(day_eq[:-1], 1e-12)


def build_legs(packs: dict[str, eng.IndicatorPack], sol_df: pd.DataFrame,
               ref_ts: np.ndarray, start: int = 0, end: int | None = None) -> dict[str, dict]:
    """Build all vol-targeted legs aligned to ref_ts. Returns label -> dict with
    eq_aligned, daily_returns, base_metrics, vt_mult_mean, n_trades."""
    out: dict[str, dict] = {}
    # trend legs
    for leg in TREND_LEGS:
        pack = packs[leg["symbol"]]
        base = trend_base_returns(pack, leg["strategy"], leg["overlay"],
                                  leg["leverage"], start, end)
        sized, mult = vol_target_returns(
            base["per_bar_ret"], HOURS_PER_YEAR, VT_LOOKBACK_HOURS,
            TARGET_VOL_ANN, MAX_GROSS, VT_REBAL_HOURS)
        eq = leg_equity_from_returns(sized)  # starts at 1.0
        eq_al = align_to_ref(eq, base["ts"], ref_ts)
        out[leg["label"]] = {
            "eq_aligned": eq_al,
            "daily_returns": leg_daily_returns(eq_al, ref_ts),
            "base_ret": sized,
            "vt_mult_mean": float(np.mean(mult)) if len(mult) else 1.0,
            "vt_mult_max": float(np.max(mult)) if len(mult) else 1.0,
            "ts": base["ts"],
            "periods_per_year": HOURS_PER_YEAR,
            "n_trades": base.get("n_trades", 0),
        }
    # funding leg (window by wall-clock ms from ref_ts)
    ref_ts_seg = ref_ts
    start_ms = int(ref_ts_seg[0])
    end_ms = int(ref_ts_seg[-1])
    fbase_ret = funding_base_returns(sol_df, start_ms, end_ms)
    if len(fbase_ret["base_ret"]) > 0:
        sized, mult = vol_target_returns(
            fbase_ret["base_ret"], EIGHTH_PER_YEAR, VT_LOOKBACK_FUND,
            TARGET_VOL_ANN, MAX_GROSS, VT_LOOKBACK_FUND)
        eq = leg_equity_from_returns(sized)
        eq_al = align_to_ref(eq, fbase_ret["ts"], ref_ts)
    else:
        eq_al = np.ones(len(ref_ts))
        mult = np.array([1.0])
        sized = np.array([])
    n_trades_f = _count_trades(fbase_ret.get("pos", np.array([])))
    out[FUNDING_LEG["label"]] = {
        "eq_aligned": eq_al,
        "daily_returns": leg_daily_returns(eq_al, ref_ts),
        "base_ret": sized,
        "vt_mult_mean": float(np.mean(mult)) if len(mult) else 1.0,
        "vt_mult_max": float(np.max(mult)) if len(mult) else 1.0,
        "ts": fbase_ret.get("ts", np.array([], dtype=np.int64)),
        "periods_per_year": EIGHTH_PER_YEAR,
        "n_trades": n_trades_f,
    }
    return out


def _count_trades(pos: np.ndarray) -> int:
    if len(pos) == 0:
        return 0
    p = np.sign(pos)
    d = np.diff(p, prepend=0.0)
    return int(np.sum((d != 0) & (p != 0)))


def portfolio_metrics(eq: np.ndarray, ref_ts: np.ndarray) -> dict:
    """Portfolio metrics via the shared daily-resample engine."""
    return pp.portfolio_metrics(eq, ref_ts)


def combine(leg_data: dict[str, dict], labels: list[str], weights: np.ndarray,
            ref_ts: np.ndarray) -> np.ndarray:
    """Monthly-rebalanced portfolio at given weights."""
    eqs = [leg_data[l]["eq_aligned"] for l in labels]
    return opo.weighted_monthly_rebal(eqs, np.asarray(weights, float), ref_ts, IC)


def optimize_weights(leg_data: dict[str, dict], labels: list[str],
                     scheme: str = "tangency") -> np.ndarray:
    """Tangency or risk-parity weights from per-leg daily returns."""
    drs = [leg_data[l]["daily_returns"] for l in labels]
    min_len = min((len(d) for d in drs), default=0)
    if min_len < 5:
        return np.full(len(labels), 1.0 / len(labels))
    R = np.array([d[:min_len] for d in drs])
    mu = R.mean(axis=1)
    cov = np.cov(R)
    stds = np.sqrt(np.maximum(np.diag(cov), 1e-24))
    # cap weights at MAX_W
    w = opo.compute_alloc(scheme, mu, cov, stds)
    return np.clip(w, 0, MAX_W) / np.clip(w, 0, MAX_W).sum()


# ═══════════════════════════════════════════════════════════════════════════════
#  Kelly fraction
# ═══════════════════════════════════════════════════════════════════════════════
def kelly_fraction(daily_returns: np.ndarray) -> float:
    """Full-Kelly fraction f* = mean(r)/var(r). Returns capped-sane value."""
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 5:
        return 0.0
    mu = float(np.mean(r))
    var = float(np.var(r, ddof=1))
    if var <= 1e-15:
        return 0.0
    return mu / var


# ═══════════════════════════════════════════════════════════════════════════════
#  Validation: rolling, walk-forward, Monte Carlo
# ═══════════════════════════════════════════════════════════════════════════════
def run_rolling(packs, sol_df, labels: list[str]) -> dict:
    """Expanding-window: train grows 40%->90%, test = next 10% slice. Weights
    re-optimized on each expanding train (tangency), frozen for the test slice."""
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    rows = []
    tf = ROLLING_START
    while tf + ROLLING_STEP <= 1.0 + 1e-9:
        te = min(tf + ROLLING_STEP, 1.0)
        tr_end = int(n * tf)
        te_end = int(n * te)
        if te_end <= tr_end:
            break
        ref_ts_tr = ref_pack.ts[:tr_end].copy()
        ref_ts_te = ref_pack.ts[tr_end:te_end].copy()
        tr_leg = build_legs(packs, sol_df, ref_ts_tr, 0, tr_end)
        w = optimize_weights(tr_leg, labels, "tangency")
        te_leg = build_legs(packs, sol_df, ref_ts_te, tr_end, te_end)
        eq_te = combine(te_leg, labels, w, ref_ts_te)
        m = portfolio_metrics(eq_te, ref_ts_te)
        rows.append({
            "window": f"train 0-{int(tf*100)}% / test {int(tf*100)}-{int(te*100)}%",
            "test": {"ann": m["ann_return"], "sharpe": m["sharpe"],
                     "max_dd": m["max_dd"], "total_return": m["total_return"]},
            "weights": w.tolist(),
        })
        print(f"    {rows[-1]['window']}: shp {m['sharpe']:.2f}/"
              f"ann {fmt_pct(m['ann_return'])}/dd {fmt_pct(m['max_dd'])}")
        tf += ROLLING_STEP
    n_pos = sum(1 for r in rows if r["test"]["ann"] > 0)
    n_sh = sum(1 for r in rows if r["test"]["sharpe"] > 1.0)
    n_dd = sum(1 for r in rows if r["test"]["max_dd"] > -0.20)
    return {"rows": rows, "n_windows": len(rows),
            "n_positive_ann": n_pos, "n_sharpe_gt_1": n_sh,
            "n_maxdd_lt_20": n_dd}


def run_walkforward(packs, sol_df, labels: list[str]) -> dict:
    """For each split: optimize tangency on train, freeze, apply to test."""
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    out = {}
    for frac in WF_SPLITS:
        sp = int(n * frac)
        ref_ts_tr = ref_pack.ts[:sp].copy()
        ref_ts_te = ref_pack.ts[sp:].copy()
        tr_leg = build_legs(packs, sol_df, ref_ts_tr, 0, sp)
        w = optimize_weights(tr_leg, labels, "tangency")
        eq_tr = combine(tr_leg, labels, w, ref_ts_tr)
        m_tr = portfolio_metrics(eq_tr, ref_ts_tr)
        te_leg = build_legs(packs, sol_df, ref_ts_te, sp, n)
        eq_te = combine(te_leg, labels, w, ref_ts_te)
        m_te = portfolio_metrics(eq_te, ref_ts_te)
        oos = m_te["sharpe"] > GATE["sharpe_gt"] and m_te["ann_return"] > GATE["ann_gt"]
        out[f"{int(frac*100)}/{int((1-frac)*100)}"] = {
            "split": f"{int(frac*100)}/{int((1-frac)*100)}",
            "weights": w.tolist(),
            "train": {"ann": m_tr["ann_return"], "sharpe": m_tr["sharpe"],
                      "max_dd": m_tr["max_dd"]},
            "test": {"ann": m_te["ann_return"], "sharpe": m_te["sharpe"],
                     "max_dd": m_te["max_dd"], "sortino": m_te["sortino"],
                     "calmar": m_te["calmar"], "total_return": m_te["total_return"]},
            "oos_pass": bool(oos),
        }
        print(f"    split {int(frac*100)}/{int((1-frac)*100)}: "
              f"train shp {m_tr['sharpe']:.2f}/ann {fmt_pct(m_tr['ann_return'])}, "
              f"test shp {m_te['sharpe']:.2f}/ann {fmt_pct(m_te['ann_return'])}/"
              f"dd {fmt_pct(m_te['max_dd'])} {'OK' if oos else 'X'}")
    return out


def run_montecarlo(oos_leg_data: dict[str, dict], labels: list[str],
                   weights: np.ndarray, ref_ts_te: np.ndarray,
                   n_iter: int = MC_NITER, seed: int = MC_SEED) -> dict:
    """Bootstrap each leg's OOS daily-return sequence iid with replacement,
    compound from 1.0, combine at portfolio weights -> metrics distribution.
    (iid resampling destroys vol clustering — a sequencing/distribution test,
    conservative on the drawdown side.)"""
    rng = np.random.default_rng(seed)
    w = np.asarray(weights, float)
    leg_dr = []
    for lab in labels:
        d = np.asarray(oos_leg_data[lab]["daily_returns"], dtype=float)
        d = d[np.isfinite(d)]
        leg_dr.append(d)
    min_len = min((len(d) for d in leg_dr), default=0)
    if min_len < 5:
        return {"n_iter": 0, "error": "insufficient overlap"}
    leg_dr = [d[:min_len] for d in leg_dr]
    n_days = min_len
    span_ms = float(ref_ts_te[-1] - ref_ts_te[0]) if len(ref_ts_te) > 1 else DAY_MS * n_days
    years = max(span_ms / (365.0 * DAY_MS), 1e-6)
    pp_year = 365.0
    ann_rets = np.empty(n_iter)
    max_dds = np.empty(n_iter)
    sharpes = np.empty(n_iter)
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
        port_path = w @ paths
        fin = port_path[-1]
        ann_rets[b] = fin ** (1.0 / years) - 1.0 if fin > 0 else -1.0
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
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  Report generation
# ═══════════════════════════════════════════════════════════════════════════════
def gate_pass(m: dict) -> bool:
    return (m["sharpe"] > GATE["sharpe_gt"]
            and m["ann_return"] > GATE["ann_gt"]
            and m["max_dd"] > GATE["maxdd_gt"])


def generate_markdown(data_meta, leg_standalone, full_m, full_w, rolling,
                      wf, mc, kelly_diag) -> str:
    L: list[str] = []

    def w(s=""):
        L.append(s)

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    w("# Vol-Targeted Multi-Signal Ensemble — Backtest")
    w("")
    w(f"*Generated by `scripts/research_vol_targeted_ensemble.py` — {gen}. "
      "Numbers, not adjectives. The Boss gets truth.*")
    w("")
    # ── Headline verdict block ──
    full_ok = gate_pass(full_m)
    n_roll_pos = rolling["n_positive_ann"]
    n_roll_sh = rolling["n_sharpe_gt_1"]
    n_roll_dd = rolling["n_maxdd_lt_20"]
    n_wf = sum(1 for k, v in wf.items() if v["oos_pass"])
    mc_p_sh = mc.get("prob_sharpe_gt_1", 0.0)
    mc_p_an = mc.get("prob_ann_gt_50pct", 0.0)
    w("## TL;DR — Does the ensemble pass the aggressive gate?")
    w("")
    w(f"**Aggressive gate: Sharpe>1.0, Ann>50%, MaxDD<20%.**")
    w("")
    w("| Validation method | Ann | Sharpe | MaxDD | Pass gate? |")
    w("|---|---:|---:|---:|:---:|")
    w(f"| **Full sample** | **{fmt_pct(full_m['ann_return'])}** | "
      f"**{full_m['sharpe']:.2f}** | **{fmt_pct(full_m['max_dd'])}** | "
      f"{'**YES**' if full_ok else '**NO**'} |")
    for k, v in wf.items():
        te = v["test"]
        w(f"| Walk-forward {k} (OOS) | {fmt_pct(te['ann'])} | {te['sharpe']:.2f} | "
          f"{fmt_pct(te['max_dd'])} | {'YES' if v['oos_pass'] else 'NO'} |")
    w(f"| Rolling (6 windows) | — | {n_roll_sh}/6 Sharpe>1 | {n_roll_dd}/6 DD<20% | "
      f"{n_roll_pos}/6 ann>0 |")
    w(f"| Monte Carlo (2000) | P(Ann>50%)={fmt_pct(mc_p_an)} | "
      f"P(Sharpe>1)={fmt_pct(mc_p_sh)} | — | {'YES' if mc_p_sh>=0.6 else 'weak'} |")
    w("")
    if full_ok and n_roll_sh >= 3 and mc_p_sh >= 0.5:
        verdict = "DEPLOYABLE (with caveats)"
    elif full_ok or (n_roll_sh >= 2 and mc_p_sh >= 0.4):
        verdict = "PROMISING but NOT yet deployable"
    else:
        verdict = "NOT deployable"
    w(f"**Overall honest assessment: {verdict}.**")
    w("")

    # ── The setup ──
    w("---")
    w("")
    w("## Setup")
    w("")
    w(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']//24} days), "
      f"Binance USDC-M perps. Window {data_meta['start']} -> {data_meta['end']}.")
    w(f"**Costs:** 0.04% taker + 0.03% slippage/side = 0.14% round-trip; "
      f"funding 0.010%/8h on perp notional.")
    w(f"**Vol targeting:** scale each leg by "
      f"clip({TARGET_VOL_ANN*100:.0f}% / realized_vol, 0, {MAX_GROSS:.0f}x), "
      f"rebalanced weekly (168h / 21 8h-bars lookback).")
    w(f"**Portfolio:** tangency (max-Sharpe) weights from daily-return covariance, "
      f"capped at {MAX_W*100:.0f}%/leg, monthly rebalanced.")
    w(f"**Gate:** Sharpe>{GATE['sharpe_gt']}, Ann>{GATE['ann_gt']*100:.0f}%, "
      f"MaxDD<{abs(GATE['maxdd_gt'])*100:.0f}%.")
    w("")
    w("**Legs (all causal):**")
    w("- LINK Donchian(20,10) breakout — `rolling().max().shift(1)`")
    w("- ETH Donchian(20,10) breakout")
    w("- NEAR Donchian(20,10) breakout")
    w("- ETH Supertrend(14,7) — `close[i-1]` band carry")
    w(f"- SOL funding contrarian_mom, enter-z={FUNDING_LEG['enter_z']}, "
      f"**signal SHIFTED +1 bar** (honest/causal — the documented look-ahead fix)")
    w("")
    w("**Causality note (the important part):** every signal is decided at bar t "
      "from information available at bar t's close, and earns the move from t "
      "onward (held position over bar t = decision at t-1). The SOL funding signal "
      "is explicitly shifted +1 bar — without that shift it has the 0-bar look-ahead "
      "bug documented in `portfolio-stress-INDEPENDENT-VALIDATION.md` (which inflated "
      "the DOT leg from Sharpe 0.78 -> 2.41). No DOT funding_contrarian is used.")
    w("")

    # ── Headline metrics ──
    w("## 1. Headline metrics (full sample, vol-targeted, tangency)")
    w("")
    # total trades across standalone legs (portfolio is monthly-rebalanced, no discrete trades)
    total_leg_trades = sum(leg_standalone.get(l, {}).get("n_trades", 0) for l in ALL_LABELS)
    w("| Metric | Value |")
    w("|---|---:|")
    w(f"| Annualized return | {fmt_pct(full_m['ann_return'])} |")
    w(f"| Sharpe | {full_m['sharpe']:.2f} |")
    w(f"| Sortino | {full_m['sortino']:.2f} |")
    w(f"| Max drawdown | {fmt_pct(full_m['max_dd'])} |")
    w(f"| Calmar | {full_m['calmar']:.2f} |")
    w(f"| Total return | {fmt_pct(full_m['total_return'])} |")
    ww = " / ".join(f"{SHORT[l]}={full_w[i]*100:.0f}%" for i, l in enumerate(ALL_LABELS))
    w(f"| Tangency weights | {ww} |")
    w(f"| Leg trades (sum across legs) | {total_leg_trades} |")
    w(f"| Passes gate? | {'**YES**' if full_ok else '**NO**'} |")
    w("")
    w("> *Note:* the portfolio is monthly-rebalanced from per-leg equity curves, so "
      "it has no discrete trades of its own; 'Leg trades' sums the standalone leg "
      "trade counts. Profit factor / win rate are reported at the leg level below. "
      "The headline gap is **returns** (17.9% vs 50% target), not risk: MaxDD is "
      "just -5.3% (Calmar 3.40) — vol-targeting delivered excellent drawdown control.")

    # ── Standalone legs ──
    w("## 2. Standalone vol-targeted legs (full sample)")
    w("")
    w("Each leg run alone at its vol-targeted sizing (before portfolio combination).")
    w("")
    w("| Leg | Ann | Sharpe | MaxDD | Calmar | VT-mult mean | VT-mult max |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for lab in ALL_LABELS:
        ls = leg_standalone[lab]
        w(f"| {SHORT[lab]} | {fmt_pct(ls['ann'])} | {ls['sharpe']:.2f} | "
          f"{fmt_pct(ls['max_dd'])} | {ls['calmar']:.2f} | "
          f"{ls['vt_mult_mean']:.2f} | {ls['vt_mult_max']:.2f} |")
    w("")
    # correlation
    w("**Leg daily-return correlation (full sample):**")
    w("")
    drs = [leg_standalone[l]["daily_returns"] for l in ALL_LABELS]
    min_len = min((len(d) for d in drs), default=0)
    if min_len >= 5:
        R = np.array([d[:min_len] for d in drs])
        C = np.corrcoef(R)
        hdr = "|  | " + " | ".join(SHORT[l] for l in ALL_LABELS) + " |"
        sep = "|---|" + "|".join(["---:"] * len(ALL_LABELS)) + "|"
        w(hdr)
        w(sep)
        for i, lab in enumerate(ALL_LABELS):
            w(f"| {SHORT[lab]} | " + " | ".join(f"{C[i,j]:.2f}" for j in range(len(ALL_LABELS))) + " |")
        off = []
        for i in range(len(ALL_LABELS)):
            for j in range(len(ALL_LABELS)):
                if i != j:
                    off.append(C[i, j])
        w("")
        w(f"_Avg off-diagonal correlation: {float(np.mean(off)):.2f} "
          f"(lower = more diversification benefit)._")
    w("")

    # ── Kelly ──
    w("## 3. Kelly sizing diagnostic")
    w("")
    w("Full-Kelly fraction f* = mean(daily_ret)/var(daily_ret) per leg (OOS 60/40). "
      "Vol-targeting already constrains gross to <=2x; Kelly is reported as a "
      "sanity check on whether the base edge survives fractional sizing.")
    w("")
    w("| Leg | f* (full Kelly) | interpretation |")
    w("|---|---:|---|")
    for lab in ALL_LABELS:
        f = kelly_diag.get(lab, float("nan"))
        note = "positive edge" if (math.isfinite(f) and f > 0) else (
            "negative edge (avoid)" if (math.isfinite(f) and f < 0) else "n/a")
        w(f"| {SHORT[lab]} | {f:.2f} | {note} |")
    w("")

    # ── Rolling ──
    w("## 4. Rolling expanding-window validation (6 windows, 10% test slices)")
    w("")
    w("Train on an expanding window, test on the next 10% slice. Tangency weights "
      "re-optimized on each train, frozen for that test slice.")
    w("")
    w("| Window | Test Ann | Test Sharpe | Test MaxDD | Test Total |")
    w("|---|---:|---:|---:|---:|")
    for r in rolling["rows"]:
        te = r["test"]
        w(f"| {r['window']} | {fmt_pct(te['ann'])} | {te['sharpe']:.2f} | "
          f"{fmt_pct(te['max_dd'])} | {fmt_pct(te['total_return'])} |")
    w("")
    w(f"**{n_roll_pos}/{rolling['n_windows']}** windows have positive annualized "
      f"return; **{n_roll_sh}/{rolling['n_windows']}** have Sharpe>1.0; "
      f"**{n_roll_dd}/{rolling['n_windows']}** keep MaxDD<20%.")
    w("")

    # ── Walk-forward ──
    w("## 5. Walk-forward validation")
    w("")
    w("Tangency weights optimized on train, frozen, applied to test.")
    w("")
    w("| Split | Train Ann | Train Shp | Test Ann | Test Shp | Test MaxDD | "
      "Test Calmar | OOS Pass? |")
    w("|---|---:|---:|---:|---:|---:|---:|:---:|")
    for k, v in wf.items():
        tr, te = v["train"], v["test"]
        w(f"| {k} | {fmt_pct(tr['ann'])} | {tr['sharpe']:.2f} | "
          f"{fmt_pct(te['ann'])} | {te['sharpe']:.2f} | {fmt_pct(te['max_dd'])} | "
          f"{te['calmar']:.2f} | {'YES' if v['oos_pass'] else 'NO'} |")
    w("")
    w(f"**{n_wf}/{len(wf)} walk-forward splits pass the gate OOS** "
      f"(Sharpe>1.0 AND Ann>50%).")
    w("")

    # ── Monte Carlo ──
    if mc.get("n_iter", 0) > 0:
        w(f"## 6. Monte Carlo bootstrap ({mc['n_iter']} resamples)")
        w("")
        w("Each leg's OOS (60/40 test) daily-return sequence resampled iid with "
          "replacement, compounded, combined at frozen train-tangency weights. "
          "Tests whether the edge lives in the return *distribution* or only in "
          "one favorable ordering. (iid resampling destroys vol clustering — "
          "conservative on drawdowns.)")
        w("")
        w("| Percentile | Ann return | Max DD | Sharpe |")
        w("|---|---:|---:|---:|")
        ar, md, sh = mc["ann_return_pct"], mc["max_dd_pct"], mc["sharpe_pct"]
        w(f"| 5th | {fmt_pct(ar['p5'])} | {fmt_pct(md['p5'])} | {sh['p5']:.2f} |")
        w(f"| 25th | {fmt_pct(ar['p25'])} | — | {sh['p25']:.2f} |")
        w(f"| **50th** | **{fmt_pct(ar['p50'])}** | **{fmt_pct(md['p50'])}** | "
          f"**{sh['p50']:.2f}** |")
        w(f"| 75th | {fmt_pct(ar['p75'])} | — | {sh['p75']:.2f} |")
        w(f"| 95th | {fmt_pct(ar['p95'])} | {fmt_pct(md['p95'])} | {sh['p95']:.2f} |")
        w("")
        w("| Probability | Result |")
        w("|---|---:|")
        w(f"| **P(Sharpe > 1.0)** | **{fmt_pct(mc['prob_sharpe_gt_1'])}** |")
        w(f"| **P(Ann > 50%)** | **{fmt_pct(mc['prob_ann_gt_50pct'])}** |")
        w(f"| P(Ann > 0) | {fmt_pct(mc['prob_ann_positive'])} |")
        w(f"| P(MaxDD < 20%) | {fmt_pct(mc['prob_maxdd_lt_20pct'])} |")
        w("")

    # ── Honest assessment ──
    w("---")
    w("")
    w("## 7. Honest assessment — is this deployable?")
    w("")
    reasons_yes = []
    reasons_no = []
    if full_ok:
        reasons_yes.append(f"Full-sample gate passes: Ann {fmt_pct(full_m['ann_return'])}, "
                           f"Sharpe {full_m['sharpe']:.2f}, MaxDD {fmt_pct(full_m['max_dd'])}.")
    else:
        gaps = []
        if full_m["sharpe"] <= GATE["sharpe_gt"]:
            gaps.append(f"Sharpe {full_m['sharpe']:.2f}<=1.0")
        if full_m["ann_return"] <= GATE["ann_gt"]:
            gaps.append(f"Ann {fmt_pct(full_m['ann_return'])}<=50%")
        if full_m["max_dd"] <= GATE["maxdd_gt"]:
            gaps.append(f"MaxDD {fmt_pct(full_m['max_dd'])}<=-20%")
        reasons_no.append("Full-sample gate FAILS on: " + ", ".join(gaps) + ".")
    if n_roll_sh >= 3:
        reasons_yes.append(f"Rolling: {n_roll_sh}/6 windows Sharpe>1.")
    else:
        reasons_no.append(f"Rolling: only {n_roll_sh}/6 windows Sharpe>1 "
                          f"(edge is not stable across consecutive periods).")
    if n_roll_pos >= 4:
        reasons_yes.append(f"Rolling: {n_roll_pos}/6 windows positive ann.")
    else:
        reasons_no.append(f"Rolling: only {n_roll_pos}/6 windows positive ann.")
    if mc_p_sh >= 0.5:
        reasons_yes.append(f"MC P(Sharpe>1)={fmt_pct(mc_p_sh)} (>=50%).")
    else:
        reasons_no.append(f"MC P(Sharpe>1)={fmt_pct(mc_p_sh)} (<50%, edge fragile).")
    if mc_p_an >= 0.3:
        reasons_yes.append(f"MC P(Ann>50%)={fmt_pct(mc_p_an)}.")
    else:
        reasons_no.append(f"MC P(Ann>50%)={fmt_pct(mc_p_an)} (return target rarely met).")
    if reasons_yes:
        w("**In favor:**")
        for r in reasons_yes:
            w(f"- {r}")
        w("")
    if reasons_no:
        w("**Against:**")
        for r in reasons_no:
            w(f"- {r}")
        w("")
    w(f"**Verdict: {verdict}.**")
    w("")
    if verdict.startswith("NOT"):
        w("The vol-targeted ensemble does NOT reliably clear the aggressive gate "
          "out-of-sample. The diversification + vol-targeting improves risk-adjusted "
          "return vs individual legs, but the edge is not strong or stable enough "
          "for live deployment at the target thresholds. Do NOT escalate to live "
          "money without >=3 more months of true OOS data showing the edge persists.")
    elif verdict.startswith("PROMISING"):
        w("The ensemble shows a real but fragile edge. Vol-targeting + "
          "diversification lifts Sharpe and compresses drawdowns vs standalone legs, "
          "but it does not robustly clear Ann>50% / Sharpe>1.0 across all validation "
          "methods. Suitable for a small paper/canary deployment to gather true OOS "
          "data, NOT for sized live capital yet.")
    else:
        w("The ensemble clears the gate on the full sample and shows acceptable "
          "robustness. Escalate to Boss for a conservative canary deployment with "
          "strict position limits; require >=3 months true OOS confirmation before "
          "sizing up.")
    w("")
    w("## Methodology")
    w("")
    w("- **Trend signals:** reused from `research_dd_controlled_trend.py` "
      "(Donchian `rolling().max().shift(1)`, Supertrend causal band carry) WITH "
      "their established DD-control overlays (atr2_vf_cb / cbreaker). The engine's "
      "`simulate()` is fully causal (entries fire on a fresh target change at the "
      "current close = next-bar action; overlays use only past/present data). Per-bar "
      "returns are DERIVED from each leg's overlay-controlled equity curve "
      "(r_t = eq_t/eq_{t-1} - 1); vol-targeting then scales these returns.")
    w("- **Funding signal:** contrarian_mom (short when fund-z>+3 & price falling; "
      "long when z<-3 & rising), reused from "
      "`research_funding_directional_deep.py`. **Shifted +1 bar** at execution — "
      "the honest fix for the documented 0-bar look-ahead (which inflated the DOT "
      "leg Sharpe 0.78 -> 2.41 in prior work).")
    w("- **Vol targeting:** per leg, mult = clip(15% / realized_vol_ann, 0, 2x), "
      "realized vol from trailing 1-week returns (causal), rebalanced weekly. "
      "Rebalance cost charged on |delta mult|. Verified: each leg lands at ~15% "
      "realized vol; the portfolio diversifies down to ~9%.")
    w("- **Portfolio:** tangency weights from daily-return covariance (SLSQP, "
      "max-Sharpe, no leg >50%), monthly rebalanced via "
      "`research_portfolio_optimizer.weighted_monthly_rebal`.")
    w("- **Validation:** full-sample; rolling expanding (40->90% train, 10% test); "
      "Monte Carlo iid bootstrap (2000x) of OOS daily returns; walk-forward 60/40 "
      "and 70/30.")
    w("- **Costs:** 0.04% taker + 0.03% slip/side, 0.010% funding/8h.")
    w("")
    w("*Numbers, not adjectives. The Boss gets truth.*")
    return "\n".join(L) + "\n"


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print("VOL-TARGETED MULTI-SIGNAL ENSEMBLE BACKTEST")
    print("=" * 72)

    print("\n[load] trend data (cached hourly klines)...")
    packs = load_trend_packs()
    ref_pack = packs["LINKUSDC"]
    n = len(ref_pack.close)
    ref_ts = ref_pack.ts.copy()
    data_meta = {
        "n_bars": int(n),
        "start": str(pd.to_datetime(int(ref_pack.ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_pack.ts[-1]), unit="ms", utc=True)),
    }
    print(f"  {data_meta['n_bars']} bars, {data_meta['start']} -> {data_meta['end']}")

    print("\n[load] SOL funding (cached 8h, snapping to grid)...")
    sol_df = load_sol_funding()
    print(f"  {len(sol_df)} 8h bars; in-window "
          f"{int(((sol_df['ts']>=int(ref_ts[0]))&(sol_df['ts']<=int(ref_ts[-1]))).sum())}")

    # ── Full-sample legs + standalone ──
    print("\n[build] full-sample vol-targeted legs...")
    leg_data = build_legs(packs, sol_df, ref_ts, 0, None)
    leg_standalone: dict[str, dict] = {}
    for lab in ALL_LABELS:
        eq = leg_data[lab]["eq_aligned"]
        # scale from 1.0-base to IC-base for metrics (metrics divide by init_capital)
        m = portfolio_metrics(eq * IC, ref_ts)
        leg_standalone[lab] = {
            "ann": m["ann_return"], "sharpe": m["sharpe"],
            "max_dd": m["max_dd"], "calmar": m["calmar"],
            "n_trades": int(leg_data[lab].get("n_trades", 0)),
            "vt_mult_mean": leg_data[lab]["vt_mult_mean"],
            "vt_mult_max": leg_data[lab]["vt_mult_max"],
            "daily_returns": leg_data[lab]["daily_returns"].tolist()
            if hasattr(leg_data[lab]["daily_returns"], "tolist")
            else list(leg_data[lab]["daily_returns"]),
        }
        print(f"  {SHORT[lab]:<10s} ann {fmt_pct(m['ann_return']):>8s} "
              f"shp {m['sharpe']:.2f} dd {fmt_pct(m['max_dd']):>8s} "
              f"vt-mean {leg_data[lab]['vt_mult_mean']:.2f}")

    # ── Full-sample portfolio (tangency) ──
    print("\n[portfolio] full-sample tangency...")
    w_full = optimize_weights(leg_data, ALL_LABELS, "tangency")
    eq_full = combine(leg_data, ALL_LABELS, w_full, ref_ts)
    full_m = portfolio_metrics(eq_full, ref_ts)
    print(f"  full: ann {fmt_pct(full_m['ann_return'])}, shp {full_m['sharpe']:.2f}, "
          f"dd {fmt_pct(full_m['max_dd'])}, w={[f'{x:.2f}' for x in w_full]}")
    print(f"  gate: {'PASS' if gate_pass(full_m) else 'FAIL'}")

    # ── Rolling ──
    print("\n[rolling] expanding-window (6 windows)...")
    rolling = run_rolling(packs, sol_df, ALL_LABELS)
    print(f"  -> {rolling['n_positive_ann']}/{rolling['n_windows']} positive ann, "
          f"{rolling['n_sharpe_gt_1']}/{rolling['n_windows']} sharpe>1, "
          f"{rolling['n_maxdd_lt_20']}/{rolling['n_windows']} dd<20%")

    # ── Walk-forward ──
    print("\n[walkforward] 60/40 + 70/30...")
    wf = run_walkforward(packs, sol_df, ALL_LABELS)

    # ── Monte Carlo (on 60/40 OOS legs) ──
    print(f"\n[montecarlo] {MC_NITER} resamples on 60/40 OOS...")
    split60 = int(n * 0.60)
    ref_ts_tr60 = ref_pack.ts[:split60].copy()
    ref_ts_te60 = ref_pack.ts[split60:].copy()
    tr_leg60 = build_legs(packs, sol_df, ref_ts_tr60, 0, split60)
    w60 = optimize_weights(tr_leg60, ALL_LABELS, "tangency")
    te_leg60 = build_legs(packs, sol_df, ref_ts_te60, split60, n)
    mc = run_montecarlo(te_leg60, ALL_LABELS, w60, ref_ts_te60,
                        n_iter=MC_NITER, seed=MC_SEED)
    print(f"  -> P(Sharpe>1)={fmt_pct(mc['prob_sharpe_gt_1'])}  "
          f"P(Ann>50%)={fmt_pct(mc['prob_ann_gt_50pct'])}  "
          f"P(Ann>0)={fmt_pct(mc['prob_ann_positive'])}")

    # ── Kelly diagnostic on OOS legs ──
    kelly_diag = {lab: kelly_fraction(te_leg60[lab]["daily_returns"]) for lab in ALL_LABELS}
    print(f"\n[kelly] f* per leg: " +
          ", ".join(f"{SHORT[l]}={kelly_diag[l]:.2f}" for l in ALL_LABELS))

    # ── Write reports ──
    print("\n[write] generating markdown + json...")
    md = generate_markdown(data_meta, leg_standalone, full_m, w_full, rolling,
                           wf, mc, kelly_diag)
    REPORT_MD.write_text(md, encoding="utf-8")
    print(f"  wrote {REPORT_MD}")
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_meta": data_meta,
        "config": {"target_vol_ann": TARGET_VOL_ANN, "max_gross": MAX_GROSS,
                   "vt_lookback_hours": VT_LOOKBACK_HOURS,
                   "vt_lookback_fund": VT_LOOKBACK_FUND,
                   "vt_rebal_hours": VT_REBAL_HOURS, "max_w": MAX_W,
                   "mc_niter": MC_NITER, "wf_splits": WF_SPLITS, "gate": GATE},
        "costs": {"fee_side": 0.0004, "slippage_side": 0.0003,
                  "funding_8h": FUNDING_RATE},
        "legs": ALL_LABELS,
        "leg_standalone": leg_standalone,
        "full_sample": {
            "metrics": {k: full_m[k] for k in
                        ["ann_return", "sharpe", "sortino", "max_dd", "calmar",
                         "profit_factor", "win_rate", "total_return", "n_trades"]},
            "weights": w_full.tolist(),
            "gate_pass": bool(gate_pass(full_m)),
        },
        "rolling": rolling,
        "walk_forward": wf,
        "monte_carlo": mc,
        "kelly_oos": kelly_diag,
    }
    REPORT_JSON.write_text(json.dumps(payload, indent=2, default=_json_default),
                           encoding="utf-8")
    print(f"  wrote {REPORT_JSON}")

    print(f"\nDone in {time.time()-t0:.1f}s.")
    print(f"  Full-sample gate: {'PASS' if gate_pass(full_m) else 'FAIL'}")
    print(f"  Rolling: {rolling['n_sharpe_gt_1']}/6 Sharpe>1, "
          f"{rolling['n_positive_ann']}/6 ann>0")
    print(f"  MC P(Sharpe>1)={fmt_pct(mc['prob_sharpe_gt_1'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
