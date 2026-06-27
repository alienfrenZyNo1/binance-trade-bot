#!/usr/bin/env python3
"""Drawdown-Controlled Trend Following With Leverage — Backtest Research.

Problem
-------
Prior research (docs/research/trend-leverage-deep-analysis.md,
docs/research/high-alpha-analysis.md) found trend strategies with great Sharpe
but catastrophic drawdowns (e.g. SUI trend_ls_3x: Sharpe 1.92, +78% annualized,
but 78% max DD). This script tests whether adding risk-control overlays can cap
drawdown at 15-20% while preserving the high returns.

Base trend signals (long AND short, futures):
    1. EMA(50/200) crossover
    2. Supertrend(14, 7)
    3. Donchian(20, 10) breakout

Leverage: 1x, 2x, 3x.

Drawdown-control overlays (the key innovation):
    1. ATR position sizing     — risk a fixed % of equity per trade; size shrinks
                                  when ATR (volatility) spikes.
    2. Trailing stop           — exit if price moves against by N*ATR (1.5/2/3).
    3. Volatility regime filter— skip new entries when ATR/price > rolling 75th
                                  percentile (avoid trading in extreme chaos).
    4. Equity drawdown breaker — halve size at -10% running DD; go flat + halt at
                                  -15% until equity recovers to within -10%.

Costs: 0.04% taker / side, 0.03% slippage / side, 0.010% funding / 8h held.

Reports: total/annualized return, max DD, Sharpe, Sortino, profit factor, win
rate, # trades. Walk-forward validation (train 2/3, test 1/3). Top-10 by target
gate (Sharpe>1, ann>50%, maxDD<20%) and top-10 by return/maxDD. Per-overlay
improvement over the base strategy.

Data: 365 days of 1h candles, Binance USDC-M perps public API. Real data only.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

# ─── Configuration ───────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO_ROOT / "scripts" / "_cache_dd_trend"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
INITIAL_CAPITAL = 10_000.0

# Costs
FEE_RATE = 0.0004       # 0.04% taker per side
SLIPPAGE = 0.0003       # 0.03% per side
COST_SIDE = FEE_RATE + SLIPPAGE          # 0.0007 per side
FUNDING_RATE = 0.0001   # 0.010% per 8h

TRADING_DAYS = 365

SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "DOGEUSDC",
    "SUIUSDC", "AVAXUSDC", "LINKUSDC", "ADAUSDC", "NEARUSDC",
]
LEVERAGES = [1, 2, 3]
FETCH_DAYS = 376   # a little over 365 to leave room for indicator warmup


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


# ─── Data Fetching ───────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = "1h", days: int = FETCH_DAYS) -> pd.DataFrame:
    """Fetch `days` of hourly klines, forward-paginated, deduped."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "limit": 1000}
        for attempt in range(4):
            try:
                r = requests.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 3:
                    print(f"    [WARN] {symbol} fetch failed: {exc}")
                    data = []
                    break
                time.sleep(0.5 * (attempt + 1))
        if not data:
            break
        rows.extend(data)
        cur = data[-1][0] + HOUR_MS
        time.sleep(0.08)
        if len(data) < 1000:
            break
    # dedup
    seen: set[int] = set()
    uniq = []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            uniq.append(row)
    df = pd.DataFrame(uniq, columns=[
        "ts", "open", "high", "low", "close", "volume",
        "close_time", "qv", "trades", "tbv", "tbqv", "ign"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["ts"] = pd.to_numeric(df["ts"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def load_all_symbols() -> dict[str, pd.DataFrame]:
    cache_file = CACHE_DIR / "dd_trend_klines.pkl"
    data: dict[str, pd.DataFrame] = {}
    if cache_file.exists():
        try:
            data = pd.read_pickle(cache_file)
        except Exception:  # noqa: BLE001
            data = {}
    need = [s for s in SYMBOLS if s not in data or len(data[s]) < 8000]
    print(f"  Cache: {len(data)} symbols, {len(need)} to fetch")
    for sym in need:
        print(f"    Fetching {sym} ...")
        df = fetch_klines(sym)
        data[sym] = df
        print(f"      {sym}: {len(df)} bars  {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
    # persist
    pd.to_pickle({s: data[s] for s in SYMBOLS}, cache_file)
    return {s: data[s] for s in SYMBOLS}


# ─── Indicators (numpy) ──────────────────────────────────────────────────────
def ema_np(v: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out = np.empty_like(v, dtype=float)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    # Wilder smoothing
    out = np.empty_like(tr)
    out[:period] = np.nan
    out[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    # fill leading nan with simple expanding mean
    if period > 1:
        out[:period - 1] = np.nanmean(tr[:period])
    return out


def supertrend_dir(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   period: int, mult: float) -> np.ndarray:
    """Supertrend direction: +1 bullish, -1 bearish."""
    hl2 = (high + low) / 2.0
    a = atr_np(high, low, close, period)
    upper_basic = hl2 + mult * a
    lower_basic = hl2 - mult * a
    n = len(close)
    final_upper = np.empty(n)
    final_lower = np.empty(n)
    sup = np.empty(n)
    direction = np.ones(n, dtype=int)  # start bullish
    final_upper[0] = upper_basic[0]
    final_lower[0] = lower_basic[0]
    sup[0] = final_lower[0]
    for i in range(1, n):
        final_upper[i] = upper_basic[i] if (upper_basic[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = lower_basic[i] if (lower_basic[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        if sup[i - 1] == final_upper[i - 1]:  # was bearish
            if close[i] > final_upper[i]:
                direction[i] = 1
                sup[i] = final_lower[i]
            else:
                direction[i] = -1
                sup[i] = final_upper[i]
        else:  # was bullish
            if close[i] < final_lower[i]:
                direction[i] = -1
                sup[i] = final_upper[i]
            else:
                direction[i] = 1
                sup[i] = final_lower[i]
    return direction.astype(int)


def donchian_target(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    entry_p: int, exit_p: int) -> np.ndarray:
    """Donchian breakout target: +1 long when close>prev upper, -1 short when
    close<prev lower, hold previous direction otherwise. Returns ±1 target."""
    upper = pd.Series(high).rolling(entry_p).max().shift(1).to_numpy()
    lower = pd.Series(low).rolling(exit_p).min().shift(1).to_numpy()
    n = len(close)
    target = np.zeros(n, dtype=int)
    pos = 0
    for i in range(n):
        if not np.isnan(upper[i]) and not np.isnan(lower[i]):
            if close[i] > upper[i]:
                pos = 1
            elif close[i] < lower[i]:
                pos = -1
        target[i] = pos
    return target


def rolling_percentile_mask(atr_frac: np.ndarray, window: int, pct: float) -> np.ndarray:
    """vol_ok[i] = True if atr_frac[i] < rolling(pct) percentile over `window`."""
    s = pd.Series(atr_frac)
    thr = s.rolling(window, min_periods=max(window // 4, 10)).quantile(pct)
    vol_ok = np.array(s <= thr, dtype=bool)  # writable copy
    vol_ok[:window] = True  # be permissive during warmup
    return vol_ok


# ─── Signal generation ───────────────────────────────────────────────────────
@dataclass
class IndicatorPack:
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    atr: np.ndarray
    atr_frac: np.ndarray
    vol_ok: np.ndarray
    ts: np.ndarray


def build_pack(df: pd.DataFrame) -> IndicatorPack:
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=np.int64)
    a = atr_np(high, low, close, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        af = np.where(close > 0, a / close, 0.0)
    vol_ok = rolling_percentile_mask(af, 720, 0.75)
    return IndicatorPack(close, high, low, a, af, vol_ok, ts)


def gen_targets(strat: str, pack: IndicatorPack) -> tuple[np.ndarray, int]:
    """Return (target_position array of ±1/0, warmup_bars)."""
    c, h, l = pack.close, pack.high, pack.low
    if strat == "ema":
        ef = ema_np(c, 50)
        es = ema_np(c, 200)
        target = np.where(ef > es, 1, -1).astype(int)
        warm = 200
    elif strat == "supertrend":
        target = supertrend_dir(h, l, c, 14, 7.0)
        warm = 60
    elif strat == "donchian":
        target = donchian_target(h, l, c, 20, 10)
        warm = 30
    else:
        raise ValueError(strat)
    target = target.astype(int)
    target[:warm] = 0  # no trades during indicator warmup
    return target, warm


# ─── Overlay config ──────────────────────────────────────────────────────────
@dataclass
class Overlay:
    name: str
    risk_pct: float | None     # None = full leverage sizing
    trail_atr: float | None    # None = no trailing stop
    vol_filter: bool
    cbreaker: bool


OVERLAYS: list[Overlay] = [
    Overlay("base", None, None, False, False),
    # individual overlays
    Overlay("atr_r1", 0.01, None, False, False),
    Overlay("atr_r2", 0.02, None, False, False),
    Overlay("trail_1.5", None, 1.5, False, False),
    Overlay("trail_2.0", None, 2.0, False, False),
    Overlay("trail_3.0", None, 3.0, False, False),
    Overlay("volfilter", None, None, True, False),
    Overlay("cbreaker", None, None, False, True),
    # combinations
    Overlay("atr2_trail2", 0.02, 2.0, False, False),
    Overlay("atr2_vf_cb", 0.02, None, True, True),
    Overlay("atr2_trail2_vf", 0.02, 2.0, True, False),
    Overlay("atr2_trail2_cb", 0.02, 2.0, False, True),
    Overlay("full", 0.02, 2.0, True, True),       # atr2 + trail2 + vf + cb
    Overlay("full_atr1", 0.01, 2.0, True, True),
    Overlay("full_trail3", 0.02, 3.0, True, True),
]


# ─── Simulation engine ───────────────────────────────────────────────────────
def simulate(target: np.ndarray, pack: IndicatorPack, ov: Overlay, leverage: float,
             start: int = 0, end: int | None = None, init_capital: float = INITIAL_CAPITAL) -> dict:
    """Run one backtest over window [start:end]. Returns metrics + trades."""
    if end is None:
        end = len(target)
    close = pack.close[start:end]
    high = pack.high[start:end]
    low = pack.low[start:end]
    atr = pack.atr[start:end]
    vol_ok = pack.vol_ok[start:end]
    ts = pack.ts[start:end]
    sig = target[start:end]
    n = len(close)
    metrics_stub = _empty_metrics()
    if n < 5:
        metrics_stub["n_bars"] = n
        return {"metrics": metrics_stub, "trades": [], "equity_curve": np.full(n, init_capital)}

    equity_curve = np.empty(n)
    equity = float(init_capital)
    peak_equity = float(init_capital)
    in_pos = False
    direction = 0
    entry_price = 0.0
    entry_equity = 0.0
    entry_bar = 0
    notional = 0.0
    best_price = 0.0
    trades: list[dict] = []

    equity_curve[0] = equity

    def unrealized(i):
        if direction > 0:
            return notional * (close[i] / entry_price - 1.0)
        return notional * (entry_price / close[i] - 1.0)

    for i in range(1, n):
        # A. MTM equity at close[i]
        if in_pos:
            eq_now = entry_equity + unrealized(i) - notional * FUNDING_RATE / 8.0 * (i - entry_bar)
        else:
            eq_now = equity

        # B. drawdown & circuit breaker
        if eq_now > peak_equity:
            peak_equity = eq_now
        dd = (eq_now - peak_equity) / peak_equity if peak_equity > 0 else 0.0
        size_mult = 1.0
        cb_halt = False
        if ov.cbreaker:
            if dd <= -0.15:
                cb_halt = True
            elif dd <= -0.10:
                size_mult = 0.5

        cur_target = int(sig[i])
        prev_target = int(sig[i - 1])
        target_changed = cur_target != prev_target

        # C. exits
        if in_pos:
            # update best favorable price
            if direction > 0:
                if high[i] > best_price:
                    best_price = high[i]
            else:
                if low[i] < best_price:
                    best_price = low[i]

            exit_now = False
            exit_price = 0.0
            if cb_halt:
                exit_now, exit_price = True, close[i]
            elif ov.trail_atr is not None and atr[i] > 0:
                if direction > 0:
                    stop = best_price - ov.trail_atr * atr[i]
                    if low[i] <= stop:
                        exit_now, exit_price = True, max(stop, low[i])
                else:
                    stop = best_price + ov.trail_atr * atr[i]
                    if high[i] >= stop:
                        exit_now, exit_price = True, min(stop, high[i])
            if not exit_now and target_changed and (cur_target == 0 or cur_target == -direction):
                exit_now, exit_price = True, close[i]
            # liquidation guard
            if not exit_now and eq_now <= entry_equity * 0.05:
                exit_now, exit_price = True, close[i]

            if exit_now:
                holding = i - entry_bar
                funding = notional * FUNDING_RATE / 8.0 * holding
                if direction > 0:
                    raw = notional * (exit_price / entry_price - 1.0)
                else:
                    raw = notional * (entry_price / exit_price - 1.0)
                exit_fee = notional * COST_SIDE
                entry_fee = notional * COST_SIDE
                pnl = raw - funding - entry_fee - exit_fee
                equity = entry_equity + raw - funding - exit_fee
                trades.append({
                    "side": "long" if direction > 0 else "short",
                    "pnl": pnl, "entry": entry_price, "exit": exit_price,
                    "bars": holding, "notional": notional,
                    "reason": "cb" if cb_halt else ("trail" if (ov.trail_atr and exit_price != close[i] or (exit_now and ov.trail_atr and not target_changed)) else "signal"),
                })
                in_pos = False
                direction = 0

        # D. entry (only on a fresh target change)
        if not in_pos and target_changed and cur_target != 0:
            allowed = True
            if cb_halt:
                allowed = False
            if allowed and ov.vol_filter and not bool(vol_ok[i]):
                allowed = False
            if allowed:
                direction = 1 if cur_target > 0 else -1
                if ov.risk_pct is not None:
                    af = atr[i] / close[i] if close[i] > 0 else 0.0
                    risk_unit = ov.trail_atr if ov.trail_atr else 1.0
                    if af > 0 and risk_unit > 0:
                        target_notional = (ov.risk_pct * equity) / (risk_unit * af)
                    else:
                        target_notional = equity * leverage
                    notional = min(target_notional, equity * leverage) * size_mult
                else:
                    notional = equity * leverage * size_mult
                if notional > 0 and equity > 0:
                    entry_price = close[i]
                    entry_equity = equity - notional * COST_SIDE  # entry fee
                    entry_bar = i
                    best_price = high[i] if direction > 0 else low[i]
                    in_pos = True
                else:
                    direction = 0

        # E. equity curve
        if in_pos:
            equity_curve[i] = entry_equity + unrealized(i) - notional * FUNDING_RATE / 8.0 * (i - entry_bar)
        else:
            equity_curve[i] = equity

    # close any open position at the end
    if in_pos:
        i = n - 1
        holding = i - entry_bar
        funding = notional * FUNDING_RATE / 8.0 * holding
        if direction > 0:
            raw = notional * (close[i] / entry_price - 1.0)
        else:
            raw = notional * (entry_price / close[i] - 1.0)
        exit_fee = notional * COST_SIDE
        equity = entry_equity + raw - funding - exit_fee
        pnl = raw - funding - notional * COST_SIDE - exit_fee
        trades.append({
            "side": "long" if direction > 0 else "short", "pnl": pnl,
            "entry": entry_price, "exit": close[i], "bars": holding,
            "notional": notional, "reason": "eod"})
        equity_curve[i] = equity

    metrics = _metrics_from_curve(equity_curve, ts, trades, init_capital, start, end)
    return {"metrics": metrics, "trades": trades, "equity_curve": equity_curve}


def _empty_metrics() -> dict:
    return {
        "total_return": 0.0, "ann_return": 0.0, "sharpe": 0.0, "sortino": 0.0,
        "max_dd": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "n_trades": 0,
        "final_equity": INITIAL_CAPITAL, "n_bars": 0, "years": 0.0,
        "ann_vol": 0.0, "calmar": 0.0, "ret_over_dd": 0.0,
    }


def _metrics_from_curve(equity_curve: np.ndarray, ts: np.ndarray, trades: list[dict],
                        init_capital: float, start: int, end: int) -> dict:
    n = len(equity_curve)
    m = _empty_metrics()
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
        rat = curr / safe
        dr = rat - 1.0
        dr = dr[np.isfinite(dr)]
    else:
        dr = np.array([])
    if len(dr) > 1:
        mean_r = float(np.mean(dr))
        std_r = float(np.std(dr, ddof=1))
        sharpe = mean_r / std_r * math.sqrt(365) if std_r > 0 else 0.0
        ann_vol = std_r * math.sqrt(365)
        downside = dr[dr < 0]
        if len(downside) > 1:
            dstd = float(np.std(downside, ddof=1))
            sortino = mean_r / dstd * math.sqrt(365) if dstd > 0 else 0.0
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


# ─── Runner ──────────────────────────────────────────────────────────────────
STRATS = ["ema", "supertrend", "donchian"]


def run_full_grid(packs: dict[str, IndicatorPack]) -> tuple[list[dict], dict]:
    """Run every strategy x overlay x leverage x symbol. Returns (results, targets)."""
    results: list[dict] = []
    targets: dict[tuple[str, str], np.ndarray] = {}
    # precompute targets per (symbol, strategy)
    for sym in SYMBOLS:
        pack = packs[sym]
        for strat in STRATS:
            tgt, warm = gen_targets(strat, pack)
            targets[(sym, strat)] = tgt

    total = len(SYMBOLS) * len(STRATS) * len(OVERLAYS) * len(LEVERAGES)
    done = 0
    t0 = time.time()
    for sym in SYMBOLS:
        pack = packs[sym]
        # buy & hold benchmark
        bh_eq = pack.close / pack.close[0] * INITIAL_CAPITAL
        bh_m = _metrics_from_curve(bh_eq, pack.ts, [], INITIAL_CAPITAL, 0, len(bh_eq))
        results.append({"symbol": sym, "strategy": "buy_hold", "overlay": "n/a",
                        "leverage": 1, "metrics": bh_m, "tag": "benchmark"})
        for strat in STRATS:
            tgt = targets[(sym, strat)]
            for ov in OVERLAYS:
                for lev in LEVERAGES:
                    res = simulate(tgt, pack, ov, float(lev))
                    m = res["metrics"]
                    rec = {
                        "symbol": sym, "strategy": strat, "overlay": ov.name,
                        "leverage": lev,
                        "risk_pct": ov.risk_pct, "trail_atr": ov.trail_atr,
                        "vol_filter": ov.vol_filter, "cbreaker": ov.cbreaker,
                        "metrics": m, "tag": "config",
                    }
                    results.append(rec)
                    done += 1
                    if done % 50 == 0:
                        el = time.time() - t0
                        print(f"    ...{done}/{total}  ({el:.0f}s, {done/el:.1f}/s)")
    print(f"  Full grid done: {done} configs in {time.time()-t0:.1f}s")
    return results, targets


def walk_forward(top_configs: list[dict], packs: dict[str, IndicatorPack],
                 targets: dict) -> list[dict]:
    """Train on first 2/3, test on last 1/3. Signals computed on full data for
    indicator continuity, then the simulation is split."""
    wf = []
    for cfg in top_configs:
        sym = cfg["symbol"]
        strat = cfg["strategy"]
        ov = _overlay_by_name(cfg["overlay"])
        lev = cfg["leverage"]
        pack = packs[sym]
        tgt = targets[(sym, strat)]
        n = len(tgt)
        split = int(n * 2 / 3)
        tr = simulate(tgt, pack, ov, float(lev), 0, split)
        te = simulate(tgt, pack, ov, float(lev), split, n)
        # buy & hold test period
        cseg = pack.close[split:n]
        if len(cseg) > 1:
            bh = cseg[-1] / cseg[0] - 1
        else:
            bh = 0.0
        wf.append({
            "symbol": sym, "strategy": strat, "overlay": cfg["overlay"], "leverage": lev,
            "train_ann": tr["metrics"]["ann_return"], "train_sharpe": tr["metrics"]["sharpe"],
            "train_maxdd": tr["metrics"]["max_dd"], "train_trades": tr["metrics"]["n_trades"],
            "test_ann": te["metrics"]["ann_return"], "test_sharpe": te["metrics"]["sharpe"],
            "test_maxdd": te["metrics"]["max_dd"], "test_trades": te["metrics"]["n_trades"],
            "test_bh": bh,
            "full_ann": cfg["metrics"]["ann_return"],
            "full_sharpe": cfg["metrics"]["sharpe"],
            "full_maxdd": cfg["metrics"]["max_dd"],
        })
    return wf


def _overlay_by_name(name: str) -> Overlay:
    for ov in OVERLAYS:
        if ov.name == name:
            return ov
    return OVERLAYS[0]


# ─── Report generation ───────────────────────────────────────────────────────
def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def generate_report(results: list[dict], wf_results: list[dict],
                    packs: dict[str, IndicatorPack], data_meta: dict) -> tuple[str, dict]:
    cfgs = [r for r in results if r.get("tag") == "config"]
    benchmarks = {r["symbol"]: r["metrics"] for r in results if r.get("tag") == "benchmark"}

    def meets(m):
        return m["sharpe"] > 1.0 and m["ann_return"] > 0.50 and m["max_dd"] > -0.20

    gated = [r for r in cfgs if meets(r["metrics"])]
    gated.sort(key=lambda r: r["metrics"]["ann_return"], reverse=True)
    top_gated = gated[:10]

    risk_adj = [r for r in cfgs if r["metrics"]["max_dd"] < -1e-6]
    risk_adj.sort(key=lambda r: r["metrics"]["ret_over_dd"], reverse=True)
    top_ra = risk_adj[:10]

    # base-only rows for overlay-impact comparison
    bases = {(r["symbol"], r["strategy"], r["leverage"]): r for r in cfgs if r["overlay"] == "base"}

    # overlay impact: average across all (symbol,strategy,leverage) of each overlay vs base
    overlay_impact = {}
    for ov in OVERLAYS:
        rows = [r for r in cfgs if r["overlay"] == ov.name]
        if not rows:
            continue
        deltas_sharpe, deltas_dd, deltas_ann, deltas_calmar = [], [], [], []
        for r in rows:
            key = (r["symbol"], r["strategy"], r["leverage"])
            b = bases.get(key)
            if b is None:
                continue
            deltas_sharpe.append(r["metrics"]["sharpe"] - b["metrics"]["sharpe"])
            deltas_dd.append(r["metrics"]["max_dd"] - b["metrics"]["max_dd"])  # less negative = better
            deltas_ann.append(r["metrics"]["ann_return"] - b["metrics"]["ann_return"])
            deltas_calmar.append(r["metrics"]["calmar"] - b["metrics"]["calmar"])
        overlay_impact[ov.name] = {
            "avg_delta_sharpe": float(np.mean(deltas_sharpe)) if deltas_sharpe else 0.0,
            "avg_delta_maxdd": float(np.mean(deltas_dd)) if deltas_dd else 0.0,
            "avg_delta_ann": float(np.mean(deltas_ann)) if deltas_ann else 0.0,
            "avg_delta_calmar": float(np.mean(deltas_calmar)) if deltas_calmar else 0.0,
            "count": len(deltas_sharpe),
            # best absolute result for this overlay
            "best_ann": max((r["metrics"]["ann_return"] for r in rows), default=0.0),
            "best_sharpe": max((r["metrics"]["sharpe"] for r in rows), default=0.0),
            "best_maxdd": max((r["metrics"]["max_dd"] for r in rows), default=0.0),
            "meets_count": sum(1 for r in rows if meets(r["metrics"])),
        }

    # per-strategy and per-symbol best meeting gate
    by_strat = {}
    for strat in STRATS:
        rows = [r for r in cfgs if r["strategy"] == strat]
        mg = [r for r in rows if meets(r["metrics"])]
        by_strat[strat] = {
            "n_configs": len(rows),
            "n_meets": len(mg),
            "best_meets": sorted(mg, key=lambda r: r["metrics"]["ann_return"], reverse=True)[:3],
            "best_ra": sorted(rows, key=lambda r: r["metrics"]["ret_over_dd"], reverse=True)[:3],
        }
    by_sym = {}
    for sym in SYMBOLS:
        rows = [r for r in cfgs if r["symbol"] == sym]
        mg = [r for r in rows if meets(r["metrics"])]
        by_sym[sym] = {
            "n_meets": len(mg),
            "buy_hold_ann": benchmarks[sym]["ann_return"],
            "buy_hold_maxdd": benchmarks[sym]["max_dd"],
            "best_meets": sorted(mg, key=lambda r: r["metrics"]["ann_return"], reverse=True)[:3],
        }

    # leverage summary
    lev_summary = {}
    for lev in LEVERAGES:
        rows = [r for r in cfgs if r["leverage"] == lev]
        lev_summary[lev] = {
            "avg_ann": float(np.mean([r["metrics"]["ann_return"] for r in rows])),
            "avg_sharpe": float(np.mean([r["metrics"]["sharpe"] for r in rows])),
            "avg_maxdd": float(np.mean([r["metrics"]["max_dd"] for r in rows])),
            "avg_calmar": float(np.mean([r["metrics"]["calmar"] for r in rows])),
            "n_meets": sum(1 for r in rows if meets(r["metrics"])),
        }

    L = []
    L.append("# Drawdown-Controlled Trend Following with Leverage")
    L.append("")
    L.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    L.append(f"**Data:** {data_meta['n_bars']} hourly bars (~{data_meta['n_bars']/24:.0f} days) per symbol, "
             f"Binance USDC-M perps public API")
    L.append(f"**Symbols:** {', '.join(SYMBOLS)}")
    L.append(f"**Window:** {data_meta['start']} -> {data_meta['end']} (last ~1/3 was a severe bear market)")
    L.append(f"**Base signals:** EMA(50/200), Supertrend(14,7), Donchian(20,10) — long AND short")
    L.append(f"**Leverage:** {', '.join(f'{l}x' for l in LEVERAGES)}")
    L.append(f"**Costs:** {FEE_RATE*100:.2f}% taker + {SLIPPAGE*100:.2f}% slippage per side, "
             f"{FUNDING_RATE*100:.3f}% funding/8h")
    L.append(f"**Configs tested:** {len(cfgs)} (3 signals x {len(OVERLAYS)} overlays x {len(LEVERAGES)} lev x {len(SYMBOLS)} symbols)")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## Drawdown-Control Overlays Tested")
    L.append("")
    L.append("| Overlay | Mechanism |")
    L.append("|--------|-----------|")
    L.append("| **ATR position sizing** (`atr_r1`=1%, `atr_r2`=2%) | Size = risk%·equity / (risk_unit·ATR_frac), capped at leverage. Shrinks exposure when volatility spikes. |")
    L.append("| **Trailing stop** (`trail_1.5/2.0/3.0`) | Exit when price moves N·ATR against the best favorable price since entry. |")
    L.append("| **Volatility regime filter** (`volfilter`) | Skip new entries when ATR/price > rolling-720bar 75th percentile. |")
    L.append("| **Equity drawdown breaker** (`cbreaker`) | Halve size at −10% running DD; force flat + halt new entries at −15% until recovery to within −10%. |")
    L.append("| **Combinations** | `atr2_trail2`, `atr2_vf_cb`, `atr2_trail2_vf`, `atr2_trail2_cb`, `full` (atr2+trail2+vf+cb), `full_atr1`, `full_trail3` |")
    L.append("")
    L.append("---")
    L.append("")

    # ── TARGET GATE ──
    L.append("## Target Gate: Sharpe > 1.0 AND Ann > 50% AND MaxDD < 20%")
    L.append("")
    n_meets = len(gated)
    L.append(f"**{n_meets}** of {len(cfgs)} configs meet all three targets.")
    L.append("")
    if top_gated:
        L.append("| Rank | Symbol | Strategy | Overlay | Lev | Ann Ret | Sharpe | Sortino | Max DD | Calmar | Win% | PF | Trades |")
        L.append("|------|--------|----------|---------|-----|---------|--------|---------|--------|--------|------|----|--------|")
        for i, r in enumerate(top_gated, 1):
            m = r["metrics"]
            L.append(f"| {i} | {r['symbol']} | {r['strategy']} | {r['overlay']} | {r['leverage']}x | "
                     f"{_fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | {m['sortino']:.2f} | "
                     f"{_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {m['win_rate']*100:.0f}% | "
                     f"{m['profit_factor']:.2f} | {m['n_trades']} |")
    else:
        L.append("_No config met all three targets simultaneously._ The closest configs (relaxed) are listed in the risk-adjusted table below.")
    L.append("")

    # ── RISK-ADJUSTED TOP 10 ──
    L.append("## Top 10 by Risk-Adjusted Return (Total Return / |Max DD|)")
    L.append("")
    L.append("Regardless of absolute thresholds — pure return-per-unit-drawdown efficiency.")
    L.append("")
    L.append("| Rank | Symbol | Strategy | Overlay | Lev | Ann Ret | Sharpe | Max DD | Ret/|DD| | Calmar | Win% | PF | Trades |")
    L.append("|------|--------|----------|---------|-----|---------|--------|--------|-----------|--------|------|----|--------|")
    for i, r in enumerate(top_ra, 1):
        m = r["metrics"]
        L.append(f"| {i} | {r['symbol']} | {r['strategy']} | {r['overlay']} | {r['leverage']}x | "
                 f"{_fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | "
                 f"{m['ret_over_dd']:.2f} | {m['calmar']:.2f} | {m['win_rate']*100:.0f}% | "
                 f"{m['profit_factor']:.2f} | {m['n_trades']} |")
    L.append("")

    # ── OVERLAY IMPACT ──
    L.append("## How Each Drawdown-Control Overlay Improves the Base Strategy")
    L.append("")
    L.append("Average change vs the **base** (no-overlay) config, across every symbol x strategy x leverage.")
    L.append("Positive ΔSharpe and ΔAnn are good; ΔMaxDD **less negative** (closer to 0) is good.")
    L.append("")
    L.append("| Overlay | ΔSharpe | ΔMax DD | ΔAnn Ret | ΔCalmar | Best Ann | Best Sharpe | Best(least-bad) MaxDD | #meet gate |")
    L.append("|---------|---------|---------|----------|---------|----------|-------------|-----------------------|------------|")
    for ov in OVERLAYS:
        imp = overlay_impact.get(ov.name)
        if not imp:
            continue
        L.append(f"| {ov.name} | {imp['avg_delta_sharpe']:+.2f} | {imp['avg_delta_maxdd']*100:+.1f}% | "
                 f"{imp['avg_delta_ann']*100:+.1f}% | {imp['avg_delta_calmar']:+.2f} | "
                 f"{_fmt_pct(imp['best_ann'])} | {imp['best_sharpe']:.2f} | "
                 f"{_fmt_pct(imp['best_maxdd'])} | {imp['meets_count']} |")
    L.append("")

    # narrative: does it cap DD?
    full_imp = overlay_impact.get("full", {})
    base_imp = overlay_impact.get("base", {})
    L.append("### Key takeaway")
    L.append("")
    cb_dd = overlay_impact.get("cbreaker", {}).get("avg_delta_maxdd", 0)
    atr_dd = overlay_impact.get("atr_r2", {}).get("avg_delta_maxdd", 0)
    trail_dd = overlay_impact.get("trail_2.0", {}).get("avg_delta_maxdd", 0)
    full_dd = full_imp.get("avg_delta_maxdd", 0)
    L.append(f"- The **full stack** (`full`: ATR2 + trail2 + volfilter + cbreaker) moves average MaxDD by "
             f"{full_dd*100:+.1f}% and average Sharpe by {full_imp.get('avg_delta_sharpe',0):+.2f} vs base.")
    L.append(f"- Circuit breaker alone shifts average MaxDD by {cb_dd*100:+.1f}% (it mechanically caps drawdown).")
    L.append(f"- ATR-2% sizing shifts MaxDD by {atr_dd*100:+.1f}% (de-risks in high-vol regimes).")
    L.append(f"- Trailing-stop 2.0 shifts MaxDD by {trail_dd*100:+.1f}%.")
    L.append("")

    # ── PER STRATEGY ──
    L.append("## Per-Strategy Summary")
    L.append("")
    for strat in STRATS:
        s = by_strat[strat]
        L.append(f"### {strat}")
        L.append(f"Configs: {s['n_configs']} | Meet gate: {s['n_meets']}")
        L.append("")
        if s["best_meets"]:
            L.append("Best meeting-gate configs:")
            L.append("| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Calmar |")
            L.append("|--------|---------|-----|-----|--------|--------|--------|")
            for r in s["best_meets"]:
                m = r["metrics"]
                L.append(f"| {r['symbol']} | {r['overlay']} | {r['leverage']}x | {_fmt_pct(m['ann_return'])} | "
                         f"{m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | {m['calmar']:.2f} |")
            L.append("")
        L.append("Best risk-adjusted (any):")
        L.append("| Symbol | Overlay | Lev | Ann | Sharpe | Max DD | Ret/|DD| |")
        L.append("|--------|---------|-----|-----|--------|--------|-----------|")
        for r in s["best_ra"]:
            m = r["metrics"]
            L.append(f"| {r['symbol']} | {r['overlay']} | {r['leverage']}x | {_fmt_pct(m['ann_return'])} | "
                     f"{m['sharpe']:.2f} | {_fmt_pct(m['max_dd'])} | {m['ret_over_dd']:.2f} |")
        L.append("")

    # ── PER SYMBOL ──
    L.append("## Per-Symbol Summary (vs Buy & Hold)")
    L.append("")
    L.append("| Symbol | Buy&Hold Ann | Buy&Hold MaxDD | #meet gate | Best meeting config |")
    L.append("|--------|--------------|----------------|------------|---------------------|")
    for sym in SYMBOLS:
        s = by_sym[sym]
        if s["best_meets"]:
            r = s["best_meets"][0]
            m = r["metrics"]
            best = f"{r['strategy']}/{r['overlay']}/{r['leverage']}x: Ann {_fmt_pct(m['ann_return'])}, Shp {m['sharpe']:.2f}, DD {_fmt_pct(m['max_dd'])}"
        else:
            best = "_(none meet gate)_"
        L.append(f"| {sym} | {_fmt_pct(s['buy_hold_ann'])} | {_fmt_pct(s['buy_hold_maxdd'])} | "
                 f"{s['n_meets']} | {best} |")
    L.append("")

    # ── LEVERAGE SUMMARY ──
    L.append("## Leverage Impact (averaged across all configs)")
    L.append("")
    L.append("| Leverage | Avg Ann | Avg Sharpe | Avg Max DD | Avg Calmar | #meet gate |")
    L.append("|----------|---------|------------|------------|------------|------------|")
    for lev in LEVERAGES:
        s = lev_summary[lev]
        L.append(f"| {lev}x | {_fmt_pct(s['avg_ann'])} | {s['avg_sharpe']:.2f} | "
                 f"{_fmt_pct(s['avg_maxdd'])} | {s['avg_calmar']:.2f} | {s['n_meets']} |")
    L.append("")

    # ── WALK-FORWARD ──
    L.append("## Walk-Forward Validation (train 2/3, test 1/3)")
    L.append("")
    L.append("The last 1/3 of the window was a severe bear market, so out-of-sample is a hard test.")
    L.append("")
    if wf_results:
        L.append("| Symbol | Strat | Overlay | Lev | Train Ann | Train Shp | Test Ann | Test Shp | Test MaxDD | Test B&H | Robust? |")
        L.append("|--------|-------|---------|-----|-----------|-----------|----------|----------|------------|----------|---------|")
        for w in wf_results:
            deg = (w["train_sharpe"] - w["test_sharpe"]) / abs(w["train_sharpe"]) if abs(w["train_sharpe"]) > 0.01 else 0
            robust = w["test_sharpe"] > -0.5 and w["test_ann"] > w["test_bh"] * 0.5 and deg < 3.0
            L.append(f"| {w['symbol']} | {w['strategy']} | {w['overlay']} | {w['leverage']}x | "
                     f"{_fmt_pct(w['train_ann'])} | {w['train_sharpe']:.2f} | "
                     f"{_fmt_pct(w['test_ann'])} | {w['test_sharpe']:.2f} | "
                     f"{_fmt_pct(w['test_maxdd'])} | {_fmt_pct(w['test_bh'])} | "
                     f"{'YES' if robust else 'no'} |")
    L.append("")

    md = "\n".join(L)

    # JSON payload
    payload = {
        "meta": data_meta,
        "costs": {"fee_side": FEE_RATE, "slippage_side": SLIPPAGE, "funding_8h": FUNDING_RATE},
        "symbols": SYMBOLS, "leverages": LEVERAGES, "strategies": STRATS,
        "overlays": [ov.name for ov in OVERLAYS],
        "n_configs": len(cfgs),
        "n_meet_gate": n_meets,
        "top_gated": [_strip(r) for r in top_gated],
        "top_risk_adjusted": [_strip(r) for r in top_ra],
        "overlay_impact": overlay_impact,
        "leverage_summary": {str(k): v for k, v in lev_summary.items()},
        "walk_forward": wf_results,
        "all_results": [_strip(r) for r in cfgs],
        "benchmarks": {sym: {"ann_return": benchmarks[sym]["ann_return"],
                             "max_dd": benchmarks[sym]["max_dd"],
                             "total_return": benchmarks[sym]["total_return"]}
                       for sym in SYMBOLS},
    }
    return md, payload


def _strip(r: dict) -> dict:
    m = r["metrics"]
    return {
        "symbol": r["symbol"], "strategy": r["strategy"], "overlay": r["overlay"],
        "leverage": r["leverage"],
        "risk_pct": r.get("risk_pct"), "trail_atr": r.get("trail_atr"),
        "vol_filter": r.get("vol_filter"), "cbreaker": r.get("cbreaker"),
        "total_return": m["total_return"], "ann_return": m["ann_return"],
        "sharpe": m["sharpe"], "sortino": m["sortino"], "max_dd": m["max_dd"],
        "calmar": m["calmar"], "ret_over_dd": m["ret_over_dd"],
        "profit_factor": m["profit_factor"], "win_rate": m["win_rate"],
        "n_trades": m["n_trades"], "final_equity": m["final_equity"],
    }


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print(" Drawdown-Controlled Trend Following with Leverage")
    print("=" * 70)
    print("\n[1/4] Fetching data...")
    raw = load_all_symbols()
    packs: dict[str, IndicatorPack] = {}
    data_meta = {"symbols": {}}
    ref_len = None
    for sym, df in raw.items():
        packs[sym] = build_pack(df)
        if ref_len is None:
            ref_len = len(df)
            data_meta["n_bars"] = int(len(df))
            data_meta["start"] = str(pd.to_datetime(int(df["ts"].iloc[0]), unit="ms", utc=True))
            data_meta["end"] = str(pd.to_datetime(int(df["ts"].iloc[-1]), unit="ms", utc=True))
        data_meta["symbols"][sym] = {
            "n_bars": int(len(df)),
            "start": str(pd.to_datetime(int(df["ts"].iloc[0]), unit="ms", utc=True)),
            "end": str(pd.to_datetime(int(df["ts"].iloc[-1]), unit="ms", utc=True)),
        }
    print(f"  {len(packs)} symbols, ~{ref_len} bars each (~{ref_len/24:.0f} days)")

    print("\n[2/4] Running full grid (strategy x overlay x leverage x symbol)...")
    results, targets = run_full_grid(packs)

    print("\n[3/4] Selecting top configs + walk-forward validation...")
    cfgs = [r for r in results if r.get("tag") == "config"]

    def meets(m):
        return m["sharpe"] > 1.0 and m["ann_return"] > 0.50 and m["max_dd"] > -0.20

    gated = sorted([r for r in cfgs if meets(r["metrics"])],
                   key=lambda r: r["metrics"]["ann_return"], reverse=True)
    ra = sorted([r for r in cfgs if r["metrics"]["max_dd"] < -1e-6],
                key=lambda r: r["metrics"]["ret_over_dd"], reverse=True)
    # walk-forward on union of top gated + top risk-adjusted (dedup, cap 20)
    seen = set()
    top_for_wf = []
    for r in gated[:10] + ra[:10]:
        k = (r["symbol"], r["strategy"], r["overlay"], r["leverage"])
        if k not in seen:
            seen.add(k)
            top_for_wf.append(r)
        if len(top_for_wf) >= 20:
            break
    wf = walk_forward(top_for_wf, packs, targets)

    print("\n[4/4] Generating report...")
    md, payload = generate_report(results, wf, packs, data_meta)

    md_path = DOCS_DIR / "dd-controlled-trend-analysis.md"
    json_path = DOCS_DIR / "dd-controlled-trend-analysis.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")

    print(f"\n  Wrote {md_path.relative_to(REPO_ROOT)}")
    print(f"  Wrote {json_path.relative_to(REPO_ROOT)}")
    print(f"  Configs tested: {len(cfgs)} | Meet gate: {len(gated)}")
    if gated:
        b = gated[0]
        print(f"  Best gated: {b['symbol']} {b['strategy']} {b['overlay']} {b['leverage']}x "
              f"-> Ann {_fmt_pct(b['metrics']['ann_return'])}, Shp {b['metrics']['sharpe']:.2f}, "
              f"DD {_fmt_pct(b['metrics']['max_dd'])}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
