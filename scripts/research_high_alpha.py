#!/usr/bin/env python3
"""HIGH-ALPHA Multi-Strategy Backtesting Framework for Crypto.

Tests aggressive directional strategies with leverage on 180 days of hourly
data for top 15 USDC pairs from Binance futures.

Strategy types:
    1. Trend Following (EMA 50/200 crossover) at 1x, 3x, 5x leverage
    2. Momentum Breakout (20-day high + volume spike), trailing stop
    3. Grid Trading (20 levels, 2% spacing) in ranging vs trending markets
    4. RSI Mean Reversion (RSI<30 long) at 3x leverage
    5. Combined Multi-Strategy (capital split across best performers)

For EACH strategy reports: total return %, annualized return %, Sharpe ratio,
max drawdown %, profit factor, win rate, avg trade duration, number of trades.

All data from public Binance API — no API keys needed.
Research only — no live trading.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

warnings.filterwarnings("ignore", category=RuntimeWarning)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _json_default(obj: Any) -> Any:
    """JSON serializer for numpy types."""
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)

# ─── Configuration ───────────────────────────────────────────────────────────
FAPI = "https://fapi.binance.com"
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
BACKTEST_DAYS = 180
INITIAL_CAPITAL = 10_000.0

# Realistic costs
FEE_RATE = 0.0004        # 0.04% taker per side (futures)
SLIPPAGE = 0.0003        # 0.03% per side
FUNDING_RATE = 0.0001    # 0.01% per 8h (avg net cost for leveraged positions)

# Annualization
TRADING_DAYS = 365
RISK_FREE_RATE = 0.05    # 5% annual risk-free

# ─── Data Fetching ───────────────────────────────────────────────────────────

def get_top_symbols(n: int = 15) -> list[str]:
    """Get top N USDC pairs by volume from Binance futures."""
    r = requests.get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=20)
    r.raise_for_status()
    tickers = r.json()
    usdc = [t for t in tickers if t["symbol"].endswith("USDC")]
    usdc.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in usdc[:n]]


def fetch_klines(symbol: str, interval: str = "1h", days: int = BACKTEST_DAYS) -> np.ndarray:
    """Fetch historical klines as structured numpy array."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur,
            "limit": 1500,
        }
        r = requests.get(f"{FAPI}/fapi/v1/klines", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        cur = data[-1][0] + HOUR_MS
        time.sleep(0.1)
    # Deduplicate
    seen = set()
    unique_rows = []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            unique_rows.append(row)

    dt = np.dtype([
        ("ts", np.int64), ("open", np.float64), ("high", np.float64),
        ("low", np.float64), ("close", np.float64), ("volume", np.float64),
    ])
    arr = np.array(
        [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
         for r in unique_rows],
        dtype=dt,
    )
    return arr


def fetch_all_symbols(symbols: list[str]) -> dict[str, np.ndarray]:
    """Fetch klines for all symbols with caching."""
    cache_dir = REPO_ROOT / "scripts" / "_cache_klines"
    cache_dir.mkdir(exist_ok=True)
    cache_file = cache_dir / "high_alpha_klines.npz"
    all_data: dict[str, np.ndarray] = {}
    cache_data: dict[str, np.ndarray] = {}

    # Try loading cache
    if cache_file.exists():
        loaded = np.load(cache_file, allow_pickle=True)
        cached_syms = set(loaded.files)
        fresh_needed = set(symbols) - cached_syms
        for sym in symbols:
            if sym in cached_syms:
                cache_data[sym] = loaded[sym]
        print(f"  Cache: {len(cache_data)} symbols cached, {len(fresh_needed)} to fetch")
    else:
        fresh_needed = set(symbols)

    for sym in symbols:
        if sym in cache_data:
            all_data[sym] = cache_data[sym]
        else:
            print(f"  Fetching {sym}...")
            data = fetch_klines(sym)
            all_data[sym] = data
            cache_data[sym] = data

    # Save updated cache
    save_dict = {sym: all_data[sym] for sym in symbols}
    np.savez(cache_file, **save_dict)
    return all_data


# ─── Indicators ──────────────────────────────────────────────────────────────

def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average."""
    alpha = 2.0 / (period + 1)
    result = np.empty_like(values)
    result[0] = values[0]
    for i in range(1, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.zeros_like(close)
    avg_loss = np.zeros_like(close)
    # Wilder's smoothing
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])
    for i in range(period + 1, len(close)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period
    rs = np.where(avg_loss > 0, avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss), 100.0)
    result = 100.0 - 100.0 / (1.0 + rs)
    result[:period] = 50.0
    return result


def rolling_max(values: np.ndarray, window: int) -> np.ndarray:
    """Rolling maximum."""
    result = np.empty_like(values)
    for i in range(len(values)):
        start = max(0, i - window + 1)
        result[i] = np.max(values[start:i + 1])
    return result


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    """Rolling mean."""
    cumsum = np.cumsum(values)
    result = np.empty_like(values)
    for i in range(len(values)):
        if i < window:
            result[i] = cumsum[i] / (i + 1)
        else:
            result[i] = (cumsum[i] - cumsum[i - window]) / window
    return result


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range."""
    prev_close = np.zeros_like(close)
    prev_close[1:] = close[:-1]
    tr = np.maximum(
        high - low,
        np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
    )
    return rolling_mean(tr, period)


# ─── Metrics ─────────────────────────────────────────────────────────────────

@dataclass
class TradeMetrics:
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    avg_trade_duration_hours: float = 0.0
    num_trades: int = 0
    final_equity: float = 0.0
    buy_hold_return_pct: float = 0.0
    buy_hold_sharpe: float = 0.0
    excess_return_pct: float = 0.0
    meets_target: bool = False
    notes: str = ""


def compute_metrics(
    equity_curve: np.ndarray,
    timestamps: np.ndarray,
    trades: list[dict],
    initial_capital: float = INITIAL_CAPITAL,
) -> TradeMetrics:
    """Compute comprehensive performance metrics."""
    if len(equity_curve) < 2:
        return TradeMetrics()

    # Total return
    total_ret = (equity_curve[-1] / initial_capital - 1) * 100

    # Annualized return
    hours_total = (timestamps[-1] - timestamps[0]) / HOUR_MS
    days_total = hours_total / 24
    years_total = max(days_total / TRADING_DAYS, 0.01)
    ann_ret = ((equity_curve[-1] / initial_capital) ** (1 / years_total) - 1) * 100

    # Hourly returns for Sharpe
    rets = np.diff(equity_curve) / np.where(equity_curve[:-1] == 0, 1e-10, equity_curve[:-1])
    rets = rets[np.isfinite(rets)]
    if len(rets) > 10:
        mean_r = np.mean(rets)
        std_r = np.std(rets, ddof=1)
        sharpe = (mean_r / max(std_r, 1e-10)) * np.sqrt(24 * TRADING_DAYS) if std_r > 0 else 0.0
        # Subtract risk-free rate
        rf_hourly = RISK_FREE_RATE / (24 * TRADING_DAYS)
        sharpe = ((mean_r - rf_hourly) / max(std_r, 1e-10)) * np.sqrt(24 * TRADING_DAYS)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(equity_curve)
    drawdown = (equity_curve - peak) / np.where(peak == 0, 1e-10, peak)
    max_dd = abs(np.min(drawdown)) * 100 if len(drawdown) > 0 else 0.0

    # Trade-level metrics
    if trades:
        profits = [t["pnl"] for t in trades]
        wins = [p for p in profits if p > 0]
        losses = [p for p in profits if p < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = gross_profit / max(gross_loss, 1e-10)
        win_rate = len(wins) / len(profits) * 100
        durations = [t.get("duration_hours", 0) for t in trades]
        avg_dur = np.mean(durations) if durations else 0.0
    else:
        profit_factor = 0.0
        win_rate = 0.0
        avg_dur = 0.0

    meets = sharpe > 1.0 and max_dd < 15.0 and ann_ret > 50.0
    hits_100 = ann_ret > 100.0

    return TradeMetrics(
        total_return_pct=total_ret,
        annualized_return_pct=ann_ret,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        profit_factor=profit_factor,
        win_rate=win_rate,
        avg_trade_duration_hours=avg_dur,
        num_trades=len(trades),
        final_equity=equity_curve[-1],
        meets_target=meets,
        notes="🎯 100%+ ANNUALIZED!" if hits_100 else "",
    )


def buy_hold_metrics(close: np.ndarray, timestamps: np.ndarray) -> tuple[TradeMetrics, np.ndarray]:
    """Compute buy-and-hold benchmark."""
    n = len(close)
    qty = INITIAL_CAPITAL / close[0]
    equity = close * qty
    bh = compute_metrics(equity, timestamps, [])
    return bh, equity


# ─── Strategy 1: Trend Following (EMA Crossover) ─────────────────────────────

def backtest_trend_following(
    data: np.ndarray,
    leverage: float = 3.0,
    fast: int = 50,
    slow: int = 200,
    allow_short: bool = False,
) -> tuple[np.ndarray, list[dict], TradeMetrics]:
    """EMA crossover trend following with leverage.

    When allow_short=True, death cross opens a short position, allowing the
    strategy to profit from downtrends. This is critical in crypto bear markets.
    """
    close = data["close"].astype(np.float64)
    high = data["high"].astype(np.float64)
    low = data["low"].astype(np.float64)
    ts = data["ts"].astype(np.int64)
    n = len(close)

    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)

    position = 0.0      # -1, 0, +1
    entry_price = 0.0
    entry_ts = 0
    equity = INITIAL_CAPITAL
    equity_curve = np.zeros(n)
    trades: list[dict] = []

    cost_per_trade = (FEE_RATE + SLIPPAGE) * 2  # entry + exit
    equity_at_entry = INITIAL_CAPITAL  # ensure defined before any use

    equity_curve[0] = equity  # FIX: initialize first element

    for i in range(1, n):
        # Check exit/entry
        if i >= slow:
            golden = ema_fast[i] > ema_slow[i] and ema_fast[i - 1] <= ema_slow[i - 1]
            death = ema_fast[i] < ema_slow[i] and ema_fast[i - 1] >= ema_slow[i - 1]

            # Exit current position on opposite signal
            if position != 0 and (death if position > 0 else golden):
                if position > 0:
                    exit_price = close[i] * (1 - SLIPPAGE)
                    raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
                    side = "long"
                else:
                    exit_price = close[i] * (1 + SLIPPAGE)
                    raw_pnl = (entry_price / exit_price - 1) * leverage * equity_at_entry
                    side = "short"
                fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
                holding_hours = (ts[i] - entry_ts) / HOUR_MS
                funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
                pnl = raw_pnl - fee - funding_cost
                equity += pnl
                trades.append({
                    "side": side,
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl": pnl,
                    "duration_hours": holding_hours,
                    "ts_entry": entry_ts,
                    "ts_exit": ts[i],
                })
                position = 0

            # Enter new position on signal
            if position == 0:
                if golden:
                    position = 1
                    entry_price = close[i] * (1 + SLIPPAGE)
                    entry_ts = ts[i]
                    equity_at_entry = equity
                    equity += -equity_at_entry * cost_per_trade / 2
                elif death and allow_short:
                    position = -1
                    entry_price = close[i] * (1 - SLIPPAGE)
                    entry_ts = ts[i]
                    equity_at_entry = equity
                    equity += -equity_at_entry * cost_per_trade / 2

        # Track equity (mark-to-market)
        if position > 0 and i > 0:
            mtm_change = (close[i] / close[i - 1] - 1) * leverage * equity_at_entry
            eq_now = equity + mtm_change
            # Liquidation check: if loss exceeds 90% of entry equity
            if eq_now <= equity_at_entry * 0.05:
                eq_now = equity_at_entry * 0.05
                equity = eq_now
                trades.append({
                    "side": "long_liq",
                    "entry": entry_price, "exit": close[i],
                    "pnl": eq_now - equity_at_entry, "duration_hours": (ts[i] - entry_ts) / HOUR_MS,
                    "ts_entry": entry_ts, "ts_exit": ts[i],
                })
                position = 0
            equity_curve[i] = eq_now
        elif position < 0 and i > 0:
            mtm_change = (close[i - 1] / close[i] - 1) * leverage * equity_at_entry
            eq_now = equity + mtm_change
            if eq_now <= equity_at_entry * 0.05:
                eq_now = equity_at_entry * 0.05
                equity = eq_now
                trades.append({
                    "side": "short_liq",
                    "entry": entry_price, "exit": close[i],
                    "pnl": eq_now - equity_at_entry, "duration_hours": (ts[i] - entry_ts) / HOUR_MS,
                    "ts_entry": entry_ts, "ts_exit": ts[i],
                })
                position = 0
            equity_curve[i] = eq_now
        else:
            equity_curve[i] = equity

    # Close any open position
    if position > 0:
        exit_price = close[-1] * (1 - SLIPPAGE)
        raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
        side = "long"
    elif position < 0:
        exit_price = close[-1] * (1 + SLIPPAGE)
        raw_pnl = (entry_price / exit_price - 1) * leverage * equity_at_entry
        side = "short"
    else:
        position = 0

    if position != 0:
        fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
        holding_hours = (ts[-1] - entry_ts) / HOUR_MS
        funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
        pnl = raw_pnl - fee - funding_cost
        equity += pnl
        trades.append({
            "side": side,
            "entry": entry_price,
            "exit": exit_price,
            "pnl": pnl,
            "duration_hours": holding_hours,
            "ts_entry": entry_ts,
            "ts_exit": ts[-1],
        })

    equity_curve[-1] = equity
    metrics = compute_metrics(equity_curve, ts, trades)
    return equity_curve, trades, metrics


# ─── Strategy 2: Momentum Breakout ───────────────────────────────────────────

def backtest_momentum_breakout(
    data: np.ndarray,
    leverage: float = 2.0,
    lookback_hours: int = 480,  # 20 days
    vol_mult: float = 1.5,
    trail_pct: float = 10.0,
) -> tuple[np.ndarray, list[dict], TradeMetrics]:
    """Momentum breakout: price breaks above N-hour high with volume confirmation."""
    close = data["close"].astype(np.float64)
    high = data["high"].astype(np.float64)
    low = data["low"].astype(np.float64)
    vol = data["volume"].astype(np.float64)
    ts = data["ts"].astype(np.int64)
    n = len(close)

    rolling_high = rolling_max(high[:-1] if len(high) > 1 else high, lookback_hours)
    vol_ma = rolling_mean(vol, 48)  # 48h volume average

    position = 0.0
    entry_price = 0.0
    entry_ts = 0
    highest_since_entry = 0.0
    equity = INITIAL_CAPITAL
    equity_at_entry = 0.0
    equity_curve = np.zeros(n)
    trades: list[dict] = []

    cost_per_trade = (FEE_RATE + SLIPPAGE) * 2
    equity_curve[:max(lookback_hours, 48)] = INITIAL_CAPITAL  # warmup = flat

    for i in range(max(lookback_hours, 48), n):
        # Check exit
        if position > 0:
            if high[i] > highest_since_entry:
                highest_since_entry = high[i]
            trail_stop = highest_since_entry * (1 - trail_pct / 100)
            if low[i] <= trail_stop:
                exit_price = trail_stop
                raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
                fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
                holding_hours = (ts[i] - entry_ts) / HOUR_MS
                funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
                pnl = raw_pnl - fee - funding_cost
                equity += pnl
                trades.append({
                    "side": "long",
                    "entry": entry_price,
                    "exit": exit_price,
                    "pnl": pnl,
                    "duration_hours": holding_hours,
                    "ts_entry": entry_ts,
                    "ts_exit": ts[i],
                })
                position = 0

        # Check entry
        if position == 0:
            breakout = close[i] > rolling_high[i - 1]
            vol_surge = vol[i] > vol_ma[i - 1] * vol_mult
            if breakout and vol_surge:
                position = 1
                entry_price = close[i] * (1 + SLIPPAGE)
                entry_ts = ts[i]
                highest_since_entry = high[i]
                equity_at_entry = equity
                equity += -equity_at_entry * cost_per_trade / 2

        # Mark-to-market
        if position > 0 and i > 0:
            mtm_change = (close[i] / close[i - 1] - 1) * leverage * equity_at_entry
            equity_curve[i] = equity + mtm_change
        else:
            equity_curve[i] = equity

    # Close open position
    if position > 0:
        exit_price = close[-1] * (1 - SLIPPAGE)
        raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
        fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
        holding_hours = (ts[-1] - entry_ts) / HOUR_MS
        funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
        pnl = raw_pnl - fee - funding_cost
        equity += pnl
        trades.append({
            "side": "long", "entry": entry_price, "exit": exit_price,
            "pnl": pnl, "duration_hours": holding_hours,
            "ts_entry": entry_ts, "ts_exit": ts[-1],
        })

    equity_curve[-1] = equity
    metrics = compute_metrics(equity_curve, ts, trades)
    return equity_curve, trades, metrics


# ─── Strategy 3: Grid Trading ────────────────────────────────────────────────

def backtest_grid_trading(
    data: np.ndarray,
    leverage: float = 1.0,
    num_levels: int = 20,
    spacing_pct: float = 2.0,
    allocation_pct: float = 0.95,
) -> tuple[np.ndarray, list[dict], TradeMetrics]:
    """Grid trading with buy/sell ladders."""
    close = data["close"].astype(np.float64)
    high = data["high"].astype(np.float64)
    low = data["low"].astype(np.float64)
    ts = data["ts"].astype(np.int64)
    n = len(close)

    # Establish grid range after warmup
    warmup = min(200, n // 4)
    price_min = np.min(low[:warmup]) * 0.9
    price_max = np.max(high[:warmup]) * 1.1
    grid_prices = np.linspace(price_min, price_max, num_levels)

    capital_per_grid = (INITIAL_CAPITAL * allocation_pct * leverage) / num_levels
    position_size = capital_per_grid  # $ per level

    # Initialize grid: each level tracks position
    grid_positions = [0] * num_levels  # 0 = flat, 1 = holding
    # Pre-fill: buy at levels below current price
    warmup_price = close[warmup]
    for j in range(num_levels):
        if grid_prices[j] < warmup_price:
            grid_positions[j] = 1

    equity = INITIAL_CAPITAL
    equity_curve = np.full(n, INITIAL_CAPITAL, dtype=np.float64)  # init to flat
    trades: list[dict] = []
    cost_per_trade = (FEE_RATE + SLIPPAGE) * 2

    for i in range(warmup, n):
        # Check each grid level
        for j in range(num_levels - 1):
            gp = grid_prices[j]
            gp_next = grid_prices[j + 1]

            # If price crosses above gp_next and we hold at gp, sell
            if grid_positions[j] == 1 and high[i] >= gp_next:
                # Sell at grid level
                pnl = (gp_next / gp - 1) * position_size - position_size * cost_per_trade
                equity += pnl
                trades.append({
                    "side": "grid_sell", "entry": gp, "exit": gp_next,
                    "pnl": pnl, "duration_hours": 0,
                    "ts_entry": ts[i], "ts_exit": ts[i],
                })
                grid_positions[j] = 0

            # If price crosses below gp and we're flat, buy
            if grid_positions[j] == 0 and low[i] <= gp:
                grid_positions[j] = 1
                equity += -position_size * cost_per_trade / 2

        # Estimate equity: cash + inventory at mark-to-market
        inventory_value = sum(
            position_size * close[i] / grid_prices[j]
            for j in range(num_levels)
            if grid_positions[j] == 1
        )
        equity_curve[i] = equity + inventory_value

    # Liquidate all positions at end
    for j in range(num_levels):
        if grid_positions[j] == 1:
            pnl = (close[-1] / grid_prices[j] - 1) * position_size - position_size * cost_per_trade / 2
            equity += pnl
            trades.append({
                "side": "grid_close", "entry": grid_prices[j], "exit": close[-1],
                "pnl": pnl, "duration_hours": 0,
                "ts_entry": ts[0], "ts_exit": ts[-1],
            })

    equity_curve[-1] = equity
    metrics = compute_metrics(equity_curve, ts, trades)
    return equity_curve, trades, metrics


# ─── Strategy 4: RSI Mean Reversion with Leverage ────────────────────────────

def backtest_rsi_mean_reversion(
    data: np.ndarray,
    leverage: float = 3.0,
    rsi_period: int = 14,
    oversold: float = 30.0,
    exit_rsi: float = 50.0,
    stop_pct: float = 5.0,
) -> tuple[np.ndarray, list[dict], TradeMetrics]:
    """RSI mean reversion: buy oversold, exit on RSI recovery or stop."""
    close = data["close"].astype(np.float64)
    high = data["high"].astype(np.float64)
    low = data["low"].astype(np.float64)
    ts = data["ts"].astype(np.int64)
    n = len(close)

    rsi_vals = rsi(close, rsi_period)

    position = 0.0
    entry_price = 0.0
    entry_ts = 0
    equity = INITIAL_CAPITAL
    equity_at_entry = 0.0
    equity_curve = np.zeros(n)
    trades: list[dict] = []
    cost_per_trade = (FEE_RATE + SLIPPAGE) * 2
    equity_curve[:rsi_period + 1] = INITIAL_CAPITAL  # warmup = flat

    for i in range(rsi_period + 1, n):
        # Check exit
        if position > 0:
            stop_hit = low[i] <= entry_price * (1 - stop_pct / 100)
            rsi_exit = rsi_vals[i] >= exit_rsi

            if stop_hit or rsi_exit:
                if stop_hit:
                    exit_price = entry_price * (1 - stop_pct / 100)
                else:
                    exit_price = close[i] * (1 - SLIPPAGE)
                raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
                fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
                holding_hours = (ts[i] - entry_ts) / HOUR_MS
                funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
                pnl = raw_pnl - fee - funding_cost
                equity += pnl
                trades.append({
                    "side": "long_mr",
                    "entry": entry_price, "exit": exit_price,
                    "pnl": pnl, "duration_hours": holding_hours,
                    "ts_entry": entry_ts, "ts_exit": ts[i],
                    "exit_reason": "stop" if stop_hit else "rsi_exit",
                })
                position = 0

        # Check entry
        if position == 0 and rsi_vals[i] < oversold:
            position = 1
            entry_price = close[i] * (1 + SLIPPAGE)
            entry_ts = ts[i]
            equity_at_entry = equity
            equity += -equity_at_entry * cost_per_trade / 2

        # Mark-to-market
        if position > 0 and i > 0:
            mtm_change = (close[i] / close[i - 1] - 1) * leverage * equity_at_entry
            equity_curve[i] = equity + mtm_change
        else:
            equity_curve[i] = equity

    # Close open position
    if position > 0:
        exit_price = close[-1] * (1 - SLIPPAGE)
        raw_pnl = (exit_price / entry_price - 1) * leverage * equity_at_entry
        fee = abs(equity_at_entry * leverage) * cost_per_trade / 2
        holding_hours = (ts[-1] - entry_ts) / HOUR_MS
        funding_cost = equity_at_entry * leverage * FUNDING_RATE * (holding_hours / 8)
        pnl = raw_pnl - fee - funding_cost
        equity += pnl
        trades.append({
            "side": "long_mr", "entry": entry_price, "exit": exit_price,
            "pnl": pnl, "duration_hours": holding_hours,
            "ts_entry": entry_ts, "ts_exit": ts[-1],
        })

    equity_curve[-1] = equity
    metrics = compute_metrics(equity_curve, ts, trades)
    return equity_curve, trades, metrics


# ─── Combined Multi-Strategy ─────────────────────────────────────────────────

def backtest_combined(
    data: np.ndarray,
    weights: list[float] | None = None,
) -> tuple[np.ndarray, TradeMetrics]:
    """Run multiple strategies simultaneously, split capital."""
    if weights is None:
        weights = [0.34, 0.33, 0.33]
    w_sum = sum(weights)
    weights = [w / w_sum for w in weights]

    # Run the three core strategies (use long+short trend for combined)
    eq1, t1, m1 = backtest_trend_following(data, leverage=3.0, allow_short=True)
    eq2, t2, m2 = backtest_momentum_breakout(data, leverage=2.0)
    eq3, t3, m3 = backtest_rsi_mean_reversion(data, leverage=3.0)

    # Normalize equity curves to start at 1.0 (use first non-zero value)
    def _normalize(eq):
        # Find first non-zero element
        nonzero = eq[eq > 0]
        if len(nonzero) == 0:
            return eq
        first_val = nonzero[0]
        return np.where(eq > 0, eq / first_val, 1.0)

    eq1_n = _normalize(eq1)
    eq2_n = _normalize(eq2)
    eq3_n = _normalize(eq3)

    # Weighted combination
    min_len = min(len(eq1_n), len(eq2_n), len(eq3_n))
    combined = (
        weights[0] * eq1_n[:min_len]
        + weights[1] * eq2_n[:min_len]
        + weights[2] * eq3_n[:min_len]
    ) * INITIAL_CAPITAL

    ts = data["ts"][:min_len].astype(np.int64)
    all_trades = t1 + t2 + t3
    metrics = compute_metrics(combined, ts, all_trades)
    return combined, metrics


# ─── Regime Classification ───────────────────────────────────────────────────

def classify_regime(data: np.ndarray, adx_threshold: float = 3.0) -> tuple[float, float, float]:
    """Classify market regime: returns (% trending, % ranging, % volatile)."""
    close = data["close"].astype(np.float64)
    high = data["high"].astype(np.float64)
    low = data["low"].astype(np.float64)
    n = len(close)

    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    # ADX-like: |EMA50 - EMA200| / EMA200
    adx_like = np.abs(ema50 - ema200) / np.where(ema200 > 0, ema200, 1e-10)
    trending_pct = np.mean(adx_like > adx_threshold / 100) * 100

    # Volatility: ATR/price
    atr_vals = atr(high, low, close, 14)
    vol_ratio = atr_vals / np.where(close > 0, close, 1e-10)
    high_vol_pct = np.mean(vol_ratio > np.median(vol_ratio) * 1.5) * 100

    ranging_pct = 100 - trending_pct - high_vol_pct * 0.3

    return trending_pct, max(0, ranging_pct), high_vol_pct


# ─── Main Runner ─────────────────────────────────────────────────────────────

def run_all_strategies(symbols: list[str], all_data: dict[str, np.ndarray]) -> dict[str, Any]:
    """Run all strategies across all symbols and return results."""
    results: dict[str, Any] = {
        "strategies": {},
        "combined": {},
        "per_symbol": {},
    }

    # Strategy configurations
    strat_configs = {
        "trend_1x": lambda d: backtest_trend_following(d, leverage=1.0),
        "trend_3x": lambda d: backtest_trend_following(d, leverage=3.0),
        "trend_5x": lambda d: backtest_trend_following(d, leverage=5.0),
        "trend_ls_3x": lambda d: backtest_trend_following(d, leverage=3.0, allow_short=True),
        "momentum_2x": lambda d: backtest_momentum_breakout(d, leverage=2.0),
        "grid_spot": lambda d: backtest_grid_trading(d, leverage=1.0),
        "rsi_mr_3x": lambda d: backtest_rsi_mean_reversion(d, leverage=3.0),
    }

    for sym in symbols:
        data = all_data[sym]
        print(f"\n{'='*60}")
        print(f"  Testing {sym} ({len(data)} candles)")
        print(f"{'='*60}")

        bh, bh_eq = buy_hold_metrics(data["close"].astype(np.float64), data["ts"].astype(np.int64))
        trending, ranging, volatile = classify_regime(data)
        print(f"  Regime: {trending:.0f}% trending, {ranging:.0f}% ranging, {volatile:.0f}% volatile")
        print(f"  Buy & Hold: {bh.total_return_pct:+.1f}% | Sharpe: {bh.sharpe_ratio:.2f} | MaxDD: {bh.max_drawdown_pct:.1f}%")

        sym_results: dict[str, Any] = {
            "buy_hold": bh,
            "regime": {"trending": trending, "ranging": ranging, "volatile": volatile},
            "strategies": {},
        }

        for sname, sfunc in strat_configs.items():
            eq, trades, metrics = sfunc(data)
            metrics.buy_hold_return_pct = bh.total_return_pct
            metrics.excess_return_pct = metrics.total_return_pct - bh.total_return_pct
            sym_results["strategies"][sname] = metrics
            flag = ""
            if metrics.annualized_return_pct > 100:
                flag = " 🎯🎯🎯"
            elif metrics.annualized_return_pct > 50:
                flag = " ✅"
            print(f"  {sname:14s}: Ret={metrics.total_return_pct:+8.1f}% | Ann={metrics.annualized_return_pct:+8.1f}% | "
                  f"Shp={metrics.sharpe_ratio:5.2f} | DD={metrics.max_drawdown_pct:5.1f}% | "
                  f"PF={metrics.profit_factor:4.2f} | WR={metrics.win_rate:4.0f}% | "
                  f"Trades={metrics.num_trades:3d}{flag}")

        # Combined
        combined_eq, combined_m = backtest_combined(data)
        combined_m.buy_hold_return_pct = bh.total_return_pct
        combined_m.excess_return_pct = combined_m.total_return_pct - bh.total_return_pct
        sym_results["strategies"]["combined"] = combined_m
        flag = ""
        if combined_m.annualized_return_pct > 100:
            flag = " 🎯🎯🎯"
        elif combined_m.annualized_return_pct > 50:
            flag = " ✅"
        print(f"  {'combined':14s}: Ret={combined_m.total_return_pct:+8.1f}% | Ann={combined_m.annualized_return_pct:+8.1f}% | "
              f"Shp={combined_m.sharpe_ratio:5.2f} | DD={combined_m.max_drawdown_pct:5.1f}% | "
              f"PF={combined_m.profit_factor:4.2f} | WR={combined_m.win_rate:4.0f}% | "
              f"Trades={combined_m.num_trades:3d}{flag}")

        results["per_symbol"][sym] = sym_results

    # Aggregate stats
    for sname in list(strat_configs.keys()) + ["combined"]:
        all_m = [
            results["per_symbol"][sym]["strategies"][sname]
            for sym in symbols
            if sname in results["per_symbol"][sym]["strategies"]
        ]
        avg_ann = np.mean([m.annualized_return_pct for m in all_m])
        avg_sharpe = np.mean([m.sharpe_ratio for m in all_m])
        avg_dd = np.mean([m.max_drawdown_pct for m in all_m])
        best_ann = np.max([m.annualized_return_pct for m in all_m])
        best_sym = symbols[np.argmax([m.annualized_return_pct for m in all_m])]
        results["strategies"][sname] = {
            "avg_annualized_return": avg_ann,
            "avg_sharpe": avg_sharpe,
            "avg_max_drawdown": avg_dd,
            "best_annualized_return": best_ann,
            "best_symbol": best_sym,
            "meets_target_count": sum(1 for m in all_m if m.meets_target),
            "hits_100_count": sum(1 for m in all_m if m.annualized_return_pct > 100),
        }
        print(f"\n  AGG [{sname}]: Avg Ann={avg_ann:+.1f}% | Avg Sharpe={avg_sharpe:.2f} | "
              f"Avg DD={avg_dd:.1f}% | Best: {best_sym} @ {best_ann:+.1f}%")

    return results


# ─── Report Generation ───────────────────────────────────────────────────────

def generate_report(symbols: list[str], all_data: dict[str, np.ndarray], results: dict[str, Any]) -> str:
    """Generate full markdown analysis report."""
    lines = []
    L = lines.append

    L("# High-Alpha Multi-Strategy Backtesting Analysis")
    L("")
    L(f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    L(f"**Data:** {BACKTEST_DAYS} days of 1h candles from Binance Futures")
    L(f"**Symbols:** {', '.join(symbols)}")
    L(f"**Initial Capital:** ${INITIAL_CAPITAL:,.0f}")
    L(f"**Costs:** {FEE_RATE*100:.2f}% fee + {SLIPPAGE*100:.2f}% slippage per side, {FUNDING_RATE*100:.3f}% funding/8h")
    L("")
    L("---")
    L("")

    # Success criteria
    L("## Success Criteria")
    L("")
    L("| Metric | Target |")
    L("|--------|--------|")
    L("| Sharpe Ratio | > 1.0 |")
    L("| Max Drawdown | < 15% |")
    L("| Annualized Return | > 50% |")
    L("| 🎯 Stretch Goal | > 100% |")
    L("")
    L("---")
    L("")

    # Strategy descriptions
    L("## Strategies Tested")
    L("")
    L("### 1. Trend Following (EMA 50/200 Crossover) with Leverage")
    L("- Enter long on golden cross (EMA50 crosses above EMA200)")
    L("- Exit on death cross (EMA50 crosses below EMA200)")
    L("- Tested at 1x, 3x, and 5x leverage")
    L("- Includes funding cost for leveraged positions")
    L("")
    L("### 2. Momentum Breakout")
    L("- Price breaks above 20-day high with 1.5x volume average")
    L("- Enter long, trailing stop at 10%")
    L("- Tested at 2x leverage")
    L("")
    L("### 3. Grid Trading")
    L("- 20 grid levels, 2% spacing")
    L("- Buy low levels, sell when price hits next level up")
    L("- Capital split across all grid levels")
    L("")
    L("### 4. RSI Mean Reversion with Leverage")
    L("- RSI(14) < 30 = oversold, enter long")
    L("- Exit on RSI > 50 or 5% stop loss")
    L("- Tested at 3x leverage")
    L("")
    L("### 5. Combined Multi-Strategy")
    L("- Equal-weight blend of Trend 3x + Momentum 2x + RSI MR 3x")
    L("- Tests whether diversification improves risk-adjusted returns")
    L("")
    L("---")
    L("")

    # Per-symbol results table
    for sname_label, sname_key in [
        ("Trend Following 1x (Long Only)", "trend_1x"),
        ("Trend Following 3x (Long Only)", "trend_3x"),
        ("Trend Following 5x (Long Only)", "trend_5x"),
        ("Trend Following 3x (Long+Short)", "trend_ls_3x"),
        ("Momentum Breakout 2x", "momentum_2x"),
        ("Grid Trading (Spot)", "grid_spot"),
        ("RSI Mean Reversion 3x", "rsi_mr_3x"),
        ("Combined Multi-Strategy", "combined"),
    ]:
        L(f"## {sname_label}")
        L("")
        L("| Symbol | Total Ret % | Annual Ret % | Sharpe | Max DD % | Profit Factor | Win Rate % | Trades | Avg Duration (h) | Buy&Hold % | Excess % |")
        L("|--------|------------|-------------|--------|---------|--------------|-----------|--------|-----------------|-----------|---------|")

        for sym in symbols:
            m = results["per_symbol"][sym]["strategies"].get(sname_key)
            if m is None:
                continue
            flag = " 🎯" if m.annualized_return_pct > 100 else (" ✅" if m.meets_target else "")
            L(f"| {sym} | {m.total_return_pct:+.1f} | {m.annualized_return_pct:+.1f} | {m.sharpe_ratio:.2f} | "
              f"{m.max_drawdown_pct:.1f} | {m.profit_factor:.2f} | {m.win_rate:.0f} | {m.num_trades} | "
              f"{m.avg_trade_duration_hours:.1f} | {m.buy_hold_return_pct:+.1f} | {m.excess_return_pct:+.1f} |{flag}")

        # Aggregate
        agg = results["strategies"].get(sname_key, {})
        if agg:
            L(f"| **AVG** | | {agg['avg_annualized_return']:+.1f} | {agg['avg_sharpe']:.2f} | {agg['avg_max_drawdown']:.1f} | | | | | | |")
            L(f"| **BEST** | | **{agg['best_annualized_return']:+.1f}** ({agg['best_symbol']}) | | | | | | | | | |")
            if agg["hits_100_count"] > 0:
                L(f"| 🎯 **{agg['hits_100_count']} symbols hit 100%+ annualized!** | | | | | | | | | | |")
        L("")

    L("---")
    L("")

    # Regime analysis
    L("## Market Regime Analysis")
    L("")
    L("| Symbol | Trending % | Ranging % | Volatile % |")
    L("|--------|-----------|----------|-----------|")
    for sym in symbols:
        r = results["per_symbol"][sym]["regime"]
        L(f"| {sym} | {r['trending']:.1f} | {r['ranging']:.1f} | {r['volatile']:.1f} |")
    L("")

    L("---")
    L("")

    # Key findings
    L("## Key Findings")
    L("")

    # Find best performers
    all_results_flat = []
    for sym in symbols:
        for sname, m in results["per_symbol"][sym]["strategies"].items():
            all_results_flat.append((sym, sname, m))

    # Top 10 by annualized return
    sorted_by_ret = sorted(all_results_flat, key=lambda x: x[2].annualized_return_pct, reverse=True)
    L("### Top 10 Strategy+Symbol by Annualized Return")
    L("")
    L("| Rank | Symbol | Strategy | Annual Ret % | Sharpe | Max DD % | Target? |")
    L("|------|--------|----------|-------------|--------|---------|---------|")
    for i, (sym, sname, m) in enumerate(sorted_by_ret[:10], 1):
        target = "✅ YES" if m.meets_target else ("🎯 100%+" if m.annualized_return_pct > 100 else "❌")
        L(f"| {i} | {sym} | {sname} | {m.annualized_return_pct:+.1f} | {m.sharpe_ratio:.2f} | {m.max_drawdown_pct:.1f} | {target} |")
    L("")

    # Top 10 by Sharpe
    sorted_by_sharpe = sorted(all_results_flat, key=lambda x: x[2].sharpe_ratio, reverse=True)
    L("### Top 10 Strategy+Symbol by Sharpe Ratio")
    L("")
    L("| Rank | Symbol | Strategy | Sharpe | Annual Ret % | Max DD % | Target? |")
    L("|------|--------|----------|--------|-------------|---------|---------|")
    for i, (sym, sname, m) in enumerate(sorted_by_sharpe[:10], 1):
        target = "✅ YES" if m.meets_target else ("🎯 100%+" if m.annualized_return_pct > 100 else "❌")
        L(f"| {i} | {sym} | {sname} | {m.sharpe_ratio:.2f} | {m.annualized_return_pct:+.1f} | {m.max_drawdown_pct:.1f} | {target} |")
    L("")

    # Strategy comparison summary
    L("### Strategy Comparison (Average Across All Symbols)")
    L("")
    L("| Strategy | Avg Annual Ret % | Avg Sharpe | Avg Max DD % | Hit 100%+ | Meet Target |")
    L("|----------|-----------------|-----------|-------------|----------|-------------|")
    for sname in ["trend_1x", "trend_3x", "trend_5x", "trend_ls_3x", "momentum_2x", "grid_spot", "rsi_mr_3x", "combined"]:
        agg = results["strategies"].get(sname, {})
        if agg:
            L(f"| {sname} | {agg['avg_annualized_return']:+.1f} | {agg['avg_sharpe']:.2f} | {agg['avg_max_drawdown']:.1f} | "
              f"{agg['hits_100_count']}/{len(symbols)} | {agg['meets_target_count']}/{len(symbols)} |")
    L("")

    # Winners
    target_hits = [(s, n, m) for s, n, m in all_results_flat if m.meets_target]
    hundred_hits = [(s, n, m) for s, n, m in all_results_flat if m.annualized_return_pct > 100]

    L("---")
    L("")
    L("## Summary")
    L("")
    L(f"- **Total strategies tested:** {len(all_results_flat)} ({len(symbols)} symbols × 7 strategies)")
    L(f"- **Strategies meeting all targets (Sharpe>1.0, DD<15%, Ann>50%):** {len(target_hits)}")
    L(f"- **Strategies hitting 100%+ annualized:** {len(hundred_hits)}")
    L("")

    if hundred_hits:
        L("### 🎯 100%+ Annualized Return Achieved!")
        L("")
        for sym, sname, m in sorted(hundred_hits, key=lambda x: x[2].annualized_return_pct, reverse=True):
            L(f"- **{sym} / {sname}**: {m.annualized_return_pct:+.1f}% annualized, Sharpe={m.sharpe_ratio:.2f}, Max DD={m.max_drawdown_pct:.1f}%")
        L("")

    if target_hits:
        L("### ✅ All Targets Met")
        L("")
        for sym, sname, m in sorted(target_hits, key=lambda x: x[2].annualized_return_pct, reverse=True):
            L(f"- **{sym} / {sname}**: {m.annualized_return_pct:+.1f}% annualized, Sharpe={m.sharpe_ratio:.2f}, Max DD={m.max_drawdown_pct:.1f}%")
        L("")

    if not target_hits and not hundred_hits:
        L("### ⚠️ No strategies met all targets simultaneously")
        L("")
        L("Strategies that came closest:")
        L("")
        close = sorted(all_results_flat, key=lambda x: (
            (1 if x[2].sharpe_ratio > 1.0 else 0) +
            (1 if x[2].max_drawdown_pct < 15 else 0) +
            (1 if x[2].annualized_return_pct > 50 else 0)
        ), reverse=True)
        for sym, sname, m in close[:5]:
            L(f"- **{sym} / {sname}**: Ann={m.annualized_return_pct:+.1f}%, Sharpe={m.sharpe_ratio:.2f}, Max DD={m.max_drawdown_pct:.1f}%")
        L("")

    L("---")
    L("")
    L("*This is a research-only analysis. No live trading was performed. Past performance does not guarantee future results.*")
    L("")

    return "\n".join(lines)


# ─── Entry Point ─────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print("  HIGH-ALPHA MULTI-STRATEGY CRYPTO BACKTESTING FRAMEWORK")
    print("  Target: 100%+ Annualized Returns")
    print("=" * 70)
    print()

    # Get top symbols
    print(">> Fetching top 15 USDC pairs by volume...")
    symbols = get_top_symbols(15)
    print(f"   Symbols: {', '.join(symbols)}")
    print()

    # Fetch data
    print(">> Fetching 180 days of hourly data...")
    all_data = fetch_all_symbols(symbols)
    for sym in symbols:
        print(f"   {sym}: {len(all_data[sym])} candles")
    print()

    # Run all strategies
    print(">> Running strategies...")
    results = run_all_strategies(symbols, all_data)

    # Generate report
    print("\n>> Generating report...")
    report = generate_report(symbols, all_data, results)

    report_dir = REPO_ROOT / "docs" / "research"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "high-alpha-analysis.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"   Report saved to: {report_path}")

    # Save raw data as JSON
    json_path = report_dir / "high-alpha-data.json"
    json_data = {
        "symbols": symbols,
        "config": {
            "backtest_days": BACKTEST_DAYS,
            "initial_capital": INITIAL_CAPITAL,
            "fee_rate": FEE_RATE,
            "slippage": SLIPPAGE,
            "funding_rate": FUNDING_RATE,
        },
        "results": {},
    }
    for sym in symbols:
        json_data["results"][sym] = {
            "buy_hold": {
                "total_return_pct": results["per_symbol"][sym]["buy_hold"].total_return_pct,
                "annualized_return_pct": results["per_symbol"][sym]["buy_hold"].annualized_return_pct,
                "sharpe_ratio": results["per_symbol"][sym]["buy_hold"].sharpe_ratio,
                "max_drawdown_pct": results["per_symbol"][sym]["buy_hold"].max_drawdown_pct,
            },
            "regime": results["per_symbol"][sym]["regime"],
            "strategies": {},
        }
        for sname, m in results["per_symbol"][sym]["strategies"].items():
            json_data["results"][sym]["strategies"][sname] = {
                "total_return_pct": m.total_return_pct,
                "annualized_return_pct": m.annualized_return_pct,
                "sharpe_ratio": m.sharpe_ratio,
                "max_drawdown_pct": m.max_drawdown_pct,
                "profit_factor": m.profit_factor,
                "win_rate": m.win_rate,
                "num_trades": m.num_trades,
                "avg_trade_duration_hours": m.avg_trade_duration_hours,
                "final_equity": m.final_equity,
                "meets_target": m.meets_target,
            }
    json_path.write_text(json.dumps(json_data, indent=2, default=_json_default), encoding="utf-8")
    print(f"   Raw data saved to: {json_path}")

    # Summary
    print("\n" + "=" * 70)
    print("  BACKTEST COMPLETE — SUMMARY")
    print("=" * 70)
    target_count = sum(results["strategies"][s]["meets_target_count"] for s in results["strategies"])
    hundred_count = sum(results["strategies"][s]["hits_100_count"] for s in results["strategies"])
    print(f"  Total strategies meeting all targets: {target_count}")
    print(f"  Total strategies hitting 100%+: {hundred_count}")
    print()

    # Best overall
    best_sym = None
    best_strat = None
    best_ret = -999
    for sym in symbols:
        for sname, m in results["per_symbol"][sym]["strategies"].items():
            if m.annualized_return_pct > best_ret:
                best_ret = m.annualized_return_pct
                best_sym = sym
                best_strat = sname
    if best_sym:
        m = results["per_symbol"][best_sym]["strategies"][best_strat]
        print(f"  🏆 BEST: {best_sym} / {best_strat}")
        print(f"     Annualized: {m.annualized_return_pct:+.1f}%")
        print(f"     Sharpe: {m.sharpe_ratio:.2f}")
        print(f"     Max DD: {m.max_drawdown_pct:.1f}%")
        print(f"     Trades: {m.num_trades}")
        if best_ret > 100:
            print(f"     🎯🎯🎯 100%+ ACHIEVED!")
    print()
    print(f"  Full report: {report_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
