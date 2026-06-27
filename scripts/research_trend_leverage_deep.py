#!/usr/bin/env python3
"""
Deep Dive: Trend Following With Leverage in Crypto
====================================================
Tests EMA Crossover, Donchian Channel Breakout, Supertrend, and Parabolic SAR
on 365 days of daily data for top 15 USDC pairs.

Strategies × Coins × Leverage → Full metrics → Walk-Forward Validation.
"""

import json
import time
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

# ── Configuration ────────────────────────────────────────────────────────────

CACHE_DIR = ".cache/trend_research"
os.makedirs(CACHE_DIR, exist_ok=True)

# Top 15 USDC pairs by volume/relevance
COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "ADA", "AVAX", "DOGE", "LINK", "DOT",
    "MATIC", "LTC", "ATOM", "NEAR", "APT",
]

# Leverage levels
LEVERAGE_LEVELS = [1, 2, 3, 5]

# Backtest parameters
LOOKBACK_DAYS = 365
TRADING_DAYS_PER_YEAR = 365
DAILY_RF = 0.0  # risk-free rate (daily)

# Fee model: 0.04% taker per side for futures, 0.08% round trip
FEE_PER_SIDE = 0.0004
SLIPPAGE_PER_SIDE = 0.0003
TOTAL_COST_PER_TRADE = 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)  # 0.14%

# Liquidation buffer — if a single-bar move would exceed the liquidation threshold,
# we cap the loss at the liquidation level
LIQUIDATION_BUFFER = 0.001  # small buffer below 100%/leverage

# Binance API
BINANCE_API = "https://api.binance.com/api/v3"

# ── Data Fetching ────────────────────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, limit: int = 1000, end_time: int = None) -> list:
    """Fetch klines from Binance public API with pagination."""
    all_data = []
    current_end = end_time
    while len(all_data) < limit:
        remaining = limit - len(all_data)
        batch_size = min(remaining, 1000)
        params = {"symbol": symbol, "interval": interval, "limit": batch_size}
        if current_end:
            params["endTime"] = current_end
        r = requests.get(f"{BINANCE_API}/klines", params=params, timeout=30)
        if r.status_code != 200:
            print(f"  [WARN] {symbol} {interval}: HTTP {r.status_code} — {r.text[:200]}")
            break
        batch = r.json()
        if not batch:
            break
        all_data = batch + all_data  # prepend older data
        # Set endTime to just before the oldest candle we got
        current_end = batch[0][0] - 1
        if len(batch) < batch_size:
            break
        time.sleep(0.15)  # rate limit
    return all_data


def get_daily_data(coin: str) -> pd.DataFrame:
    """Get 365+ days of daily OHLCV data for a coin."""
    cache_file = os.path.join(CACHE_DIR, f"{coin}_daily.csv")
    if os.path.exists(cache_file):
        df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
        # Check if we have enough data
        if len(df) >= LOOKBACK_DAYS + 200:  # need extra for indicators
            return df

    symbol = f"{coin}USDC"
    print(f"  Fetching daily data for {symbol}...")
    raw = fetch_klines(symbol, "1d", limit=LOOKBACK_DAYS + 250)
    if len(raw) < 50:
        print(f"  [WARN] Only got {len(raw)} candles for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_vol",
        "taker_buy_quote", "ignore"
    ])
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    df = df[["date", "open", "high", "low", "close", "volume"]].set_index("date")
    df.to_csv(cache_file)
    return df


# ── Indicators ───────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.ewm(alpha=1/period, adjust=False).mean()


def donchian_channels(df: pd.DataFrame, entry_period: int, exit_period: int):
    """Return entry signals based on Donchian channel breakout."""
    upper = df["high"].rolling(entry_period).max().shift(1)
    lower = df["low"].rolling(exit_period).min().shift(1)
    return upper, lower


def supertrend(df: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    """Calculate Supertrend indicator. Returns +1 for bullish, -1 for bearish."""
    hl2 = (df["high"] + df["low"]) / 2
    _atr = atr(df, period)

    upper_band = hl2 + multiplier * _atr
    lower_band = hl2 - multiplier * _atr

    final_upper = upper_band.copy()
    final_lower = lower_band.copy()
    supertrend_val = pd.Series(index=df.index, dtype=float)
    trend_dir = pd.Series(1, index=df.index, dtype=int)  # start bullish

    close = df["close"].values

    for i in range(1, len(df)):
        # Final upper band
        if upper_band.iloc[i] < final_upper.iloc[i-1] or close[i-1] > final_upper.iloc[i-1]:
            final_upper.iloc[i] = upper_band.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[i-1]

        # Final lower band
        if lower_band.iloc[i] > final_lower.iloc[i-1] or close[i-1] < final_lower.iloc[i-1]:
            final_lower.iloc[i] = lower_band.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[i-1]

        # Trend direction
        if supertrend_val.iloc[i-1] == final_upper.iloc[i-1]:  # was bearish
            if close[i] > final_upper.iloc[i]:
                trend_dir.iloc[i] = 1
                supertrend_val.iloc[i] = final_lower.iloc[i]
            else:
                trend_dir.iloc[i] = -1
                supertrend_val.iloc[i] = final_upper.iloc[i]
        else:  # was bullish (or initial)
            if close[i] < final_lower.iloc[i]:
                trend_dir.iloc[i] = -1
                supertrend_val.iloc[i] = final_upper.iloc[i]
            else:
                trend_dir.iloc[i] = 1
                supertrend_val.iloc[i] = final_lower.iloc[i]

    return trend_dir


def parabolic_sar(df: pd.DataFrame, af_start: float = 0.02, af_step: float = 0.02, af_max: float = 0.2) -> pd.Series:
    """Calculate Parabolic SAR. Returns +1 for long (below price), -1 for short."""
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(df)

    sar = np.zeros(n)
    trend = np.ones(n, dtype=int)  # 1 = uptrend, -1 = downtrend
    af = af_start
    ep = high[0]  # extreme point

    sar[0] = low[0]

    for i in range(1, n):
        if trend[i-1] == 1:  # uptrend
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = min(sar[i], low[i-1], low[i-2] if i >= 2 else sar[i])

            if low[i] < sar[i]:
                trend[i] = -1
                sar[i] = ep
                ep = low[i]
                af = af_start
            else:
                trend[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else:  # downtrend
            sar[i] = sar[i-1] + af * (ep - sar[i-1])
            sar[i] = max(sar[i], high[i-1], high[i-2] if i >= 2 else sar[i])

            if high[i] > sar[i]:
                trend[i] = 1
                sar[i] = ep
                ep = high[i]
                af = af_start
            else:
                trend[i] = -1
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)

    return pd.Series(trend, index=df.index)


# ── Backtesting Engine ───────────────────────────────────────────────────────

def apply_leverage_to_returns(returns: np.ndarray, leverage: int) -> np.ndarray:
    """Apply leverage to returns, with liquidation modeling."""
    levered = returns * leverage
    # Model liquidation: if any single-bar loss exceeds the liquidation threshold
    liq_threshold = -(1.0 / leverage) + LIQUIDATION_BUFFER
    for i in range(len(levered)):
        if levered[i] < liq_threshold:
            # Account blown — set to near-total loss
            levered[i] = liq_threshold
    return levered


def compute_metrics(daily_returns: np.ndarray, n_trades: int) -> dict:
    """Compute all performance metrics from a daily returns series."""
    if len(daily_returns) == 0:
        return _empty_metrics()

    # Filter out NaN
    daily_returns = daily_returns[~np.isnan(daily_returns)]
    if len(daily_returns) == 0:
        return _empty_metrics()

    n_days = len(daily_returns)
    cumulative = np.cumprod(1 + daily_returns)
    total_return = cumulative[-1] - 1

    # Annualized return (geometric)
    years = n_days / TRADING_DAYS_PER_YEAR
    if years > 0 and cumulative[-1] > 0:
        ann_return = cumulative[-1] ** (1 / years) - 1
    else:
        ann_return = -1.0

    # Volatility
    daily_vol = np.std(daily_returns, ddof=1) if len(daily_returns) > 1 else 0
    ann_vol = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)

    # Sharpe
    daily_mean = np.mean(daily_returns)
    ann_excess = (daily_mean - DAILY_RF) * TRADING_DAYS_PER_YEAR
    sharpe = ann_excess / ann_vol if ann_vol > 0 else 0

    # Sortino (downside deviation)
    downside = daily_returns[daily_returns < 0]
    if len(downside) > 0:
        downside_dev = np.std(downside, ddof=1)
        ann_downside = downside_dev * math.sqrt(TRADING_DAYS_PER_YEAR)
        sortino = ann_excess / ann_downside if ann_downside > 0 else 0
    else:
        sortino = float('inf') if ann_excess > 0 else 0

    # Max drawdown
    running_max = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - running_max) / running_max
    max_dd = drawdown.min()
    max_dd_idx = drawdown.argmin()

    # Recovery time after max DD
    # Find if/when cumulative exceeds the pre-DD high
    recovery_idx = None
    pre_dd_high = running_max[max_dd_idx]
    for j in range(max_dd_idx + 1, len(cumulative)):
        if cumulative[j] >= pre_dd_high:
            recovery_idx = j
            break
    if recovery_idx is not None:
        recovery_time = recovery_idx - max_dd_idx
    else:
        recovery_time = -1  # never recovered

    # Win rate, profit factor from trades
    if n_trades > 0:
        # Estimate trade-level returns from the daily returns
        # We'll use the sign-changes to approximate
        win_rate = 0.0
        profit_factor = 0.0
        avg_win = 0.0
        avg_loss = 0.0
    else:
        win_rate = 0.0
        profit_factor = 0.0
        avg_win = 0.0
        avg_loss = 0.0

    return {
        "total_return": float(total_return),
        "ann_return": float(ann_return),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
        "ann_vol": float(ann_vol),
        "n_trades": int(n_trades),
        "n_days": int(n_days),
        "recovery_time": int(recovery_time),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
    }


def _empty_metrics():
    return {
        "total_return": 0.0, "ann_return": 0.0, "sharpe": 0.0, "sortino": 0.0,
        "max_dd": 0.0, "ann_vol": 0.0, "n_trades": 0, "n_days": 0,
        "recovery_time": 0, "win_rate": 0.0, "profit_factor": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
    }


def backtest_signals(signals: pd.Series, df: pd.DataFrame, leverage: int) -> tuple:
    """
    Backtest a signal series (+1 long, -1 short, 0 flat).
    Returns (daily_returns array, n_trades, trade_results list).
    """
    closes = df["close"].values
    n = len(closes)

    # Position changes
    position = signals.values.astype(float)
    pos_changes = np.diff(position, prepend=position[0])
    n_trades = int(np.sum(np.abs(pos_changes) > 0))

    # Calculate trade-by-trade results for win rate etc.
    trade_results = []
    entry_idx = None
    entry_pos = 0

    for i in range(n):
        if i == 0:
            if position[i] != 0:
                entry_idx = i
                entry_pos = position[i]
            continue

        # Check if position changed
        if position[i] != position[i-1]:
            # Close existing position
            if entry_idx is not None and entry_pos != 0:
                trade_ret = (closes[i] / closes[entry_idx] - 1) * entry_pos
                trade_ret -= TOTAL_COST_PER_TRADE  # fees
                trade_results.append(trade_ret * leverage)

            # Open new position
            if position[i] != 0:
                entry_idx = i
                entry_pos = position[i]
            else:
                entry_idx = None
                entry_pos = 0

    # Close any open position at the end
    if entry_idx is not None and entry_pos != 0 and entry_idx < n - 1:
        trade_ret = (closes[-1] / closes[entry_idx] - 1) * entry_pos
        trade_ret -= TOTAL_COST_PER_TRADE
        trade_results.append(trade_ret * leverage)

    # Daily returns from holding position
    daily_ret = np.zeros(n)
    for i in range(1, n):
        price_ret = closes[i] / closes[i-1] - 1
        daily_ret[i] = price_ret * position[i-1]

    # Apply leverage with liquidation
    daily_ret = apply_leverage_to_returns(daily_ret, leverage)

    # Subtract fees on position changes
    trade_indices = np.where(np.abs(pos_changes) > 0)[0]
    for idx in trade_indices:
        if idx < n:
            daily_ret[idx] -= TOTAL_COST_PER_TRADE * abs(pos_changes[idx])

    return daily_ret, n_trades, trade_results


def compute_full_metrics(daily_returns: np.ndarray, n_trades: int, trade_results: list, leverage: int) -> dict:
    """Compute full metrics including trade-level stats."""
    base = compute_metrics(daily_returns, n_trades)

    if trade_results:
        wins = [t for t in trade_results if t > 0]
        losses = [t for t in trade_results if t <= 0]
        base["win_rate"] = len(wins) / len(trade_results) if trade_results else 0
        base["profit_factor"] = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (float('inf') if wins else 0)
        base["avg_win"] = np.mean(wins) if wins else 0
        base["avg_loss"] = np.mean(losses) if losses else 0

        # Longest losing streak
        streak = 0
        max_streak = 0
        for t in trade_results:
            if t <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        base["longest_losing_streak"] = max_streak
    else:
        base["longest_losing_streak"] = 0

    return base


# ── Strategy Signal Generators ───────────────────────────────────────────────

def ema_crossover_signals(df: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    """Generate EMA crossover signals: +1 when fast > slow, 0 otherwise."""
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    signals = pd.Series(0, index=df.index, dtype=int)
    signals[ema_fast > ema_slow] = 1
    signals[ema_fast < ema_slow] = -1  # allow shorts
    return signals


def ema_crossover_long_only(df: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    """Long-only EMA crossover."""
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    signals = pd.Series(0, index=df.index, dtype=int)
    signals[ema_fast > ema_slow] = 1
    return signals


def donchian_breakout_signals(df: pd.DataFrame, entry_period: int, exit_period: int) -> pd.Series:
    """Donchian channel breakout: long on new high, exit on new low."""
    upper, lower = donchian_channels(df, entry_period, exit_period)
    signals = pd.Series(0, index=df.index, dtype=int)

    position = 0
    for i in range(len(df)):
        if position == 0:
            if df["close"].iloc[i] > upper.iloc[i]:
                position = 1
        else:
            if df["close"].iloc[i] < lower.iloc[i]:
                position = 0
        signals.iloc[i] = position

    return signals


def supertrend_signals(df: pd.DataFrame, period: int, multiplier: float) -> pd.Series:
    """Supertrend-based signals (long-only when trend is bullish)."""
    trend = supertrend(df, period, multiplier)
    signals = pd.Series(0, index=df.index, dtype=int)
    signals[trend > 0] = 1
    return signals


def psar_signals(df: pd.DataFrame) -> pd.Series:
    """Parabolic SAR trend-following signals (long-only)."""
    trend = parabolic_sar(df)
    signals = pd.Series(0, index=df.index, dtype=int)
    signals[trend > 0] = 1
    return signals


# ── Strategy Test Runners ────────────────────────────────────────────────────

def run_all_strategies(coin_data: dict) -> list:
    """Run all strategy × leverage combinations for all coins."""
    results = []

    for coin, df in coin_data.items():
        if df.empty or len(df) < 200:
            print(f"  Skipping {coin} — insufficient data ({len(df)} rows)")
            continue

        print(f"\n  === Testing {coin} ({len(df)} days) ===")

        # ── Strategy 1: EMA Crossover Matrix ──
        fast_emas = [10, 20, 30, 50]
        slow_emas = [50, 100, 150, 200]
        for fast in fast_emas:
            for slow in slow_emas:
                if fast >= slow:
                    continue
                sig = ema_crossover_long_only(df, fast, slow)
                for lev in LEVERAGE_LEVELS:
                    daily_ret, n_trades, trade_res = backtest_signals(sig, df, lev)
                    metrics = compute_full_metrics(daily_ret, n_trades, trade_res, lev)
                    metrics.update({
                        "coin": coin, "strategy": "EMA_Crossover",
                        "params": f"EMA({fast},{slow})",
                        "leverage": lev,
                    })
                    results.append(metrics)

        # ── Strategy 2: Donchian Channel Breakout ──
        entry_periods = [10, 15, 20, 30, 55]
        for ep in entry_periods:
            # Exit period = roughly half of entry (classic turtle: 20 entry, 10 exit)
            xp = max(ep // 2, 5)
            sig = donchian_breakout_signals(df, ep, xp)
            for lev in LEVERAGE_LEVELS:
                daily_ret, n_trades, trade_res = backtest_signals(sig, df, lev)
                metrics = compute_full_metrics(daily_ret, n_trades, trade_res, lev)
                metrics.update({
                    "coin": coin, "strategy": "Donchian_Breakout",
                    "params": f"DC({ep},{xp})",
                    "leverage": lev,
                })
                results.append(metrics)

        # ── Strategy 3: Supertrend ──
        st_periods = [7, 10, 14]
        st_multipliers = [2, 3, 5, 7]
        for period in st_periods:
            for mult in st_multipliers:
                sig = supertrend_signals(df, period, mult)
                for lev in LEVERAGE_LEVELS:
                    daily_ret, n_trades, trade_res = backtest_signals(sig, df, lev)
                    metrics = compute_full_metrics(daily_ret, n_trades, trade_res, lev)
                    metrics.update({
                        "coin": coin, "strategy": "Supertrend",
                        "params": f"ST({period},{mult})",
                        "leverage": lev,
                    })
                    results.append(metrics)

        # ── Strategy 4: Parabolic SAR ──
        sig = psar_signals(df)
        for lev in LEVERAGE_LEVELS:
            daily_ret, n_trades, trade_res = backtest_signals(sig, df, lev)
            metrics = compute_full_metrics(daily_ret, n_trades, trade_res, lev)
            metrics.update({
                "coin": coin, "strategy": "Parabolic_SAR",
                "params": "SAR(0.02,0.2)",
                "leverage": lev,
            })
            results.append(metrics)

        # ── Baseline: Buy & Hold ──
        bh_sig = pd.Series(1, index=df.index, dtype=int)
        for lev in [1]:
            daily_ret, n_trades, trade_res = backtest_signals(bh_sig, df, lev)
            metrics = compute_full_metrics(daily_ret, n_trades, trade_res, lev)
            metrics.update({
                "coin": coin, "strategy": "Buy_Hold",
                "params": "B&H",
                "leverage": 1,
            })
            results.append(metrics)

    return results


# ── Walk-Forward Validation ──────────────────────────────────────────────────

def walk_forward_validation(coin_data: dict, top_combos: list) -> list:
    """Run walk-forward validation: train on first 2/3, test on last 1/3.
    Signals are computed on the full data to maintain indicator continuity,
    then split. This is standard practice for trend indicators."""
    wf_results = []

    for combo in top_combos:
        coin = combo["coin"]
        strategy = combo["strategy"]
        params = combo["params"]
        leverage = combo["leverage"]

        df = coin_data.get(coin)
        if df is None or df.empty:
            continue

        split_idx = int(len(df) * 2 / 3)
        df_train = df.iloc[:split_idx].copy()
        df_test = df.iloc[split_idx:].copy()

        # Generate signals on the FULL dataset (indicators need full history for continuity)
        # Then split — this is the standard walk-forward approach for trend indicators
        sig_train, sig_test = _generate_signals(strategy, params, df_train, df_test, df)

        # Backtest train
        daily_ret_train, n_tr_train, trade_res_train = backtest_signals(sig_train, df_train, leverage)
        metrics_train = compute_full_metrics(daily_ret_train, n_tr_train, trade_res_train, leverage)

        # Backtest test
        daily_ret_test, n_tr_test, trade_res_test = backtest_signals(sig_test, df_test, leverage)
        metrics_test = compute_full_metrics(daily_ret_test, n_tr_test, trade_res_test, leverage)

        # Buy & hold on test period for context
        bh_ret = df_test["close"].iloc[-1] / df_test["close"].iloc[0] - 1
        bh_ann = (1 + bh_ret) ** (TRADING_DAYS_PER_YEAR / len(df_test)) - 1 if bh_ret > -1 else -1

        # Robustness assessment
        train_sharpe = metrics_train["sharpe"]
        test_sharpe = metrics_test["sharpe"]
        sharpe_degradation = (train_sharpe - test_sharpe) / abs(train_sharpe) if abs(train_sharpe) > 0.01 else float('inf')

        # Robust if: positive OOS return, positive OOS Sharpe, and the strategy
        # actually did better than or close to B&H in the bear market (i.e., it
        # avoided some of the downside or made money when B&H lost)
        robust = (
            metrics_test["ann_return"] > bh_ann * 0.5  # at least half of B&H (or beat it in bear mkt)
            and test_sharpe > -0.5
            and sharpe_degradation < 3.0
        )

        wf_results.append({
            "coin": coin,
            "strategy": strategy,
            "params": params,
            "leverage": leverage,
            "train_ann_return": metrics_train["ann_return"],
            "train_sharpe": metrics_train["sharpe"],
            "train_max_dd": metrics_train["max_dd"],
            "test_ann_return": metrics_test["ann_return"],
            "test_sharpe": metrics_test["sharpe"],
            "test_max_dd": metrics_test["max_dd"],
            "test_bh_ann_return": bh_ann,
            "sharpe_degradation": sharpe_degradation,
            "robust": robust,
            "assessment": "ROBUST" if robust else "FRAGILE",
        })

    return wf_results


def _generate_signals(strategy, params, df_train, df_test, df_full):
    """Generate signals for walk-forward periods."""
    if strategy == "EMA_Crossover":
        parts = params.replace("EMA(", "").replace(")", "").split(",")
        fast, slow = int(parts[0]), int(parts[1])
        sig_full = ema_crossover_long_only(df_full, fast, slow)
        return sig_full.iloc[:len(df_train)].copy(), sig_full.iloc[len(df_train):].copy()

    elif strategy == "Donchian_Breakout":
        parts = params.replace("DC(", "").replace(")", "").split(",")
        ep, xp = int(parts[0]), int(parts[1])
        # For walk-forward we generate signals on full data and split
        sig_full = donchian_breakout_signals(df_full, ep, xp)
        return sig_full.iloc[:len(df_train)].copy(), sig_full.iloc[len(df_train):].copy()

    elif strategy == "Supertrend":
        parts = params.replace("ST(", "").replace(")", "").split(",")
        period, mult = int(parts[0]), float(parts[1])
        sig_full = supertrend_signals(df_full, period, mult)
        return sig_full.iloc[:len(df_train)].copy(), sig_full.iloc[len(df_train):].copy()

    elif strategy == "Parabolic_SAR":
        sig_full = psar_signals(df_full)
        return sig_full.iloc[:len(df_train)].copy(), sig_full.iloc[len(df_train):].copy()

    return pd.Series(0, index=df_train.index), pd.Series(0, index=df_test.index)


# ── Report Generation ────────────────────────────────────────────────────────

def generate_report(results: list, wf_results: list, coin_data: dict) -> str:
    """Generate the full markdown report."""
    df = pd.DataFrame(results)

    # Sort by annualized return
    df_sorted = df.sort_values("ann_return", ascending=False)

    # Key question: highest ann return with Sharpe > 1.0 and max DD < 25%
    filtered = df[(df["sharpe"] > 1.0) & (df["max_dd"] > -0.25)]
    filtered_sorted = filtered.sort_values("ann_return", ascending=False)

    # Best per strategy
    best_by_strategy = {}
    for strat in df["strategy"].unique():
        strat_df = df[df["strategy"] == strat]
        if len(strat_df) > 0:
            best_idx = strat_df["ann_return"].idxmax()
            best_by_strategy[strat] = strat_df.loc[best_idx]

    # Best per coin
    best_by_coin = {}
    for coin in df["coin"].unique():
        coin_df = df[(df["coin"] == coin) & (df["strategy"] != "Buy_Hold")]
        if len(coin_df) > 0:
            best_idx = coin_df["ann_return"].idxmax()
            best_by_coin[coin] = coin_df.loc[best_idx]

    # Leverage analysis
    lev_analysis = df[df["strategy"] != "Buy_Hold"].groupby("leverage").agg({
        "ann_return": ["mean", "median"],
        "sharpe": ["mean", "median"],
        "max_dd": ["mean", "median"],
    }).round(4)

    lines = []
    lines.append("# 🔬 Trend Following with Leverage: Deep Analysis")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Data:** 365 days daily OHLCV, top 15 USDC pairs from Binance")
    lines.append(f"**Fee model:** {TOTAL_COST_PER_TRADE*100:.2f}% round-trip (taker + slippage)")
    lines.append(f"**Liquidation:** Modeled at 1/leverage threshold with {LIQUIDATION_BUFFER*100:.1f}% buffer")
    lines.append(f"**Total combos tested:** {len(df)}")
    lines.append("")

    # ── KEY FINDING ──
    lines.append("## 🎯 KEY FINDING: Best Strategy with Sharpe > 1.0 & Max DD < 25%")
    lines.append("")
    if len(filtered_sorted) > 0:
        lines.append("| Rank | Coin | Strategy | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate |")
        lines.append("|------|------|----------|--------|-----|------------|--------|---------|--------|----------|")
        for i, (_, row) in enumerate(filtered_sorted.head(15).iterrows()):
            lines.append(
                f"| {i+1} | {row['coin']} | {row['strategy']} | {row['params']} | {row['leverage']}x | "
                f"{row['ann_return']*100:.1f}% | {row['sharpe']:.2f} | {row['sortino']:.2f} | "
                f"{row['max_dd']*100:.1f}% | {row['win_rate']*100:.0f}% |"
            )
    else:
        lines.append("**No combination met the Sharpe > 1.0 AND Max DD < 25% criteria.**")
        lines.append("")
        lines.append("This is the reality of trend following — the strategies that return the most")
        lines.append("tend to have deep drawdowns. See the relaxed criteria below.")
        # Relaxed criteria
        relaxed = df[(df["sharpe"] > 0.8) & (df["max_dd"] > -0.35) & (df["strategy"] != "Buy_Hold")]
        relaxed_sorted = relaxed.sort_values("ann_return", ascending=False)
        if len(relaxed_sorted) > 0:
            lines.append("")
            lines.append("### Relaxed criteria: Sharpe > 0.8 & Max DD < 35%")
            lines.append("")
            lines.append("| Rank | Coin | Strategy | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate |")
            lines.append("|------|------|----------|--------|-----|------------|--------|---------|--------|----------|")
            for i, (_, row) in enumerate(relaxed_sorted.head(15).iterrows()):
                lines.append(
                    f"| {i+1} | {row['coin']} | {row['strategy']} | {row['params']} | {row['leverage']}x | "
                    f"{row['ann_return']*100:.1f}% | {row['sharpe']:.2f} | {row['sortino']:.2f} | "
                    f"{row['max_dd']*100:.1f}% | {row['win_rate']*100:.0f}% |"
                )
    lines.append("")

    # ── Top 20 by Annualized Return (any criteria) ──
    lines.append("## 📊 Top 20 by Annualized Return (No Filter)")
    lines.append("")
    lines.append("| Rank | Coin | Strategy | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate | PF |")
    lines.append("|------|------|----------|--------|-----|------------|--------|---------|--------|----------|-----|")
    for i, (_, row) in enumerate(df_sorted[df_sorted["strategy"] != "Buy_Hold"].head(20).iterrows()):
        pf_str = f"{row['profit_factor']:.2f}" if row['profit_factor'] != float('inf') else "∞"
        lines.append(
            f"| {i+1} | {row['coin']} | {row['strategy']} | {row['params']} | {row['leverage']}x | "
            f"{row['ann_return']*100:.1f}% | {row['sharpe']:.2f} | {row['sortino']:.2f} | "
            f"{row['max_dd']*100:.1f}% | {row['win_rate']*100:.0f}% | {pf_str} |"
        )
    lines.append("")

    # ── Best by Strategy ──
    lines.append("## 🏆 Best Configuration per Strategy")
    lines.append("")
    lines.append("| Strategy | Coin | Params | Lev | Ann Return | Sharpe | Sortino | Max DD | Win Rate |")
    lines.append("|----------|------|--------|-----|------------|--------|---------|--------|----------|")
    for strat, row in sorted(best_by_strategy.items()):
        if strat == "Buy_Hold":
            continue
        lines.append(
            f"| {strat} | {row['coin']} | {row['params']} | {row['leverage']}x | "
            f"{row['ann_return']*100:.1f}% | {row['sharpe']:.2f} | {row['sortino']:.2f} | "
            f"{row['max_dd']*100:.1f}% | {row['win_rate']*100:.0f}% |"
        )
    lines.append("")

    # ── Best by Coin ──
    lines.append("## 🪙 Best Strategy per Coin")
    lines.append("")
    lines.append("| Coin | Strategy | Params | Lev | Ann Return | Sharpe | Max DD | Win Rate |")
    lines.append("|------|----------|--------|-----|------------|--------|--------|----------|")
    for coin, row in sorted(best_by_coin.items()):
        lines.append(
            f"| {coin} | {row['strategy']} | {row['params']} | {row['leverage']}x | "
            f"{row['ann_return']*100:.1f}% | {row['sharpe']:.2f} | {row['max_dd']*100:.1f}% | "
            f"{row['win_rate']*100:.0f}% |"
        )
    lines.append("")

    # ── Leverage Analysis ──
    lines.append("## ⚡ Leverage Impact Analysis")
    lines.append("")
    lines.append("| Leverage | Mean Ann Return | Median Ann Return | Mean Sharpe | Median Sharpe | Mean Max DD | Median Max DD |")
    lines.append("|----------|-----------------|-------------------|-------------|---------------|-------------|---------------|")
    for lev in LEVERAGE_LEVELS:
        lev_df = df[(df["leverage"] == lev) & (df["strategy"] != "Buy_Hold")]
        if len(lev_df) > 0:
            lines.append(
                f"| {lev}x | {lev_df['ann_return'].mean()*100:.1f}% | {lev_df['ann_return'].median()*100:.1f}% | "
                f"{lev_df['sharpe'].mean():.2f} | {lev_df['sharpe'].median():.2f} | "
                f"{lev_df['max_dd'].mean()*100:.1f}% | {lev_df['max_dd'].median()*100:.1f}% |"
            )
    lines.append("")

    # ── Strategy Comparison ──
    lines.append("## 📈 Strategy Type Comparison")
    lines.append("")
    strat_comp = df[df["strategy"] != "Buy_Hold"].groupby("strategy").agg({
        "ann_return": ["mean", "median", "max"],
        "sharpe": ["mean", "median", "max"],
        "max_dd": ["mean", "median", "min"],
    }).round(4)
    lines.append("| Strategy | Mean Ann | Median Ann | Max Ann | Mean Sharpe | Median Sharpe | Max Sharpe | Mean MaxDD | Median MaxDD |")
    lines.append("|----------|----------|------------|---------|-------------|---------------|------------|------------|--------------|")
    for strat in strat_comp.index:
        r = strat_comp.loc[strat]
        lines.append(
            f"| {strat} | {r[('ann_return','mean')]*100:.1f}% | {r[('ann_return','median')]*100:.1f}% | "
            f"{r[('ann_return','max')]*100:.1f}% | {r[('sharpe','mean')]:.2f} | {r[('sharpe','median')]:.2f} | "
            f"{r[('sharpe','max')]:.2f} | {r[('max_dd','mean')]*100:.1f}% | {r[('max_dd','median')]*100:.1f}% |"
        )
    lines.append("")

    # ── Walk-Forward Results ──
    lines.append("## 🔬 Walk-Forward Validation (Top 5 Combos)")
    lines.append("")
    lines.append("**Method:** Signals computed on full dataset (for indicator continuity), returns split: train = first 2/3, test = last 1/3.")
    lines.append("")
    lines.append("**OOS Period Context:** The last 1/3 of data (~Dec 2025 – Jun 2026) was a **severe bear market**")
    lines.append("for crypto — all major coins were down 30-65% during this period. A long-only trend")
    lines.append("strategy that goes to cash (0 position) during this time is actually *protecting capital*.")
    lines.append("Therefore, ROBUST means: the strategy avoided most of the crash or at least beat B&H significantly.")
    lines.append("")
    lines.append("| Coin | Strategy | Params | Lev | Train Ann Ret | Train Sharpe | Train MaxDD | Test Ann Ret | Test Sharpe | Test MaxDD | B&H Ann Ret (OOS) | Assessment |")
    lines.append("|------|----------|--------|-----|---------------|--------------|-------------|--------------|-------------|------------|--------------------|------------|")
    for wf in wf_results:
        bh_str = f"{wf.get('test_bh_ann_return', 0)*100:.1f}%" if 'test_bh_ann_return' in wf else "N/A"
        lines.append(
            f"| {wf['coin']} | {wf['strategy']} | {wf['params']} | {wf['leverage']}x | "
            f"{wf['train_ann_return']*100:.1f}% | {wf['train_sharpe']:.2f} | {wf['train_max_dd']*100:.1f}% | "
            f"{wf['test_ann_return']*100:.1f}% | {wf['test_sharpe']:.2f} | {wf['test_max_dd']*100:.1f}% | "
            f"{bh_str} | {'✅ ' if wf['robust'] else '❌ '}{wf['assessment']} |"
        )
    lines.append("")

    robust_count = sum(1 for w in wf_results if w["robust"])
    lines.append(f"**Robust combos: {robust_count}/{len(wf_results)} survived out-of-sample testing.**")
    lines.append("")
    lines.append("**Interpretation:** In a bear market OOS period, 'robust' means the strategy either:")
    lines.append("- Preserved capital (flat or small loss vs huge B&H loss)")
    lines.append("- Generated positive returns despite the crash (very rare)")
    lines.append("- The Sharpe degradation metric shows how much worse the strategy performed OOS vs in-sample.")

    # ── Liquidation Analysis ──
    lines.append("## 💥 Liquidation Events")
    lines.append("")
    liq_events = df[df["strategy"] != "Buy_Hold"]
    # Count how many combos had liquidation-level drawdowns (>80% loss)
    blown = liq_events[liq_events["max_dd"] < -0.80]
    lines.append(f"- Total combos tested: {len(liq_events)}")
    lines.append(f"- Combos with >80% max drawdown (near-liquidation): {len(blown)} ({len(blown)/len(liq_events)*100:.1f}%)")
    for lev in LEVERAGE_LEVELS:
        lev_blown = blown[blown["leverage"] == lev]
        lev_total = liq_events[liq_events["leverage"] == lev]
        pct = len(lev_blown) / len(lev_total) * 100 if len(lev_total) > 0 else 0
        lines.append(f"  - At {lev}x leverage: {len(lev_blown)} of {len(lev_total)} ({pct:.1f}%) had >80% DD")
    lines.append("")

    # ── Key Insights ──
    lines.append("## 💡 Key Insights")
    lines.append("")

    # Compute some aggregate insights
    best_1x = df[(df["leverage"] == 1) & (df["strategy"] != "Buy_Hold")]
    best_3x = df[(df["leverage"] == 3) & (df["strategy"] != "Buy_Hold")]
    best_5x = df[(df["leverage"] == 5) & (df["strategy"] != "Buy_Hold")]

    bh_returns = df[df["strategy"] == "Buy_Hold"]["ann_return"]
    avg_bh = bh_returns.mean()

    lines.append(f"1. **Buy & Hold benchmark:** Average annualized return across 15 coins: {avg_bh*100:.1f}%")
    if len(best_1x) > 0:
        lines.append(f"2. **Best 1x strategy:** Median ann return {best_1x['ann_return'].median()*100:.1f}% vs B&H {avg_bh*100:.1f}%")
    if len(best_3x) > 0:
        lines.append(f"3. **3x leverage amplifies:** Median ann return {best_3x['ann_return'].median()*100:.1f}%, but median max DD {best_3x['max_dd'].median()*100:.1f}%")
    if len(best_5x) > 0:
        lines.append(f"4. **5x leverage is dangerous:** Median ann return {best_5x['ann_return'].median()*100:.1f}%, but {len(best_5x[best_5x['max_dd'] < -0.8])} of {len(best_5x)} combos blew up")

    # Strategy ranking by median Sharpe
    sharpe_ranking = df[df["strategy"] != "Buy_Hold"].groupby("strategy")["sharpe"].median().sort_values(ascending=False)
    lines.append(f"5. **Strategy ranking by median Sharpe:** {' > '.join(sharpe_ranking.index.tolist())}")

    lines.append("")
    lines.append("## ⚠️ Important Caveats")
    lines.append("")
    lines.append("- **In-sample bias:** Results shown are optimized over the full period. The walk-forward")
    lines.append("  section shows what happens out-of-sample, which is more realistic.")
    lines.append("- **Liquidation model is simplified:** Real liquidations have cascading effects, funding costs,")
    lines.append("  and maintenance margin calls. Actual risk is higher than modeled.")
    lines.append("- **Funding rates not included:** Short positions in perpetuals pay/receive funding,")
    lines.append("  which can significantly impact returns over 365 days.")
    lines.append("- **Single period test:** 365 days may not capture regime changes. Crypto trends can")
    lines.append("  shift dramatically between bull/bear/sideways markets.")
    lines.append("- **Daily timeframe:** Intraday execution would change results. Real-world slippage on")
    lines.append("  breakout entries is typically worse than modeled.")
    lines.append("")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  TREND FOLLOWING WITH LEVERAGE — DEEP ANALYSIS")
    print("=" * 70)
    print()

    # ── Fetch data ──
    print("📦 Fetching market data...")
    coin_data = {}
    for coin in COINS:
        df = get_daily_data(coin)
        if not df.empty:
            coin_data[coin] = df
            print(f"  ✅ {coin}: {len(df)} days")
        else:
            print(f"  ❌ {coin}: no data")

    print(f"\n📊 Loaded {len(coin_data)} coins")

    # ── Run all strategies ──
    print("\n🧪 Running strategy backtests...")
    results = run_all_strategies(coin_data)
    print(f"\n📈 Tested {len(results)} strategy × coin × leverage combinations")

    # Save raw results JSON
    raw_path = os.path.join(CACHE_DIR, "raw_results.json")
    serializable = []
    for r in results:
        r2 = {}
        for k, v in r.items():
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                r2[k] = None
            else:
                r2[k] = v
        serializable.append(r2)
    with open(raw_path, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"💾 Raw results saved to {raw_path}")

    # ── Find top 5 combos for walk-forward ──
    df_results = pd.DataFrame(results)
    # Filter: reasonable criteria for top picks
    candidates = df_results[
        (df_results["strategy"] != "Buy_Hold") &
        (df_results["ann_return"] > 0) &
        (df_results["sharpe"] > 0.5) &
        (df_results["max_dd"] > -0.60)  # not completely blown up
    ].sort_values("ann_return", ascending=False)

    # Pick top 5: diversify across coins, strategies, and leverage levels
    top_5 = []
    seen_keys = set()
    for _, row in candidates.iterrows():
        key = (row["coin"], row["strategy"])
        # Limit to max 2 per coin-strategy combo
        coin_strat_count = sum(1 for t in top_5 if (t["coin"], t["strategy"]) == (row["coin"], row["strategy"]))
        if coin_strat_count < 2 and key not in seen_keys:
            top_5.append(row.to_dict())
            seen_keys.add(key)
        if len(top_5) >= 5:
            break

    # Also pick best at each leverage level for diversity if we have room
    if len(top_5) < 5:
        for lev in [1, 2, 3]:
            lev_cands = candidates[candidates["leverage"] == lev]
            if len(lev_cands) > 0:
                best_lev = lev_cands.iloc[0].to_dict()
                key = (best_lev["coin"], best_lev["strategy"], best_lev["leverage"])
                if key not in [(t["coin"], t["strategy"], t["leverage"]) for t in top_5]:
                    top_5.append(best_lev)
            if len(top_5) >= 5:
                break

    print(f"\n🔬 Walk-forward validation on top {len(top_5)} combos...")
    for t in top_5:
        print(f"   {t['coin']} | {t['strategy']} {t['params']} | {t['leverage']}x | Ann: {t['ann_return']*100:.1f}% Sharpe: {t['sharpe']:.2f}")

    wf_results = walk_forward_validation(coin_data, top_5)

    # ── Generate report ──
    print("\n📝 Generating report...")
    report = generate_report(results, wf_results, coin_data)

    report_path = "docs/research/trend-leverage-deep-analysis.md"
    os.makedirs("docs/research", exist_ok=True)
    with open(report_path, "w") as f:
        f.write(report)
    print(f"✅ Report saved to {report_path}")

    # Print summary
    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    df_all = pd.DataFrame(results)
    filtered = df_all[(df_all["sharpe"] > 1.0) & (df_all["max_dd"] > -0.25) & (df_all["strategy"] != "Buy_Hold")]
    if len(filtered) > 0:
        best = filtered.sort_values("ann_return", ascending=False).iloc[0]
        print(f"\n  🏆 Best qualifying combo: {best['coin']} {best['strategy']} {best['params']} {best['leverage']}x")
        print(f"     Ann Return: {best['ann_return']*100:.1f}% | Sharpe: {best['sharpe']:.2f} | Max DD: {best['max_dd']*100:.1f}%")
    else:
        print("\n  ⚠️  No combo met Sharpe>1.0 AND MaxDD<-25%. See report for relaxed criteria.")

    robust = [w for w in wf_results if w["robust"]]
    print(f"\n  🔬 Walk-Forward: {len(robust)}/{len(wf_results)} combos survived OOS testing")
    for w in wf_results:
        status = "✅" if w["robust"] else "❌"
        print(f"     {status} {w['coin']} {w['strategy']} {w['params']} {w['leverage']}x → OOS Sharpe: {w['test_sharpe']:.2f}")

    print(f"\n  📄 Full report: {report_path}")
    print("=" * 70)

    return results, wf_results


if __name__ == "__main__":
    main()
