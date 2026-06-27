#!/usr/bin/env python3
"""Momentum Breakout (Volatility-Expansion) Backtest — Research Only.

PROBLEM / GAP
-------------
Prior research rounds covered trend-following (Donchian/Supertrend/EMA) and grid
trading, both with good results. The GAP in coverage is pure momentum breakout:
catching altcoin *pumps* via momentum + volatility expansion. This script fills
that gap.

STRATEGY (momentum / volatility breakout, long-only — a "pump catcher")
----------------------------------------------------------------------
    ENTRY (long):  close breaks above its rolling N-day HIGH
                   AND a volatility-expansion filter confirms:
                       - atr_exp  : current ATR/price  >= k * rolling-mean(ATR/price)
                       - vol_spike: current volume    >= k * rolling-mean(volume)
                       - none     : breakout alone (control)
    EXIT:           trailing stop (price falls k*ATR below best-since-entry)
                   OR opposite signal (close breaks below rolling M-day LOW)

This is long-only by design — the strategy thesis is catching upside pumps.
Shorts are authorized under Directive 002 but a pure pump-catcher is long-biased;
we report long-only honestly rather than dressing up a weak short side.

LEVERAGE: 1x, 2x, 3x (Directive 002 authorizes 2-5x; we cap research at 3x).
COSTS:    0.04% taker + 0.03% slippage per side (0.07%/side, 0.14% round-trip),
          0.010% funding per 8h held (futures).
UNIVERSE: 15 USDC pairs: BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK, ADA, DOT,
          NEAR, APT, ARB, OP, INJ. Data from Binance USDC-M perps public API,
          reusing the existing _cache_dd_trend pickle where present. DOT/APT/
          OP/INJ are not listed on USDC-M futures, so for those 4 we use the
          USDT-M perp of the same underlying (identical price dynamics; only the
          quote coin differs). This substitution is flagged in the report.
METRICS:  annualized return, Sharpe, Sortino, max drawdown, profit factor, win
          rate, # trades, Calmar.
TARGET:   Sharpe > 1.0  AND  Ann > 50%  AND  MaxDD < 20%  ("aggressive gate").

Engine pattern, data loading, cost model, and metrics reused from
    scripts/research_dd_controlled_trend.py  (indicators, metrics, simulate)
so results are directly comparable to the prior trend-following round.

RESEARCH ONLY — never touches live config or places orders.
"""
from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

# ─── Reuse the engine's proven indicators + metrics (identical cost model) ────
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import research_dd_controlled_trend as eng  # noqa: E402

REPO_ROOT = HERE.parent
CACHE_DIR = REPO_ROOT / "scripts" / "_cache_dd_trend"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR = REPO_ROOT / "docs" / "research"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_MD = DOCS_DIR / "momentum-breakout-analysis.md"
REPORT_JSON = DOCS_DIR / "momentum-breakout-data.json"
MOM_CACHE = REPO_ROOT / "scripts" / "_cache_momentum"
MOM_CACHE.mkdir(parents=True, exist_ok=True)

FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
INITIAL_CAPITAL = 10_000.0

# Costs — identical to the trend-following engine for comparability.
FEE_RATE = eng.FEE_RATE        # 0.0004 taker / side
SLIPPAGE = eng.SLIPPAGE        # 0.0003 / side
COST_SIDE = eng.COST_SIDE      # 0.0007 / side
FUNDING_RATE = eng.FUNDING_RATE  # 0.0001 / 8h

# 15 USDC pairs. (symbol_for_fetch, display_symbol, quote_note)
# DOT/APT/OP/INJ fall back to USDT-M futures (not on USDC-M perp).
SYMBOL_SPECS = [
    ("BTCUSDC",  "BTCUSDC",  "USDC-M"),
    ("ETHUSDC",  "ETHUSDC",  "USDC-M"),
    ("SOLUSDC",  "SOLUSDC",  "USDC-M"),
    ("BNBUSDC",  "BNBUSDC",  "USDC-M"),
    ("XRPUSDC",  "XRPUSDC",  "USDC-M"),
    ("DOGEUSDC", "DOGEUSDC", "USDC-M"),
    ("AVAXUSDC", "AVAXUSDC", "USDC-M"),
    ("LINKUSDC", "LINKUSDC", "USDC-M"),
    ("ADAUSDC",  "ADAUSDC",  "USDC-M"),
    ("NEARUSDC", "NEARUSDC", "USDC-M"),
    ("DOTUSDT",  "DOTUSDC",  "USDT-M fallback"),
    ("APTUSDT",  "APTUSDC",  "USDT-M fallback"),
    ("ARBUSDC",  "ARBUSDC",  "USDC-M"),
    ("OPUSDT",   "OPUSDC",   "USDT-M fallback"),
    ("INJUSDT",  "INJUSDC",  "USDT-M fallback"),
]
FETCH_SYMBOLS = [s[0] for s in SYMBOL_SPECS]
DISPLAY_SYMBOLS = [s[1] for s in SYMBOL_SPECS]
CACHED_SYMBOLS = set(pd.read_pickle(CACHE_DIR / "dd_trend_klines.pkl").keys()) \
    if (CACHE_DIR / "dd_trend_klines.pkl").exists() else set()

LEVERAGES = [1, 2, 3]
FETCH_DAYS = 376  # a little over 365 for indicator warmup


# ═══════════════════════════════════════════════════════════════════════════════
#  Data loading — reuse cache, fetch the rest
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_klines(symbol: str, interval: str = "1h", days: int = FETCH_DAYS) -> pd.DataFrame:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        params = {"symbol": symbol, "interval": interval, "startTime": cur, "limit": 1500}
        data = None
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


def load_all_data() -> tuple[dict[str, pd.DataFrame], dict]:
    """Load cached USDC-M klines + fetch missing symbols. Align all to the
    common hourly grid (intersection of timestamps, or BTC grid as reference)."""
    base_cache = CACHE_DIR / "dd_trend_klines.pkl"
    data: dict[str, pd.DataFrame] = {}
    if base_cache.exists():
        try:
            data = pd.read_pickle(base_cache)
        except Exception:  # noqa: BLE001
            data = {}

    # Map fetch-symbol -> display-symbol; reuse cache when display symbol present
    out: dict[str, pd.DataFrame] = {}
    fetch_log = []
    for fetch_sym, disp_sym, quote_note in SYMBOL_SPECS:
        # prefer cached by display name (USDC)
        if disp_sym in data and len(data[disp_sym]) >= 8000:
            out[disp_sym] = data[disp_sym]
            fetch_log.append((disp_sym, len(data[disp_sym]), "cache(USDC-M)"))
            continue
        # else fetch live
        df = fetch_klines(fetch_sym)
        out[disp_sym] = df
        fetch_log.append((disp_sym, len(df), f"fetch {fetch_sym} ({quote_note})"))

    # Reference grid from BTC (most complete). Align every symbol to this grid.
    ref_ts = out["BTCUSDC"]["ts"].to_numpy()
    aligned: dict[str, pd.DataFrame] = {}
    for disp_sym, df in out.items():
        d = df.set_index("ts").reindex(ref_ts)
        # forward-fill any small gaps (maintains contiguity for rolling ops)
        d = d.ffill().bfill()
        d = d.reset_index().rename(columns={"index": "ts"})
        aligned[disp_sym] = d
    return aligned, {"fetch_log": fetch_log, "n_ref_bars": len(ref_ts)}


# ═══════════════════════════════════════════════════════════════════════════════
#  Momentum-breakout signal generation (vectorized)
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class MomPack:
    close: np.ndarray
    high: np.ndarray
    low: np.ndarray
    volume: np.ndarray
    atr: np.ndarray
    atr_frac: np.ndarray   # ATR / close
    ts: np.ndarray


def build_pack(df: pd.DataFrame) -> MomPack:
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy(dtype=np.int64)
    a = eng.atr_np(high, low, close, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        af = np.where(close > 0, a / close, 0.0)
    return MomPack(close, high, low, volume, a, af, ts)


@dataclass
class MomConfig:
    breakout_days: int      # N-day high lookback (hours = N*24)
    exit_days: int          # M-day low lookback for opposite-signal exit
    vol_confirm: str        # "none" | "atr_exp" | "vol_spike"
    confirm_k: float        # multiplier for the expansion threshold
    trail_atr: float        # trailing stop in ATR units (0 = use exit_days only)
    leverage: int


def _roll_mean(x: np.ndarray, win: int) -> np.ndarray:
    s = pd.Series(x)
    return s.rolling(win, min_periods=max(win // 4, 2)).mean().to_numpy()


def gen_breakout_signal(pack: MomPack, cfg: MomConfig) -> tuple[np.ndarray, int]:
    """Return (entry_long_mask, warmup_bars).

    entry_long_mask[i] = True on bars where:
        close[i] > rolling(breakout_days*24).max of high (shifted by 1)
        AND vol-confirm passes.
    """
    hours = cfg.breakout_days * 24
    # breakout level = prior max of high over `hours` (don't include current bar)
    up_level = pd.Series(pack.high).rolling(hours).max().shift(1).to_numpy()
    breakout_up = pack.close > up_level

    confirm = np.ones(len(pack.close), dtype=bool)
    if cfg.vol_confirm == "atr_exp":
        win = min(hours, 24 * 30)
        m = _roll_mean(pack.atr_frac, win)
        confirm = pack.atr_frac >= (m * cfg.confirm_k)
    elif cfg.vol_confirm == "vol_spike":
        win = min(hours, 24 * 30)
        m = _roll_mean(pack.volume, win)
        safe_m = np.where(m > 0, m, np.nan)
        ratio = np.where(np.isfinite(safe_m), pack.volume / safe_m, 0.0)
        confirm = ratio >= cfg.confirm_k
    entry = breakout_up & confirm
    warmup = max(hours, 24 * 14)  # at least 14d + lookback
    entry[:warmup] = False
    return entry, warmup


def gen_exit_level(pack: MomPack, exit_days: int) -> np.ndarray:
    """rolling min of low over exit_days*24, shifted by 1 (don't include current)."""
    hours = exit_days * 24
    return pd.Series(pack.low).rolling(hours).min().shift(1).to_numpy()


# ═══════════════════════════════════════════════════════════════════════════════
#  Simulation (per-bar, mirrors engine semantics + costs)
# ═══════════════════════════════════════════════════════════════════════════════
def simulate(pack: MomPack, cfg: MomConfig,
             init_capital: float = INITIAL_CAPITAL) -> dict:
    """Long-only momentum breakout simulation with trailing stop + opposite-signal
    exit. Returns metrics + trades + equity curve (reuses engine metrics)."""
    entry_mask, warm = gen_breakout_signal(pack, cfg)
    exit_low = gen_exit_level(pack, cfg.exit_days)
    close, high, low, atr, ts = pack.close, pack.high, pack.low, pack.atr, pack.ts
    n = len(close)
    if n < warm + 5:
        return {"metrics": eng._empty_metrics(), "trades": [], "equity_curve": np.full(n, init_capital)}

    lev = float(cfg.leverage)
    equity_curve = np.empty(n)
    equity = float(init_capital)
    in_pos = False
    entry_price = 0.0
    entry_equity = 0.0
    entry_bar = 0
    notional = 0.0
    best_price = 0.0
    trades: list[dict] = []

    equity_curve[0] = equity

    def unrealized(i):
        return notional * (close[i] / entry_price - 1.0)

    for i in range(1, n):
        # MTM equity at close[i]
        if in_pos:
            eq_now = entry_equity + unrealized(i) - notional * FUNDING_RATE / 8.0 * (i - entry_bar)
        else:
            eq_now = equity

        # exits
        if in_pos:
            if high[i] > best_price:
                best_price = high[i]
            exit_now = False
            exit_price = 0.0
            reason = ""
            # trailing stop
            if cfg.trail_atr > 0 and atr[i] > 0:
                stop = best_price - cfg.trail_atr * atr[i]
                if low[i] <= stop:
                    exit_now, exit_price, reason = True, max(stop, low[i]), "trail"
            # opposite signal: close breaks below exit-day low
            if not exit_now and not np.isnan(exit_low[i]) and close[i] < exit_low[i]:
                exit_now, exit_price, reason = True, close[i], "breakdown"
            # liquidation guard
            if not exit_now and eq_now <= entry_equity * 0.05:
                exit_now, exit_price, reason = True, close[i], "liquidation"

            if exit_now:
                holding = i - entry_bar
                funding = notional * FUNDING_RATE / 8.0 * holding
                raw = notional * (exit_price / entry_price - 1.0)
                exit_fee = notional * COST_SIDE
                pnl = raw - funding - notional * COST_SIDE - exit_fee
                equity = entry_equity + raw - funding - exit_fee
                trades.append({"side": "long", "pnl": pnl, "entry": entry_price,
                               "exit": exit_price, "bars": holding, "notional": notional,
                               "reason": reason})
                in_pos = False

        # entry (only if flat and signal fires)
        if not in_pos and entry_mask[i]:
            notional = equity * lev
            if notional > 0 and equity > 0:
                entry_price = close[i]
                entry_equity = equity - notional * COST_SIDE  # entry fee
                entry_bar = i
                best_price = high[i]
                in_pos = True

        if in_pos:
            equity_curve[i] = entry_equity + unrealized(i) - notional * FUNDING_RATE / 8.0 * (i - entry_bar)
        else:
            equity_curve[i] = equity

    # close open position at end
    if in_pos:
        i = n - 1
        holding = i - entry_bar
        funding = notional * FUNDING_RATE / 8.0 * holding
        raw = notional * (close[i] / entry_price - 1.0)
        exit_fee = notional * COST_SIDE
        equity = entry_equity + raw - funding - exit_fee
        pnl = raw - funding - notional * COST_SIDE - exit_fee
        trades.append({"side": "long", "pnl": pnl, "entry": entry_price,
                       "exit": close[i], "bars": holding, "notional": notional, "reason": "eod"})
        equity_curve[i] = equity

    metrics = eng._metrics_from_curve(equity_curve, ts, trades, init_capital, 0, n)
    return {"metrics": metrics, "trades": trades, "equity_curve": equity_curve}


# ═══════════════════════════════════════════════════════════════════════════════
#  Grid runner
# ═══════════════════════════════════════════════════════════════════════════════
# Config grid: breakout × exit × vol-confirm × trail × leverage
GRID = []
for bd in [10, 20, 40]:
    for ed in [5, 10]:
        for vc, ck in [("none", 1.0), ("atr_exp", 1.3), ("vol_spike", 1.5)]:
            for trail in [2.0, 3.0]:
                for lev in LEVERAGES:
                    GRID.append(MomConfig(bd, ed, vc, ck, trail, lev))


def run_grid(packs: dict[str, MomPack]) -> tuple[list[dict], dict]:
    results: list[dict] = []
    # buy & hold benchmarks
    benchmarks = {}
    for sym, pack in packs.items():
        bh_eq = pack.close / pack.close[0] * INITIAL_CAPITAL
        m = eng._metrics_from_curve(bh_eq, pack.ts, [], INITIAL_CAPITAL, 0, len(bh_eq))
        benchmarks[sym] = m
        results.append({"symbol": sym, "config": None, "leverage": 1,
                        "metrics": m, "tag": "benchmark", "label": "buy_hold"})
    total = len(GRID) * len(packs)
    done = 0
    t0 = time.time()
    for sym, pack in packs.items():
        for cfg in GRID:
            res = simulate(pack, cfg)
            m = res["metrics"]
            label = f"brk{cfg.breakout_days}d_exit{cfg.exit_days}d_{cfg.vol_confirm}{cfg.confirm_k}_tr{cfg.trail_atr}_{cfg.leverage}x"
            results.append({
                "symbol": sym, "config": {
                    "breakout_days": cfg.breakout_days, "exit_days": cfg.exit_days,
                    "vol_confirm": cfg.vol_confirm, "confirm_k": cfg.confirm_k,
                    "trail_atr": cfg.trail_atr, "leverage": cfg.leverage,
                },
                "leverage": cfg.leverage, "metrics": m,
                "n_trades": len(res["trades"]), "tag": "config", "label": label,
            })
            done += 1
            if done % 200 == 0:
                el = time.time() - t0
                print(f"    ...{done}/{total}  ({el:.0f}s, {done/el:.1f}/s)")
    print(f"  Grid done: {done} configs in {time.time()-t0:.1f}s")
    return results, benchmarks


# ─── helpers ──────────────────────────────────────────────────────────────────
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


def meets_gate(m: dict) -> bool:
    return m["sharpe"] > 1.0 and m["ann_return"] > 0.50 and m["max_dd"] > -0.20


# ═══════════════════════════════════════════════════════════════════════════════
#  Report generation
# ═══════════════════════════════════════════════════════════════════════════════
def generate_report(results: list[dict], benchmarks: dict, data_meta: dict) -> tuple[str, dict]:
    cfgs = [r for r in results if r.get("tag") == "config"]
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    gated = [r for r in cfgs if meets_gate(r["metrics"])]
    by_sharpe = sorted(cfgs, key=lambda r: r["metrics"]["sharpe"], reverse=True)
    by_ann = sorted(cfgs, key=lambda r: r["metrics"]["ann_return"], reverse=True)
    by_calmar = sorted(cfgs, key=lambda r: r["metrics"]["calmar"], reverse=True)

    # per-symbol best config
    by_symbol: dict[str, list[dict]] = {}
    for r in cfgs:
        by_symbol.setdefault(r["symbol"], []).append(r)
    symbol_best = {}
    for sym, rows in by_symbol.items():
        symbol_best[sym] = sorted(rows, key=lambda r: r["metrics"]["sharpe"], reverse=True)[:1]

    # per-leverage summary
    lev_summary = {}
    for lev in LEVERAGES:
        rows = [r for r in cfgs if r["leverage"] == lev]
        lev_summary[lev] = {
            "avg_ann": float(np.mean([r["metrics"]["ann_return"] for r in rows])),
            "avg_sharpe": float(np.mean([r["metrics"]["sharpe"] for r in rows])),
            "avg_maxdd": float(np.mean([r["metrics"]["max_dd"] for r in rows])),
            "avg_calmar": float(np.mean([r["metrics"]["calmar"] for r in rows])),
            "median_ann": float(np.median([r["metrics"]["ann_return"] for r in rows])),
            "n_meets": sum(1 for r in rows if meets_gate(r["metrics"])),
        }

    # per config-params (pooled across symbols)
    param_pools: dict[str, list[dict]] = {}
    for r in cfgs:
        for key in ["vol_confirm", "breakout_days", "trail_atr"]:
            v = r["config"][key]
            pk = f"{key}={v}"
            param_pools.setdefault(pk, []).append(r)

    L: list[str] = []
    def w(s=""): L.append(s)

    ref = data_meta["ref_start"]
    w("# Momentum Breakout (Volatility-Expansion) Backtest")
    w("")
    w(f"*Generated by `scripts/research_momentum_breakout.py` — {gen}. Numbers, not adjectives.*")
    w("")
    w("**Strategy:** Enter long when close breaks its rolling N-day HIGH, confirmed by a")
    w("volatility-expansion filter (ATR expansion, volume spike, or none). Exit on a trailing")
    w("ATR stop or an opposite breakdown (close < rolling M-day LOW). Long-only by design —")
    w("the thesis is catching upside altcoin pumps.")
    w("")
    w(f"**Data:** {data_meta['n_ref_bars']} hourly bars (~{data_meta['n_ref_bars']//24} days), "
      f"Binance USDC-M perps. Window {ref['start']} → {ref['end']}.")
    w(f"**Universe:** {len(DISPLAY_SYMBOLS)} USDC pairs — {', '.join(DISPLAY_SYMBOLS)}.")
    w("**Quote-coin note:** DOT/APT/OP/INJ are not listed on USDC-M futures, so those 4 use the")
    w("USDT-M perp of the same underlying (identical price dynamics; only quote coin differs).")
    w(f"**Costs:** {FEE_RATE*100:.2f}% taker + {SLIPPAGE*100:.2f}% slippage/side = "
      f"{(FEE_RATE+SLIPPAGE)*200:.2f}% round-trip, {FUNDING_RATE*100:.3f}% funding/8h.")
    w(f"**Leverage:** {', '.join(f'{l}x' for l in LEVERAGES)}. **Grid:** "
      f"{len(GRID)} configs × {len(DISPLAY_SYMBOLS)} symbols = {len(GRID)*len(DISPLAY_SYMBOLS)} backtests.")
    w(f"**Engine:** indicators + metrics reused from `research_dd_controlled_trend.py` "
      f"(identical cost model) for direct comparability with the prior trend-following round.")
    w("")
    w("---")
    w("")
    w("## Aggressive Target Gate: Sharpe > 1.0 AND Ann > 50% AND MaxDD < 20%")
    w("")
    n_meets = len(gated)
    w(f"**{n_meets}** of {len(cfgs)} configs meet **all three** targets simultaneously.")
    w("")
    if n_meets == 0:
        # report how many meet 2/3
        def score(r):
            s = 0
            if r["metrics"]["sharpe"] > 1.0: s += 1
            if r["metrics"]["ann_return"] > 0.50: s += 1
            if r["metrics"]["max_dd"] > -0.20: s += 1
            return s
        twothirds = [r for r in cfgs if score(r) >= 2]
        w(f"{len(twothirds)} configs meet **at least 2 of 3** targets.")
    w("")

    # ── TOP 5 by Sharpe ──
    w("## Top 5 by Sharpe Ratio")
    w("")
    w("| # | Symbol | Config | Ann | Sharpe | Sortino | MaxDD | Calmar | PF | Win% | Trades |")
    w("|---|--------|--------|----:|-------:|--------:|------:|-------:|---:|-----:|-------:|")
    for i, r in enumerate(by_sharpe[:5], 1):
        m = r["metrics"]
        w(f"| {i} | {r['symbol']} | {r['label']} | {fmt_pct(m['ann_return'])} | "
          f"{m['sharpe']:.2f} | {m['sortino']:.2f} | {fmt_pct(m['max_dd'])} | "
          f"{m['calmar']:.2f} | {m['profit_factor']:.2f} | {m['win_rate']*100:.0f}% | "
          f"{r['n_trades']} |")
    w("")

    # ── TOP 5 by annualized return ──
    w("## Top 5 by Annualized Return")
    w("")
    w("| # | Symbol | Config | Ann | Sharpe | Sortino | MaxDD | Calmar | PF | Win% | Trades |")
    w("|---|--------|--------|----:|-------:|--------:|------:|-------:|---:|-----:|-------:|")
    for i, r in enumerate(by_ann[:5], 1):
        m = r["metrics"]
        w(f"| {i} | {r['symbol']} | {r['label']} | {fmt_pct(m['ann_return'])} | "
          f"{m['sharpe']:.2f} | {m['sortino']:.2f} | {fmt_pct(m['max_dd'])} | "
          f"{m['calmar']:.2f} | {m['profit_factor']:.2f} | {m['win_rate']*100:.0f}% | "
          f"{r['n_trades']} |")
    w("")

    # ── TOP 5 by Calmar (return / maxDD) ──
    w("## Top 5 by Calmar (Ann / |MaxDD|)")
    w("")
    w("| # | Symbol | Config | Ann | Sharpe | MaxDD | Calmar | Trades |")
    w("|---|--------|--------|----:|-------:|------:|-------:|-------:|")
    for i, r in enumerate(by_calmar[:5], 1):
        m = r["metrics"]
        w(f"| {i} | {r['symbol']} | {r['label']} | {fmt_pct(m['ann_return'])} | "
          f"{m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {r['n_trades']} |")
    w("")

    # ── Gate-passing configs (if any) ──
    w("## Gate-Passing Configs (Sharpe>1.0 & Ann>50% & MaxDD<20%)")
    w("")
    if gated:
        gd = sorted(gated, key=lambda r: r["metrics"]["sharpe"], reverse=True)
        w(f"**{len(gd)} config(s)** clear all three bars.")
        w("")
        w("| # | Symbol | Config | Ann | Sharpe | MaxDD | Calmar | Trades |")
        w("|---|--------|--------|----:|-------:|------:|-------:|-------:|")
        for i, r in enumerate(gd[:25], 1):
            m = r["metrics"]
            w(f"| {i} | {r['symbol']} | {r['label']} | {fmt_pct(m['ann_return'])} | "
              f"{m['sharpe']:.2f} | {fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {r['n_trades']} |")
        w("")
    else:
        w("**No config clears all three bars simultaneously.** See Top-5 tables above for the")
        w("closest results, and the failure analysis below.")
        w("")

    # ── Per-symbol best ──
    w("## Best Config per Symbol (by Sharpe)")
    w("")
    w("| Symbol | Buy&Hold Ann | B&H MaxDD | Best Config | Ann | Sharpe | MaxDD | Calmar | Trades |")
    w("|--------|------------:|----------:|-------------|----:|-------:|------:|-------:|-------:|")
    for sym in DISPLAY_SYMBOLS:
        bh = benchmarks[sym]
        best_rows = symbol_best.get(sym, [])
        if best_rows:
            r = best_rows[0]
            m = r["metrics"]
            w(f"| {sym} | {fmt_pct(bh['ann_return'])} | {fmt_pct(bh['max_dd'])} | "
              f"{r['label']} | {fmt_pct(m['ann_return'])} | {m['sharpe']:.2f} | "
              f"{fmt_pct(m['max_dd'])} | {m['calmar']:.2f} | {r['n_trades']} |")
        else:
            w(f"| {sym} | {fmt_pct(bh['ann_return'])} | {fmt_pct(bh['max_dd'])} | — | — | — | — | — | — |")
    w("")

    # ── Leverage summary ──
    w("## Leverage Summary (averaged across all configs)")
    w("")
    w("| Lev | Avg Ann | Median Ann | Avg Sharpe | Avg MaxDD | Avg Calmar | # Meet Gate |")
    w("|----:|--------:|-----------:|-----------:|----------:|-----------:|------------:|")
    for lev in LEVERAGES:
        s = lev_summary[lev]
        w(f"| {lev}x | {fmt_pct(s['avg_ann'])} | {fmt_pct(s['median_ann'])} | "
          f"{s['avg_sharpe']:.2f} | {fmt_pct(s['avg_maxdd'])} | {s['avg_calmar']:.2f} | "
          f"{s['n_meets']} |")
    w("")

    # ── Vol-confirm comparison ──
    w("## Volatility-Confirmation Filter Comparison (pooled across symbols)")
    w("")
    w("Does adding an ATR-expansion or volume-spike confirmation to the raw breakout help?")
    w("")
    w("| Filter | Avg Ann | Avg Sharpe | Avg MaxDD | # Meet Gate |")
    w("|--------|--------:|-----------:|----------:|------------:|")
    for vc in ["none", "atr_exp", "vol_spike"]:
        rows = param_pools.get(f"vol_confirm={vc}", [])
        if not rows:
            continue
        w(f"| {vc} | {fmt_pct(float(np.mean([r['metrics']['ann_return'] for r in rows])))} | "
          f"{float(np.mean([r['metrics']['sharpe'] for r in rows])):.2f} | "
          f"{fmt_pct(float(np.mean([r['metrics']['max_dd'] for r in rows])))} | "
          f"{sum(1 for r in rows if meets_gate(r['metrics']))} |")
    w("")

    # ── Breakout lookback comparison ──
    w("## Breakout Lookback Comparison (pooled)")
    w("")
    w("| Lookback | Avg Ann | Avg Sharpe | Avg MaxDD | # Meet Gate |")
    w("|---------:|--------:|-----------:|----------:|------------:|")
    for bd in [10, 20, 40]:
        rows = param_pools.get(f"breakout_days={bd}", [])
        if not rows:
            continue
        w(f"| {bd}d | {fmt_pct(float(np.mean([r['metrics']['ann_return'] for r in rows])))} | "
          f"{float(np.mean([r['metrics']['sharpe'] for r in rows])):.2f} | "
          f"{fmt_pct(float(np.mean([r['metrics']['max_dd'] for r in rows])))} | "
          f"{sum(1 for r in rows if meets_gate(r['metrics']))} |")
    w("")

    # ── Honest failure analysis ──
    w("## Honest Assessment")
    w("")
    n_pos = sum(1 for r in cfgs if r["metrics"]["ann_return"] > 0)
    n_neg = sum(1 for r in cfgs if r["metrics"]["ann_return"] <= 0)
    avg_sharpe = float(np.mean([r["metrics"]["sharpe"] for r in cfgs]))
    avg_ann = float(np.mean([r["metrics"]["ann_return"] for r in cfgs]))
    median_ann = float(np.median([r["metrics"]["ann_return"] for r in cfgs]))
    avg_dd = float(np.mean([r["metrics"]["max_dd"] for r in cfgs]))
    w(f"- **{n_pos}** of {len(cfgs)} configs are profitable; **{n_neg}** are not.")
    w(f"- Mean Sharpe across all configs: **{avg_sharpe:.2f}**; mean annualized: **{fmt_pct(avg_ann)}**; "
      f"median annualized: **{fmt_pct(median_ann)}**; mean max DD: **{fmt_pct(avg_dd)}**.")
    w(f"- **{n_meets}** config(s) meet the aggressive gate (Sharpe>1.0 & Ann>50% & MaxDD<20%).")
    best_sharpe = by_sharpe[0]["metrics"]["sharpe"] if by_sharpe else 0.0
    best_ann = by_ann[0]["metrics"]["ann_return"] if by_ann else 0.0
    w(f"- Best single-config Sharpe: **{best_sharpe:.2f}**; best single-config annualized: **{fmt_pct(best_ann)}**.")
    # compare to buy & hold
    bh_anns = [benchmarks[s]["ann_return"] for s in DISPLAY_SYMBOLS]
    mean_bh = float(np.mean(bh_anns))
    w(f"- Buy & hold annualized across the universe: mean **{fmt_pct(mean_bh)}**, "
      f"range **{fmt_pct(min(bh_anns))}** to **{fmt_pct(max(bh_anns))}**.")
    if n_meets > 0:
        w("- **Verdict:** Momentum breakout has signal in this regime — at least one config clears the")
        w("  aggressive gate. Candidate for escalation with walk-forward validation.")
    else:
        avg_profitable = avg_ann > 0
        beats_bh = avg_ann > mean_bh
        if avg_profitable and beats_bh:
            w("- **Verdict:** Momentum breakout is profitable on average and beats buy & hold, but no")
            w("  single config clears the full aggressive gate (typically the MaxDD or Sharpe bar).")
            w("  Promising as a component but not standalone-deployable at these targets.")
        elif beats_bh:  # loses less than B&H but still net-negative (e.g. a bear market)
            w(f"- **Verdict:** Momentum breakout loses money on average (mean **{fmt_pct(avg_ann)}**) but")
            w(f"  still beats buy & hold (**{fmt_pct(mean_bh)}**) in this window — the trailing-stop exits")
            w("  cap losses relative to a passive long. No config clears the aggressive gate: returns and")
            w("  Sharpe are too low and drawdowns too deep. The window is a severe bear market for the")
            w("  universe, which is structurally hostile to a long-only breakout. Promising as a defensive")
            w("  long-only satellite with a regime filter, but not standalone-deployable here.")
        else:
            w("- **Verdict:** Momentum breakout does **not** beat buy & hold on average in this window.")
            w("  The breakout entry gets whipsawed in choppy/ranging periods common to this regime.")
            w("  Not a standalone winner here; revisit with a regime filter or as a long-only satellite.")
    w("")

    md = "\n".join(L) + "\n"

    raw = {
        "generated_utc": gen,
        "data": {
            "n_bars": data_meta["n_ref_bars"],
            "start": ref["start"], "end": ref["end"],
            "symbols": DISPLAY_SYMBOLS,
            "fetch_log": data_meta["fetch_log"],
        },
        "config": {
            "fee_rate": FEE_RATE, "slippage": SLIPPAGE, "funding_rate": FUNDING_RATE,
            "leverages": LEVERAGES, "grid_size": len(GRID),
            "grid_params": {"breakout_days": [10, 20, 40], "exit_days": [5, 10],
                            "vol_confirm": ["none", "atr_exp", "vol_spike"],
                            "trail_atr": [2.0, 3.0]},
            "gate": {"sharpe_gt": 1.0, "ann_gt": 0.50, "maxdd_gt": -0.20},
        },
        "benchmarks": {s: benchmarks[s] for s in DISPLAY_SYMBOLS},
        "results": [{"symbol": r["symbol"], "label": r["label"],
                     "leverage": r["leverage"], "n_trades": r.get("n_trades", 0),
                     "config": r["config"], "metrics": r["metrics"]}
                    for r in cfgs],
        "summary": {
            "n_configs": len(cfgs), "n_meet_gate": n_meets,
            "n_profitable": n_pos, "n_unprofitable": n_neg,
            "mean_sharpe": avg_sharpe, "mean_ann": avg_ann,
            "median_ann": median_ann, "mean_maxdd": avg_dd,
            "best_sharpe": {"symbol": by_sharpe[0]["symbol"], "label": by_sharpe[0]["label"],
                            "metrics": by_sharpe[0]["metrics"]} if by_sharpe else None,
            "best_ann": {"symbol": by_ann[0]["symbol"], "label": by_ann[0]["label"],
                         "metrics": by_ann[0]["metrics"]} if by_ann else None,
            "top5_by_sharpe": [{"symbol": r["symbol"], "label": r["label"],
                                "leverage": r["leverage"], "n_trades": r["n_trades"],
                                "metrics": r["metrics"]} for r in by_sharpe[:5]],
            "top5_by_ann": [{"symbol": r["symbol"], "label": r["label"],
                             "leverage": r["leverage"], "n_trades": r["n_trades"],
                             "metrics": r["metrics"]} for r in by_ann[:5]],
            "best_per_symbol": {sym: ({"label": symbol_best[sym][0]["label"],
                                       "leverage": symbol_best[sym][0]["leverage"],
                                       "n_trades": symbol_best[sym][0]["n_trades"],
                                       "metrics": symbol_best[sym][0]["metrics"]}
                                      if symbol_best.get(sym) else None)
                                for sym in DISPLAY_SYMBOLS},
            "leverage_summary": {str(k): v for k, v in lev_summary.items()},
        },
    }
    return md, raw


# ═══════════════════════════════════════════════════════════════════════════════
def main():
    print("═" * 72)
    print(" MOMENTUM BREAKOUT (VOLATILITY-EXPANSION) BACKTEST — RESEARCH ONLY")
    print("═" * 72)
    print("Loading data ...")
    data, meta = load_all_data()
    btc = data["BTCUSDC"]
    ref_start = pd.to_datetime(btc["ts"].iloc[0], unit="ms", utc=True).strftime("%Y-%m-%d")
    ref_end = pd.to_datetime(btc["ts"].iloc[-1], unit="ms", utc=True).strftime("%Y-%m-%d")
    print(f"  Reference grid: {meta['n_ref_bars']} bars ({meta['n_ref_bars']//24} days)  {ref_start} -> {ref_end}")
    for sym, n, src in meta["fetch_log"]:
        print(f"    {sym:10s} {n:>6d} bars  [{src}]")

    print("Building indicator packs ...")
    packs: dict[str, MomPack] = {}
    for sym in DISPLAY_SYMBOLS:
        packs[sym] = build_pack(data[sym])

    print(f"Running grid: {len(GRID)} configs x {len(packs)} symbols = {len(GRID)*len(packs)} backtests ...")
    results, benchmarks = run_grid(packs)

    print("Generating report ...")
    data_meta = {"n_ref_bars": meta["n_ref_bars"], "ref_start": {"start": ref_start, "end": ref_end},
                 "fetch_log": meta["fetch_log"]}
    md, raw = generate_report(results, benchmarks, data_meta)
    REPORT_MD.write_text(md)
    REPORT_JSON.write_text(json.dumps(raw, default=_json_default, indent=2))
    print(f"  Report:  {REPORT_MD}")
    print(f"  Data:    {REPORT_JSON}")

    n_meets = raw["summary"]["n_meet_gate"]
    print()
    print(f"RESULT: {n_meets}/{len(GRID)*len(packs)} configs meet the aggressive gate.")
    bs = raw["summary"]["best_sharpe"]
    ba = raw["summary"]["best_ann"]
    if bs:
        print(f"  Best Sharpe: {bs['metrics']['sharpe']:.2f}  ({bs['symbol']} {bs['label']})")
    if ba:
        print(f"  Best Ann:    {ba['metrics']['ann_return']*100:.1f}%  ({ba['symbol']} {ba['label']})")
    print()
    print("Top 5 by Sharpe:")
    for r in raw["summary"]["top5_by_sharpe"]:
        m = r["metrics"]
        print(f"  {r['symbol']:9s} {r['label']:42s}  Sharpe {m['sharpe']:.2f}  "
              f"Ann {m['ann_return']*100:.1f}%  MaxDD {m['max_dd']*100:.1f}%  Trades {r['n_trades']}")
    print("Top 5 by Ann:")
    for r in raw["summary"]["top5_by_ann"]:
        m = r["metrics"]
        print(f"  {r['symbol']:9s} {r['label']:42s}  Sharpe {m['sharpe']:.2f}  "
              f"Ann {m['ann_return']*100:.1f}%  MaxDD {m['max_dd']*100:.1f}%  Trades {r['n_trades']}")


if __name__ == "__main__":
    main()
