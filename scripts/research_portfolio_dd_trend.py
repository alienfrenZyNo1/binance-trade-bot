#!/usr/bin/env python3
"""Portfolio-Level Backtest of Drawdown-Controlled Trend Following.

Problem
-------
Single-symbol winners from docs/research/dd-controlled-trend-analysis.md have
great risk-adjusted returns (LINK 3x → Sharpe 2.09, NEAR 1x → 1.95, ETH 1x →
1.55, DOGE 1x → 1.71), each with MaxDD ~-15-17%. This script tests whether
combining these uncorrelated legs into a portfolio improves the risk-adjusted
return (diversification benefit: same or higher return, lower drawdown, higher
Sharpe).

Method
------
Each leg is simulated independently with its own equity curve, using the exact
signal + overlay engine from research_dd_controlled_trend.py (reused verbatim:
Donchian(20,10), Supertrend(14,7), atr2_vf_cb, cbreaker, circuit breaker, ATR
position sizing, vol filter, all costs). The legs are combined with equal-
weight capital allocation: each leg starts with INITIAL_CAPITAL / n_legs, and
the portfolio equity at each bar = sum of leg equities. This is equivalent to
fixed fractional allocation with no rebalancing overhead (rebalanced at each
signal within the leg's own simulation). A separate equal-weight rebalanced
variant is also reported.

Combos tested:
  1. LINK 3x + NEAR 1x + DOGE 1x              (all donchian)
  2. LINK 3x + ETH 1x + NEAR 1x + DOGE 1x     (diversified signals)
  3. LINK 2x + NEAR 1x + DOGE 1x              (reduced leverage)
  4. All 7 gate-passing configs equal-weight
  5. LINK 3x + DOT funding-contrarian 1x      (cross-strategy: trend + funding)

For each combo: total/annualized return, Sharpe, Sortino, Max DD, Calmar,
profit factor, win rate, correlation matrix between legs, and walk-forward
(60/40 split) results.

Goal gate: Sharpe > 1.5 AND annualized > 100% AND Max DD < 15%.

Costs: 0.04% taker + 0.03% slippage per side, 0.010% funding/8h.
Data: Binance USDC-M perps public API, 1h candles for trend, 8h for funding.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ─── Reuse the single-symbol engine ───────────────────────────────────────────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402

REPO_ROOT = HERE.parent
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
FUND_CACHE = DOCS_DIR / "_cache_funding_dir"

INITIAL_CAPITAL = eng.INITIAL_CAPITAL  # 10_000
DAY_MS = eng.DAY_MS
FUNDING_RATE = eng.FUNDING_RATE
COST_SIDE = eng.COST_SIDE

WALK_FORWARD_SPLIT = 0.60  # 60% train, 40% test (as requested)


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


# ─── Trend leg definitions ────────────────────────────────────────────────────
# The 7 gate-passing configs from dd-controlled-trend-analysis.md
TREND_LEGS: dict[str, dict] = {
    "LINKUSDC_donchian_atr2_vf_cb_3x": {
        "symbol": "LINKUSDC", "strategy": "donchian", "overlay": "atr2_vf_cb",
        "leverage": 3,
    },
    "LINKUSDC_donchian_atr2_vf_cb_2x": {
        "symbol": "LINKUSDC", "strategy": "donchian", "overlay": "atr2_vf_cb",
        "leverage": 2,
    },
    "LINKUSDC_donchian_atr2_vf_cb_1x": {
        "symbol": "LINKUSDC", "strategy": "donchian", "overlay": "atr2_vf_cb",
        "leverage": 1,
    },
    "NEARUSDC_donchian_cbreaker_1x": {
        "symbol": "NEARUSDC", "strategy": "donchian", "overlay": "cbreaker",
        "leverage": 1,
    },
    "NEARUSDC_donchian_atr2_vf_cb_1x": {
        "symbol": "NEARUSDC", "strategy": "donchian", "overlay": "atr2_vf_cb",
        "leverage": 1,
    },
    "ETHUSDC_supertrend_cbreaker_1x": {
        "symbol": "ETHUSDC", "strategy": "supertrend", "overlay": "cbreaker",
        "leverage": 1,
    },
    "DOGEUSDC_donchian_atr2_vf_cb_1x": {
        "symbol": "DOGEUSDC", "strategy": "donchian", "overlay": "atr2_vf_cb",
        "leverage": 1,
    },
}

# The 7 gate-passing configs, in rank order, for combo #4
ALL_GATE_CONFIG_KEYS = list(TREND_LEGS.keys())


def run_trend_leg(leg_key: str, packs: dict, targets: dict,
                  start: int = 0, end: int | None = None,
                  leg_capital: float = INITIAL_CAPITAL) -> dict:
    """Run one trend leg using the engine's simulate(). Returns equity curve +
    trades + metrics computed at the leg level (with leg_capital as the base)."""
    cfg = TREND_LEGS[leg_key]
    pack = packs[cfg["symbol"]]
    tgt = targets[(cfg["symbol"], cfg["strategy"])]
    ov = eng._overlay_by_name(cfg["overlay"])
    lev = cfg["leverage"]
    res = eng.simulate(tgt, pack, ov, float(lev), start, end, leg_capital)
    return res


# ─── Funding contrarian leg ───────────────────────────────────────────────────
def load_funding_dataset(symbol: str) -> pd.DataFrame:
    """Load the cached funding + 8h kline dataset and compute the contrarian
    z-score signal features (mirrors research_funding_directional.build_dataset).
    Uses 1h->8h resampling is NOT needed: the funding cache already has 8h
    klines aligned to funding settlement times."""
    fund_path = FUND_CACHE / f"funding_{symbol}.pkl"
    kl_path = FUND_CACHE / f"klines_{symbol}.pkl"
    if not fund_path.exists() or not kl_path.exists():
        raise FileNotFoundError(
            f"Funding/kline cache missing for {symbol}: {fund_path}, {kl_path}")
    fund = pd.read_pickle(fund_path)
    kl = pd.read_pickle(kl_path)
    # klines columns: ts, open, high, low, close, volume
    if isinstance(kl, pd.DataFrame):
        kl = kl.rename(columns={kl.columns[0]: "time_ms"})
    else:
        kl = pd.DataFrame(kl, columns=["time_ms", "open", "high", "low", "close", "volume"])
    df = pd.merge(kl, fund, on="time_ms", how="inner").sort_values("time_ms").reset_index(drop=True)
    # features for contrarian z-score
    MA_WINDOW = 90
    ZSCORE_WINDOW = 90
    df["fund_ma"] = df["funding_rate"].rolling(MA_WINDOW).mean()
    roll_std = df["funding_rate"].rolling(ZSCORE_WINDOW).std()
    df["fund_zscore"] = (df["funding_rate"] - df["fund_ma"]) / roll_std.replace(0, np.nan)
    df["price_ret"] = (df["close"] - df["open"]) / df["open"]
    df["ts"] = df["time_ms"]
    return df


def funding_contrarian_signal(df: pd.DataFrame) -> np.ndarray:
    """Short when funding z-score>2.0, long when z<-2.0, exit when |z|<0.5.

    The funding z-score at bar t uses ``funding_rate[t]``, which is only
    knowable at bar t's close. A live bot observes funding at bar t's close
    and can only act from bar t+1, so the z-score is shifted forward by one
    bar (the position decided from bar t's funding data executes from bar
    t+1). This removes the 0-bar look-ahead that would otherwise let the
    backtest earn bar t's own close-open move from information not yet
    available. Shift applied here (in the signal) rather than in
    ``simulate_funding_leg`` so every consumer of the signal is causal.
    """
    z = df["fund_zscore"].to_numpy()
    n = len(z)
    pos = np.zeros(n)
    cur = 0.0
    ENTER = 2.0
    EXIT = 0.5
    for t in range(n):
        # z[t-1] == funding z-score of the PREVIOUS bar, observed at its
        # close, and therefore tradable from bar t's open. z[0] has no
        # predecessor -> flat.
        zt = z[t - 1] if t > 0 else float("nan")
        if math.isnan(zt):
            pos[t] = cur
            continue
        if cur == 0:
            if zt > ENTER:
                cur = -1.0
            elif zt < -ENTER:
                cur = 1.0
        else:
            if abs(zt) < EXIT:
                cur = 0.0
        pos[t] = cur
    return pos


def simulate_funding_leg(df: pd.DataFrame, leverage: float,
                         start: int = 0, end: int | None = None,
                         init_capital: float = INITIAL_CAPITAL) -> dict:
    """Vectorized funding contrarian backtest on 8h bars. Per-period return =
    pos * lev * (price_ret - funding_rate) - trade_cost. Equity compounds.
    Mirrors research_funding_directional.backtest but returns equity curve +
    trades + metrics in the same dict shape as the trend engine."""
    if end is None:
        end = len(df)
    seg = df.iloc[start:end].reset_index(drop=True)
    n = len(seg)
    price_ret = seg["price_ret"].to_numpy(dtype=float)
    funding = seg["funding_rate"].to_numpy(dtype=float)
    ts = seg["ts"].to_numpy(dtype=np.int64)
    pos = funding_contrarian_signal(seg)

    dpos = np.diff(pos, prepend=0.0)
    trade_cost = COST_SIDE * np.abs(dpos) * leverage
    period_ret = pos * leverage * (price_ret - funding) - trade_cost
    # liquidation guard
    liq_mask = (pos != 0) & (leverage * np.abs(price_ret) >= 0.90)
    if liq_mask.any():
        period_ret = np.where(liq_mask, -0.95, period_ret)

    growth = np.clip(1.0 + period_ret, 1e-9, None)
    equity = init_capital * np.cumprod(growth)

    # trades: contiguous runs of same nonzero side
    trades: list[dict] = []
    i = 0
    while i < n:
        if pos[i] != 0:
            side = float(pos[i])
            j = i
            cum = 0.0
            while j < n and pos[j] == side:
                cum += period_ret[j]
                j += 1
            trades.append({
                "side": "long" if side > 0 else "short",
                "pnl": cum * init_capital,  # approximate pnl in currency
                "bars": j - i,
            })
            i = j
        else:
            i += 1

    m = _metrics_from_curve_period(equity, ts, trades, init_capital,
                                   period_ret, periods_per_year=PERIODS_PER_YEAR_F)
    return {"metrics": m, "trades": trades, "equity_curve": equity}


PERIODS_PER_YEAR_F = 3 * 365  # 8h funding bars


# ─── Metrics ──────────────────────────────────────────────────────────────────
def _metrics_from_curve_period(equity_curve: np.ndarray, ts: np.ndarray,
                               trades: list[dict], init_capital: float,
                               period_ret: np.ndarray | None = None,
                               periods_per_year: float = 365.0) -> dict:
    """Compute metrics from an equity curve. If period_ret is given (the per-bar
    returns at the native bar frequency), Sharpe/Sortino are annualized using
    periods_per_year. Otherwise we daily-resample like the trend engine."""
    n = len(equity_curve)
    m = eng._empty_metrics()
    m["n_bars"] = n
    if n < 2:
        return m
    final_eq = float(equity_curve[-1])
    total_ret = final_eq / init_capital - 1.0
    span_ms = float(ts[-1] - ts[0]) if len(ts) > 1 else float(DAY_MS * n / 24)
    years = span_ms / (365.0 * DAY_MS)
    years = max(years, 1e-6)
    if final_eq > 0:
        ann = (final_eq / init_capital) ** (1.0 / years) - 1.0
    else:
        ann = -1.0

    if period_ret is not None and len(period_ret) == n:
        dr = period_ret[np.isfinite(period_ret)]
        pp = periods_per_year
    else:
        # daily resample (trend engine style)
        day_keys = (ts // DAY_MS).astype(np.int64)
        last_per_day: dict[int, float] = {}
        for k, v in zip(day_keys.tolist(), equity_curve.tolist()):
            last_per_day[k] = v
        day_eq = np.array([last_per_day[k] for k in sorted(last_per_day)], dtype=float)
        if len(day_eq) >= 2:
            prev = day_eq[:-1]
            curr = day_eq[1:]
            safe = np.where(prev > 0, prev, np.nan)
            dr = (curr / safe - 1.0)
            dr = dr[np.isfinite(dr)]
        else:
            dr = np.array([])
        pp = 365.0

    if len(dr) > 1:
        mean_r = float(np.mean(dr))
        std_r = float(np.std(dr, ddof=1))
        sharpe = mean_r / std_r * math.sqrt(pp) if std_r > 0 else 0.0
        ann_vol = std_r * math.sqrt(pp)
        downside = dr[dr < 0]
        if len(downside) > 1:
            dstd = float(np.std(downside, ddof=1))
            sortino = mean_r / dstd * math.sqrt(pp) if dstd > 0 else 0.0
        else:
            sortino = 0.0
    else:
        sharpe = sortino = 0.0
        ann_vol = 0.0

    peak = np.maximum.accumulate(equity_curve)
    safe_peak = np.where(peak > 0, peak, 1e-12)
    dd_series = (equity_curve - peak) / safe_peak
    max_dd = float(dd_series.min())

    ntr = len(trades)
    if trades:
        pnls = np.array([t["pnl"] for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]
        gp = float(wins.sum()) if len(wins) else 0.0
        gl = float(abs(losses.sum())) if len(losses) else 0.0
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        wr = len(wins) / ntr if ntr else 0.0
    else:
        pf = 0.0
        wr = 0.0

    calmar = ann / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0
    ret_over_dd = total_ret / abs(max_dd) if abs(max_dd) > 1e-9 else 0.0
    return {
        "total_return": total_ret, "ann_return": ann, "sharpe": sharpe,
        "sortino": sortino, "max_dd": max_dd, "ann_vol": ann_vol,
        "profit_factor": pf, "win_rate": wr, "n_trades": ntr,
        "final_equity": final_eq, "n_bars": n, "years": years,
        "calmar": calmar, "ret_over_dd": ret_over_dd,
    }


def portfolio_metrics(equity_curve: np.ndarray, ts: np.ndarray,
                      leg_returns: dict[str, np.ndarray] | None = None,
                      trades: list[dict] | None = None,
                      init_capital: float = INITIAL_CAPITAL) -> dict:
    """Portfolio-level metrics. Uses daily-resampled returns (trend engine
    style) for Sharpe/Sortino so trend and funding legs are comparable."""
    return _metrics_from_curve_period(equity_curve, ts, trades or [],
                                      init_capital, None, 365.0)


# ─── Leg runner that normalizes equity curves to a common timeline ────────────
def _align_leg_equity(leg_equity: np.ndarray, leg_ts: np.ndarray,
                      ref_ts: np.ndarray, leg_capital: float) -> np.ndarray:
    """Map a leg's equity curve onto the reference (hourly) timeline via
    forward-fill on the nearest bar at-or-before each ref timestamp. Legs run
    on their own bar grid (1h for trend, 8h for funding)."""
    # for each ref_ts, find the largest leg_ts <= ref_ts
    idx = np.searchsorted(leg_ts, ref_ts, side="right") - 1
    idx = np.clip(idx, 0, len(leg_equity) - 1)
    # before the leg starts, hold capital flat (no position)
    aligned = np.empty(len(ref_ts), dtype=float)
    started = ref_ts >= leg_ts[0]
    aligned[~started] = leg_capital
    aligned[started] = leg_equity[idx[started]]
    return aligned


def run_leg(leg_spec: dict, packs: dict, targets: dict,
            funding_data: dict[str, pd.DataFrame],
            ref_ts: np.ndarray, leg_capital: float,
            start: int = 0, end: int | None = None) -> dict:
    """Run a leg (trend or funding), return its equity curve aligned to ref_ts,
    its native equity curve + ts, native returns, and metrics. For windowed
    runs (walk-forward) start/end index into the leg's native bars."""
    if leg_spec["type"] == "trend":
        cfg = TREND_LEGS[leg_spec["key"]]
        pack = packs[cfg["symbol"]]
        # window the pack arrays
        s = start
        e = end if end is not None else len(pack.close)
        # build a sub-pack
        sub = eng.IndicatorPack(
            close=pack.close[s:e], high=pack.high[s:e], low=pack.low[s:e],
            atr=pack.atr[s:e], atr_frac=pack.atr_frac[s:e],
            vol_ok=pack.vol_ok[s:e], ts=pack.ts[s:e])
        tgt_full = targets[(cfg["symbol"], cfg["strategy"])]
        tgt = tgt_full[s:e]
        ov = eng._overlay_by_name(cfg["overlay"])
        res = eng.simulate(tgt, sub, ov, float(cfg["leverage"]), 0, None, leg_capital)
        eq_native = res["equity_curve"]
        ts_native = sub.ts
        trades = res["trades"]
        m = res["metrics"]
    else:  # funding
        df = funding_data[leg_spec["symbol"]]
        # The funding dataset spans 2020→2026 (8h bars) while the trend window
        # is only ~376 days (1h). To make equal-weight allocation fair, the
        # funding leg must START at the trend window start so all legs begin
        # fresh with leg_capital at the same wall-clock time.
        if end is None:
            ref_pack_ts = packs["LINKUSDC"].ts
        else:
            ref_pack_ts = packs["LINKUSDC"].ts[start:end]
        win_start_ms = int(ref_pack_ts[0])
        win_end_ms = int(ref_pack_ts[-1])
        mask = (df["ts"] >= win_start_ms) & (df["ts"] <= win_end_ms)
        df_win = df[mask].reset_index(drop=True)
        if len(df_win) < 10:
            # not enough overlap — flat equity at leg_capital
            eq_native = np.full(len(ref_ts), leg_capital)
            ts_native = np.array([], dtype=np.int64)
            trades = []
            m = eng._empty_metrics()
            m["final_equity"] = leg_capital
        else:
            res = simulate_funding_leg(df_win, float(leg_spec["leverage"]), 0, None, leg_capital)
            eq_native = res["equity_curve"]
            ts_native = df_win["ts"].to_numpy()
            trades = res["trades"]
            m = res["metrics"]

    # native per-bar returns for correlation
    dr_native = np.diff(eq_native, prepend=eq_native[0]) / np.maximum(eq_native, 1e-12)
    # aligned to reference timeline
    eq_aligned = _align_leg_equity(eq_native, ts_native, ref_ts, leg_capital)
    return {
        "eq_native": eq_native, "ts_native": ts_native, "trades": trades,
        "metrics": m, "dr_native": dr_native,
        "eq_aligned": eq_aligned, "leg_capital": leg_capital,
    }


def build_portfolio(legs: list[dict], packs: dict, targets: dict,
                    funding_data: dict[str, pd.DataFrame],
                    init_capital: float = INITIAL_CAPITAL,
                    start: int = 0, end: int | None = None,
                    rebalance: bool = False) -> dict:
    """Run all legs and combine. Equal-weight capital: each leg gets
    init_capital / n_legs. Portfolio equity = sum of aligned leg equities.
    If rebalance=True, periodically rebalance to equal weight (monthly)."""
    n_legs = len(legs)
    leg_capital = init_capital / n_legs
    # reference timeline = the trend 1h grid (use LINK as canonical — all trend
    # symbols share the same hourly grid)
    ref_pack = packs["LINKUSDC"]
    if end is None:
        ref_ts = ref_pack.ts.copy()
    else:
        ref_ts = ref_pack.ts[start:end].copy()

    leg_results = []
    for spec in legs:
        lr = run_leg(spec, packs, targets, funding_data, ref_ts, leg_capital,
                     start, end)
        leg_results.append((spec, lr))

    if not rebalance:
        # simple sum of aligned equity curves
        port_eq = np.zeros(len(ref_ts))
        for _, lr in leg_results:
            port_eq += lr["eq_aligned"]
    else:
        # monthly equal-weight rebalance on the aligned daily grid
        port_eq = _rebalanced_portfolio(leg_results, ref_ts, leg_capital, n_legs)

    port_m = portfolio_metrics(port_eq, ref_ts, None, None, init_capital)

    # correlation matrix between legs (on aligned daily returns)
    leg_names = [_leg_label(spec) for spec, _ in leg_results]
    aligned_daily = []
    for _, lr in leg_results:
        eq = lr["eq_aligned"]
        day_keys = (ref_ts // DAY_MS).astype(np.int64)
        last_per_day: dict[int, float] = {}
        for k, v in zip(day_keys.tolist(), eq.tolist()):
            last_per_day[k] = v
        day_eq = np.array([last_per_day[k] for k in sorted(last_per_day)], dtype=float)
        day_ret = np.diff(day_eq) / np.maximum(day_eq[:-1], 1e-12)
        aligned_daily.append(day_ret)
    corr = _correlation_matrix(aligned_daily, leg_names)

    return {
        "portfolio_metrics": port_m,
        "portfolio_equity": port_eq,
        "leg_results": [
            {
                "label": _leg_label(spec),
                "spec": spec,
                "metrics": lr["metrics"],
                "leg_capital": lr["leg_capital"],
                "n_trades": len(lr["trades"]),
            }
            for spec, lr in leg_results
        ],
        "correlation": corr,
        "leg_daily_returns": dict(zip(leg_names, aligned_daily)),
    }


def _rebalanced_portfolio(leg_results: list, ref_ts: np.ndarray,
                          leg_capital: float, n_legs: int) -> np.ndarray:
    """Monthly equal-weight rebalance. At the start of each month, reset each
    leg's allocation to total/ n_legs based on current leg equity."""
    # stack aligned leg equity curves
    eqs = np.array([lr["eq_aligned"] for _, lr in leg_results])  # (n_legs, T)
    port_eq = np.empty(eqs.shape[1])
    # determine month boundaries
    dt = pd.to_datetime(ref_ts, unit="ms", utc=True)
    month_id = dt.year * 12 + dt.month
    prev_month = None
    # running per-leg capital; rebalance at month change
    leg_cap = np.array([eqs[j, 0] for j in range(n_legs)], dtype=float)
    port_eq[0] = leg_cap.sum()
    for i in range(1, eqs.shape[1]):
        # apply this bar's growth to each leg from previous bar
        growth = np.where(eqs[:, i - 1] > 1e-12, eqs[:, i] / eqs[:, i - 1], 1.0)
        leg_cap = leg_cap * growth
        # rebalance at month boundary
        if month_id[i] != month_id[i - 1]:
            total = leg_cap.sum()
            leg_cap = np.full(n_legs, total / n_legs)
        port_eq[i] = leg_cap.sum()
    return port_eq


def _correlation_matrix(daily_returns_list: list[np.ndarray], names: list[str]) -> dict:
    """Pairwise Pearson correlation on the intersection of valid (finite) bars."""
    n = len(names)
    mat = [[1.0] * n for _ in range(n)]
    # align all to the min length
    min_len = min((len(r) for r in daily_returns_list), default=0)
    if min_len < 2:
        for i in range(n):
            for j in range(n):
                mat[i][j] = 1.0 if i == j else 0.0
        return {"matrix": mat, "names": names}
    arrs = [r[:min_len] for r in daily_returns_list]
    A = np.vstack(arrs)
    # mask non-finite
    finite = np.all(np.isfinite(A), axis=0)
    Af = A[:, finite]
    if Af.shape[1] < 3:
        return {"matrix": mat, "names": names}
    C = np.corrcoef(Af)
    for i in range(n):
        for j in range(n):
            try:
                mat[i][j] = float(C[i, j]) if np.isfinite(C[i, j]) else (1.0 if i == j else 0.0)
            except Exception:
                mat[i][j] = 1.0 if i == j else 0.0
    return {"matrix": mat, "names": names}


def _leg_label(spec: dict) -> str:
    if spec["type"] == "trend":
        return spec["key"]
    return f"{spec['symbol']}_funding_contrarian_{int(spec['leverage'])}x"


# ─── Combo definitions ────────────────────────────────────────────────────────
def trend_leg(key: str) -> dict:
    return {"type": "trend", "key": key}


def funding_leg(symbol: str, leverage: float = 1.0) -> dict:
    return {"type": "funding", "symbol": symbol, "leverage": leverage}


COMBOS: list[dict] = [
    {
        "name": "1. LINK3x + NEAR1x + DOGE1x (all donchian)",
        "short": "combo1_all_donchian",
        "legs": [
            trend_leg("LINKUSDC_donchian_atr2_vf_cb_3x"),
            trend_leg("NEARUSDC_donchian_cbreaker_1x"),
            trend_leg("DOGEUSDC_donchian_atr2_vf_cb_1x"),
        ],
    },
    {
        "name": "2. LINK3x + ETH1x + NEAR1x + DOGE1x (diversified signals)",
        "short": "combo2_diversified",
        "legs": [
            trend_leg("LINKUSDC_donchian_atr2_vf_cb_3x"),
            trend_leg("ETHUSDC_supertrend_cbreaker_1x"),
            trend_leg("NEARUSDC_donchian_cbreaker_1x"),
            trend_leg("DOGEUSDC_donchian_atr2_vf_cb_1x"),
        ],
    },
    {
        "name": "3. LINK2x + NEAR1x + DOGE1x (reduced leverage)",
        "short": "combo3_reduced_lev",
        "legs": [
            trend_leg("LINKUSDC_donchian_atr2_vf_cb_2x"),
            trend_leg("NEARUSDC_donchian_cbreaker_1x"),
            trend_leg("DOGEUSDC_donchian_atr2_vf_cb_1x"),
        ],
    },
    {
        "name": "4. All 7 gate-passing configs equal-weight",
        "short": "combo4_all_gate",
        "legs": [trend_leg(k) for k in ALL_GATE_CONFIG_KEYS],
    },
    {
        "name": "5. LINK3x + DOT funding-contrarian 1x (cross-strategy)",
        "short": "combo5_trend_funding",
        "legs": [
            trend_leg("LINKUSDC_donchian_atr2_vf_cb_3x"),
            funding_leg("DOTUSDT", 1.0),
        ],
    },
]


def walk_forward_combo(combo: dict, packs: dict, targets: dict,
                        funding_data: dict[str, pd.DataFrame]) -> dict:
    """60/40 walk-forward: train on first 60%, test on last 40%. Signals are
    computed on the full series for indicator continuity (same as the engine),
    then the simulation window is split."""
    # use LINK pack length as the canonical split index (trend legs share grid)
    n = len(packs["LINKUSDC"].close)
    split = int(n * WALK_FORWARD_SPLIT)
    train = build_portfolio(combo["legs"], packs, targets, funding_data,
                            INITIAL_CAPITAL, 0, split)
    test = build_portfolio(combo["legs"], packs, targets, funding_data,
                           INITIAL_CAPITAL, split, n)
    # buy & hold benchmark on test (LINK as proxy for the basket's main mover)
    cseg = packs["LINKUSDC"].close[split:n]
    test_bh = float(cseg[-1] / cseg[0] - 1) if len(cseg) > 1 else 0.0
    return {
        "train": {
            "ann_return": train["portfolio_metrics"]["ann_return"],
            "sharpe": train["portfolio_metrics"]["sharpe"],
            "max_dd": train["portfolio_metrics"]["max_dd"],
            "n_trades": sum(l["n_trades"] for l in train["leg_results"]),
        },
        "test": {
            "ann_return": test["portfolio_metrics"]["ann_return"],
            "sharpe": test["portfolio_metrics"]["sharpe"],
            "max_dd": test["portfolio_metrics"]["max_dd"],
            "n_trades": sum(l["n_trades"] for l in test["leg_results"]),
        },
        "test_bh_link": test_bh,
    }


# ─── Report ───────────────────────────────────────────────────────────────────
def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def generate_report(combo_results: list[dict], data_meta: dict) -> tuple[str, dict]:
    L: list[str] = []
    L.append("# Portfolio-Level Drawdown-Controlled Trend Following")
    L.append("")
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L.append(f"**Generated:** {gen}")
    L.append(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']/24:.0f} days) trend, "
             f"Binance USDC-M perps public API. Funding leg uses 8h bars (~{data_meta.get('funding_periods','?')} periods).")
    L.append(f"**Window:** {data_meta['start']} -> {data_meta['end']}")
    L.append(f"**Engine:** Reuses the exact signal + overlay code from "
             f"`scripts/research_dd_controlled_trend.py` (Donchian(20,10), Supertrend(14,7), "
             f"atr2_vf_cb, cbreaker, ATR position sizing, vol filter, circuit breaker).")
    L.append(f"**Method:** Each leg simulated independently with its own equity curve; "
             f"equal-weight capital allocation (each leg gets INITIAL_CAPITAL/n_legs); "
             f"portfolio equity = sum of leg equity curves aligned to the hourly grid. "
             f"Monthly-rebalanced variant also reported.")
    L.append(f"**Costs:** {eng.FEE_RATE*100:.2f}% taker + {eng.SLIPPAGE*100:.2f}% slippage per side, "
             f"{eng.FUNDING_RATE*100:.3f}% funding/8h.")
    L.append(f"**Initial capital:** ${INITIAL_CAPITAL:,.0f}")
    L.append(f"**Walk-forward:** {int(WALK_FORWARD_SPLIT*100)}/{int((1-WALK_FORWARD_SPLIT)*100)} train/test split.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Goal Gate")
    L.append("")
    L.append("**Sharpe > 1.5 AND Annualized > 100% AND Max DD < 15%**")
    L.append("")
    L.append("Single-symbol winners each hover at Sharpe 1.5-2.1 with MaxDD ~-15-17%. "
             "The portfolio thesis: combining uncorrelated legs should preserve the high "
             "returns while lowering drawdown (diversification), pushing Sharpe higher.")
    L.append("")
    L.append("---")
    L.append("")

    # ── Portfolio summary table ──
    L.append("## Portfolio Results (equal-weight, no rebalance)")
    L.append("")
    L.append("| # | Combo | Ann Ret | Sharpe | Sortino | Max DD | Calmar | Profit Factor | Win Rate | Total Ret | Trades |")
    L.append("|---|-------|---------|--------|---------|--------|--------|---------------|----------|-----------|--------|")
    for cr in combo_results:
        m = cr["full"]["portfolio_metrics"]
        nt = sum(l["n_trades"] for l in cr["full"]["leg_results"])
        L.append(f"| {cr['idx']} | {cr['name']} | {_fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | "
                 f"{m['sortino']:.2f} | {_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | "
                 f"{m['profit_factor']:.2f} | {m['win_rate']*100:.0f}% | "
                 f"{_fmt_pct(m['total_return'])} | {nt} |")
    L.append("")

    # ── Monthly rebalanced variant ──
    L.append("## Portfolio Results (monthly equal-weight rebalance)")
    L.append("")
    L.append("| # | Combo | Ann Ret | Sharpe | Max DD | Calmar | Trades |")
    L.append("|---|-------|---------|--------|--------|--------|--------|")
    for cr in combo_results:
        m = cr["rebalanced"]["portfolio_metrics"]
        nt = sum(l["n_trades"] for l in cr["rebalanced"]["leg_results"])
        L.append(f"| {cr['idx']} | {cr['name']} | {_fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | "
                 f"{_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {nt} |")
    L.append("")

    # ── Per-leg breakdown ──
    L.append("## Per-Leg Breakdown")
    L.append("")
    for cr in combo_results:
        L.append(f"### Combo {cr['idx']}: {cr['name']}")
        L.append("")
        L.append("| Leg | Symbol | Strategy | Overlay | Lev | Ann Ret | Sharpe | Max DD | Calmar | PF | Win% | Trades |")
        L.append("|-----|--------|----------|---------|-----|---------|--------|--------|--------|----|------|--------|")
        for leg in cr["full"]["leg_results"]:
            spec = leg["spec"]
            m = leg["metrics"]
            if spec["type"] == "trend":
                tcfg = TREND_LEGS[spec["key"]]
                sym = tcfg["symbol"]; strat = tcfg["strategy"]; ov = tcfg["overlay"]; lev = tcfg["leverage"]
            else:
                sym = spec["symbol"]; strat = "funding_contrarian"; ov = "z2.0"; lev = int(spec["leverage"])
            L.append(f"| {leg['label']} | {sym} | {strat} | {ov} | {lev}x | "
                     f"{_fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | "
                     f"{m['calmar']:.2f} | {m['profit_factor']:.2f} | {m['win_rate']*100:.0f}% | {leg['n_trades']} |")
        L.append("")

    # ── Correlation matrices ──
    L.append("## Leg Correlation Matrices (daily returns)")
    L.append("")
    L.append("Low correlation is the whole point — it's what makes diversification work.")
    L.append("")
    for cr in combo_results:
        names = cr["full"]["correlation"]["names"]
        mat = cr["full"]["correlation"]["matrix"]
        L.append(f"### Combo {cr['idx']}: {cr['name']}")
        L.append("")
        short = [n.replace("USDC_", "_").replace("donchian_", "don_").replace("atr2_vf_cb_", "")
                  .replace("supertrend_", "st_").replace("cbreaker_", "cb_")
                  .replace("USDT_funding_contrarian_1x", "_fundCon") for n in names]
        header = "| Leg | " + " | ".join(short) + " |"
        sep = "|-----|" + "|".join(["-----"] * len(short)) + "|"
        L.append(header)
        L.append(sep)
        for i, nm in enumerate(short):
            row = f"| {nm} | " + " | ".join(f"{mat[i][j]:.2f}" for j in range(len(short))) + " |"
            L.append(row)
        L.append("")
        # avg off-diagonal
        off = []
        for i in range(len(mat)):
            for j in range(len(mat)):
                if i != j:
                    off.append(mat[i][j])
        avg_corr = float(np.mean(off)) if off else 0.0
        L.append(f"_Avg off-diagonal correlation: {avg_corr:.2f}_")
        L.append("")

    # ── Walk-forward ──
    L.append(f"## Walk-Forward Validation ({int(WALK_FORWARD_SPLIT*100)}/{int((1-WALK_FORWARD_SPLIT)*100)} split)")
    L.append("")
    L.append("Train on the first 60% (the bull run), test on the last 40% (the severe bear market). "
             "The out-of-sample window is a hard test — buy & hold of the basket's main mover "
             "(LINK) was deeply negative.")
    L.append("")
    L.append("| # | Combo | Train Ann | Train Shp | Train MaxDD | Test Ann | Test Shp | Test MaxDD | Test B&H(LINK) | Robust? |")
    L.append("|---|-------|-----------|-----------|-------------|----------|----------|------------|----------------|---------|")
    for cr in combo_results:
        w = cr["walk_forward"]
        deg = (w["train"]["sharpe"] - w["test"]["sharpe"]) / abs(w["train"]["sharpe"]) if abs(w["train"]["sharpe"]) > 0.01 else 0
        robust = w["test"]["sharpe"] > 0.0 and w["test"]["ann_return"] > w["test_bh_link"] and deg < 3.0
        L.append(f"| {cr['idx']} | {cr['name']} | {_fmt_pct(w['train']['ann_return'])} | "
                 f"{w['train']['sharpe']:.2f} | {_fmt_pct(w['train']['max_dd'])} | "
                 f"{_fmt_pct(w['test']['ann_return'])} | {w['test']['sharpe']:.2f} | "
                 f"{_fmt_pct(w['test']['max_dd'])} | {_fmt_pct(w['test_bh_link'])} | "
                 f"{'YES' if robust else 'no'} |")
    L.append("")

    # ── Verdict ──
    L.append("## Verdict vs Goal Gate")
    L.append("")
    L.append("| # | Combo | Sharpe>1.5 | Ann>100% | MaxDD<15% | ALL PASS |")
    L.append("|---|-------|------------|----------|-----------|----------|")
    for cr in combo_results:
        m = cr["full"]["portfolio_metrics"]
        s = m["sharpe"] > 1.5
        a = m["ann_return"] > 1.0
        d = m["max_dd"] > -0.15
        L.append(f"| {cr['idx']} | {cr['name']} | {'✅' if s else '❌'} ({m['sharpe']:.2f}) | "
                 f"{'✅' if a else '❌'} ({_fmt_pct(m['ann_return'])}) | "
                 f"{'✅' if d else '❌'} ({_fmt_pct(m['max_dd'])}) | "
                 f"{'✅ **PASS**' if (s and a and d) else '❌'} |")
    L.append("")

    md = "\n".join(L)

    # JSON payload
    payload = {
        "generated": gen,
        "data_meta": data_meta,
        "costs": {
            "fee_side": eng.FEE_RATE, "slippage_side": eng.SLIPPAGE,
            "funding_8h": eng.FUNDING_RATE,
        },
        "initial_capital": INITIAL_CAPITAL,
        "walk_forward_split": WALK_FORWARD_SPLIT,
        "goal_gate": {"sharpe_gt": 1.5, "ann_gt": 1.0, "maxdd_gt": -0.15},
        "combos": [],
    }
    for cr in combo_results:
        m = cr["full"]["portfolio_metrics"]
        rm = cr["rebalanced"]["portfolio_metrics"]
        w = cr["walk_forward"]
        s = m["sharpe"] > 1.5
        a = m["ann_return"] > 1.0
        d = m["max_dd"] > -0.15
        payload["combos"].append({
            "idx": cr["idx"], "name": cr["name"], "short": cr["short"],
            "n_legs": len(cr["full"]["leg_results"]),
            "portfolio": {
                "total_return": m["total_return"], "ann_return": m["ann_return"],
                "sharpe": m["sharpe"], "sortino": m["sortino"], "max_dd": m["max_dd"],
                "calmar": m["calmar"], "profit_factor": m["profit_factor"],
                "win_rate": m["win_rate"], "n_trades": sum(l["n_trades"] for l in cr["full"]["leg_results"]),
            },
            "portfolio_rebalanced": {
                "total_return": rm["total_return"], "ann_return": rm["ann_return"],
                "sharpe": rm["sharpe"], "max_dd": rm["max_dd"], "calmar": rm["calmar"],
            },
            "legs": [
                {
                    "label": leg["label"],
                    "type": leg["spec"]["type"],
                    "symbol": (TREND_LEGS[leg["spec"]["key"]]["symbol"]
                               if leg["spec"]["type"] == "trend" else leg["spec"]["symbol"]),
                    "strategy": (TREND_LEGS[leg["spec"]["key"]]["strategy"]
                                 if leg["spec"]["type"] == "trend" else "funding_contrarian"),
                    "overlay": (TREND_LEGS[leg["spec"]["key"]]["overlay"]
                                if leg["spec"]["type"] == "trend" else "z2.0"),
                    "leverage": (TREND_LEGS[leg["spec"]["key"]]["leverage"]
                                 if leg["spec"]["type"] == "trend" else int(leg["spec"]["leverage"])),
                    "ann_return": leg["metrics"]["ann_return"],
                    "sharpe": leg["metrics"]["sharpe"],
                    "max_dd": leg["metrics"]["max_dd"],
                    "calmar": leg["metrics"]["calmar"],
                    "profit_factor": leg["metrics"]["profit_factor"],
                    "win_rate": leg["metrics"]["win_rate"],
                    "n_trades": leg["n_trades"],
                }
                for leg in cr["full"]["leg_results"]
            ],
            "correlation": cr["full"]["correlation"],
            "walk_forward": w,
            "goal_gate_pass": {
                "sharpe_gt_1p5": s, "ann_gt_100pct": a, "maxdd_gt_neg15pct": d,
                "all_pass": bool(s and a and d),
            },
        })
    return md, payload


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print(" PORTFOLIO-LEVEL DD-CONTROLLED TREND BACKTEST")
    print("=" * 72)

    print("\n[1/5] Loading trend data (klines cache)...")
    raw = eng.load_all_symbols()
    packs: dict[str, eng.IndicatorPack] = {}
    for sym, df in raw.items():
        packs[sym] = eng.build_pack(df)
    # precompute targets per (symbol, strategy)
    targets: dict = {}
    for sym in ["LINKUSDC", "NEARUSDC", "ETHUSDC", "DOGEUSDC"]:
        for strat in ["donchian", "supertrend"]:
            tgt, warm = eng.gen_targets(strat, packs[sym])
            targets[(sym, strat)] = tgt
    ref_pack = packs["LINKUSDC"]
    data_meta = {
        "n_bars": int(len(ref_pack.close)),
        "start": str(pd.to_datetime(int(ref_pack.ts[0]), unit="ms", utc=True)),
        "end": str(pd.to_datetime(int(ref_pack.ts[-1]), unit="ms", utc=True)),
    }
    print(f"  Trend: {data_meta['n_bars']} bars, {data_meta['start']} -> {data_meta['end']}")

    print("\n[2/5] Loading funding data (DOTUSDT 8h)...")
    funding_data: dict[str, pd.DataFrame] = {}
    try:
        fdf = load_funding_dataset("DOTUSDT")
        funding_data["DOTUSDT"] = fdf
        data_meta["funding_periods"] = int(len(fdf))
        data_meta["funding_start"] = str(pd.to_datetime(int(fdf["ts"].iloc[0]), unit="ms", utc=True))
        data_meta["funding_end"] = str(pd.to_datetime(int(fdf["ts"].iloc[-1]), unit="ms", utc=True))
        print(f"  Funding: {len(fdf)} 8h bars, {data_meta['funding_start']} -> {data_meta['funding_end']}")
    except FileNotFoundError as e:
        print(f"  [WARN] {e} — combo #5 funding leg will be skipped/filled flat.")
        data_meta["funding_periods"] = 0

    print("\n[3/5] Running portfolio combos (full + rebalanced)...")
    combo_results: list[dict] = []
    for i, combo in enumerate(COMBOS, 1):
        print(f"  [{i}/{len(COMBOS)}] {combo['name']}")
        full = build_portfolio(combo["legs"], packs, targets, funding_data)
        rebal = build_portfolio(combo["legs"], packs, targets, funding_data, rebalance=True)
        combo_results.append({
            "idx": i, "name": combo["name"], "short": combo["short"],
            "full": full, "rebalanced": rebal,
        })
        m = full["portfolio_metrics"]
        print(f"        Ann {_fmt_pct(m['ann_return'])}, Shp {m['sharpe']:.2f}, "
              f"DD {_fmt_pct(m['max_dd'])}, Calmar {m['calmar']:.2f}")

    print("\n[4/5] Walk-forward validation...")
    for cr in combo_results:
        combo = next(c for c in COMBOS if c["short"] == cr["short"])
        w = walk_forward_combo(combo, packs, targets, funding_data)
        cr["walk_forward"] = w
        print(f"  Combo {cr['idx']}: train Shp {w['train']['sharpe']:.2f}, "
              f"test Shp {w['test']['sharpe']:.2f}, test DD {_fmt_pct(w['test']['max_dd'])}")

    print("\n[5/5] Generating report...")
    md, payload = generate_report(combo_results, data_meta)
    md_path = DOCS_DIR / "portfolio-dd-trend-analysis.md"
    json_path = DOCS_DIR / "portfolio-dd-trend-data.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    print(f"\n  Wrote {md_path.relative_to(REPO_ROOT)}")
    print(f"  Wrote {json_path.relative_to(REPO_ROOT)}")

    # summary
    print("\n" + "=" * 72)
    print(" SUMMARY (equal-weight, no rebalance)")
    print("=" * 72)
    print(f"{'#':<3} {'Ann':>8} {'Sharpe':>7} {'MaxDD':>8} {'Calmar':>7}  Combo")
    for cr in combo_results:
        m = cr["full"]["portfolio_metrics"]
        print(f"{cr['idx']:<3} {_fmt_pct(m['ann_return']):>8} {m['sharpe']:>7.2f} "
              f"{_fmt_pct(m['max_dd']):>8} {m['calmar']:>7.2f}  {cr['name']}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
