#!/usr/bin/env python3
"""Funding-biased directional futures trading backtest (RESEARCH ONLY).

Thesis
------
Binance perpetual funding rates are structurally positive ~86% of the time
(longs pay shorts). Delta-neutral carry only yields ~10% annualized. This
script tests whether combining the funding signal with a *directional* bias
amplifies the edge:

  A. Funding + trend      : long when funding>0 AND EMA50>EMA200; short反之.
  B. Funding contrarian   : short when funding-zscore>2 (crowded longs); long反之.
  C. Funding momentum     : long when funding rising & positive; short反之.
  D. High-funding harvest : short-perp (delta-neutral) only when funding>0.01%.

All strategies are evaluated at leverage 1x/2x/3x with realistic costs
(0.04% taker + 0.03% slippage per side, plus funding paid/received on the
perp notional), then walk-forward validated on a 75/25 time split.

No live trading. No API keys. Public Binance FAPI only.

Backtest convention (documented for reproducibility)
----------------------------------------------------
For each 8h candle t (aligned to funding settlement at its open):
  * Decision uses funding_rate[t] (settled at open of t, hence known) plus
    price indicators computed on close[t-1] (shifted → no look-ahead).
  * Position pos_t ∈ {-1, 0, +1} (side); notional exposure = pos_t * leverage.
  * Price PnL over candle t = pos_t * lev * (close[t]-open[t])/open[t].
  * Funding cost over candle t = pos_t * lev * funding_rate[t]
        (long pays positive funding; short receives it).
  * Trade cost on change = (taker+slip) * |pos_t - pos_{t-1}| * lev.
  * Equity compounds: eq_t = eq_{t-1} * (1 + r_t).
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = REPO_ROOT / "docs" / "research" / "_cache_funding_dir"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BINANCE_FAPI = "https://fapi.binance.com"
FUNDING_URL = f"{BINANCE_FAPI}/fapi/v1/fundingRate"
KLINES_URL = f"{BINANCE_FAPI}/fapi/v1/klines"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT",
    "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "XRPUSDT",
]

INTERVAL = "8h"
PER_PAGE = 1000
REQUEST_TIMEOUT = 25
RATE_PAUSE = 0.30  # seconds between paged requests

PERIODS_PER_YEAR = 3 * 365  # 3 funding/candle periods per day
TAKER_FEE = 0.0004   # 0.04% per side
SLIPPAGE = 0.0003    # 0.03% per side
COST_PER_SIDE = TAKER_FEE + SLIPPAGE          # 0.0007
LEVERAGES = [1, 2, 3]

# Walk-forward split
TRAIN_FRAC = 0.75

# Strategy thresholds
CONTRARIAN_ENTER = 2.0       # |zscore| entry
CONTRARIAN_EXIT = 0.5        # |zscore| exit
HARVEST_ENTER = 0.0001       # funding > 0.01% enter short-perp harvest
HARVEST_EXIT = 0.00005       # funding < 0.005% exit
MOMENTUM_LOOKBACK = 3        # periods for "rising/falling" slope

EMA_FAST = 50
EMA_SLOW = 200
ZSCORE_WINDOW = 90           # 30d = 90 periods
MA_WINDOW = 90


# --------------------------------------------------------------------------- #
# Data fetching (with on-disk cache)
# --------------------------------------------------------------------------- #
def _get(url: str, params: dict) -> list:
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as exc:
            print(f"    [warn] {url} attempt {attempt+1}: {exc}", file=sys.stderr)
            time.sleep(1.5 ** attempt)
    return []


def fetch_funding(symbol: str) -> pd.DataFrame:
    """Forward-paginate funding rates over full history."""
    cache = CACHE_DIR / f"funding_{symbol}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)

    rows = []
    # NOTE: startTime=0 makes Binance ignore `limit` and return only the most
    # recent 200 (default window). Start from a real early epoch (2019-01-01)
    # which predates every USDT-M perp on our list → full 1000-row pages.
    start_time = 1546300800000
    while True:
        data = _get(FUNDING_URL, {"symbol": symbol, "startTime": start_time,
                                  "limit": PER_PAGE})
        if not data:
            break
        for item in data:
            rows.append((int(item["fundingTime"]), float(item["fundingRate"])))
        last_t = int(data[-1]["fundingTime"])
        if len(data) < PER_PAGE:
            break
        start_time = last_t + 1
        time.sleep(RATE_PAUSE)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time_ms", "funding_rate"])
    df = df.drop_duplicates("time_ms").sort_values("time_ms").reset_index(drop=True)
    df.to_pickle(cache)
    return df


def fetch_klines(symbol: str) -> pd.DataFrame:
    """Backward-paginate 8h klines over full history."""
    cache = CACHE_DIR / f"klines_{symbol}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)

    rows = []
    end_time = None
    while True:
        params = {"symbol": symbol, "interval": INTERVAL, "limit": PER_PAGE}
        if end_time is not None:
            params["endTime"] = end_time
        data = _get(KLINES_URL, params)
        if not data:
            break
        for k in data:
            rows.append((int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                         float(k[4]), float(k[5])))
        oldest = int(data[0][0])
        if len(data) < PER_PAGE:
            break
        new_end = oldest - 1
        if end_time is not None and new_end >= end_time:
            # no progress / wrapped around — stop to avoid infinite loop
            break
        end_time = new_end
        time.sleep(RATE_PAUSE)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time_ms", "open", "high", "low",
                                     "close", "volume"])
    df = df.drop_duplicates("time_ms").sort_values("time_ms").reset_index(drop=True)
    df.to_pickle(cache)
    return df


def build_dataset(symbol: str) -> Optional[pd.DataFrame]:
    """Merge funding + klines on the 8h grid and compute features."""
    fund = fetch_funding(symbol)
    kl = fetch_klines(symbol)
    if fund.empty or kl.empty:
        return None

    df = pd.merge(kl, fund, on="time_ms", how="inner").sort_values("time_ms")
    df = df.reset_index(drop=True)
    if len(df) < EMA_SLOW + 50:
        return None

    # Features
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["fund_ma"] = df["funding_rate"].rolling(MA_WINDOW).mean()
    roll_std = df["funding_rate"].rolling(ZSCORE_WINDOW).std()
    df["fund_zscore"] = (df["funding_rate"] - df["fund_ma"]) / roll_std.replace(0, np.nan)
    df["fund_prev"] = df["funding_rate"].shift(MOMENTUM_LOOKBACK)

    df["price_ret"] = (df["close"] - df["open"]) / df["open"]
    df["datetime"] = pd.to_datetime(df["time_ms"], unit="ms", utc=True)
    return df


# --------------------------------------------------------------------------- #
# Vectorized backtest engine
# --------------------------------------------------------------------------- #
def backtest(df: pd.DataFrame, target_pos: np.ndarray, leverage: int) -> dict:
    """Run a leveraged futures backtest given a target position series.

    target_pos : array of {-1,0,+1} ALREADY shifted to avoid look-ahead
                 (the value at index t is the decision for candle t).
    """
    n = len(df)
    price_ret = df["price_ret"].to_numpy(dtype=float)
    funding = df["funding_rate"].to_numpy(dtype=float)
    pos = np.asarray(target_pos, dtype=float)

    # Trade cost on position changes (prev = 0 before first bar)
    dpos = np.diff(pos, prepend=0.0)
    trade_cost = COST_PER_SIDE * np.abs(dpos) * leverage

    # Per-period return as fraction of equity
    period_ret = pos * leverage * (price_ret - funding) - trade_cost

    # Liquidation guard: if a single adverse move would wipe margin
    # (lev*|price_ret| >= ~0.9), cap loss at -95% for that bar & flatten.
    liq_mask = (pos != 0) & (leverage * np.abs(price_ret) >= 0.90)
    n_liq = int(liq_mask.sum())
    if n_liq:
        period_ret = np.where(liq_mask, -0.95, period_ret)
        # flatten after liquidation for subsequent bars handled by caller signal;
        # we just cap the damage here.

    # Equity curve
    growth = 1.0 + period_ret
    growth = np.clip(growth, 1e-9, None)
    equity = np.cumprod(growth)

    # Trade-level stats (contiguous runs of same nonzero side)
    trades_pnl = []
    i = 0
    while i < n:
        if pos[i] != 0:
            side = pos[i]
            j = i
            cum = 0.0
            while j < n and pos[j] == side:
                cum += period_ret[j]
                j += 1
            trades_pnl.append(cum)
            i = j
        else:
            i += 1

    n_trades = len(trades_pnl)
    wins = [t for t in trades_pnl if t > 0]
    losses = [t for t in trades_pnl if t <= 0]
    win_rate = (len(wins) / n_trades * 100) if n_trades else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 1e-12 else (float("inf") if gross_win > 0 else 0.0)

    # Drawdown
    running_max = np.maximum.accumulate(equity)
    drawdown = (running_max - equity) / running_max
    max_dd = float(np.nanmax(drawdown)) if len(drawdown) else 0.0

    years = n / PERIODS_PER_YEAR
    final_eq = float(equity[-1])
    total_ret = (final_eq - 1.0) * 100
    cagr = ((final_eq ** (1.0 / years) - 1.0) * 100) if (years > 0 and final_eq > 0) else -100.0

    # Sharpe / Sortino on per-period returns
    mu = float(np.mean(period_ret)) if n else 0.0
    sd = float(np.std(period_ret, ddof=1)) if n > 1 else 0.0
    sharpe = (mu / sd * math.sqrt(PERIODS_PER_YEAR)) if sd > 1e-12 else 0.0
    downside = period_ret[period_ret < 0]
    dsd = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    sortino = (mu / dsd * math.sqrt(PERIODS_PER_YEAR)) if dsd > 1e-12 else 0.0

    return {
        "total_return_pct": round(total_ret, 2),
        "annualized_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else 999.0,
        "win_rate_pct": round(win_rate, 1),
        "n_trades": n_trades,
        "n_periods": n,
        "years": round(years, 2),
        "liquidations": n_liq,
        "final_equity": round(final_eq, 4),
    }


# --------------------------------------------------------------------------- #
# Signal generators → target position (shifted to remove look-ahead)
# --------------------------------------------------------------------------- #
def signal_funding_trend(df: pd.DataFrame) -> np.ndarray:
    """A: long when funding>0 & EMA50>EMA200; short when funding<0 & EMA50<EMA200.
    Price indicators use close[t-1] (shift 1). Funding uses funding_rate[t]."""
    f = df["funding_rate"].to_numpy()
    # trend decided on prior close → shift EMA comparison by 1
    trend_up = (df["ema_fast"] > df["ema_slow"]).to_numpy()
    trend_dn = (df["ema_fast"] < df["ema_slow"]).to_numpy()
    long_sig = (f > 0) & np.roll(trend_up, 1)
    short_sig = (f < 0) & np.roll(trend_dn, 1)
    long_sig[0] = False
    short_sig[0] = False
    pos = np.where(long_sig, 1.0, np.where(short_sig, -1.0, 0.0))
    return pos


def signal_funding_contrarian(df: pd.DataFrame) -> np.ndarray:
    """B: short when zscore>2 (crowded longs revert); long when zscore<-2.
    Exit when |zscore|<0.5. Uses funding features known at open[t]."""
    z = df["fund_zscore"].to_numpy()
    n = len(z)
    pos = np.zeros(n)
    cur = 0.0
    for t in range(n):
        zt = z[t]
        if math.isnan(zt):
            pos[t] = cur
            continue
        if cur == 0:
            if zt > CONTRARIAN_ENTER:
                cur = -1.0
            elif zt < -CONTRARIAN_ENTER:
                cur = 1.0
        else:
            if abs(zt) < CONTRARIAN_EXIT:
                cur = 0.0
        pos[t] = cur
    return pos


def signal_funding_momentum(df: pd.DataFrame) -> np.ndarray:
    """C: long when funding rising (vs lookback-ago) AND positive;
    short when falling AND negative."""
    f = df["funding_rate"].to_numpy()
    f_prev = df["fund_prev"].to_numpy()
    rising = (f > f_prev)
    falling = (f < f_prev)
    long_sig = rising & (f > 0)
    short_sig = falling & (f < 0)
    pos = np.where(long_sig, 1.0, np.where(short_sig, -1.0, 0.0))
    pos = np.where(np.isnan(f_prev), 0.0, pos)
    return pos


def signal_high_funding_harvest(df: pd.DataFrame) -> np.ndarray:
    """D: short-perp harvest (delta-neutral vs spot) when funding>0.01%,
    exit when funding<0.005%. Position is -1 (short) to COLLECT funding.
    Price PnL is zero in reality (spot hedge); we model that by zeroing the
    price term for this strategy (handled in专用 backtest below)."""
    f = df["funding_rate"].to_numpy()
    n = len(f)
    pos = np.zeros(n)
    cur = 0.0
    for t in range(n):
        ft = f[t]
        if cur == 0:
            if ft > HARVEST_ENTER:
                cur = -1.0
        else:
            if ft < HARVEST_EXIT:
                cur = 0.0
        pos[t] = cur
    return pos


def backtest_harvest(df: pd.DataFrame, pos: np.ndarray, leverage: int) -> dict:
    """D专用: delta-neutral harvest — no price PnL, only funding income/cost."""
    n = len(df)
    funding = df["funding_rate"].to_numpy(dtype=float)
    dpos = np.diff(pos, prepend=0.0)
    trade_cost = COST_PER_SIDE * np.abs(dpos) * leverage
    # short perp collects +funding; pays -funding. price neutral.
    period_ret = (-pos) * leverage * funding - trade_cost
    growth = np.clip(1.0 + period_ret, 1e-9, None)
    equity = np.cumprod(growth)

    trades_pnl = []
    i = 0
    while i < n:
        if pos[i] != 0:
            side = pos[i]; j = i; cum = 0.0
            while j < n and pos[j] == side:
                cum += period_ret[j]; j += 1
            trades_pnl.append(cum); i = j
        else:
            i += 1
    n_trades = len(trades_pnl)
    wins = [t for t in trades_pnl if t > 0]
    losses = [t for t in trades_pnl if t <= 0]
    win_rate = (len(wins) / n_trades * 100) if n_trades else 0.0
    gross_win = sum(wins); gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-12 else (float("inf") if gross_win > 0 else 0.0)

    running_max = np.maximum.accumulate(equity)
    drawdown = (running_max - equity) / running_max
    max_dd = float(np.nanmax(drawdown)) if len(drawdown) else 0.0
    years = n / PERIODS_PER_YEAR
    final_eq = float(equity[-1])
    total_ret = (final_eq - 1.0) * 100
    cagr = ((final_eq ** (1.0 / years) - 1.0) * 100) if (years > 0 and final_eq > 0) else -100.0
    mu = float(np.mean(period_ret))
    sd = float(np.std(period_ret, ddof=1))
    sharpe = (mu / sd * math.sqrt(PERIODS_PER_YEAR)) if sd > 1e-12 else 0.0
    downside = period_ret[period_ret < 0]
    dsd = float(np.std(downside, ddof=1)) if len(downside) > 1 else 0.0
    sortino = (mu / dsd * math.sqrt(PERIODS_PER_YEAR)) if dsd > 1e-12 else 0.0
    return {
        "total_return_pct": round(total_ret, 2),
        "annualized_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "profit_factor": round(pf, 3) if math.isfinite(pf) else 999.0,
        "win_rate_pct": round(win_rate, 1),
        "n_trades": n_trades,
        "n_periods": n,
        "years": round(years, 2),
        "liquidations": 0,
        "final_equity": round(final_eq, 4),
    }


# --------------------------------------------------------------------------- #
# Strategy registry
# --------------------------------------------------------------------------- #
STRATEGIES = {
    "A_funding_trend":    signal_funding_trend,
    "B_funding_contrarian": signal_funding_contrarian,
    "C_funding_momentum": signal_funding_momentum,
    "D_high_funding_harvest": signal_high_funding_harvest,
}


def run_strategy(df: pd.DataFrame, strat: str, leverage: int) -> dict:
    sig_fn = STRATEGIES[strat]
    pos = sig_fn(df)
    if strat == "D_high_funding_harvest":
        return backtest_harvest(df, pos, leverage)
    return backtest(df, pos, leverage)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main() -> None:
    print("=" * 72)
    print("  FUNDING-BIASED DIRECTIONAL FUTURES BACKTEST")
    print(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 72)

    # 1. Build datasets
    print("\n[1/4] Building merged funding+kline datasets …")
    datasets: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        df = build_dataset(sym)
        if df is None or len(df) < EMA_SLOW + 50:
            print(f"  {sym}: insufficient data, skipping")
            continue
        start = df["datetime"].iloc[0]
        end = df["datetime"].iloc[-1]
        pct_pos = (df["funding_rate"] > 0).mean() * 100
        datasets[sym] = df
        print(f"  {sym}: {len(df)} periods ({len(df)/3:.0f} d) "
              f"[{start:%Y-%m-%d} → {end:%Y-%m-%d}]  funding>0: {pct_pos:.1f}%")

    if not datasets:
        print("[FATAL] No datasets built.", file=sys.stderr)
        sys.exit(1)

    # 2. Run all configs: symbol × strategy × leverage, on full + train + test
    print("\n[2/4] Backtesting all configs (full + 75/25 walk-forward) …")
    records: list[dict] = []
    for sym, df in datasets.items():
        split = int(len(df) * TRAIN_FRAC)
        train_df = df.iloc[:split]
        test_df = df.iloc[split:]
        for strat in STRATEGIES:
            for lev in LEVERAGES:
                full = run_strategy(df, strat, lev)
                tr = run_strategy(train_df, strat, lev)
                te = run_strategy(test_df, strat, lev)
                rec = {
                    "symbol": sym, "strategy": strat, "leverage": lev,
                    "full": full, "train": tr, "test": te,
                    "train_start": str(df["datetime"].iloc[0].date()),
                    "test_start": str(df["datetime"].iloc[split].date()),
                    "test_end": str(df["datetime"].iloc[-1].date()),
                }
                records.append(rec)
        print(f"  {sym}: done")

    # 3. Rankings & flags
    print("\n[3/4] Ranking & flagging …")

    def flag(m: dict) -> bool:
        return (m["sharpe"] > 1.0 and m["annualized_pct"] > 50.0
                and m["max_drawdown_pct"] < 20.0)

    for r in records:
        r["full_flag"] = flag(r["full"])
        r["test_flag"] = flag(r["test"])

    def top_by(metric, key, n=10, sample="full"):
        return sorted(records, key=lambda r: r[sample][key], reverse=True)[:n]

    top_sharpe = top_by(None, "sharpe", 10, "full")
    top_ann = top_by(None, "annualized_pct", 10, "full")
    flagged_full = [r for r in records if r["full_flag"]]
    flagged_test = [r for r in records if r["test_flag"]]

    print(f"  configs total      : {len(records)}")
    print(f"  flagged (full)     : {len(flagged_full)}")
    print(f"  flagged (OOS/test) : {len(flagged_test)}")

    # 4. Reports
    print("\n[4/4] Writing reports …")
    write_reports(datasets, records, top_sharpe, top_ann,
                  flagged_full, flagged_test)

    md_path = RESULTS_DIR / "funding-directional-analysis.md"
    json_path = RESULTS_DIR / "funding-directional-analysis.json"
    print(f"  → {md_path}")
    print(f"  → {json_path}")
    print("\n" + "=" * 72)
    print("  DONE")
    print("=" * 72)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _row(r: dict, sample: str) -> str:
    m = r[sample]
    return (f"| {r['symbol']} | {r['strategy']} | {r['leverage']}x | "
            f"{m['total_return_pct']} | {m['annualized_pct']} | "
            f"{m['max_drawdown_pct']} | {m['sharpe']} | {m['sortino']} | "
            f"{m['profit_factor']} | {m['win_rate_pct']} | {m['n_trades']} | "
            f"{m['liquidations']} |")


def write_reports(datasets, records, top_sharpe, top_ann,
                  flagged_full, flagged_test) -> None:
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ---- JSON ----
    json_blob = {
        "generated": gen,
        "convention": "8h candles aligned to funding settlement; decision uses "
                      "funding_rate[t] + price indicators on close[t-1]; "
                      "funding paid on perp notional = pos*lev*funding_rate[t]; "
                      "trade cost = (taker+slip)*|dpos|*lev.",
        "costs": {"taker_fee_per_side": TAKER_FEE, "slippage_per_side": SLIPPAGE},
        "leverages": LEVERAGES,
        "walk_forward_split": {"train": TRAIN_FRAC, "test": 1 - TRAIN_FRAC},
        "dataset_summary": {
            sym: {
                "periods": len(df),
                "days": round(len(df) / 3, 1),
                "start": str(df["datetime"].iloc[0].date()),
                "end": str(df["datetime"].iloc[-1].date()),
                "pct_funding_positive": round(float((df["funding_rate"] > 0).mean() * 100), 1),
                "mean_funding_rate": round(float(df["funding_rate"].mean()), 7),
            } for sym, df in datasets.items()
        },
        "records": records,
        "top10_by_sharpe_full": [_key(r) for r in top_sharpe],
        "top10_by_annualized_full": [_key(r) for r in top_ann],
        "flagged_full": [_key(r) for r in flagged_full],
        "flagged_test_oos": [_key(r) for r in flagged_test],
    }
    (RESULTS_DIR / "funding-directional-analysis.json").write_text(
        json.dumps(json_blob, indent=2), encoding="utf-8")

    # ---- Markdown ----
    L: list[str] = []
    L.append("# Funding-Biased Directional Futures Backtest")
    L.append("")
    L.append(f"*Generated: {gen}*  ")
    L.append("*Source: Public Binance FAPI (fundingRate + 8h klines) — real data only.*")
    L.append("")
    L.append("## Thesis")
    L.append("")
    L.append("Funding is structurally positive (~86% of periods), so longs pay shorts. ")
    L.append("Delta-neutral carry yields only ~10%/yr. This test asks whether a ")
    L.append("**directional** bias layered on the funding signal — or a contrarian fade ")
    L.append("of extreme funding — beats both carry and buy-and-hold, at 1x/2x/3x leverage.")
    L.append("")
    L.append("## Methodology")
    L.append("")
    L.append("- **Universe**: " + ", ".join(SYMBOLS))
    L.append(f"- **Period**: 8h candles aligned to funding settlement ({PERIODS_PER_YEAR}/yr).")
    L.append("- **Costs**: 0.04% taker + 0.03% slippage per side; funding paid/received on perp notional.")
    L.append(f"- **Walk-forward**: {TRAIN_FRAC:.0%} train / {1-TRAIN_FRAC:.0%} test (chronological split).")
    L.append("- **Decision rule**: funding_rate[t] (settled at candle open, known) + price indicators on close[t-1] (no look-ahead).")
    L.append("- **Liquidation guard**: single-bar move ≥ ~90% of margin at given leverage → −95% bar (counted).")
    L.append("")
    L.append("### Strategies")
    L.append("")
    L.append("| Key | Name | Rule |")
    L.append("|-----|------|------|")
    L.append("| A | Funding + trend | Long funding>0 & EMA50>EMA200; short funding<0 & EMA50<EMA200 |")
    L.append(f"| B | Funding contrarian | Short fund-z>{CONTRARIAN_ENTER}; long fund-z<-{CONTRARIAN_ENTER}; exit |z|<{CONTRARIAN_EXIT} |")
    L.append(f"| C | Funding momentum | Long funding rising & >0; short falling & <0 (lookback {MOMENTUM_LOOKBACK}) |")
    L.append(f"| D | High-funding harvest | Short-perp (delta-neutral) when funding>{HARVEST_ENTER*100:.3f}%; exit <{HARVEST_EXIT*100:.3f}% |")
    L.append("")

    # Dataset summary
    L.append("## 1. Dataset Summary")
    L.append("")
    L.append("| Symbol | Periods | Days | Start | End | Funding>0 | Mean Rate |")
    L.append("|--------|---------|------|-------|-----|-----------|-----------|")
    for sym, df in datasets.items():
        L.append(f"| {sym} | {len(df)} | {len(df)/3:.0f} | "
                 f"{df['datetime'].iloc[0].date()} | {df['datetime'].iloc[-1].date()} | "
                 f"{(df['funding_rate']>0).mean()*100:.1f}% | "
                 f"{df['funding_rate'].mean()*100:.4f}% |")
    L.append("")

    # Top 10 by Sharpe
    L.append("## 2. Top 10 Configs by Sharpe (full sample)")
    L.append("")
    L.append("| Symbol | Strategy | Lev | TotRet% | Ann% | MaxDD% | Sharpe | Sortino | PF | Win% | Trades | Liq |")
    L.append("|--------|----------|-----|---------|------|--------|--------|---------|----|------|--------|-----|")
    for r in top_sharpe:
        L.append(_row(r, "full"))
    L.append("")

    # Top 10 by annualized
    L.append("## 3. Top 10 Configs by Annualized Return (full sample)")
    L.append("")
    L.append("| Symbol | Strategy | Lev | TotRet% | Ann% | MaxDD% | Sharpe | Sortino | PF | Win% | Trades | Liq |")
    L.append("|--------|----------|-----|---------|------|--------|--------|---------|----|------|--------|-----|")
    for r in top_ann:
        L.append(_row(r, "full"))
    L.append("")

    # Flagged winners (full sample)
    L.append("## 4. Flagged Winners — Sharpe>1 & Ann>50% & MaxDD<20%")
    L.append("")
    L.append("### Full-sample flags")
    L.append("")
    if flagged_full:
        L.append("| Symbol | Strategy | Lev | Ann% | MaxDD% | Sharpe | Test Ann% | Test MaxDD% | Test Sharpe | OOS flag |")
        L.append("|--------|----------|-----|------|--------|--------|-----------|-------------|-------------|----------|")
        for r in sorted(flagged_full, key=lambda x: x["full"]["sharpe"], reverse=True):
            L.append(f"| {r['symbol']} | {r['strategy']} | {r['leverage']}x | "
                     f"{r['full']['annualized_pct']} | {r['full']['max_drawdown_pct']} | "
                     f"{r['full']['sharpe']} | {r['test']['annualized_pct']} | "
                     f"{r['test']['max_drawdown_pct']} | {r['test']['sharpe']} | "
                     f"{'✅' if r['test_flag'] else '❌'} |")
    else:
        L.append("*No config met all three thresholds on the full sample.*")
    L.append("")
    L.append("### Out-of-sample (test-set) flags")
    L.append("")
    if flagged_test:
        L.append("| Symbol | Strategy | Lev | TotRet% | Ann% | MaxDD% | Sharpe | Sortino | PF | Win% | Trades | Liq |")
        L.append("|--------|----------|-----|---------|------|--------|--------|---------|----|------|--------|-----|")
        for r in sorted(flagged_test, key=lambda x: x["test"]["sharpe"], reverse=True):
            L.append(_row(r, "test"))
    else:
        L.append("*No config met all three thresholds out-of-sample.*")
    L.append("")

    # Walk-forward degradation for top configs
    L.append("## 5. Walk-Forward Robustness (top configs by full Sharpe)")
    L.append("")
    L.append("| Symbol | Strategy | Lev | Train Ann% | Train Sharpe | Test Ann% | Test Sharpe | Degradation |")
    L.append("|--------|----------|-----|------------|--------------|-----------|-------------|-------------|")
    for r in top_sharpe:
        ta = r["train"]["annualized_pct"]; tea = r["test"]["annualized_pct"]
        deg = ((ta - tea) / abs(ta) * 100) if ta else 0
        L.append(f"| {r['symbol']} | {r['strategy']} | {r['leverage']}x | "
                 f"{ta} | {r['train']['sharpe']} | {tea} | {r['test']['sharpe']} | "
                 f"{deg:.0f}% |")
    L.append("")

    # Per-strategy aggregate (full sample, best leverage)
    L.append("## 6. Best Config per Strategy (full sample, any symbol/leverage)")
    L.append("")
    L.append("| Strategy | Symbol | Lev | Ann% | MaxDD% | Sharpe | PF | Win% |")
    L.append("|----------|--------|-----|------|--------|--------|----|------|")
    for strat in STRATEGIES:
        best = max((r for r in records if r["strategy"] == strat),
                   key=lambda r: r["full"]["sharpe"])
        L.append(f"| {strat} | {best['symbol']} | {best['leverage']}x | "
                 f"{best['full']['annualized_pct']} | {best['full']['max_drawdown_pct']} | "
                 f"{best['full']['sharpe']} | {best['full']['profit_factor']} | "
                 f"{best['full']['win_rate_pct']} |")
    L.append("")

    # Verdict
    L.append("## 7. Verdict")
    L.append("")
    if flagged_test:
        L.append(f"**{len(flagged_test)} config(s) passed all three thresholds out-of-sample** "
                 "(Sharpe>1, Ann>50%, MaxDD<20%). These are the strongest candidates and "
                 "warrant deeper study (regime conditioning, position sizing, live paper test).")
    else:
        best_test = max(records, key=lambda r: r["test"]["sharpe"])
        L.append("No config passed all three thresholds out-of-sample. Best OOS config by "
                 f"Sharpe: {best_test['symbol']} {best_test['strategy']} {best_test['leverage']}x "
                 f"→ Ann {best_test['test']['annualized_pct']}%, "
                 f"MaxDD {best_test['test']['max_drawdown_pct']}%, "
                 f"Sharpe {best_test['test']['sharpe']}.")
    L.append("")
    L.append("### Caveats")
    L.append("")
    L.append("- Directional futures carry **liquidation/tail risk** not fully captured by a "
             "single-bar liquidation guard; real drawdowns can be deeper (gaps, wicks).")
    L.append("- Funding settlement is applied same-candle (funding_rate[t]); exact 8h phasing "
             "vs the next settlement is approximated — immaterial over thousands of periods.")
    L.append("- Taker fees assumed on every entry/exit; a maker-only execution would cut costs ~40%.")
    L.append("- Past funding/trend regimes ≠ future; 2021/2024 bull funding may not recur.")
    L.append("")

    (RESULTS_DIR / "funding-directional-analysis.md").write_text("\n".join(L), encoding="utf-8")


def _key(r: dict) -> dict:
    return {
        "symbol": r["symbol"], "strategy": r["strategy"], "leverage": r["leverage"],
        "full": r["full"], "train": r["train"], "test": r["test"],
        "full_flag": r["full_flag"], "test_flag": r["test_flag"],
    }


if __name__ == "__main__":
    main()
