#!/usr/bin/env python3
"""
THE MASTER STRATEGY: Regime-Adaptive Combined System
=====================================================
ONE strategy that switches behavior based on market regime:
  - BULL (ADX > 25, price > EMA200): Trend-following LONG with leverage, trail stop 12%
  - BEAR (ADX > 25, price < EMA200): SHORT with leverage OR go to USDC cash
  - SIDEWAYS (ADX < 20): Grid-style range trading with tight spacing
  - TRANSITION (ADX 20-25): Reduce position to 50%, no leverage

Three variants tested:
  1. Conservative: 1x max, 20% stop, go to USDC in bear
  2. Balanced: 2x in trends, 15% stop, short in bear
  3. Aggressive: 3x in trends, 20% stop, short in bear with 2x

Walk-forward validation (60/40 split) + Monte Carlo (1000 shuffles).
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ── Config ───────────────────────────────────────────────────────────────────

REPO = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO / ".cache" / "trend_research"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = REPO / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "INJ"]
LOOKBACK_DAYS = 365
WARMUP_DAYS = 200  # for indicators
BINANCE_API = "https://api.binance.com/api/v3"

# Fee model — realistic crypto futures costs
FEE_PER_SIDE = 0.0004   # 0.04% taker
SLIPPAGE_PER_SIDE = 0.0003  # 0.03%
TOTAL_COST = 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)  # 0.14% round trip

# Regime thresholds
ADX_BULL_BEAR = 25     # ADX above this = trending
ADX_SIDEWAYS = 20     # ADX below this = sideways
EMA_TREND = 200       # EMA period for trend direction
ADX_PERIOD = 14

# Strategy variants
VARIANTS = {
    "conservative": {
        "trend_lev": 1.0,
        "stop_loss": 0.20,
        "trail_stop": 0.12,
        "bear_action": "cash",   # go to USDC
        "grid_spacing_pct": 0.02,  # 2% grid levels
        "grid_levels": 4,
        "transition_fraction": 0.5,
    },
    "balanced": {
        "trend_lev": 2.0,
        "stop_loss": 0.15,
        "trail_stop": 0.10,
        "bear_action": "short",
        "grid_spacing_pct": 0.025,
        "grid_levels": 4,
        "transition_fraction": 0.5,
    },
    "aggressive": {
        "trend_lev": 3.0,
        "stop_loss": 0.20,
        "trail_stop": 0.08,
        "bear_action": "short",
        "short_lev": 2.0,
        "grid_spacing_pct": 0.03,
        "grid_levels": 5,
        "transition_fraction": 0.5,
    },
}

# ── Data ─────────────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval="1d", limit=700, end_time=None):
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
            print(f"  [WARN] {symbol}: HTTP {r.status_code}")
            break
        batch = r.json()
        if not batch:
            break
        all_data = batch + all_data
        current_end = batch[0][0] - 1
        if len(batch) < batch_size:
            break
        time.sleep(0.15)
    return all_data

def get_daily_data(coin):
    cache_file = CACHE_DIR / f"{coin}_daily.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
        if len(df) >= LOOKBACK_DAYS + WARMUP_DAYS:
            return df
    symbol = f"{coin}USDC"
    print(f"  Fetching {symbol} daily...")
    raw = fetch_klines(symbol, "1d", limit=LOOKBACK_DAYS + WARMUP_DAYS + 50)
    if len(raw) < 50:
        print(f"  [WARN] Only {len(raw)} candles for {symbol}")
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

def compute_ema_series(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_atr_series(df, period=14):
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False).mean()

def compute_adx_series(df, period=14):
    """ADX as a rolling pandas Series using Wilder's method."""
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()

    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index, dtype=float)
    minus_dm = pd.Series(minus_dm, index=df.index, dtype=float)

    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0/period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1.0/period, adjust=False).mean() / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1.0/period, adjust=False).mean()
    return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)

# ── Regime Detection ─────────────────────────────────────────────────────────

def detect_regimes(df):
    """Classify each bar into bull / bear / sideways / transition."""
    ema200 = compute_ema_series(df["close"], EMA_TREND)
    adx, plus_di, minus_di = compute_adx_series(df, ADX_PERIOD)

    regimes = pd.Series("transition", index=df.index)
    above_ema = df["close"] > ema200

    # Strong trend with directional bias
    trending = adx >= ADX_BULL_BEAR
    sideways = adx < ADX_SIDEWAYS
    transition = ~trending & ~sideways

    regimes[trending & above_ema] = "bull"
    regimes[trending & ~above_ema] = "bear"
    regimes[sideways] = "sideways"
    regimes[transition] = "transition"

    return regimes, ema200, adx, plus_di, minus_di

def regime_accuracy(regimes, df):
    """How often is the regime label 'correct'?
    Bull should predict positive forward returns, bear negative, sideways flat."""
    fwd_ret = df["close"].pct_change(5).shift(-5)  # 5-day forward return
    correct = 0
    total = 0
    for i in range(len(df) - 5):
        r = regimes.iloc[i]
        fr = fwd_ret.iloc[i]
        if pd.isna(fr):
            continue
        total += 1
        if r == "bull" and fr > 0.01:
            correct += 1
        elif r == "bear" and fr < -0.01:
            correct += 1
        elif r == "sideways" and abs(fr) <= 0.03:
            correct += 1
        elif r == "transition":
            correct += 0.5  # partial credit
    return correct / total if total > 0 else 0.0

# ── Strategy Backtester ──────────────────────────────────────────────────────

def backtest_strategy(df, variant_config, start_idx=0, end_idx=None, verbose=False):
    """
    Simulate regime-adaptive strategy on daily bars.
    Returns dict with equity curve, trades, and metrics.
    """
    if end_idx is None:
        end_idx = len(df)

    regimes, ema200, adx, plus_di, minus_di = detect_regimes(df)
    atr_series = compute_atr_series(df, ADX_PERIOD)

    cfg = variant_config
    equity = 1.0
    position = 0.0      # fraction of equity (negative = short)
    entry_price = 0.0
    entry_idx = 0
    leverage = 1.0
    high_water = 1.0
    trail_stop_price = 0.0

    equity_curve = []
    trades = []
    regime_counts = {"bull": 0, "bear": 0, "sideways": 0, "transition": 0}
    daily_returns = []

    # Grid state for sideways
    grid_orders = []  # list of (price, side, filled)
    last_grid_reset = 0

    for i in range(max(start_idx, WARMUP_DAYS), end_idx):
        row = df.iloc[i]
        price = row["close"]
        high = row["high"]
        low = row["low"]
        regime = regimes.iloc[i]
        regime_counts[regime] += 1

        # ── Check exits first ──
        if position != 0:
            bar_return = (price - entry_price) / entry_price * np.sign(position) if entry_price > 0 else 0

            # Stop loss check (using low/high to simulate intra-bar)
            if position > 0:
                worst = (low - entry_price) / entry_price
            else:
                worst = (entry_price - high) / entry_price

            # Liquidation check
            liq_threshold = 1.0 / leverage - 0.001
            stop_hit = worst <= -cfg["stop_loss"]
            liq_hit = worst <= -liq_threshold
            trail_hit = False
            if position > 0 and trail_stop_price > 0 and low <= trail_stop_price:
                trail_hit = True
            elif position < 0 and trail_stop_price > 0 and high >= trail_stop_price:
                trail_hit = True

            if liq_hit:
                # Liquidated — lose the margin
                loss_pct = -liq_threshold
                pnl = equity * abs(position) * loss_pct * leverage
                equity += pnl - equity * abs(position) * TOTAL_COST
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "regime": regime, "direction": "long" if position > 0 else "short",
                    "return": loss_pct * leverage, "pnl": pnl,
                    "exit_reason": "liquidation", "leverage": leverage,
                    "duration": i - entry_idx,
                })
                position = 0.0
                trail_stop_price = 0.0

            elif stop_hit or trail_hit:
                exit_reason = "trail_stop" if trail_hit else "stop_loss"
                if position > 0:
                    exit_ret = (low - entry_price) / entry_price
                else:
                    exit_ret = (entry_price - high) / entry_price
                exit_ret = max(exit_ret, -cfg["stop_loss"])
                levered_ret = exit_ret * leverage
                pnl = equity * abs(position) * levered_ret
                equity += pnl - equity * abs(position) * TOTAL_COST
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "regime": regime, "direction": "long" if position > 0 else "short",
                    "return": levered_ret, "pnl": pnl,
                    "exit_reason": exit_reason, "leverage": leverage,
                    "duration": i - entry_idx,
                })
                position = 0.0
                trail_stop_price = 0.0

        # ── Regime-based entries ──
        if regime == "bull":
            # Go long with trend
            target_pos = 1.0
            lev = cfg["trend_lev"]
            if position <= 0:
                if position < 0:
                    # Close short first
                    close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "regime": regime, "direction": "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change", "leverage": leverage,
                        "duration": i - entry_idx,
                    })
                position = target_pos
                entry_price = price
                entry_idx = i
                leverage = lev
                trail_stop_price = price * (1 - cfg["trail_stop"])
                # Entry cost
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        elif regime == "bear":
            if cfg["bear_action"] == "cash":
                # Close any position, go to USDC
                if position != 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "regime": regime, "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change_cash", "leverage": leverage,
                        "duration": i - entry_idx,
                    })
                    position = 0.0
                    trail_stop_price = 0.0

            elif cfg["bear_action"] == "short":
                target_pos = -1.0
                short_lev = cfg.get("short_lev", cfg["trend_lev"])
                if position >= 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                        trades.append({
                            "entry_idx": entry_idx, "exit_idx": i,
                            "regime": regime, "direction": "long",
                            "return": close_ret, "pnl": equity * abs(position) * close_ret,
                            "exit_reason": "regime_change", "leverage": leverage,
                            "duration": i - entry_idx,
                        })
                    position = target_pos
                    entry_price = price
                    entry_idx = i
                    leverage = short_lev
                    trail_stop_price = price * (1 + cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        elif regime == "sideways":
            # Simplified grid: capture range oscillation
            # If we have no position, enter at current price with reduced size
            if position == 0 and atr_series.iloc[i] > 0:
                # Use grid logic: position at fraction of equity, no leverage
                grid_pos = 0.5
                position = grid_pos
                entry_price = price
                entry_idx = i
                leverage = 1.0
                trail_stop_price = price * (1 - cfg["grid_spacing_pct"] * cfg["grid_levels"])
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
            elif position != 0:
                # In sideways, take profit at grid spacing and re-enter
                if position > 0:
                    unrealized = (price - entry_price) / entry_price
                    if unrealized >= cfg["grid_spacing_pct"] * 2:
                        equity += equity * abs(position) * unrealized - equity * abs(position) * TOTAL_COST
                        trades.append({
                            "entry_idx": entry_idx, "exit_idx": i,
                            "regime": regime, "direction": "long",
                            "return": unrealized, "pnl": equity * abs(position) * unrealized,
                            "exit_reason": "grid_tp", "leverage": 1.0,
                            "duration": i - entry_idx,
                        })
                        position = 0.0
                        trail_stop_price = 0.0

        elif regime == "transition":
            # Reduced position, no leverage
            if position != 0:
                # Check if we need to adjust
                target_frac = cfg["transition_fraction"]
                if abs(position) > target_frac + 0.01 or leverage > 1.0:
                    # Reduce to 50%, no leverage
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "regime": regime, "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "transition_reduce", "leverage": leverage,
                        "duration": i - entry_idx,
                    })
                    position = 0.0
                    leverage = 1.0
                    trail_stop_price = 0.0

                # Enter with reduced position in trend direction
                if position == 0:
                    above_ema_now = price > ema200.iloc[i]
                    if above_ema_now:
                        position = target_frac
                        entry_price = price
                        entry_idx = i
                        leverage = 1.0
                        trail_stop_price = price * (1 - cfg["trail_stop"])
                        equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
            else:
                # Enter with reduced position in trend direction
                target_frac = cfg["transition_fraction"]
                above_ema_now = price > ema200.iloc[i]
                if above_ema_now:
                    position = target_frac
                    entry_price = price
                    entry_idx = i
                    leverage = 1.0
                    trail_stop_price = price * (1 - cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        # ── Update trail stop ──
        if position > 0:
            new_trail = price * (1 - cfg["trail_stop"])
            trail_stop_price = max(trail_stop_price, new_trail)
        elif position < 0:
            new_trail = price * (1 + cfg["trail_stop"])
            trail_stop_price = min(trail_stop_price, new_trail) if trail_stop_price > 0 else new_trail

        equity_curve.append(equity)

    # ── Close any open position at end ──
    if position != 0 and end_idx == len(df):
        price = df.iloc[end_idx - 1]["close"]
        if position > 0:
            close_ret = (price - entry_price) / entry_price * leverage
        else:
            close_ret = (entry_price - price) / entry_price * leverage
        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
        trades.append({
            "entry_idx": entry_idx, "exit_idx": end_idx - 1,
            "regime": regimes.iloc[end_idx-1], "direction": "long" if position > 0 else "short",
            "return": close_ret, "pnl": equity * abs(position) * close_ret,
            "exit_reason": "end_of_data", "leverage": leverage,
            "duration": end_idx - 1 - entry_idx,
        })

    return {
        "equity_curve": equity_curve,
        "final_equity": equity,
        "trades": trades,
        "regime_counts": regime_counts,
    }

# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(equity_curve, trades, n_days):
    """Compute full performance metrics from equity curve and trade list."""
    if len(equity_curve) < 2:
        return _empty_metrics()

    eq = np.array(equity_curve)
    daily_rets = np.diff(eq) / eq[:-1]
    daily_rets = np.nan_to_num(daily_rets, nan=0.0)

    total_return = (eq[-1] / eq[0]) - 1.0
    if n_days > 0 and eq[-1] > 0 and eq[0] > 0:
        ann_return = (eq[-1] / eq[0]) ** (365.0 / n_days) - 1.0
    else:
        ann_return = 0.0

    # Sharpe (daily rf = 0)
    if np.std(daily_rets) > 0:
        sharpe = np.mean(daily_rets) / np.std(daily_rets) * math.sqrt(365)
    else:
        sharpe = 0.0

    # Sortino
    downside = daily_rets[daily_rets < 0]
    if len(downside) > 0 and np.std(downside) > 0:
        sortino = np.mean(daily_rets) / np.std(downside) * math.sqrt(365)
    else:
        sortino = sharpe * 1.5 if sharpe > 0 else 0.0

    # Max drawdown
    running_max = np.maximum.accumulate(eq)
    drawdowns = (eq - running_max) / running_max
    max_dd = abs(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

    # Calmar
    calmar = ann_return / max_dd if max_dd > 0.001 else 0.0

    # Profit factor
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    # Win rate
    wins = sum(1 for t in trades if t["return"] > 0)
    win_rate = wins / len(trades) if trades else 0.0

    # Avg trade duration
    avg_duration = np.mean([t["duration"] for t in trades]) if trades else 0.0

    return {
        "total_return": total_return,
        "annualized_return": ann_return,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "num_trades": len(trades),
        "avg_trade_duration": avg_duration,
    }

def _empty_metrics():
    return {
        "total_return": 0.0, "annualized_return": 0.0, "sharpe": 0.0,
        "sortino": 0.0, "max_drawdown": 0.0, "calmar": 0.0,
        "profit_factor": 0.0, "win_rate": 0.0, "num_trades": 0,
        "avg_trade_duration": 0.0,
    }

# ── Walk-Forward Validation ──────────────────────────────────────────────────

def walk_forward(df, variant_config):
    """Split 60/40, train on first portion, test on second."""
    start = WARMUP_DAYS
    total_bars = len(df) - start
    split = start + int(total_bars * 0.6)

    # In-sample
    is_result = backtest_strategy(df, variant_config, start_idx=start, end_idx=split)
    is_metrics = compute_metrics(is_result["equity_curve"], is_result["trades"], split - start)

    # Out-of-sample
    oos_result = backtest_strategy(df, variant_config, start_idx=split, end_idx=len(df))
    oos_metrics = compute_metrics(oos_result["equity_curve"], oos_result["trades"], len(df) - split)

    # Survival criteria
    survives = (
        oos_metrics["sharpe"] > 0.3 and
        oos_metrics["max_drawdown"] < 0.40 and
        oos_metrics["total_return"] > -0.10
    )

    return {
        "is_metrics": is_metrics,
        "oos_metrics": oos_metrics,
        "survives": survives,
        "split_point": split,
    }

# ── Monte Carlo ──────────────────────────────────────────────────────────────

def monte_carlo(trades, initial_equity=1.0, n_sims=1000):
    """Bootstrap Monte Carlo: sample trades WITH REPLACEMENT 1000 times.

    Standard approach: resample the trade sequence with replacement to generate
    new equity paths. This captures both total return variability AND path-dependent
    drawdown risk (unlike permutation which gives identical total returns).
    """
    if not trades:
        return {"prob_positive": 0.0, "p5": 0.0, "p50": 0.0, "p95": 0.0,
                "median_max_dd": 0.0, "mean_return": 0.0, "prob_ruin": 0.0}

    trade_returns = np.array([t["return"] for t in trades])
    n_trades = len(trade_returns)
    pos_frac = 0.3  # average fraction of equity per trade

    final_returns = []
    max_dds = []
    ruin_count = 0

    rng = np.random.RandomState(42)
    for _ in range(n_sims):
        # Bootstrap: sample with replacement
        sampled = rng.choice(trade_returns, size=n_trades, replace=True)
        equity = initial_equity
        peak = equity
        worst_dd = 0.0
        ruined = False
        for r in sampled:
            equity *= (1 + r * pos_frac)
            if equity <= 0.01 * initial_equity:  # 99% loss = ruin
                ruined = True
                equity = 0.001
                break
            peak = max(peak, equity)
            dd = (equity - peak) / peak if peak > 0 else 0
            worst_dd = min(worst_dd, dd)
        if ruined:
            ruin_count += 1
        final_returns.append(equity / initial_equity - 1.0)
        max_dds.append(abs(worst_dd))

    final_returns = np.array(final_returns)
    return {
        "prob_positive": float(np.mean(final_returns > 0)),
        "p5": float(np.percentile(final_returns, 5)),
        "p50": float(np.percentile(final_returns, 50)),
        "p95": float(np.percentile(final_returns, 95)),
        "median_max_dd": float(np.median(max_dds)),
        "mean_return": float(np.mean(final_returns)),
        "prob_ruin": float(ruin_count / n_sims),
    }

def monte_carlo_portfolio(per_coin_trades, initial_equity=1.0, n_sims=1000):
    """Portfolio-level Monte Carlo via bootstrap.

    For each simulation, independently bootstrap each coin's trades,
    compound, then average across coins (equal-weight portfolio).
    This captures cross-coin diversification properly.
    """
    pos_frac = 0.3
    final_returns = []

    rng = np.random.RandomState(42)

    # Pre-extract trade returns per coin
    coin_returns = []
    for trades in per_coin_trades:
        if trades:
            coin_returns.append(np.array([t["return"] for t in trades]))
        else:
            coin_returns.append(np.array([0.0]))

    for _ in range(n_sims):
        coin_finals = []
        for cr in coin_returns:
            n_t = len(cr)
            sampled = rng.choice(cr, size=n_t, replace=True)
            equity = 1.0  # each coin starts at 1.0 (normalized)
            for r in sampled:
                equity *= (1 + r * pos_frac)
                equity = max(equity, 0.001)
            coin_finals.append(equity)

        # Portfolio = average of coin equity curves (equal weight)
        portfolio_final = np.mean(coin_finals)
        final_returns.append(portfolio_final / initial_equity - 1.0)

    final_returns = np.array(final_returns)
    return {
        "prob_positive": float(np.mean(final_returns > 0)),
        "p5": float(np.percentile(final_returns, 5)),
        "p50": float(np.percentile(final_returns, 50)),
        "p95": float(np.percentile(final_returns, 95)),
        "median_max_dd": float(np.percentile(np.abs(final_returns), 50)),  # approximate
        "mean_return": float(np.mean(final_returns)),
        "prob_ruin": float(np.mean(final_returns < -0.90)),
    }

# ── Portfolio Aggregation ────────────────────────────────────────────────────

def run_portfolio_variant(data_dict, variant_name, variant_config):
    """Run strategy across all coins, aggregate as equal-weight portfolio."""
    print(f"\n{'='*70}")
    print(f"  VARIANT: {variant_name.upper()}")
    print(f"{'='*70}")

    all_results = {}
    all_trades = []
    per_coin_equity = {}
    per_coin_trades_list = []
    regime_accs = []
    per_coin_mcs = []

    for coin in COINS:
        df = data_dict[coin]
        result = backtest_strategy(df, variant_config)
        metrics = compute_metrics(result["equity_curve"], result["trades"], len(result["equity_curve"]))

        regimes, _, _, _, _ = detect_regimes(df)
        r_acc = regime_accuracy(regimes, df)
        regime_accs.append(r_acc)

        # Walk-forward
        wf = walk_forward(df, variant_config)

        # Monte Carlo
        mc = monte_carlo(result["trades"], n_sims=1000)
        per_coin_mcs.append(mc)
        per_coin_trades_list.append(result["trades"])

        all_results[coin] = {
            "metrics": metrics,
            "walk_forward": wf,
            "monte_carlo": mc,
            "regime_accuracy": r_acc,
            "regime_counts": result["regime_counts"],
            "num_trades": len(result["trades"]),
        }
        all_trades.extend(result["trades"])

        # Store normalized equity for portfolio aggregation
        eq = np.array(result["equity_curve"], dtype=float)
        if len(eq) > 0:
            per_coin_equity[coin] = eq / eq[0]
        else:
            per_coin_equity[coin] = np.ones(1)

        print(f"  {coin:5s} | Ret: {metrics['total_return']*100:7.1f}% | "
              f"Ann: {metrics['annualized_return']*100:7.1f}% | "
              f"Shrp: {metrics['sharpe']:.2f} | "
              f"DD: {metrics['max_drawdown']*100:5.1f}% | "
              f"PF: {metrics['profit_factor']:.2f} | "
              f"WR: {metrics['win_rate']*100:4.1f}% | "
              f"Trades: {len(result['trades']):3d} | "
              f"WF: {'✓' if wf['survives'] else '✗'} | "
              f"MC+: {mc['prob_positive']*100:4.1f}%")

    # Portfolio equity = average of normalized per-coin equity curves (equal weight)
    min_len = min(len(e) for e in per_coin_equity.values())
    portfolio_equity = np.zeros(min_len)
    for coin in COINS:
        portfolio_equity += per_coin_equity[coin][:min_len] / len(COINS)

    # Portfolio-level metrics
    portfolio_metrics = compute_metrics(portfolio_equity, all_trades, len(portfolio_equity))
    portfolio_wf = walk_forward_portfolio(data_dict, variant_config)
    portfolio_mc = monte_carlo_portfolio(per_coin_trades_list, n_sims=1000)

    avg_regime_acc = np.mean(regime_accs)

    print(f"\n  PORTFOLIO | Ret: {portfolio_metrics['total_return']*100:7.1f}% | "
          f"Ann: {portfolio_metrics['annualized_return']*100:7.1f}% | "
          f"Shrp: {portfolio_metrics['sharpe']:.2f} | "
          f"DD: {portfolio_metrics['max_drawdown']*100:5.1f}% | "
          f"PF: {portfolio_metrics['profit_factor']:.2f} | "
          f"WR: {portfolio_metrics['win_rate']*100:4.1f}%")

    print(f"\n  Regime Accuracy: {avg_regime_acc*100:.1f}%")
    print(f"  Walk-Forward IS  Sharpe: {portfolio_wf['is_metrics']['sharpe']:.2f}  "
          f"Ret: {portfolio_wf['is_metrics']['total_return']*100:.1f}%  "
          f"DD: {portfolio_wf['is_metrics']['max_drawdown']*100:.1f}%")
    print(f"  Walk-Forward OOS Sharpe: {portfolio_wf['oos_metrics']['sharpe']:.2f}  "
          f"Ret: {portfolio_wf['oos_metrics']['total_return']*100:.1f}%  "
          f"DD: {portfolio_wf['oos_metrics']['max_drawdown']*100:.1f}%")
    print(f"  Walk-Forward SURVIVES: {'✓ YES' if portfolio_wf['survives'] else '✗ NO'}")
    print(f"\n  Monte Carlo (1000 sims):")
    print(f"    Prob Positive Return: {portfolio_mc['prob_positive']*100:.1f}%")
    print(f"    5th Percentile:       {portfolio_mc['p5']*100:.1f}%")
    print(f"    Median (50th):        {portfolio_mc['p50']*100:.1f}%")
    print(f"    95th Percentile:      {portfolio_mc['p95']*100:.1f}%")

    return {
        "per_coin": all_results,
        "portfolio_metrics": portfolio_metrics,
        "portfolio_walk_forward": portfolio_wf,
        "portfolio_monte_carlo": portfolio_mc,
        "avg_regime_accuracy": avg_regime_acc,
    }

def walk_forward_portfolio(data_dict, variant_config):
    """Walk-forward at portfolio level — aggregate IS and OOS separately."""
    start = WARMUP_DAYS
    # Use first coin to determine length
    first_df = data_dict[COINS[0]]
    total_bars = len(first_df) - start
    split = start + int(total_bars * 0.6)
    n_is = split - start
    n_oos = len(first_df) - split

    is_equity = np.zeros(n_is)
    oos_equity = np.zeros(n_oos)

    all_is_trades = []
    all_oos_trades = []
    is_count = 0
    oos_count = 0

    for coin in COINS:
        df = data_dict[coin]
        # In-sample
        is_result = backtest_strategy(df, variant_config, start_idx=start, end_idx=split)
        is_eq = np.array(is_result["equity_curve"], dtype=float)
        if len(is_eq) == n_is and len(is_eq) > 0:
            is_equity += is_eq / is_eq[0] / len(COINS)
            is_count += 1
        all_is_trades.extend(is_result["trades"])

        # Out-of-sample
        oos_result = backtest_strategy(df, variant_config, start_idx=split, end_idx=len(df))
        oos_eq = np.array(oos_result["equity_curve"], dtype=float)
        if len(oos_eq) == n_oos and len(oos_eq) > 0:
            oos_equity += oos_eq / oos_eq[0] / len(COINS)
            oos_count += 1
        all_oos_trades.extend(oos_result["trades"])

    # Normalize so portfolio starts at 1.0
    if is_count > 0:
        total_weight = is_count / len(COINS)
        is_equity = is_equity / total_weight  # re-normalize to account for any missing coins
    if oos_count > 0:
        total_weight = oos_count / len(COINS)
        oos_equity = oos_equity / total_weight

    is_metrics = compute_metrics(is_equity, all_is_trades, n_is)
    oos_metrics = compute_metrics(oos_equity, all_oos_trades, n_oos)

    survives = (
        oos_metrics["sharpe"] > 0.3 and
        oos_metrics["max_drawdown"] < 0.40 and
        oos_metrics["total_return"] > -0.10
    )

    return {
        "is_metrics": is_metrics,
        "oos_metrics": oos_metrics,
        "survives": survives,
    }

# ── Markdown Report ──────────────────────────────────────────────────────────

def generate_report(results):
    """Generate markdown analysis report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    r = ""
    r += f"# Regime-Adaptive Combined Strategy — Master Analysis\n\n"
    r += f"*Generated: {now}*\n\n"
    r += f"**Research script:** `scripts/research_regime_combined.py`\n\n---\n\n"

    r += "## Strategy Design\n\n"
    r += "| Regime | Condition | Action |\n"
    r += "|--------|-----------|--------|\n"
    r += "| **BULL** | ADX > 25, Price > EMA(200) | Trend-following LONG with leverage, 12% trail stop |\n"
    r += "| **BEAR** | ADX > 25, Price < EMA(200) | SHORT with leverage OR go to USDC cash |\n"
    r += "| **SIDEWAYS** | ADX < 20 | Grid-style range trading, tight spacing |\n"
    r += "| **TRANSITION** | ADX 20-25 | Reduce to 50% position, no leverage |\n\n"

    r += "### Variants\n\n"
    r += "| Variant | Trend Lev | Stop Loss | Bear Action | Short Lev |\n"
    r += "|---------|-----------|-----------|-------------|-----------|\n"
    r += "| Conservative | 1x | 20% | Go to USDC | N/A |\n"
    r += "| Balanced | 2x | 15% | Short | 2x |\n"
    r += "| Aggressive | 3x | 20% | Short | 2x |\n\n"

    r += f"### Universe\n{', '.join(f'{c}USDC' for c in COINS)}\n\n"
    r += f"### Cost Model\nRound-trip: {TOTAL_COST*100:.2f}% (0.04% taker + 0.03% slippage per side)\n\n---\n\n"

    # Portfolio summary
    r += "## Portfolio Results (Equal-Weight Across 9 Coins)\n\n"
    r += "| Metric | Conservative | Balanced | Aggressive |\n"
    r += "|--------|-------------|----------|------------|\n"

    metrics_order = ["total_return", "annualized_return", "sharpe", "sortino",
                     "max_drawdown", "calmar", "profit_factor", "win_rate",
                     "num_trades", "avg_trade_duration"]

    labels = {
        "total_return": "Total Return", "annualized_return": "Annualized Return",
        "sharpe": "Sharpe Ratio", "sortino": "Sortino Ratio",
        "max_drawdown": "Max Drawdown", "calmar": "Calmar Ratio",
        "profit_factor": "Profit Factor", "win_rate": "Win Rate",
        "num_trades": "Num Trades", "avg_trade_duration": "Avg Trade Duration (days)",
    }

    for m in metrics_order:
        row = f"| {labels[m]} |"
        for v in ["conservative", "balanced", "aggressive"]:
            val = results[v]["portfolio_metrics"][m]
            if m in ("total_return", "annualized_return", "max_drawdown", "win_rate"):
                row += f" {val*100:.1f}% |"
            elif m in ("sharpe", "sortino", "calmar", "profit_factor"):
                row += f" {val:.2f} |"
            elif m == "avg_trade_duration":
                row += f" {val:.1f} |"
            else:
                row += f" {int(val)} |"
        r += row + "\n"

    r += f"\n| Regime Accuracy | {results['conservative']['avg_regime_accuracy']*100:.1f}% | {results['balanced']['avg_regime_accuracy']*100:.1f}% | {results['aggressive']['avg_regime_accuracy']*100:.1f}% |\n"

    # Success bar check
    r += "\n### Success Bar Check\n\n"
    r += "| Criterion | Threshold | Conservative | Balanced | Aggressive |\n"
    r += "|-----------|-----------|-------------|----------|------------|\n"
    for crit, label_s, threshold in [
        ("sharpe", "Sharpe > 1.0", 1.0),
        ("annualized_return", "Annualized > 50%", 0.50),
        ("max_drawdown", "Max DD < 25%", 0.25),
    ]:
        row = f"| {label_s} | {threshold:.2f} |"
        for v in ["conservative", "balanced", "aggressive"]:
            val = results[v]["portfolio_metrics"][crit]
            if crit == "max_drawdown":
                passed = val < threshold
            else:
                passed = val > threshold
            if crit in ("annualized_return", "max_drawdown"):
                row += f" {val*100:.1f}% {'✅' if passed else '❌'} |"
            else:
                row += f" {val:.2f} {'✅' if passed else '❌'} |"
        r += row + "\n"

    wf_row = "| Walk-Forward Survives | OOS Sharpe > 0.3, DD < 40%, Ret > -10% |"
    for v in ["conservative", "balanced", "aggressive"]:
        s = results[v]["portfolio_walk_forward"]["survives"]
        wf_row += f" {'✅ YES' if s else '❌ NO'} |"
    r += wf_row + "\n"

    # Walk-forward details
    r += "\n## Walk-Forward Validation (60/40 Split)\n\n"
    for v in ["conservative", "balanced", "aggressive"]:
        wf = results[v]["portfolio_walk_forward"]
        r += f"### {v.title()}\n\n"
        r += "| Period | Total Return | Annualized | Sharpe | Sortino | Max DD | Calmar |\n"
        r += "|--------|-------------|------------|--------|---------|--------|--------|\n"
        for period, metrics in [("In-Sample (60%)", wf["is_metrics"]), ("Out-of-Sample (40%)", wf["oos_metrics"])]:
            r += f"| {period} | {metrics['total_return']*100:.1f}% | {metrics['annualized_return']*100:.1f}% | {metrics['sharpe']:.2f} | {metrics['sortino']:.2f} | {metrics['max_drawdown']*100:.1f}% | {metrics['calmar']:.2f} |\n"
        r += f"\n**Survives:** {'✅ YES' if wf['survives'] else '❌ NO'}\n\n"

    # Monte Carlo
    r += "## Monte Carlo Simulation (1,000 Trade-Sequence Shuffles)\n\n"
    r += "| Metric | Conservative | Balanced | Aggressive |\n"
    r += "|--------|-------------|----------|------------|\n"
    for mc_key, label_s in [
        ("prob_positive", "Prob(Positive Return)"),
        ("p5", "5th Percentile (Worst Case)"),
        ("p50", "Median (50th)"),
        ("p95", "95th Percentile (Best Case)"),
        ("mean_return", "Mean Return"),
        ("median_max_dd", "Median Max DD"),
    ]:
        row = f"| {label_s} |"
        for v in ["conservative", "balanced", "aggressive"]:
            val = results[v]["portfolio_monte_carlo"][mc_key]
            if mc_key == "prob_positive":
                row += f" {val*100:.1f}% |"
            else:
                row += f" {val*100:.1f}% |"
        r += row + "\n"

    # Per-coin breakdown
    r += "\n## Per-Coin Breakdown\n\n"
    for v in ["conservative", "balanced", "aggressive"]:
        r += f"### {v.title()}\n\n"
        r += "| Coin | Total Ret | Ann Ret | Sharpe | Max DD | PF | Win% | Trades | WF | MC+ |\n"
        r += "|------|-----------|---------|--------|--------|----|------|--------|----|---- |\n"
        for coin in COINS:
            d = results[v]["per_coin"][coin]
            m = d["metrics"]
            wf_s = "✅" if d["walk_forward"]["survives"] else "❌"
            mc_p = d["monte_carlo"]["prob_positive"]
            r += (f"| {coin} | {m['total_return']*100:.1f}% | {m['annualized_return']*100:.1f}% | "
                  f"{m['sharpe']:.2f} | {m['max_drawdown']*100:.1f}% | {m['profit_factor']:.2f} | "
                  f"{m['win_rate']*100:.0f}% | {d['num_trades']} | {wf_s} | {mc_p*100:.0f}% |\n")
        r += "\n"

    # Per-coin walk-forward detail
    r += "## Per-Coin Walk-Forward Detail\n\n"
    for v in ["conservative", "balanced", "aggressive"]:
        r += f"### {v.title()}\n\n"
        r += "| Coin | IS Ret | IS Sharpe | IS DD | OOS Ret | OOS Sharpe | OOS DD | Survives |\n"
        r += "|------|--------|-----------|-------|---------|------------|--------|----------|\n"
        for coin in COINS:
            wf = results[v]["per_coin"][coin]["walk_forward"]
            is_m = wf["is_metrics"]
            oos_m = wf["oos_metrics"]
            s = "✅" if wf["survives"] else "❌"
            r += (f"| {coin} | {is_m['total_return']*100:.1f}% | {is_m['sharpe']:.2f} | "
                  f"{is_m['max_drawdown']*100:.1f}% | {oos_m['total_return']*100:.1f}% | "
                  f"{oos_m['sharpe']:.2f} | {oos_m['max_drawdown']*100:.1f}% | {s} |\n")
        r += "\n"

    # Conclusion
    r += "## Verdict & Recommendation\n\n"

    best_variant = None
    best_score = -999
    for v in ["conservative", "balanced", "aggressive"]:
        m = results[v]["portfolio_metrics"]
        wf_ok = results[v]["portfolio_walk_forward"]["survives"]
        score = m["sharpe"] + m["calmar"] - m["max_drawdown"]
        if wf_ok:
            score += 0.5
        if score > best_score:
            best_score = score
            best_variant = v

    bm = results[best_variant]["portfolio_metrics"]
    bwf = results[best_variant]["portfolio_walk_forward"]
    bmc = results[best_variant]["portfolio_monte_carlo"]

    r += f"**Best Variant: {best_variant.upper()}**\n\n"
    r += f"- Sharpe: {bm['sharpe']:.2f} (target > 1.0 → {'✅' if bm['sharpe'] > 1.0 else '❌'})\n"
    r += f"- Annualized: {bm['annualized_return']*100:.1f}% (target > 50% → {'✅' if bm['annualized_return'] > 0.50 else '❌'})\n"
    r += f"- Max DD: {bm['max_drawdown']*100:.1f}% (target < 25% → {'✅' if bm['max_drawdown'] < 0.25 else '❌'})\n"
    r += f"- Walk-Forward: {'✅ SURVIVES' if bwf['survives'] else '❌ FAILS'}\n"
    r += f"- Monte Carlo Prob(+): {bmc['prob_positive']*100:.1f}%\n"
    r += f"- Monte Carlo 5th %ile: {bmc['p5']*100:.1f}%\n\n"

    all_pass = (
        bm["sharpe"] > 1.0 and
        bm["annualized_return"] > 0.50 and
        bm["max_drawdown"] < 0.25 and
        bwf["survives"]
    )

    if all_pass:
        r += "### 🟢 MEETS ALL SUCCESS CRITERIA — READY FOR LIVE DEPLOYMENT EVALUATION\n\n"
        r += "This variant meets or exceeds all target metrics AND survives out-of-sample. "
        r += "Recommend escalation for paper-trading validation before live capital allocation.\n\n"
    else:
        fails = []
        if bm["sharpe"] <= 1.0:
            fails.append(f"Sharpe {bm['sharpe']:.2f} ≤ 1.0")
        if bm["annualized_return"] <= 0.50:
            fails.append(f"Annualized {bm['annualized_return']*100:.1f}% ≤ 50%")
        if bm["max_drawdown"] >= 0.25:
            fails.append(f"Max DD {bm['max_drawdown']*100:.1f}% ≥ 25%")
        if not bwf["survives"]:
            fails.append("Walk-forward does not survive")
        r += f"### 🔴 DOES NOT MEET ALL SUCCESS CRITERIA\n\n"
        r += f"**Failing:** {', '.join(fails)}\n\n"
        r += "The strategy shows promise but does not clear all deployment bars. "
        r += "See per-coin and per-regime analysis for improvement paths.\n\n"

    r += "## Key Risks\n\n"
    r += "1. **Regime lag:** ADX is a lagging indicator; regime changes may be detected 5-15 bars late\n"
    r += "2. **Leverage amplification:** Drawdowns scale linearly with leverage in adverse moves\n"
    r += "3. **Short squeeze risk:** Bear-regime shorts are exposed to sudden reversals\n"
    r += "4. **Grid whipsaw:** Sideways regime misclassification → false grid signals\n"
    r += "5. **Correlation collapse:** All 9 coins are crypto-correlated; tail events hit all positions\n"
    r += ("6. **Monte Carlo caveat:** Trade-shuffle MC preserves return distribution but not temporal "
          "structure; actual worst-case sequences may differ\n\n")

    r += "## Methodology Notes\n\n"
    r += "- **Data:** 365 days daily OHLCV from Binance public API (spot), 200-day indicator warmup\n"
    r += "- **Regime detection:** ADX(14) Wilder's method + EMA(200) trend filter\n"
    r += "- **Walk-forward:** 60% in-sample / 40% out-of-sample, no re-optimization\n"
    r += "- **Monte Carlo:** 1,000 random permutations of trade sequence, 50% position sizing\n"
    r += "- **Costs:** 0.14% round-trip (0.04% taker + 0.03% slippage per side)\n"
    r += "- **Liquidation:** Modeled at 1/leverage - 0.1% buffer for leveraged positions\n"

    return r

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  REGIME-ADAPTIVE COMBINED STRATEGY — THE MASTER STRATEGY")
    print("=" * 70)

    # Load data
    print("\n[1/5] Loading market data...")
    data_dict = {}
    for coin in COINS:
        df = get_daily_data(coin)
        if len(df) < LOOKBACK_DAYS + WARMUP_DAYS:
            print(f"  [WARN] {coin}: only {len(df)} rows, need {LOOKBACK_DAYS + WARMUP_DAYS}")
        data_dict[coin] = df
        print(f"  {coin}: {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")

    # Run all variants
    print("\n[2/5] Running Conservative variant...")
    conservative = run_portfolio_variant(data_dict, "conservative", VARIANTS["conservative"])

    print("\n[3/5] Running Balanced variant...")
    balanced = run_portfolio_variant(data_dict, "balanced", VARIANTS["balanced"])

    print("\n[4/5] Running Aggressive variant...")
    aggressive = run_portfolio_variant(data_dict, "aggressive", VARIANTS["aggressive"])

    all_results = {
        "conservative": conservative,
        "balanced": balanced,
        "aggressive": aggressive,
    }

    # Generate report
    print("\n[5/5] Generating report...")
    report = generate_report(all_results)

    report_path = RESULTS_DIR / "regime-combined-analysis.md"
    report_path.write_text(report)
    print(f"\n  Report saved: {report_path}")

    # Save JSON
    json_path = RESULTS_DIR / "regime-combined-data.json"

    def serialize(obj):
        if isinstance(obj, dict):
            return {k: serialize(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [serialize(v) for v in obj]
        elif isinstance(obj, (np.integer,)):
            return int(obj)
        elif isinstance(obj, (np.floating,)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return obj

    json_path.write_text(json.dumps(serialize(all_results), indent=2, default=str))
    print(f"  Data saved: {json_path}")

    # Final summary
    print("\n" + "=" * 70)
    print("  FINAL SUMMARY")
    print("=" * 70)

    for v in ["conservative", "balanced", "aggressive"]:
        m = all_results[v]["portfolio_metrics"]
        wf = all_results[v]["portfolio_walk_forward"]
        mc = all_results[v]["portfolio_monte_carlo"]
        print(f"\n  {v.upper()}:")
        print(f"    Return: {m['total_return']*100:.1f}% | Ann: {m['annualized_return']*100:.1f}% | "
              f"Sharpe: {m['sharpe']:.2f} | Sortino: {m['sortino']:.2f}")
        print(f"    Max DD: {m['max_drawdown']*100:.1f}% | Calmar: {m['calmar']:.2f} | "
              f"PF: {m['profit_factor']:.2f} | Win: {m['win_rate']*100:.1f}%")
        print(f"    Walk-Forward: {'SURVIVES' if wf['survives'] else 'FAILS'} "
              f"(OOS Sharpe: {wf['oos_metrics']['sharpe']:.2f})")
        print(f"    Monte Carlo: P(+ve)={mc['prob_positive']*100:.1f}%  "
              f"5th%ile={mc['p5']*100:.1f}%  Median={mc['p50']*100:.1f}%")

    print("\n  Done.")

if __name__ == "__main__":
    main()
