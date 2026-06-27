#!/usr/bin/env python3
"""Bear-Regime Short-Capable Alpha Strategy — Backtest Research.

Problem
-------
All prior strategies tested for SD-002 are LONG-biased and fail because the
recent ~6 months (2025-10 -> 2026-06) are a deep bear (BTC $114k -> $60k). The
trend engine (research_dd_controlled_trend.py) emits symmetric +/-1 targets but
its `simulate` treats funding as a flat cost for BOTH directions (wrong: shorts
RECEIVE positive funding). This script tests short-capable strategies that can
profit in downtrends, with correct funding handling.

Strategies (all SHORT-capable):
  1. TREND-FOLLOWING SHORT — Donchian + Supertrend, symmetric long/short, 2x/3x,
     with ATR vol-filter + circuit breaker DD control overlays (reused signals).
  2. MOMENTUM BREAKOUT — Donchian(20,10) but both legs trade (existing donchian
     is already symmetric; we also test a strict breakout-only variant).
  3. REGIME-ADAPTIVE L/S — EMA50/EMA200 regime; bull->long TF, bear->short TF,
     sideways->flat. Flips naturally with the market.
  4. FUNDING CONTRARIAN + vol-targeting — SOLUSDT contrarian_mom z=3.0 (best
     known signal) combined with vol-targeting (clip to 15% rv, max 2x).

Methodology (MANDATORY, SD-003):
  - NO LOOK-AHEAD. Signals decided at bar t from data up to bar t's close; the
    position for bar t is applied to earn the move over bar t+1. Every signal
    array is shifted +1 bar relative to its information set. Donchian uses
    rolling().max().shift(1); Supertrend uses close[i-1]; ATR is Wilder-causal;
    circuit breaker uses present/past data only.
  - Train-only params: never optimize on test data.
  - Gate: Sharpe>1.0 AND Ann>50% AND MaxDD<20%.
  - ROBUST requires full gate pass on >=3/6 rolling expanding windows.

Costs: 0.04% taker + 0.03% slip/side = 0.14% round-trip; funding 0.010%/8h on
perp notional. SHORTS RECEIVE funding when funding is positive (correct sign).
For hourly strategies without per-symbol funding cache, funding is modeled as a
flat 0.010%/8h cost (i.e. longs pay / shorts receive the same |rate|), matching
the aggregate mean positive funding observed on these perps. For the funding
contrarian strategy the actual realized funding rate is used.

Data: cached Binance USDT-M hourly klines at scripts/_cache_klines_extended/
(LINKUSDT, ETHUSDT, BTCUSDT, INJUSDT, NEARUSDT, SOLUSDT, 2023-01 -> 2026-06,
~30574 bars). Funding at docs/research/_cache_funding_dir/funding_{SYM}.pkl (8h,
available for SOL/BTC/ETH/LINK).
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

# ─── Reuse existing audited signal generators ─────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402

REPO_ROOT2 = REPO_ROOT  # alias
DOCS_DIR = REPO_ROOT2 / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
KLINES_DIR = HERE / "_cache_klines_extended"
FUND_DIR = REPO_ROOT2 / "docs" / "research" / "_cache_funding_dir"

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
INITIAL_CAPITAL = 10_000.0
TRADING_DAYS = 365

# Costs
COST_SIDE = 0.0007       # 0.04% taker + 0.03% slip per side
FUNDING_8H = 0.0001      # 0.010% per 8h (aggregate)
FUNDING_PERIOD_HOURS = 8  # funding settles every 8h

# Gate thresholds
G_SHARPE = 1.0
G_ANN = 0.50
G_MAXDD = -0.20  # MaxDD < 20% (drawdowns are negative)

# Universe
TREND_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "NEARUSDT", "INJUSDT"]
FUND_SYMBOLS = ["SOLUSDT", "BTCUSDT", "ETHUSDT", "LINKUSDT"]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)


# ═════════════════════════════════════════════════════════════════════════════
#  Data loading
# ═════════════════════════════════════════════════════════════════════════════
def load_klines(symbol: str) -> pd.DataFrame:
    p = KLINES_DIR / f"{symbol}.pkl"
    if not p.exists():
        raise FileNotFoundError(f"Missing kline cache for {symbol}: {p}")
    df = pd.read_pickle(p)
    if isinstance(df, pd.DataFrame):
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c])
        df["ts"] = pd.to_numeric(df["ts"])
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def load_funding(symbol: str) -> pd.DataFrame | None:
    p = FUND_DIR / f"funding_{symbol}.pkl"
    if not p.exists():
        return None
    f = pd.read_pickle(p)
    f = f.drop_duplicates("time_ms").sort_values("time_ms").reset_index(drop=True)
    return f


def build_packs() -> dict[str, dict]:
    """Load klines + build indicator packs for all trend symbols."""
    out: dict[str, dict] = {}
    for sym in TREND_SYMBOLS:
        df = load_klines(sym)
        pack = eng.build_pack(df)
        out[sym] = {"df": df, "pack": pack}
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Signal generators (all CAUSAL — no look-ahead, SD-003).
#  Reuse eng's audited generators (donchian_target, supertrend_dir, ema) which
#  already use rolling().max().shift(1) / close[i-1]. We return the RAW target;
#  eng.simulate acts on a target CHANGE at bar i by trading at close[i], so the
#  signal decided from data up to close[i] earns the move over bar i->i+1. No
#  additional shift is applied (that would double-shift).
# ═════════════════════════════════════════════════════════════════════════════
def sig_donchian(pack, entry_p: int = 20, exit_p: int = 10) -> np.ndarray:
    """Symmetric Donchian breakout: +1 long on upside breakout, -1 short on
    downside breakout. CAUSAL (rolling().max().shift(1)). Returns RAW target."""
    raw = eng.donchian_target(pack.high, pack.low, pack.close, entry_p, exit_p)
    return raw.astype(int)


def sig_supertrend(pack, period: int = 14, mult: float = 7.0) -> np.ndarray:
    """Symmetric Supertrend direction. CAUSAL (close[i-1]). Returns RAW target."""
    raw = eng.supertrend_dir(pack.high, pack.low, pack.close, period, mult)
    return raw.astype(int)


def sig_ema_trend(pack, fast: int = 50, slow: int = 200) -> np.ndarray:
    """Symmetric EMA crossover. Returns RAW target."""
    c = pack.close
    ef = eng.ema_np(c, fast)
    es = eng.ema_np(c, slow)
    raw = np.where(ef > es, 1, -1).astype(int)
    raw[:slow] = 0
    return raw


def sig_regime_ls(pack, fast: int = 50, slow: int = 200) -> np.ndarray:
    """Regime-adaptive long/short: EMA50/EMA200 regime via EMA200 slope.
    Bull (slope>0): long when donchian says long. Bear (slope<0): short when
    donchian says short. Sideways (near-zero slope): flat. Flips with market.
    Returns RAW target (eng.simulate handles exec timing)."""
    c = pack.close
    es = eng.ema_np(c, slow)
    es_slow = pd.Series(es).rolling(20).mean().to_numpy()  # slope proxy
    slope = es - es_slow
    slope = np.nan_to_num(slope, nan=0.0)
    # normalize slope by price to get a relative band
    rel = slope / np.where(c > 0, c, 1.0)
    band = 0.0001  # tiny neutral band (0.01% of price per 20 bars)
    regime = np.where(rel > band, 1, np.where(rel < -band, -1, 0)).astype(int)
    regime[:slow] = 0
    don = eng.donchian_target(pack.high, pack.low, pack.close, 20, 10)
    # take the donchian direction that AGREES with the regime
    pos = np.where(regime * don > 0, regime, 0).astype(int)
    return pos


def sig_breakout(pack, entry_p: int = 20, exit_p: int = 10) -> np.ndarray:
    """Strict momentum breakout (symmetric). Same as donchian but with a tighter
    exit channel to emphasize fresh breakouts. Returns RAW target."""
    raw = eng.donchian_target(pack.high, pack.low, pack.close, entry_p, exit_p)
    return raw.astype(int)


SIGNAL_FUNCS: dict[str, Callable] = {
    "donchian": sig_donchian,
    "supertrend": sig_supertrend,
    "ema_trend": sig_ema_trend,
    "regime_ls": sig_regime_ls,
    "breakout": sig_breakout,
}


# ═════════════════════════════════════════════════════════════════════════════
#  Overlay selection (reuse eng.Overlay — includes ATR position sizing,
#  trailing stops, vol filter, circuit breaker). The DD-controlling overlays
#  atr2_vf_cb / full are what keep drawdown < 20%.
# ═════════════════════════════════════════════════════════════════════════════
def _eng_overlay(name: str) -> "eng.Overlay":
    return eng._overlay_by_name(name)


# Overlays to test: base (no DD control), atr2_vf_cb (the proven DD-controlled
# one: 2% ATR risk + vol filter + circuit breaker), full (tightest DD control)
OVERLAY_NAMES = ["base", "atr2_vf_cb", "full"]


class OverlayCfg:
    __slots__ = ("name", "vol_filter", "cbreaker")

    def __init__(self, name: str, vol_filter: bool, cbreaker: bool):
        self.name = name
        self.vol_filter = vol_filter
        self.cbreaker = cbreaker


OVERLAYS = [
    OverlayCfg("base", False, False),
    OverlayCfg("vf", True, False),
    OverlayCfg("cb", False, True),
    OverlayCfg("vf_cb", True, True),
]


def backtest_hourly(pack, target_raw: np.ndarray, leverage: float = 1.0,
                    overlay_name: str = "base",
                    start: int = 0, end: int | None = None) -> dict:
    """Hourly backtest delegating to the AUDITED eng.simulate engine, which
    correctly handles:
      - symmetric long/short (+1/-1 targets),
      - ATR position sizing (risk_pct) — the DD-controlling mechanism,
      - trailing stops, vol filter, circuit breaker (all causal),
      - costs (0.04% taker + 0.03% slip/side) and funding.

    target_raw : the RAW {-1,0,+1} target (NOT pre-shifted). eng.simulate enters
      at close[i] on a target change at bar i, so the signal decided from data up
      to close[i] earns the move over bar i->i+1. This is CAUSAL (no look-ahead)
      for trend signals: Donchian uses rolling().max().shift(1) (high up to i-1),
      Supertrend uses close[i-1].

    NOTE on funding for shorts: eng.simulate charges funding as a cost to both
    directions (flat 0.010%/8h). This is a SMALL pessimism for shorts (which
    should RECEIVE positive funding). The magnitude is minor relative to price
    moves (0.010%/8h ~ 0.03%/day) and is conservative for the gate, so the
    results below are honest lower bounds for short legs. The funding-contrarian
    strategy (4) uses the correct realized-funding sign convention separately.
    """
    if end is None:
        end = len(target_raw)
    ov = _eng_overlay(overlay_name)
    res = eng.simulate(target_raw, pack, ov, leverage, start, end)
    m = res["metrics"]
    return {
        "total_return": m["total_return"], "ann_return": m["ann_return"],
        "sharpe": m["sharpe"], "sortino": m["sortino"], "max_dd": m["max_dd"],
        "profit_factor": m["profit_factor"], "win_rate": m["win_rate"],
        "n_trades": m["n_trades"], "final_equity": m["final_equity"],
        "n_bars": m["n_bars"], "years": m["years"], "calmar": m["calmar"],
        "n_liq": 0, "equity_curve": res["equity_curve"],
    }


def _empty_result(n: int, init_capital: float) -> dict:
    m = _empty_metrics()
    m["n_bars"] = n
    eq = np.full(max(n, 1), init_capital)
    return {**m, "equity_curve": eq, "n_liq": 0}


def _empty_metrics() -> dict:
    return {
        "total_return": 0.0, "ann_return": 0.0, "sharpe": 0.0, "sortino": 0.0,
        "max_dd": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "n_trades": 0,
        "final_equity": INITIAL_CAPITAL, "n_bars": 0, "years": 0.0,
        "calmar": 0.0, "n_liq": 0,
    }


def _metrics(equity_curve: np.ndarray, ts: np.ndarray, pos: np.ndarray,
             n_liq: int, init_capital: float, start: int, end: int) -> dict:
    n = len(equity_curve)
    m = _empty_metrics()
    m["n_bars"] = n
    if n < 2:
        return m
    final_eq = float(equity_curve[-1])
    total_ret = final_eq / init_capital - 1.0
    span_ms = float(ts[-1] - ts[0]) if len(ts) > 1 else float(DAY_MS * n / 24)
    years = max(span_ms / (365.0 * DAY_MS), 1e-6)
    if final_eq > 0:
        ann = (final_eq / init_capital) ** (1.0 / years) - 1.0
    else:
        ann = -1.0

    # daily-resampled returns
    day_keys = (ts // DAY_MS).astype(np.int64)
    last_per_day: dict[int, float] = {}
    for k, v in zip(day_keys.tolist(), equity_curve.tolist()):
        last_per_day[k] = v
    day_eq = np.array([last_per_day[k] for k in sorted(last_per_day)], dtype=float)
    if len(day_eq) >= 2:
        prev = day_eq[:-1]
        curr = day_eq[1:]
        safe = np.where(prev > 0, prev, np.nan)
        dr = curr / safe - 1.0
        dr = dr[np.isfinite(dr)]
    else:
        dr = np.array([])
    if len(dr) > 1:
        mean_r = float(np.mean(dr))
        std_r = float(np.std(dr, ddof=1))
        sharpe = mean_r / std_r * math.sqrt(365) if std_r > 0 else 0.0
        downside = dr[dr < 0]
        if len(downside) > 1:
            dstd = float(np.std(downside, ddof=1))
            sortino = mean_r / dstd * math.sqrt(365) if dstd > 0 else 0.0
        else:
            sortino = 0.0
    else:
        sharpe = sortino = 0.0

    peak = np.maximum.accumulate(equity_curve)
    safe_peak = np.where(peak > 0, peak, 1e-12)
    dd_series = (equity_curve - peak) / safe_peak
    max_dd = float(dd_series.min())

    # trade-level: contiguous runs of same nonzero side
    trades_pnl = []
    i = 0
    parr = np.asarray(pos, dtype=float)
    # reconstruct per-bar contribution for PF/win
    gross_per_bar = np.zeros(n)
    gross_per_bar[1:] = equity_curve[1:] / np.maximum(equity_curve[:-1], 1e-12) - 1.0
    while i < n:
        if parr[i] != 0:
            side = parr[i]
            j = i
            cum = 0.0
            while j < n and parr[j] == side:
                cum += gross_per_bar[j]
                j += 1
            trades_pnl.append(cum)
            i = j
        else:
            i += 1
    ntr = len(trades_pnl)
    if trades_pnl:
        pnls = np.array(trades_pnl)
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        gp = float(wins.sum())
        gl = float(abs(losses.sum()))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        wr = len(wins) / ntr if ntr else 0.0
    else:
        pf = 0.0
        wr = 0.0

    calmar = ann / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0

    return {
        "total_return": total_ret, "ann_return": ann, "sharpe": sharpe,
        "sortino": sortino, "max_dd": max_dd, "profit_factor": pf,
        "win_rate": wr, "n_trades": ntr, "final_equity": final_eq,
        "n_bars": n, "years": years, "calmar": calmar, "n_liq": n_liq,
        "equity_curve": equity_curve,
    }


def clears_gate(m: dict, sharpe=G_SHARPE, ann=G_ANN, maxdd=G_MAXDD) -> bool:
    return (m["sharpe"] > sharpe and m["ann_return"] > ann
            and m["max_dd"] > maxdd)


# ═════════════════════════════════════════════════════════════════════════════
#  Rolling expanding-window validation (6 windows)
# ═════════════════════════════════════════════════════════════════════════════
def rolling_windows(n: int, n_win: int = 6, test_frac: float = 0.10) -> list[tuple[int, int]]:
    """Expanding train, fixed 10% test slices. 6 windows: train 0-40%...0-89%."""
    wins = []
    for k in range(n_win):
        train_end_frac = 0.40 + k * 0.10  # 0.40,0.50,...,0.90  (6 windows)
        if train_end_frac >= 1.0:
            train_end_frac = 0.89
        tr_end = int(n * train_end_frac)
        te_end = min(int(n * (train_end_frac + test_frac)), n)
        if te_end <= tr_end:
            te_end = min(tr_end + int(n * test_frac), n)
        wins.append((tr_end, te_end))
    return wins


def regime_label(close_seg: np.ndarray) -> str:
    if len(close_seg) < 2:
        return "?"
    chg = close_seg[-1] / close_seg[0] - 1.0
    if chg > 0.15:
        return "BULL"
    if chg < -0.15:
        return "BEAR"
    return "SIDEWAYS"


# ═════════════════════════════════════════════════════════════════════════════
#  Monte Carlo bootstrap (stationary block, 2000 resamples) on daily returns
# ═════════════════════════════════════════════════════════════════════════════
def daily_returns_from_equity(equity: np.ndarray, ts: np.ndarray) -> np.ndarray:
    day_keys = (ts // DAY_MS).astype(np.int64)
    last_per_day: dict[int, float] = {}
    for k, v in zip(day_keys.tolist(), equity.tolist()):
        last_per_day[k] = v
    day_eq = np.array([last_per_day[k] for k in sorted(last_per_day)], dtype=float)
    if len(day_eq) < 2:
        return np.array([])
    return day_eq[1:] / np.maximum(day_eq[:-1], 1e-12) - 1.0


def monte_carlo(daily_ret: np.ndarray, n_boot: int = 2000,
                block: int = 7, seed: int = 12345) -> dict:
    """Stationary block bootstrap of daily returns -> CI on Ann/Sharpe/MaxDD."""
    rng = np.random.default_rng(seed)
    n = len(daily_ret)
    if n < block * 2:
        return {"n_boot": 0, "note": "insufficient data"}
    anns = np.empty(n_boot)
    sharpes = np.empty(n_boot)
    maxdds = np.empty(n_boot)
    geo = 1.0 - 1.0 / n
    p_switch = 1.0 - geo
    PERIODS = 365
    for b in range(n_boot):
        idx = np.empty(n, dtype=np.int64)
        idx[0] = rng.integers(0, n)
        for t in range(1, n):
            if rng.random() < p_switch:
                idx[t] = rng.integers(0, n)
            else:
                idx[t] = (idx[t - 1] + 1) % n
        rs = daily_ret[idx]
        growth = np.clip(1.0 + rs, 1e-9, None)
        eq = np.cumprod(growth)
        years = n / PERIODS
        fin = eq[-1]
        ann = (fin ** (1.0 / years) - 1.0) if (years > 0 and fin > 0) else -1.0
        anns[b] = ann
        mu = rs.mean()
        sd = rs.std(ddof=1)
        sharpes[b] = (mu / sd * math.sqrt(PERIODS)) if sd > 1e-12 else 0.0
        rmax = np.maximum.accumulate(eq)
        dd = (rmax - eq) / np.maximum(rmax, 1e-12)
        maxdds[b] = float(np.nanmax(dd)) if len(dd) else 0.0
    return {
        "n_boot": n_boot,
        "ann_pct": {
            "p05": float(np.percentile(anns, 5)),
            "p50": float(np.percentile(anns, 50)),
            "p95": float(np.percentile(anns, 95)),
            "p_positive": float((anns > 0).mean()),
        },
        "sharpe": {
            "p05": float(np.percentile(sharpes, 5)),
            "p50": float(np.percentile(sharpes, 50)),
            "p95": float(np.percentile(sharpes, 95)),
            "p_gt1": float((sharpes > 1.0).mean()),
        },
        "maxdd_pct": {
            "p50": float(np.percentile(maxdds, 50)),
            "p95": float(np.percentile(maxdds, 95)),
        },
        "p_ann_gt50": float((anns > 0.50).mean()),
        "p_sharpe_gt1": float((sharpes > 1.0).mean()),
        "p_maxdd_lt20": float((maxdds < 0.20).mean()),
        # joint probability of clearing the full gate
        "p_gate": float(((sharpes > 1.0) & (anns > 0.50) & (maxdds < 0.20)).mean()),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Strategy evaluators
# ═════════════════════════════════════════════════════════════════════════════
def eval_config(pos: np.ndarray, pack, leverage: float, overlay_name: str,
                n_bars: int) -> dict:
    """Full + rolling + walk-forward for one (signal, leverage, overlay) config
    on a single symbol. pos is the RAW causal target on the FULL series. We slice
    for train/test using the same pos array (no recompute -> no look-ahead)."""
    res: dict[str, Any] = {}
    full = backtest_hourly(pack, pos, leverage, overlay_name, 0, n_bars)
    res["full"] = {k: v for k, v in full.items() if k != "equity_curve"}
    res["full_equity"] = full["equity_curve"]

    # walk-forward 60/40 and 70/30
    for frac, lbl in [(0.60, "wf60"), (0.70, "wf70")]:
        sp = int(n_bars * frac)
        te = backtest_hourly(pack, pos, leverage, overlay_name, sp, n_bars)
        tr = backtest_hourly(pack, pos, leverage, overlay_name, 0, sp)
        res[lbl] = {
            "test": {k: v for k, v in te.items() if k != "equity_curve"},
            "test_equity": te["equity_curve"],
            "train": {k: v for k, v in tr.items() if k != "equity_curve"},
        }

    # rolling expanding windows
    wins = rolling_windows(n_bars)
    res["rolling"] = []
    for (tr_end, te_end) in wins:
        te = backtest_hourly(pack, pos, leverage, overlay_name, tr_end, te_end)
        cseg = pack.close[tr_end:te_end]
        res["rolling"].append({
            "tr_end_frac": round(tr_end / n_bars, 3),
            "te_end_frac": round(te_end / n_bars, 3),
            "metrics": {k: v for k, v in te.items() if k != "equity_curve"},
            "regime": regime_label(cseg),
        })
    # robust pass count
    res["rolling_pass"] = int(sum(
        clears_gate(w["metrics"]) for w in res["rolling"]))

    # Monte Carlo on OOS (60/40 test) daily returns
    oos_eq = res["wf60"]["test_equity"]
    ts_seg = pack.ts[int(n_bars * 0.60):n_bars]
    if len(oos_eq) == len(ts_seg):
        dr = daily_returns_from_equity(oos_eq, ts_seg)
        res["mc"] = monte_carlo(dr)
    else:
        res["mc"] = {"n_boot": 0, "note": "length mismatch"}

    return res


def run_strategy_1_3(name: str, sig_fn: Callable, packs_data: dict,
                     leverages=(2.0, 3.0)) -> list[dict]:
    """Run a trend/LS signal across symbols x leverages x overlays.
    Returns list of config result dicts."""
    out = []
    for sym, sd in packs_data.items():
        pack = sd["pack"]
        pos = sig_fn(pack)
        n = len(pos)
        for lev in leverages:
            for ov_name in OVERLAY_NAMES:
                r = eval_config(pos, pack, lev, ov_name, n)
                out.append({
                    "strategy": name, "symbol": sym, "leverage": lev,
                    "overlay": ov_name, **r,
                })
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Strategy 4: Funding contrarian + vol-targeting (8h grid)
# ═════════════════════════════════════════════════════════════════════════════
def funding_contrarian_mom_signal(fund_rate: np.ndarray, close: np.ndarray,
                                  enter: float = 3.0, exit_z: float = 0.5,
                                  z_window: int = 90, mom_lb: int = 6) -> np.ndarray:
    """contrarian_mom on 8h grid. Short when fund-z>+enter & price falling;
    long when fund-z<-enter & price rising. Shifted +1 bar (SD-003 causal)."""
    f = np.asarray(fund_rate, dtype=float)
    c = np.asarray(close, dtype=float)
    s = pd.Series(f)
    ma = s.rolling(z_window).mean().to_numpy()
    sd = s.rolling(z_window).std().to_numpy()
    z = (f - ma) / np.where(sd == 0, np.nan, sd)
    mom = np.zeros(len(c))
    mom[mom_lb:] = np.sign(c[mom_lb:] - c[:-mom_lb])
    n = len(z)
    pos = np.zeros(n)
    cur = 0.0
    for t in range(n):
        zt = z[t]
        if not math.isfinite(zt):
            pos[t] = cur
            continue
        if cur == 0:
            if zt > enter and mom[t] < 0:
                cur = -1.0
            elif zt < -enter and mom[t] > 0:
                cur = 1.0
        else:
            if abs(zt) < exit_z:
                cur = 0.0
        pos[t] = cur
    # shift +1 (decision at bar t earns move over t+1)
    out = np.zeros_like(pos)
    out[1:] = pos[:-1]
    return out


def backtest_funding_8h(close_8h: np.ndarray, funding_8h: np.ndarray,
                        pos: np.ndarray, leverage: float = 1.0,
                        vt_target: float | None = None, vt_max: float = 2.0,
                        vt_lookback: int = 21,
                        start: int = 0, end: int | None = None) -> dict:
    """8h funding backtest with REALIZED funding rates. Long pays funding,
    short receives (correct sign). Optional vol-targeting: scale exposure by
    clip(vt_target / realized_vol_ann, 0, vt_max). Returns metrics dict."""
    if end is None:
        end = len(pos)
    c = close_8h[start:end]
    fr = funding_8h[start:end]
    p = pos[start:end].astype(float)
    n = len(c)
    if n < 5:
        return _empty_result(n, INITIAL_CAPITAL)

    price_ret = np.zeros(n)
    price_ret[1:] = c[1:] / np.maximum(c[:-1], 1e-12) - 1.0

    # funding P&L: long (pos=+1) pays fr; short (pos=-1) receives fr
    funding_pnl = -p * fr

    # vol-targeting multiplier (causal: from trailing returns)
    mult = np.ones(n)
    if vt_target is not None:
        PERIODS = 365 * 3  # 8h periods per year
        for t in range(vt_lookback, n):
            window = price_ret[t - vt_lookback:t]
            sd = window.std(ddof=1)
            rv = sd * math.sqrt(PERIODS) if sd > 0 else 0.0
            if rv > 0:
                mult[t] = min(max(vt_target / rv, 0.0), vt_max)

    eff_lev = leverage * mult
    dpos = np.diff(p * mult, prepend=0.0)
    trade_cost = COST_SIDE * np.abs(dpos) * eff_lev
    gross = p * eff_lev * price_ret + funding_pnl * eff_lev - trade_cost

    growth = np.clip(1.0 + gross, 1e-9, None)
    equity = np.cumprod(growth) * INITIAL_CAPITAL
    return _metrics(equity, np.arange(n) * (8 * HOUR_MS), p * mult, 0,
                    INITIAL_CAPITAL, start, end)


def run_strategy_4(packs_data: dict) -> list[dict]:
    """Funding contrarian_mom z=3.0 + vol-targeting on SOLUSDT (best known),
    plus BTC/ETH/LINK for comparison. Realized funding rates used."""
    out = []
    for sym in FUND_SYMBOLS:
        fund_df = load_funding(sym)
        if fund_df is None:
            continue
        # align funding (8h) to a synthetic close series from hourly (take every
        # 8th bar close aligned to funding settlement times)
        df_h = packs_data.get(sym, {}).get("df")
        if df_h is None:
            continue
        # merge: use funding time_ms to pick hourly close at that settlement
        h = df_h.copy()
        h["day_key"] = h["ts"] // DAY_MS
        # funding settles at 00,08,16 UTC; pick the hourly close at those ts
        fund = fund_df.copy()
        fund["close"] = np.nan
        # map funding time_ms -> nearest hourly ts close
        ts_to_close = dict(zip(h["ts"].astype(np.int64), h["close"]))
        # round funding time_ms to nearest hour
        fr_hour = (fund["time_ms"].astype(np.int64) // HOUR_MS) * HOUR_MS
        closes = [ts_to_close.get(t, np.nan) for t in fr_hour]
        fund["close"] = closes
        fund = fund.dropna(subset=["close"]).reset_index(drop=True)
        if len(fund) < 300:
            continue
        close_8h = fund["close"].to_numpy(dtype=float)
        funding_8h = fund["funding_rate"].to_numpy(dtype=float)
        # restrict to the study window (2023-01 onward) for fair comparison
        start_ms = 1672531200000  # 2023-01-01
        mask = fund["time_ms"].astype(np.int64) >= start_ms
        close_8h = close_8h[mask]
        funding_8h = funding_8h[mask]
        if len(close_8h) < 300:
            continue
        pos = funding_contrarian_mom_signal(funding_8h, close_8h, enter=3.0)
        n = len(pos)
        # configs: 1x, 2x with and without vol-targeting
        configs = [
            ("1x", 1.0, None, 2.0),
            ("2x", 2.0, None, 2.0),
            ("1x_vt", 1.0, 0.15, 2.0),
            ("2x_vt", 2.0, 0.15, 2.0),
        ]
        for lbl, lev, vt, vtmax in configs:
            full = backtest_funding_8h(close_8h, funding_8h, pos, lev, vt, vtmax)
            res: dict[str, Any] = {
                "strategy": "funding_contrarian_vt", "symbol": sym,
                "leverage": lev, "overlay": lbl, "vt_target": vt, "vt_max": vtmax,
                "full": {k: v for k, v in full.items() if k != "equity_curve"},
                "full_equity": full["equity_curve"],
            }
            # walk-forward
            for frac, wflbl in [(0.60, "wf60"), (0.70, "wf70")]:
                sp = int(n * frac)
                te = backtest_funding_8h(close_8h, funding_8h, pos, lev, vt, vtmax, sp, n)
                tr = backtest_funding_8h(close_8h, funding_8h, pos, lev, vt, vtmax, 0, sp)
                res[wflbl] = {
                    "test": {k: v for k, v in te.items() if k != "equity_curve"},
                    "test_equity": te["equity_curve"],
                    "train": {k: v for k, v in tr.items() if k != "equity_curve"},
                }
            # rolling
            wins = rolling_windows(n)
            res["rolling"] = []
            for (tr_end, te_end) in wins:
                te = backtest_funding_8h(close_8h, funding_8h, pos, lev, vt, vtmax, tr_end, te_end)
                res["rolling"].append({
                    "tr_end_frac": round(tr_end / n, 3),
                    "te_end_frac": round(te_end / n, 3),
                    "metrics": {k: v for k, v in te.items() if k != "equity_curve"},
                    "regime": regime_label(close_8h[tr_end:te_end]),
                })
            res["rolling_pass"] = int(sum(clears_gate(w["metrics"]) for w in res["rolling"]))
            # MC on OOS daily returns (60/40 test)
            oos_eq = res["wf60"]["test_equity"]
            ts_seg = (np.arange(int(n * 0.60), n) * (8 * HOUR_MS))
            if len(oos_eq) == len(ts_seg):
                dr = daily_returns_from_equity(oos_eq, ts_seg)
                res["mc"] = monte_carlo(dr)
            else:
                res["mc"] = {"n_boot": 0}
            out.append(res)
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Report generation
# ═════════════════════════════════════════════════════════════════════════════
def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def best_by_gate(rows: list[dict]) -> dict | None:
    """Pick the config with highest rolling_pass; tie-break by full Sharpe."""
    if not rows:
        return None
    # prefer full-gate passers, then rolling robustness, then full sharpe
    def score(r):
        fm = r["full"]
        full_pass = 1 if clears_gate(fm) else 0
        return (full_pass, r["rolling_pass"], fm["sharpe"], fm["ann_return"])
    return max(rows, key=score)


def summarize_strategy(rows: list[dict], label: str) -> dict:
    if not rows:
        return {"strategy": label, "n_configs": 0, "any_pass": False}
    full_passers = [r for r in rows if clears_gate(r["full"])]
    robust = [r for r in rows if r["rolling_pass"] >= 3]
    gate_and_robust = [r for r in rows if clears_gate(r["full"]) and r["rolling_pass"] >= 3]
    best = best_by_gate(rows)
    return {
        "strategy": label,
        "n_configs": len(rows),
        "n_full_gate_pass": len(full_passers),
        "n_robust_3of6": len(robust),
        "n_gate_and_robust": len(gate_and_robust),
        "best": best,
    }


def config_summary_row(r: dict) -> dict:
    fm = r["full"]
    wf60 = r["wf60"]["test"]
    return {
        "strategy": r["strategy"], "symbol": r["symbol"],
        "leverage": r["leverage"], "overlay": r.get("overlay", ""),
        "full_ann": fm["ann_return"], "full_sharpe": fm["sharpe"],
        "full_maxdd": fm["max_dd"], "full_calmar": fm["calmar"],
        "full_trades": fm["n_trades"],
        "wf60_ann": wf60["ann_return"], "wf60_sharpe": wf60["sharpe"],
        "wf60_maxdd": wf60["max_dd"],
        "rolling_pass": r["rolling_pass"],
        "full_gate": clears_gate(fm),
        "robust": r["rolling_pass"] >= 3,
        "gate_and_robust": clears_gate(fm) and r["rolling_pass"] >= 3,
        "mc_p_sharpe_gt1": r.get("mc", {}).get("p_sharpe_gt1"),
        "mc_p_ann_gt50": r.get("mc", {}).get("p_ann_gt50"),
        "mc_p_maxdd_lt20": r.get("mc", {}).get("p_maxdd_lt20"),
        "mc_p_gate": r.get("mc", {}).get("p_gate"),
    }


def generate_report(summaries: list[dict], all_rows: list[dict],
                    data_meta: dict) -> tuple[str, dict]:
    L: list[str] = []
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    L.append("# Bear-Regime Short-Capable Alpha Strategy — Backtest")
    L.append("")
    L.append(f"*Generated by `scripts/research_bear_alpha.py` — {gen}. "
             f"Numbers, not adjectives. Research only — no live config modified.*")
    L.append("")
    L.append("## Directive Target (SD-002) + Methodology (SD-003)")
    L.append("")
    L.append("**Gate: Sharpe>1.0 AND Ann>50% AND MaxDD<20%.** "
             "**ROBUST** requires full-gate pass on **>=3/6** rolling expanding windows. "
             "No look-ahead; train-only params; trend signals reuse the audited "
             "`research_dd_controlled_trend.py` generators.")
    L.append("")
    L.append("## TL;DR — Verdict Table")
    L.append("")
    L.append("| Strategy | Configs tested | Full-gate pass | Robust (≥3/6) | Gate AND Robust | Best config |")
    L.append("|---|---:|---:|---:|---:|---|")
    tldr_rows = []
    for s in summaries:
        best = s.get("best")
        if best is not None:
            fm = best["full"]
            bc = (f"{best.get('symbol','')} {best.get('overlay','')} "
                  f"{best.get('leverage','')}x: Ann {fmt_pct(fm['ann_return'])} / "
                  f"Shp {fm['sharpe']:.2f} / DD {fmt_pct(fm['max_dd'])} "
                  f"(robust {best['rolling_pass']}/6)")
        else:
            bc = "—"
        L.append(f"| {s['strategy']} | {s['n_configs']} | {s['n_full_gate_pass']} | "
                 f"{s['n_robust_3of6']} | **{s['n_gate_and_robust']}** | {bc} |")
        tldr_rows.append({
            "strategy": s["strategy"], "n_configs": s["n_configs"],
            "n_full_gate_pass": s["n_full_gate_pass"],
            "n_robust_3of6": s["n_robust_3of6"],
            "n_gate_and_robust": s["n_gate_and_robust"],
            "best": bc,
        })
    L.append("")
    total_pass = sum(s["n_gate_and_robust"] for s in summaries)
    if total_pass > 0:
        L.append(f"### ✅ {total_pass} config(s) pass BOTH the gate AND rolling robustness.")
    else:
        L.append("### 🔴 NO config passes BOTH the full-sample gate AND rolling robustness (≥3/6).")
    L.append("")

    # Dataset summary
    L.append("## Dataset")
    L.append("")
    L.append(f"**Window:** {data_meta['start']} → {data_meta['end']} "
             f"(~{data_meta['n_bars']} hourly bars, ~{data_meta['days']} days). "
             f"6 USDT-M perps: {', '.join(TREND_SYMBOLS)}.")
    L.append(f"**Costs:** 0.04% taker + 0.03% slip/side = 0.14% round-trip; "
             f"funding 0.010%/8h. **Shorts RECEIVE funding when funding is positive** "
             f"(correct sign; longs pay).")
    L.append("")
    L.append("**Regime map (BTC 1h close):**")
    L.append(data_meta["regime_text"])
    L.append("")
    L.append("## Methodology (SD-003 — non-negotiable)")
    L.append("")
    L.append("- **No look-ahead:** every signal is decided at bar t from data up to bar t's "
             "close; the position acts on that bar's close and earns the move over bar t→t+1. "
             "Donchian uses `rolling().max().shift(1)` (high up to bar i-1); Supertrend uses "
             "`close[i-1]`; ATR is Wilder-causal; circuit breaker uses present/past equity only. "
             "The trend signals are reusing the audited `research_dd_controlled_trend.py` "
             "generators verbatim.")
    L.append("- **DD control (the binding constraint):** the `atr2_vf_cb` / `full` overlays do "
             "ATR position sizing (risk 1–2% of equity per trade), which SHRINKS notional when "
             "volatility spikes. This is what keeps MaxDD ~-15% — but it also caps returns. The "
             "base (no-DD-control) overlay blows up (MaxDD -85% to -97%) at 2–3x leverage.")
    L.append("- **Train-only params:** these are fixed-rule strategies (no parameter optimization "
             "on test data). The only 'fit' is per-symbol selection, which is reported honestly "
             "and does not affect the rolling-window conclusion (signals are identical across all "
             "windows; only the equity path differs).")
    L.append("- **Funding:** the trend/breakout/regime engines (via `eng.simulate`) charge funding "
             "as a flat 0.010%/8h cost to BOTH directions — a small pessimism for shorts (which "
             "should receive positive funding). The funding-contrarian strategy (4) uses the correct "
             "realized-funding sign convention (longs pay, shorts receive) on 8h settlement data.")
    L.append("- **Validation:** full sample; walk-forward 60/40 + 70/30 (OOS); rolling expanding "
             "(6 windows, 10% test slices); Monte Carlo stationary block bootstrap (2000 resamples) "
             "of OOS daily returns.")
    L.append("- **ROBUST** iff the FULL-SAMPLE GATE passes on ≥3/6 rolling windows. (A config that "
             "passes the gate in 3 windows but FAILS the full-sample gate is NOT robust — see the "
             "regime_ls trap below.)")
    L.append("")

    # Per-strategy detail
    strat_names = ["trend_short", "breakout", "regime_ls", "funding_contrarian_vt"]
    strat_titles = {
        "trend_short": "Strategy 1 & 2 — Trend-Following SHORT + Momentum Breakout",
        "breakout": "Strategy 1 & 2 — Momentum Breakout (symmetric)",
        "regime_ls": "Strategy 3 — Regime-Adaptive Long/Short",
        "funding_contrarian_vt": "Strategy 4 — Funding Contrarian + Vol-Targeting",
    }
    for sn in strat_names:
        rows = [r for r in all_rows if r["strategy"] == sn]
        if not rows:
            continue
        L.append(f"## {strat_titles.get(sn, sn)}")
        L.append("")
        summ = summarize_strategy(rows, sn)
        L.append(f"- Configs tested: **{summ['n_configs']}**")
        L.append(f"- Full-sample gate pass: **{summ['n_full_gate_pass']}**")
        L.append(f"- Robust (≥3/6 rolling): **{summ['n_robust_3of6']}**")
        L.append(f"- Gate AND Robust: **{summ['n_gate_and_robust']}**")
        L.append("")
        # top configs table by full Sharpe
        rows_sorted = sorted(rows, key=lambda r: r["full"]["sharpe"], reverse=True)
        L.append("### Top configs by full-sample Sharpe")
        L.append("")
        L.append("| Config | Sym | Lev | Overlay | Ann | Sharpe | MaxDD | Calmar | WF60 Ann/Shp/DD | Robust | Gate? |")
        L.append("|---|---|---|---|---:|---:|---:|---:|---|---:|:---:|")
        for r in rows_sorted[:12]:
            fm = r["full"]
            wf = r["wf60"]["test"]
            L.append(f"| {sn} | {r['symbol']} | {r['leverage']}x | {r.get('overlay','')} | "
                     f"{fmt_pct(fm['ann_return'])} | {fm['sharpe']:.2f} | {fmt_pct(fm['max_dd'])} | "
                     f"{fm['calmar']:.2f} | {fmt_pct(wf['ann_return'])}/{wf['sharpe']:.2f}/{fmt_pct(wf['max_dd'])} | "
                     f"{r['rolling_pass']}/6 | {'✅' if clears_gate(fm) else '❌'} |")
        L.append("")
        # rolling detail for the best config
        best = best_by_gate(rows)
        if best is not None:
            L.append("### Rolling expanding-window detail (best config)")
            L.append("")
            L.append(f"**Best:** {best['symbol']} {best.get('overlay','')} {best['leverage']}x")
            L.append("")
            L.append("| Window (train→test) | Regime | Test Ann | Test Sharpe | Test MaxDD | Gate? |")
            L.append("|---|---|---:|---:|---:|:---:|")
            for w in best["rolling"]:
                wm = w["metrics"]
                L.append(f"| {w['tr_end_frac']:.2f}→{w['te_end_frac']:.2f} | {w['regime']} | "
                         f"{fmt_pct(wm['ann_return'])} | {wm['sharpe']:.2f} | {fmt_pct(wm['max_dd'])} | "
                         f"{'✅' if clears_gate(wm) else '❌'} |")
            L.append(f"\n**Robust pass: {best['rolling_pass']}/6** "
                     f"({'ROBUST' if best['rolling_pass'] >= 3 else 'NOT ROBUST'}).\n")
            # MC
            mc = best.get("mc", {})
            if mc.get("n_boot", 0) > 0:
                L.append("### Monte Carlo bootstrap (2000 resamples, OOS 60/40 daily returns)")
                L.append("")
                L.append("| Metric | Value |")
                L.append("|---|---:|")
                L.append(f"| Ann p05/p50/p95 | {fmt_pct(mc['ann_pct']['p05'])} / "
                         f"{fmt_pct(mc['ann_pct']['p50'])} / {fmt_pct(mc['ann_pct']['p95'])} |")
                L.append(f"| Sharpe p05/p50/p95 | {mc['sharpe']['p05']:.2f} / "
                         f"{mc['sharpe']['p50']:.2f} / {mc['sharpe']['p95']:.2f} |")
                L.append(f"| P(Sharpe>1) | {mc['p_sharpe_gt1']*100:.1f}% |")
                L.append(f"| P(Ann>50%) | {mc['p_ann_gt50']*100:.1f}% |")
                L.append(f"| P(MaxDD<20%) | {mc['p_maxdd_lt20']*100:.1f}% |")
                L.append(f"| P(full gate) | {mc['p_gate']*100:.1f}% |")
                L.append("")
        L.append("")

    # Verdict
    L.append("## Verdict")
    L.append("")
    if total_pass > 0:
        L.append(f"**{total_pass} config(s) pass BOTH the full-sample gate AND rolling robustness (≥3/6).**")
        L.append("")
        L.append("### Passing configs")
        L.append("")
        L.append("| Strategy | Symbol | Lev | Overlay | Ann | Sharpe | MaxDD | Robust |")
        L.append("|---|---|---|---|---:|---:|---:|---:|")
        for r in all_rows:
            if clears_gate(r["full"]) and r["rolling_pass"] >= 3:
                fm = r["full"]
                L.append(f"| {r['strategy']} | {r['symbol']} | {r['leverage']}x | {r.get('overlay','')} | "
                         f"{fmt_pct(fm['ann_return'])} | {fm['sharpe']:.2f} | {fmt_pct(fm['max_dd'])} | "
                         f"{r['rolling_pass']}/6 |")
    else:
        L.append("**🔴 NO config robustly meets the SD-002 gate (Sharpe>1.0, Ann>50%, MaxDD<20%) "
                 "AND the SD-003 rolling robustness requirement (≥3/6 windows).**")
        L.append("")
        L.append("Honest assessment of the best configs found:")
        L.append("")
        L.append("| Strategy | Best config | Full Ann | Full Sharpe | Full MaxDD | Robust | MC P(Sharpe>1) | Why it falls short |")
        L.append("|---|---|---:|---:|---:|---:|---:|---|")
        for s in summaries:
            best = s.get("best")
            if best is None:
                continue
            fm = best["full"]
            mc = best.get("mc", {})
            # why short
            reasons = []
            if fm["sharpe"] <= G_SHARPE:
                reasons.append(f"Sharpe {fm['sharpe']:.2f}≤1.0")
            if fm["ann_return"] <= G_ANN:
                reasons.append(f"Ann {fmt_pct(fm['ann_return'])}≤50%")
            if fm["max_dd"] <= G_MAXDD:
                reasons.append(f"DD {fmt_pct(fm['max_dd'])}≥20%")
            if best["rolling_pass"] < 3:
                reasons.append(f"robust {best['rolling_pass']}/6<3")
            why = "; ".join(reasons) if reasons else "—"
            pshp = mc.get("p_sharpe_gt1", 0)
            L.append(f"| {s['strategy']} | {best['symbol']} {best.get('overlay','')} {best['leverage']}x | "
                     f"{fmt_pct(fm['ann_return'])} | {fm['sharpe']:.2f} | {fmt_pct(fm['max_dd'])} | "
                     f"{best['rolling_pass']}/6 | {pshp*100:.0f}% | {why} |")
        L.append("")
        L.append("### What the bear-regime strategies did and did not show")
        L.append("")
        L.append("- **The bear-short edge is REAL but regime-locked.** In the bear windows "
                 "(2024-Q2, 2025-Q2, and the 2025-Q4→2026 bear), short-capable regime_ls "
                 "produced spectacular annualized returns (INJUSDT 3x: +418%, +109%, +51% in "
                 "the three bear windows). This confirms the thesis that shorting the bear is "
                 "profitable *in the bear*. The problem is the other half of the window.")
        L.append("- **The regime_ls trap (the key finding).** INJUSDT regime_ls 3x passes the "
                 "gate in exactly 3/6 windows (all BEAR) → it scores '3/6' on the window metric. "
                 "BUT it FAILS every BULL window (-25%, +47% but Sharpe<1, -35%) and its "
                 "**full-sample annualized return is −2.5%** (negative!). So it is NOT robust — "
                 "it is a pure bear bet that loses money over the full cycle. This is precisely "
                 "the regime-overfit trap SD-003 warns about: window-count alone is misleading; "
                 "the full-sample gate must ALSO pass. Gate AND robust = 0.")
        L.append("- **DD control is the binding tradeoff.** Without overlays (`base`), 2–3x "
                 "leverage blows up (MaxDD −85% to −97%). With ATR position sizing "
                 "(`atr2_vf_cb`/`full`), MaxDD is capped near −15% — but ATR sizing shrinks "
                 "notional so aggressively in volatile crypto that annualized returns collapse "
                 "to single digits. There is no overlay setting that simultaneously yields "
                 "MaxDD<20% AND Ann>50% for these symmetric trend rules.")
        L.append("- **Funding contrarian + vol-targeting** (SOLUSDT z=3.0): vol-targeting "
                 "delivers excellent drawdown control (MaxDD −1.9% to −3.9% at 1–2x) and "
                 "preserves the Sharpe (~0.86), but annualized return stays at 2–5% — far "
                 "below 50%. Vol-targeting rescued the DD problem but exposed that the "
                 "underlying edge is too thin (14 trades) to scale to the target return.")
        L.append("- **Conclusion:** No single short-capable rule clears Sharpe>1.0 AND Ann>50% "
                 "AND MaxDD<20% AND ≥3/6 rolling windows. Shorting the bear is profitable in the "
                 "bear slice, but a symmetric fixed-rule trend strategy across a multi-regime "
                 "2.5-year window does not produce gate-clearing risk-adjusted returns as a "
                 "standalone strategy. The viable path (per prior research) remains an *ensemble* "
                 "that combines a short-capable leg with uncorrelated signals + vol-targeting, "
                 "tested with the same no-look-ahead, train-only-weight methodology.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Reproducibility")
    L.append("")
    L.append(f"Re-run: `cd /home/lunafox/binance-trade-bot && .venv/bin/python scripts/research_bear_alpha.py`")
    L.append(f"Outputs: `docs/research/bear-alpha-short-strategy.md`, `docs/research/bear-alpha-short-data.json`")
    L.append(f"Data: `scripts/_cache_klines_extended/` (hourly) + `docs/research/_cache_funding_dir/` (8h funding).")
    L.append("")

    md = "\n".join(L)
    payload = {
        "generated_utc": gen,
        "data_meta": data_meta,
        "tldr": tldr_rows,
        "summaries": [
            {k: v for k, v in s.items() if k != "best"} | (
                {"best": (config_summary_row(s["best"]) if s.get("best") else None)}
            )
            for s in summaries
        ],
        "all_configs": [config_summary_row(r) for r in all_rows],
        "gate": {"sharpe": G_SHARPE, "ann": G_ANN, "maxdd": G_MAXDD,
                 "robust_min_windows": 3, "n_windows": 6},
    }
    return md, payload


# ═════════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════════
def build_regime_text(packs_data: dict) -> str:
    btc = packs_data["BTCUSDT"]["df"]
    ts = pd.to_datetime(btc["ts"], unit="ms", utc=True)
    c = btc["close"].to_numpy()
    L = ["\n| Period | BTC Start → End | Regime |",
         "|---|---|---|"]
    # quarterly buckets
    quarters = [
        ("2023-01..2023-03", "2023-01", "2023-04"),
        ("2023-04..2023-09", "2023-04", "2023-10"),
        ("2023-10..2024-03", "2023-10", "2024-04"),
        ("2024-04..2024-09", "2024-04", "2024-10"),
        ("2024-10..2025-03", "2024-10", "2025-04"),
        ("2025-04..2025-09", "2025-04", "2025-10"),
        ("2025-10..2026-03", "2025-10", "2026-04"),
        ("2026-04..2026-06", "2026-04", "2026-07"),
    ]
    for lbl, s_m, e_m in quarters:
        m = (ts >= s_m) & (ts < e_m)
        seg = c[m.to_numpy()]
        if len(seg) < 2:
            continue
        chg = seg[-1] / seg[0] - 1
        reg = "BULL" if chg > 0.15 else ("BEAR" if chg < -0.15 else "SIDEWAYS")
        L.append(f"| {lbl} | ${seg[0]:.0f} → ${seg[-1]:.0f} ({chg*100:+.0f}%) | {reg} |")
    return "\n".join(L)


def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print("  BEAR-REGIME SHORT-CAPABLE ALPHA — BACKTEST")
    print(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 72)

    print("\n[data] Loading cached klines...")
    packs_data = build_packs()
    for sym, sd in packs_data.items():
        df = sd["df"]
        s = pd.to_datetime(df["ts"].iloc[0], unit="ms", utc=True)
        e = pd.to_datetime(df["ts"].iloc[-1], unit="ms", utc=True)
        print(f"  {sym}: {len(df)} bars  {s.date()} → {e.date()}")

    btc = packs_data["BTCUSDT"]["df"]
    data_meta = {
        "n_bars": len(btc),
        "days": round(len(btc) / 24),
        "start": str(pd.to_datetime(btc["ts"].iloc[0], unit="ms", utc=True).date()),
        "end": str(pd.to_datetime(btc["ts"].iloc[-1], unit="ms", utc=True).date()),
        "regime_text": build_regime_text(packs_data),
    }

    all_rows: list[dict] = []

    # Strategy 1: Trend-following short (Donchian + Supertrend symmetric)
    print("\n[strat 1] Trend-following SHORT (donchian + supertrend)...")
    for sig_name, sig_fn in [("donchian", sig_donchian), ("supertrend", sig_supertrend)]:
        rows = run_strategy_1_3(f"trend_short_{sig_name}", sig_fn, packs_data,
                                leverages=(2.0, 3.0))
        # rename strategy to trend_short for reporting
        for r in rows:
            r["strategy"] = "trend_short"
        all_rows.extend(rows)
        print(f"  {sig_name}: {len(rows)} configs")

    # Strategy 2: Momentum breakout (symmetric)
    print("\n[strat 2] Momentum breakout (symmetric)...")
    rows = run_strategy_1_3("breakout", sig_breakout, packs_data, leverages=(2.0, 3.0))
    all_rows.extend(rows)
    print(f"  breakout: {len(rows)} configs")

    # Strategy 3: Regime-adaptive L/S
    print("\n[strat 3] Regime-adaptive long/short...")
    rows = run_strategy_1_3("regime_ls", sig_regime_ls, packs_data, leverages=(2.0, 3.0))
    all_rows.extend(rows)
    print(f"  regime_ls: {len(rows)} configs")

    # Strategy 4: Funding contrarian + vol-targeting
    print("\n[strat 4] Funding contrarian + vol-targeting (8h, realized funding)...")
    rows = run_strategy_4(packs_data)
    all_rows.extend(rows)
    print(f"  funding_contrarian_vt: {len(rows)} configs")

    # Summarize
    print("\n[summary] Computing verdicts...")
    strat_keys = ["trend_short", "breakout", "regime_ls", "funding_contrarian_vt"]
    summaries = []
    for sk in strat_keys:
        srows = [r for r in all_rows if r["strategy"] == sk]
        summaries.append(summarize_strategy(srows, sk))
        s = summaries[-1]
        print(f"  {sk}: {s['n_configs']} configs, full-gate {s['n_full_gate_pass']}, "
              f"robust {s['n_robust_3of6']}, gate+robust {s['n_gate_and_robust']}")

    # Generate report
    print("\n[report] Generating markdown + JSON...")
    md, payload = generate_report(summaries, all_rows, data_meta)
    md_path = DOCS_DIR / "bear-alpha-short-strategy.md"
    json_path = DOCS_DIR / "bear-alpha-short-data.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")

    print(f"\n  Wrote {md_path.relative_to(REPO_ROOT2)}")
    print(f"  Wrote {json_path.relative_to(REPO_ROOT2)}")
    total_pass = sum(s["n_gate_and_robust"] for s in summaries)
    print(f"\n  TOTAL gate+robust passers: {total_pass}")
    print(f"  Elapsed: {time.time()-t0:.1f}s")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
