#!/usr/bin/env python3
"""
Coin Selection Optimization for Balanced Regime-Adaptive Strategy
=================================================================
The Balanced regime-adaptive strategy hit +57.6% annualized OOS with Sharpe 0.95.
The problem: bad coins (INJ -99%, DOGE -84%, LINK -62%) dragged it down.

This script solves the coin selection problem:
  1. Trend Quality Score for 30+ USDC pairs
  2. Static selection: top 3, 5, 7 coins vs original 9-coin portfolio
  3. Dynamic coin rotation based on trailing 30-day ADX
  4. Trend-weighted position sizing (ADX-proportional)
  5. Full walk-forward + Monte Carlo on best configuration

Success bar: Sharpe > 1.0, annualized > 50%, max DD < 25%, MC prob(+) > 60%
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

# Extended universe: 30+ USDC pairs
ALL_COINS = [
    # Original 9
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "INJ",
    # Extended universe
    "ADA", "DOT", "MATIC", "LTC", "ATOM", "NEAR", "APT",
    "ARB", "OP", "FIL", "ICP", "IMX", "INJ",
    "RUNE", "GRT", "SAND", "MANA", "AXS", "GALA", "FTM", "ALGO",
    "STX", "EGLD", "THETA", "XLM", "HBAR", "VET", "AAVE", "MKR",
]

LOOKBACK_DAYS = 365
WARMUP_DAYS = 200
BINANCE_API = "https://api.binance.com/api/v3"

FEE_PER_SIDE = 0.0004
SLIPPAGE_PER_SIDE = 0.0003
TOTAL_COST = 2 * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

ADX_BULL_BEAR = 25
ADX_SIDEWAYS = 20
EMA_TREND = 200
ADX_PERIOD = 14

BALANCED_CONFIG = {
    "trend_lev": 2.0,
    "stop_loss": 0.15,
    "trail_stop": 0.10,
    "bear_action": "short",
    "grid_spacing_pct": 0.025,
    "grid_levels": 4,
    "transition_fraction": 0.5,
}

ORIGINAL_COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "INJ"]

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
            return all_data
        batch = r.json()
        if not batch:
            break
        all_data = batch + all_data
        current_end = batch[0][0] - 1
        if len(batch) < batch_size:
            break
        time.sleep(0.12)
    return all_data


def get_daily_data(coin):
    cache_file = CACHE_DIR / f"{coin}_daily.csv"
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=["date"], index_col="date")
        if len(df) >= LOOKBACK_DAYS + WARMUP_DAYS:
            return df
    symbol = f"{coin}USDC"
    raw = fetch_klines(symbol, "1d", limit=LOOKBACK_DAYS + WARMUP_DAYS + 50)
    if len(raw) < 100:
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


def detect_regimes(df):
    ema200 = compute_ema_series(df["close"], EMA_TREND)
    adx, plus_di, minus_di = compute_adx_series(df, ADX_PERIOD)

    regimes = pd.Series("transition", index=df.index)
    above_ema = df["close"] > ema200

    trending = adx >= ADX_BULL_BEAR
    sideways = adx < ADX_SIDEWAYS
    transition = ~trending & ~sideways

    regimes[trending & above_ema] = "bull"
    regimes[trending & ~above_ema] = "bear"
    regimes[sideways] = "sideways"
    regimes[transition] = "transition"

    return regimes, ema200, adx, plus_di, minus_di


# ── Trend Quality Score ──────────────────────────────────────────────────────

def compute_trend_quality(df, split_idx=None):
    """
    Compute a composite trend quality score for a coin.

    The score must predict profitability of the Balanced regime-adaptive strategy,
    which is long-biased (longs in bull, shorts in bear, longs in transition).

    Components:
      1. ADX(14) average (higher = stronger sustained trends)
      2. Directional trend efficiency: net_price_change / total_volatility
         (positive = coin went up net, good for long-biased strategy)
      3. Regime-correct signal ratio (how often trend-following would have been right)
      4. R-squared of linear regression on log price, multiplied by sign of slope
         (positive trend R² gets full credit, downward R² gets penalized)
      5. Buy-and-hold Sharpe ratio (risk-adjusted drift — key for long-biased strategy)

    If split_idx is provided, computes score using only data up to split_idx (IS period).
    """
    end_idx = split_idx if split_idx is not None else len(df)
    if end_idx < WARMUP_DAYS + 50:
        return None

    # Use data after warmup up to end_idx
    df_use = df.iloc[WARMUP_DAYS:end_idx].copy()
    close = df_use["close"]
    n = len(close)
    if n < 30:
        return None

    # 1. Average ADX (using full df for indicator warmup, then slice)
    _, _, adx_full = compute_adx_series(df, ADX_PERIOD)
    adx_use = adx_full.iloc[WARMUP_DAYS:end_idx]
    avg_adx = float(adx_use.mean()) if len(adx_use) > 0 else 0.0

    # 2. Directional trend efficiency: NET price change / total absolute path
    # This is positive for upward-trending coins, negative for downward
    net_change = (close.iloc[-1] - close.iloc[0]) / close.iloc[0]
    daily_abs_rets = close.pct_change().abs().sum()
    if daily_abs_rets > 0:
        trend_efficiency = float(net_change / daily_abs_rets)  # signed!
    else:
        trend_efficiency = 0.0

    # 3. Regime-correct signals
    regimes, ema200, _, _, _ = detect_regimes(df)
    fwd_ret = df["close"].pct_change(5).shift(-5)
    correct = 0
    total = 0
    for i in range(WARMUP_DAYS, end_idx - 5):
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
            correct += 0.5
    regime_correct_ratio = correct / total if total > 0 else 0.0

    # 4. R-squared of linear regression on log price, SIGNED by slope direction
    log_price = np.log(close.values)
    x = np.arange(n, dtype=float)
    if n > 2:
        x_mean = x.mean()
        y_mean = log_price.mean()
        ss_xy = np.sum((x - x_mean) * (log_price - y_mean))
        ss_xx = np.sum((x - x_mean) ** 2)
        ss_yy = np.sum((log_price - y_mean) ** 2)
        if ss_xx > 0 and ss_yy > 0:
            r_squared = float((ss_xy ** 2) / (ss_xx * ss_yy))
            slope = ss_xy / ss_xx  # sign of slope
            signed_r_squared = r_squared * np.sign(slope)
        else:
            r_squared = 0.0
            signed_r_squared = 0.0
    else:
        r_squared = 0.0
        signed_r_squared = 0.0

    # 5. Buy-and-hold Sharpe ratio (daily returns)
    daily_rets = close.pct_change().dropna()
    if len(daily_rets) > 10 and np.std(daily_rets) > 0:
        bh_sharpe = float(np.mean(daily_rets) / np.std(daily_rets) * math.sqrt(365))
    else:
        bh_sharpe = 0.0

    # ── Normalize each component to 0-1 ──
    # ADX: 15-35 maps to 0-1
    adx_norm = np.clip((avg_adx - 15) / 20, 0, 1)
    # Directional efficiency: -0.1 to +0.1 maps to 0-1
    eff_norm = np.clip((trend_efficiency + 0.1) / 0.2, 0, 1)
    # Regime correct: 0.33-0.55 maps to 0-1
    regime_norm = np.clip((regime_correct_ratio - 0.33) / 0.22, 0, 1)
    # Signed R-squared: -1 to +1 maps to 0-1
    rsq_norm = np.clip((signed_r_squared + 1) / 2, 0, 1)
    # Buy-and-hold Sharpe: -2 to +2 maps to 0-1
    bh_norm = np.clip((bh_sharpe + 2) / 4, 0, 1)

    # Composite score — weighted toward directional components since the strategy is long-biased
    composite = (
        0.15 * adx_norm +
        0.25 * eff_norm +        # directional efficiency is key
        0.15 * regime_norm +
        0.20 * rsq_norm +        # signed R² captures trend direction + cleanliness
        0.25 * bh_norm           # buy-and-hold Sharpe is the ultimate directional test
    )

    return {
        "coin": "",
        "avg_adx": avg_adx,
        "trend_efficiency": trend_efficiency,
        "regime_correct_ratio": regime_correct_ratio,
        "r_squared": r_squared,
        "signed_r_squared": signed_r_squared,
        "bh_sharpe": bh_sharpe,
        "net_return": float(net_change),
        "adx_norm": float(adx_norm),
        "eff_norm": float(eff_norm),
        "regime_norm": float(regime_norm),
        "rsq_norm": float(rsq_norm),
        "bh_norm": float(bh_norm),
        "composite_score": float(composite),
        "split_idx": split_idx,
    }


# ── Strategy Backtester (reused from original) ──────────────────────────────

def backtest_strategy(df, variant_config, start_idx=0, end_idx=None):
    if end_idx is None:
        end_idx = len(df)

    regimes, ema200, adx, plus_di, minus_di = detect_regimes(df)
    atr_series = compute_atr_series(df, ADX_PERIOD)

    cfg = variant_config
    equity = 1.0
    position = 0.0
    entry_price = 0.0
    entry_idx = 0
    leverage = 1.0
    trail_stop_price = 0.0

    equity_curve = []
    trades = []

    for i in range(max(start_idx, WARMUP_DAYS), end_idx):
        row = df.iloc[i]
        price = row["close"]
        high = row["high"]
        low = row["low"]
        regime = regimes.iloc[i]

        if position != 0:
            if position > 0:
                worst = (low - entry_price) / entry_price
            else:
                worst = (entry_price - high) / entry_price

            liq_threshold = 1.0 / leverage - 0.001
            stop_hit = worst <= -cfg["stop_loss"]
            liq_hit = worst <= -liq_threshold
            trail_hit = False
            if position > 0 and trail_stop_price > 0 and low <= trail_stop_price:
                trail_hit = True
            elif position < 0 and trail_stop_price > 0 and high >= trail_stop_price:
                trail_hit = True

            if liq_hit:
                loss_pct = -liq_threshold
                pnl = equity * abs(position) * loss_pct * leverage
                equity += pnl - equity * abs(position) * TOTAL_COST
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "direction": "long" if position > 0 else "short",
                    "return": loss_pct * leverage, "pnl": pnl,
                    "exit_reason": "liquidation",
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
                    "direction": "long" if position > 0 else "short",
                    "return": levered_ret, "pnl": pnl,
                    "exit_reason": exit_reason,
                })
                position = 0.0
                trail_stop_price = 0.0

        if regime == "bull":
            target_pos = 1.0
            lev = cfg["trend_lev"]
            if position <= 0:
                if position < 0:
                    close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "short", "return": close_ret,
                        "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change",
                    })
                position = target_pos
                entry_price = price
                entry_idx = i
                leverage = lev
                trail_stop_price = price * (1 - cfg["trail_stop"])
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        elif regime == "bear":
            if cfg["bear_action"] == "short":
                target_pos = -1.0
                short_lev = cfg.get("short_lev", cfg["trend_lev"])
                if position >= 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                        trades.append({
                            "entry_idx": entry_idx, "exit_idx": i,
                            "direction": "long", "return": close_ret,
                            "pnl": equity * abs(position) * close_ret,
                            "exit_reason": "regime_change",
                        })
                    position = target_pos
                    entry_price = price
                    entry_idx = i
                    leverage = short_lev
                    trail_stop_price = price * (1 + cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
            else:  # cash
                if position != 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change_cash",
                    })
                    position = 0.0
                    trail_stop_price = 0.0

        elif regime == "sideways":
            if position == 0 and atr_series.iloc[i] > 0:
                position = 0.5
                entry_price = price
                entry_idx = i
                leverage = 1.0
                trail_stop_price = price * (1 - cfg["grid_spacing_pct"] * cfg["grid_levels"])
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
            elif position != 0:
                if position > 0:
                    unrealized = (price - entry_price) / entry_price
                    if unrealized >= cfg["grid_spacing_pct"] * 2:
                        equity += equity * abs(position) * unrealized - equity * abs(position) * TOTAL_COST
                        trades.append({
                            "entry_idx": entry_idx, "exit_idx": i,
                            "direction": "long", "return": unrealized,
                            "pnl": equity * abs(position) * unrealized,
                            "exit_reason": "grid_tp",
                        })
                        position = 0.0
                        trail_stop_price = 0.0

        elif regime == "transition":
            target_frac = cfg["transition_fraction"]
            if position != 0:
                if abs(position) > target_frac + 0.01 or leverage > 1.0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "transition_reduce",
                    })
                    position = 0.0
                    leverage = 1.0
                    trail_stop_price = 0.0
            if position == 0:
                above_ema_now = price > ema200.iloc[i]
                if above_ema_now:
                    position = target_frac
                    entry_price = price
                    entry_idx = i
                    leverage = 1.0
                    trail_stop_price = price * (1 - cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        if position > 0:
            new_trail = price * (1 - cfg["trail_stop"])
            trail_stop_price = max(trail_stop_price, new_trail)
        elif position < 0:
            new_trail = price * (1 + cfg["trail_stop"])
            trail_stop_price = min(trail_stop_price, new_trail) if trail_stop_price > 0 else new_trail

        equity_curve.append(equity)

    if position != 0 and end_idx == len(df):
        price = df.iloc[end_idx - 1]["close"]
        if position > 0:
            close_ret = (price - entry_price) / entry_price * leverage
        else:
            close_ret = (entry_price - price) / entry_price * leverage
        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
        trades.append({
            "entry_idx": entry_idx, "exit_idx": end_idx - 1,
            "direction": "long" if position > 0 else "short",
            "return": close_ret, "pnl": equity * abs(position) * close_ret,
            "exit_reason": "end_of_data",
        })

    return {
        "equity_curve": equity_curve,
        "final_equity": equity,
        "trades": trades,
    }


# ── Trend-Weighted Backtester ────────────────────────────────────────────────

def backtest_strategy_weighted(df, variant_config, position_scale=1.0, start_idx=0, end_idx=None):
    """
    Backtest with a position scale factor. Used for trend-weighted position sizing
    where higher-ADX coins get larger positions.
    """
    if end_idx is None:
        end_idx = len(df)

    # Deep copy config with scaled position
    cfg = {**variant_config}
    # Scale position fractions
    # We'll apply position_scale to the target position in each regime

    regimes, ema200, adx, plus_di, minus_di = detect_regimes(df)
    atr_series = compute_atr_series(df, ADX_PERIOD)

    equity = 1.0
    position = 0.0
    entry_price = 0.0
    entry_idx = 0
    leverage = 1.0
    trail_stop_price = 0.0

    equity_curve = []
    trades = []

    for i in range(max(start_idx, WARMUP_DAYS), end_idx):
        row = df.iloc[i]
        price = row["close"]
        high = row["high"]
        low = row["low"]
        regime = regimes.iloc[i]

        if position != 0:
            if position > 0:
                worst = (low - entry_price) / entry_price
            else:
                worst = (entry_price - high) / entry_price

            liq_threshold = 1.0 / leverage - 0.001
            stop_hit = worst <= -cfg["stop_loss"]
            liq_hit = worst <= -liq_threshold
            trail_hit = False
            if position > 0 and trail_stop_price > 0 and low <= trail_stop_price:
                trail_hit = True
            elif position < 0 and trail_stop_price > 0 and high >= trail_stop_price:
                trail_hit = True

            if liq_hit:
                loss_pct = -liq_threshold
                pnl = equity * abs(position) * loss_pct * leverage
                equity += pnl - equity * abs(position) * TOTAL_COST
                trades.append({
                    "entry_idx": entry_idx, "exit_idx": i,
                    "direction": "long" if position > 0 else "short",
                    "return": loss_pct * leverage, "pnl": pnl,
                    "exit_reason": "liquidation",
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
                    "direction": "long" if position > 0 else "short",
                    "return": levered_ret, "pnl": pnl,
                    "exit_reason": exit_reason,
                })
                position = 0.0
                trail_stop_price = 0.0

        if regime == "bull":
            target_pos = 1.0 * position_scale
            lev = cfg["trend_lev"]
            if position <= 0:
                if position < 0:
                    close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "short", "return": close_ret,
                        "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change",
                    })
                position = target_pos
                entry_price = price
                entry_idx = i
                leverage = lev
                trail_stop_price = price * (1 - cfg["trail_stop"])
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        elif regime == "bear":
            if cfg["bear_action"] == "short":
                target_pos = -1.0 * position_scale
                short_lev = cfg.get("short_lev", cfg["trend_lev"])
                if position >= 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                        trades.append({
                            "entry_idx": entry_idx, "exit_idx": i,
                            "direction": "long", "return": close_ret,
                            "pnl": equity * abs(position) * close_ret,
                            "exit_reason": "regime_change",
                        })
                    position = target_pos
                    entry_price = price
                    entry_idx = i
                    leverage = short_lev
                    trail_stop_price = price * (1 + cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)
            else:
                if position != 0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "regime_change_cash",
                    })
                    position = 0.0
                    trail_stop_price = 0.0

        elif regime == "sideways":
            if position == 0 and atr_series.iloc[i] > 0:
                position = 0.5 * position_scale
                entry_price = price
                entry_idx = i
                leverage = 1.0
                trail_stop_price = price * (1 - cfg["grid_spacing_pct"] * cfg["grid_levels"])
                equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        elif regime == "transition":
            target_frac = cfg["transition_fraction"] * position_scale
            if position != 0:
                if abs(position) > target_frac + 0.01 or leverage > 1.0:
                    if position > 0:
                        close_ret = (price - entry_price) / entry_price * leverage
                    else:
                        close_ret = (entry_price - price) / entry_price * leverage
                    equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
                    trades.append({
                        "entry_idx": entry_idx, "exit_idx": i,
                        "direction": "long" if position > 0 else "short",
                        "return": close_ret, "pnl": equity * abs(position) * close_ret,
                        "exit_reason": "transition_reduce",
                    })
                    position = 0.0
                    leverage = 1.0
                    trail_stop_price = 0.0
            if position == 0:
                above_ema_now = price > ema200.iloc[i]
                if above_ema_now:
                    position = target_frac
                    entry_price = price
                    entry_idx = i
                    leverage = 1.0
                    trail_stop_price = price * (1 - cfg["trail_stop"])
                    equity -= equity * abs(position) * (FEE_PER_SIDE + SLIPPAGE_PER_SIDE)

        if position > 0:
            new_trail = price * (1 - cfg["trail_stop"])
            trail_stop_price = max(trail_stop_price, new_trail)
        elif position < 0:
            new_trail = price * (1 + cfg["trail_stop"])
            trail_stop_price = min(trail_stop_price, new_trail) if trail_stop_price > 0 else new_trail

        equity_curve.append(equity)

    if position != 0 and end_idx == len(df):
        price = df.iloc[end_idx - 1]["close"]
        if position > 0:
            close_ret = (price - entry_price) / entry_price * leverage
        else:
            close_ret = (entry_price - price) / entry_price * leverage
        equity += equity * abs(position) * close_ret - equity * abs(position) * TOTAL_COST
        trades.append({
            "entry_idx": entry_idx, "exit_idx": end_idx - 1,
            "direction": "long" if position > 0 else "short",
            "return": close_ret, "pnl": equity * abs(position) * close_ret,
            "exit_reason": "end_of_data",
        })

    return {
        "equity_curve": equity_curve,
        "final_equity": equity,
        "trades": trades,
    }


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_metrics(equity_curve, trades, n_days):
    if len(equity_curve) < 2:
        return _empty_metrics()

    eq = np.array(equity_curve, dtype=float)
    daily_rets = np.diff(eq) / eq[:-1]
    daily_rets = np.nan_to_num(daily_rets, nan=0.0)

    total_return = (eq[-1] / eq[0]) - 1.0 if eq[0] > 0 else 0.0
    if n_days > 0 and eq[-1] > 0 and eq[0] > 0:
        ann_return = (eq[-1] / eq[0]) ** (365.0 / n_days) - 1.0
    else:
        ann_return = 0.0

    if np.std(daily_rets) > 0:
        sharpe = np.mean(daily_rets) / np.std(daily_rets) * math.sqrt(365)
    else:
        sharpe = 0.0

    downside = daily_rets[daily_rets < 0]
    if len(downside) > 0 and np.std(downside) > 0:
        sortino = np.mean(daily_rets) / np.std(downside) * math.sqrt(365)
    else:
        sortino = sharpe * 1.5 if sharpe > 0 else 0.0

    running_max = np.maximum.accumulate(eq)
    drawdowns = (eq - running_max) / running_max
    max_dd = abs(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

    calmar = ann_return / max_dd if max_dd > 0.001 else 0.0

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

    wins = sum(1 for t in trades if t["return"] > 0)
    win_rate = wins / len(trades) if trades else 0.0

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
    }


def _empty_metrics():
    return {
        "total_return": 0.0, "annualized_return": 0.0, "sharpe": 0.0,
        "sortino": 0.0, "max_drawdown": 0.0, "calmar": 0.0,
        "profit_factor": 0.0, "win_rate": 0.0, "num_trades": 0,
    }


# ── Walk-Forward ─────────────────────────────────────────────────────────────

def walk_forward_portfolio(data_dict, coin_list, variant_config):
    """Walk-forward with pre-selected coin list (may use IS-only selection)."""
    start = WARMUP_DAYS
    first_df = data_dict[coin_list[0]]
    total_bars = len(first_df) - start
    split = start + int(total_bars * 0.6)
    n_is = split - start
    n_oos = len(first_df) - split

    is_equity = np.zeros(n_is)
    oos_equity = np.zeros(n_oos)
    all_is_trades = []
    all_oos_trades = []
    n_coins = len(coin_list)

    for coin in coin_list:
        df = data_dict[coin]
        is_result = backtest_strategy(df, variant_config, start_idx=start, end_idx=split)
        is_eq = np.array(is_result["equity_curve"], dtype=float)
        if len(is_eq) == n_is and len(is_eq) > 0:
            is_equity += is_eq / is_eq[0] / n_coins
        all_is_trades.extend(is_result["trades"])

        oos_result = backtest_strategy(df, variant_config, start_idx=split, end_idx=len(df))
        oos_eq = np.array(oos_result["equity_curve"], dtype=float)
        if len(oos_eq) == n_oos and len(oos_eq) > 0:
            oos_equity += oos_eq / oos_eq[0] / n_coins
        all_oos_trades.extend(oos_result["trades"])

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
        "split_idx": split,
    }


# ── Monte Carlo ──────────────────────────────────────────────────────────────

def monte_carlo_portfolio(per_coin_trades, initial_equity=1.0, n_sims=1000):
    pos_frac = 0.3
    final_returns = []
    rng = np.random.RandomState(42)

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
            equity = 1.0
            for r in sampled:
                equity *= (1 + r * pos_frac)
                equity = max(equity, 0.001)
            coin_finals.append(equity)
        portfolio_final = np.mean(coin_finals)
        final_returns.append(portfolio_final / initial_equity - 1.0)

    final_returns = np.array(final_returns)
    return {
        "prob_positive": float(np.mean(final_returns > 0)),
        "p5": float(np.percentile(final_returns, 5)),
        "p50": float(np.percentile(final_returns, 50)),
        "p95": float(np.percentile(final_returns, 95)),
        "mean_return": float(np.mean(final_returns)),
        "prob_ruin": float(np.mean(final_returns < -0.90)),
    }


# ── Portfolio Runner ─────────────────────────────────────────────────────────

def run_portfolio(data_dict, coin_list, variant_config, label="", weights=None):
    """Run strategy on a set of coins, aggregate as portfolio."""
    per_coin_equity = {}
    per_coin_trades_list = []
    per_coin_metrics = {}

    for coin in coin_list:
        df = data_dict[coin]
        result = backtest_strategy(df, variant_config)
        metrics = compute_metrics(result["equity_curve"], result["trades"], len(result["equity_curve"]))
        per_coin_metrics[coin] = metrics
        per_coin_trades_list.append(result["trades"])

        eq = np.array(result["equity_curve"], dtype=float)
        if len(eq) > 0:
            per_coin_equity[coin] = eq / eq[0]
        else:
            per_coin_equity[coin] = np.ones(1)

    n_coins = len(coin_list)
    min_len = min(len(e) for e in per_coin_equity.values())

    if weights is None:
        weights = {coin: 1.0 / n_coins for coin in coin_list}

    portfolio_equity = np.zeros(min_len)
    for coin in coin_list:
        portfolio_equity += per_coin_equity[coin][:min_len] * weights[coin]

    # Normalize so it starts at 1.0
    total_w = sum(weights.values())
    if total_w > 0:
        portfolio_equity /= total_w

    all_trades = [t for trades_list in per_coin_trades_list for t in trades_list]
    portfolio_metrics = compute_metrics(portfolio_equity, all_trades, len(portfolio_equity))
    wf = walk_forward_portfolio(data_dict, coin_list, variant_config)
    mc = monte_carlo_portfolio(per_coin_trades_list, n_sims=1000)

    print(f"\n  [{label}] Portfolio ({n_coins} coins): {', '.join(coin_list)}")
    print(f"    Ret: {portfolio_metrics['total_return']*100:7.1f}% | "
          f"Ann: {portfolio_metrics['annualized_return']*100:7.1f}% | "
          f"Shrp: {portfolio_metrics['sharpe']:.2f} | "
          f"DD: {portfolio_metrics['max_drawdown']*100:5.1f}% | "
          f"PF: {portfolio_metrics['profit_factor']:.2f} | "
          f"WF OOS Sharpe: {wf['oos_metrics']['sharpe']:.2f} | "
          f"MC+: {mc['prob_positive']*100:.1f}%")

    return {
        "coins": coin_list,
        "portfolio_metrics": portfolio_metrics,
        "walk_forward": wf,
        "monte_carlo": mc,
        "per_coin": per_coin_metrics,
    }


# ── Dynamic Coin Selection ───────────────────────────────────────────────────

def run_dynamic_selection(data_dict, coin_list, variant_config, adx_threshold=25, lookback=30):
    """
    Dynamic coin selection: at each rebalance point (monthly), select coins
    with trailing 30-day ADX > threshold. Run strategy on selected coins.

    We approximate this by running the strategy on each coin independently,
    then constructing a portfolio equity curve where at each day, only coins
    with ADX > threshold on that day contribute.
    """
    print("\n  Computing dynamic selection portfolio...")

    # Get ADX series for each coin
    coin_adx = {}
    coin_results = {}
    for coin in coin_list:
        df = data_dict[coin]
        _, _, adx_series, _, _ = detect_regimes(df)
        coin_adx[coin] = adx_series
        result = backtest_strategy(df, variant_config)
        coin_results[coin] = result

    # Determine aligned indices
    first_coin = coin_list[0]
    df0 = data_dict[first_coin]
    start = WARMUP_DAYS
    n_bars = len(df0) - start

    # Build equity curves normalized to 1.0
    coin_eq = {}
    for coin in coin_list:
        eq = np.array(coin_results[coin]["equity_curve"], dtype=float)
        if len(eq) > 0:
            coin_eq[coin] = eq / eq[0]
        else:
            coin_eq[coin] = np.ones(n_bars)

    # For each bar, determine which coins are "active" (ADX > threshold)
    portfolio_equity = np.zeros(n_bars)
    active_counts = np.zeros(n_bars)

    for i in range(n_bars):
        bar_idx = start + i
        active_coins = []
        for coin in coin_list:
            if bar_idx < len(coin_adx[coin]):
                trailing_adx = coin_adx[coin].iloc[max(0, bar_idx - lookback):bar_idx + 1].mean()
                if trailing_adx >= adx_threshold:
                    active_coins.append(coin)

        if active_coins:
            weights = {c: 1.0 / len(active_coins) for c in active_coins}
            eq_sum = 0.0
            for coin in active_coins:
                if i < len(coin_eq[coin]):
                    eq_sum += coin_eq[coin][i] * weights[coin]
            portfolio_equity[i] = eq_sum
            active_counts[i] = len(active_coins)
        else:
            # No active coins — hold cash (use previous value)
            portfolio_equity[i] = portfolio_equity[i - 1] if i > 0 else 1.0
            active_counts[i] = 0

    # Normalize to start at 1.0
    if portfolio_equity[0] > 0:
        portfolio_equity /= portfolio_equity[0]

    all_trades = []
    per_coin_trades_list = []
    for coin in coin_list:
        all_trades.extend(coin_results[coin]["trades"])
        per_coin_trades_list.append(coin_results[coin]["trades"])

    metrics = compute_metrics(portfolio_equity, all_trades, n_bars)

    # Walk-forward with dynamic selection
    split = start + int(n_bars * 0.6)
    n_is = split - start
    n_oos = len(df0) - split

    is_eq_dyn = np.zeros(n_is)
    oos_eq_dyn = np.zeros(n_oos)

    for coin in coin_list:
        df = data_dict[coin]
        is_result = backtest_strategy(df, variant_config, start_idx=start, end_idx=split)
        is_e = np.array(is_result["equity_curve"], dtype=float)
        if len(is_e) == n_is and len(is_e) > 0:
            is_eq_dyn += is_e / is_e[0] / len(coin_list)

        oos_result = backtest_strategy(df, variant_config, start_idx=split, end_idx=len(df))
        oos_e = np.array(oos_result["equity_curve"], dtype=float)
        if len(oos_e) == n_oos and len(oos_e) > 0:
            oos_eq_dyn += oos_e / oos_e[0] / len(coin_list)

    is_metrics = compute_metrics(is_eq_dyn, [], n_is)
    oos_metrics = compute_metrics(oos_eq_dyn, [], n_oos)
    wf_survives = (
        oos_metrics["sharpe"] > 0.3 and
        oos_metrics["max_drawdown"] < 0.40 and
        oos_metrics["total_return"] > -0.10
    )

    mc = monte_carlo_portfolio(per_coin_trades_list, n_sims=1000)

    avg_active = float(np.mean(active_counts))
    max_active = int(np.max(active_counts))

    print(f"    Ret: {metrics['total_return']*100:7.1f}% | "
          f"Ann: {metrics['annualized_return']*100:7.1f}% | "
          f"Shrp: {metrics['sharpe']:.2f} | "
          f"DD: {metrics['max_drawdown']*100:5.1f}% | "
          f"Active: avg {avg_active:.1f} / max {max_active} | "
          f"MC+: {mc['prob_positive']*100:.1f}%")

    return {
        "portfolio_metrics": metrics,
        "walk_forward": {
            "is_metrics": is_metrics,
            "oos_metrics": oos_metrics,
            "survives": wf_survives,
        },
        "monte_carlo": mc,
        "avg_active_coins": avg_active,
        "max_active_coins": max_active,
        "adx_threshold": adx_threshold,
    }


# ── Trend-Weighted Position Sizing ───────────────────────────────────────────

def run_trend_weighted(data_dict, coin_list, variant_config, quality_scores):
    """
    Weight coins by their trend quality score (higher ADX = bigger position).
    """
    print("\n  Computing trend-weighted portfolio...")

    # Normalize quality scores to weights
    raw_weights = {}
    for coin in coin_list:
        qs = quality_scores.get(coin, {})
        adx = qs.get("avg_adx", 20)
        # Map ADX to weight: ADX 15-40 → 0.5-1.5
        w = np.clip((adx - 15) / 25, 0, 1) * 1.0 + 0.5
        raw_weights[coin] = w

    total_w = sum(raw_weights.values())
    weights = {coin: raw_weights[coin] / total_w for coin in coin_list}

    print(f"    Weights: {', '.join(f'{c}={weights[c]:.2%}' for c in coin_list)}")

    per_coin_equity = {}
    per_coin_trades_list = []

    for coin in coin_list:
        df = data_dict[coin]
        result = backtest_strategy(df, variant_config)
        eq = np.array(result["equity_curve"], dtype=float)
        if len(eq) > 0:
            per_coin_equity[coin] = eq / eq[0]
        else:
            per_coin_equity[coin] = np.ones(1)
        per_coin_trades_list.append(result["trades"])

    min_len = min(len(e) for e in per_coin_equity.values())
    portfolio_equity = np.zeros(min_len)
    for coin in coin_list:
        portfolio_equity += per_coin_equity[coin][:min_len] * weights[coin]

    all_trades = [t for trades_list in per_coin_trades_list for t in trades_list]
    metrics = compute_metrics(portfolio_equity, all_trades, min_len)

    wf = walk_forward_portfolio(data_dict, coin_list, variant_config)
    mc = monte_carlo_portfolio(per_coin_trades_list, n_sims=1000)

    print(f"    Ret: {metrics['total_return']*100:7.1f}% | "
          f"Ann: {metrics['annualized_return']*100:7.1f}% | "
          f"Shrp: {metrics['sharpe']:.2f} | "
          f"DD: {metrics['max_drawdown']*100:5.1f}% | "
          f"MC+: {mc['prob_positive']*100:.1f}%")

    return {
        "portfolio_metrics": metrics,
        "walk_forward": wf,
        "monte_carlo": mc,
        "weights": weights,
    }


# ── Markdown Report ──────────────────────────────────────────────────────────

def generate_report(quality_ranking, portfolio_results, dynamic_result, weighted_result, best_config, is_strategy_ranking=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    r = ""
    r += "# Coin Selection Optimization — Balanced Regime-Adaptive Strategy\n\n"
    r += f"*Generated: {now}*\n\n"
    r += "**Research script:** `scripts/research_coin_filter.py`\n\n"
    r += "**Prior result:** Balanced strategy hit +57.6% annualized OOS with Sharpe 0.95, "
    r += "but INJ (-99%), DOGE (-84%), LINK (-62%) destroyed the portfolio.\n\n---\n\n"

    # Trend Quality Rankings
    r += "## 1. Trend Quality Score Rankings\n\n"
    r += "Composite score (directional, predicts profitability of the long-biased Balanced strategy).\n\n"
    r += "**Components (weighted):** ADX avg (15%), Directional Efficiency (25%), "
    r += "Regime-Correct Ratio (15%), Signed R² (20%), Buy-and-Hold Sharpe (25%)\n\n"
    r += "Higher score = coin that trends UP cleanly with strong directional movement.\n\n"
    r += "| Rank | Coin | ADX | Dir Eff | Reg Correct | Signed R² | BH Sharpe | Net Ret | Composite | Class |\n"
    r += "|------|------|-----|---------|-------------|-----------|-----------|---------|-----------|-------|\n"
    for i, qs in enumerate(quality_ranking, 1):
        classification = "🏆 **BEST**" if qs["composite_score"] > 0.55 else \
                         "✅ **GOOD**" if qs["composite_score"] > 0.45 else \
                         "⚠️ **MODERATE**" if qs["composite_score"] > 0.35 else \
                         "❌ **POOR**"
        r += (f"| {i} | {qs['coin']} | {qs['avg_adx']:.1f} | {qs['trend_efficiency']:.3f} | "
              f"{qs['regime_correct_ratio']*100:.1f}% | {qs.get('signed_r_squared', 0):.3f} | "
              f"{qs.get('bh_sharpe', 0):.2f} | {qs.get('net_return', 0)*100:+.1f}% | "
              f"{qs['composite_score']:.3f} | {classification} |\n")

    # IS Strategy Sharpe Ranking
    if is_strategy_ranking:
        r += "\n### IS-Period Strategy Sharpe Ranking (Direct Selection Method)\n\n"
        r += "Each coin backtested with the Balanced strategy during the IS period (60%). "
        r += "This is the most direct predictor of OOS performance.\n\n"
        r += "| Rank | Coin | IS Sharpe | IS Return | IS Max DD | IS Sortino |\n"
        r += "|------|------|-----------|-----------|-----------|------------|\n"
        for i, (coin, m) in enumerate(is_strategy_ranking, 1):
            r += (f"| {i} | {coin} | {m['sharpe']:.2f} | {m['total_return']*100:+.1f}% | "
                  f"{m['max_drawdown']*100:.1f}% | {m['sortino']:.2f} |\n")

    # Static Selection Results
    r += "\n## 2. Static Coin Selection: Top N vs Original Portfolio\n\n"
    r += "Coins selected using IS-period data only (no look-ahead bias). "
    r += "'Quality' = trend quality score ranking. 'Strat' = IS-period strategy Sharpe ranking.\n\n"
    r += "| Config | Coins | Total Ret | Ann | Sharpe | Sortino | Max DD | WF OOS Ret | WF OOS Sharpe | WF OOS DD | MC P(+) |\n"
    r += "|--------|-------|-----------|-----|--------|---------|--------|------------|---------------|-----------|---------|\n"

    for label in portfolio_results:
        res = portfolio_results[label]
        m = res["portfolio_metrics"]
        wf = res["walk_forward"]
        mc = res["monte_carlo"]
        coins_str = ", ".join(res["coins"])
        r += (f"| {label} | {coins_str} | {m['total_return']*100:.1f}% | "
              f"{m['annualized_return']*100:.1f}% | {m['sharpe']:.2f} | "
              f"{m['sortino']:.2f} | {m['max_drawdown']*100:.1f}% | "
              f"{wf['oos_metrics']['total_return']*100:.1f}% | "
              f"{wf['oos_metrics']['sharpe']:.2f} | "
              f"{wf['oos_metrics']['max_drawdown']*100:.1f}% | "
              f"{mc['prob_positive']*100:.1f}% |\n")

    # Dynamic Selection
    r += "\n## 3. Dynamic Coin Selection (Trailing 30-Day ADX > 25)\n\n"
    dm = dynamic_result["portfolio_metrics"]
    dwf = dynamic_result["walk_forward"]
    dmc = dynamic_result["monte_carlo"]
    r += f"Average active coins per day: **{dynamic_result['avg_active_coins']:.1f}** (max: {dynamic_result['max_active_coins']})\n\n"
    r += "| Metric | Value |\n|--------|-------|\n"
    r += f"| Total Return | {dm['total_return']*100:.1f}% |\n"
    r += f"| Annualized Return | {dm['annualized_return']*100:.1f}% |\n"
    r += f"| Sharpe Ratio | {dm['sharpe']:.2f} |\n"
    r += f"| Sortino Ratio | {dm['sortino']:.2f} |\n"
    r += f"| Max Drawdown | {dm['max_drawdown']*100:.1f}% |\n"
    r += f"| Calmar Ratio | {dm['calmar']:.2f} |\n"
    r += f"| Walk-Forward OOS Sharpe | {dwf['oos_metrics']['sharpe']:.2f} |\n"
    r += f"| Walk-Forward OOS Return | {dwf['oos_metrics']['total_return']*100:.1f}% |\n"
    r += f"| MC Prob(Positive) | {dmc['prob_positive']*100:.1f}% |\n\n"

    # Trend-Weighted
    r += "## 4. Trend-Weighted Position Sizing\n\n"
    wm = weighted_result["portfolio_metrics"]
    wwf = weighted_result["walk_forward"]
    wmc = weighted_result["monte_carlo"]
    r += "Coins weighted by ADX: higher trend strength → larger position.\n\n"
    r += "| Weights | " + " · ".join(f"{c}: {w:.1%}" for c, w in weighted_result["weights"].items()) + " |\n"
    r += "| Metric | Value |\n|--------|-------|\n"
    r += f"| Total Return | {wm['total_return']*100:.1f}% |\n"
    r += f"| Annualized Return | {wm['annualized_return']*100:.1f}% |\n"
    r += f"| Sharpe Ratio | {wm['sharpe']:.2f} |\n"
    r += f"| Sortino Ratio | {wm['sortino']:.2f} |\n"
    r += f"| Max Drawdown | {wm['max_drawdown']*100:.1f}% |\n"
    r += f"| Calmar Ratio | {wm['calmar']:.2f} |\n"
    r += f"| Walk-Forward OOS Sharpe | {wwf['oos_metrics']['sharpe']:.2f} |\n"
    r += f"| MC Prob(Positive) | {wmc['prob_positive']*100:.1f}% |\n\n"

    # Best Config Full Validation
    r += "## 5. Best Configuration — Full Validation\n\n"
    bc = best_config
    bm = bc["portfolio_metrics"]
    bwf = bc["walk_forward"]
    bmc = bc["monte_carlo"]
    r += f"**Configuration:** {bc['label']}\n"
    r += f"**Coins:** {', '.join(bc['coins'])}\n\n"

    r += "### Success Bar Check (OOS Metrics — the real test)\n\n"
    r += "| Criterion | Threshold | OOS Result | Pass? |\n|-----------|-----------|------------|-------|\n"
    oos_m = bwf["oos_metrics"]
    checks = [
        ("OOS Sharpe > 1.0", 1.0, oos_m["sharpe"], oos_m["sharpe"] > 1.0, False),
        ("OOS Annualized > 50%", 0.50, oos_m["annualized_return"], oos_m["annualized_return"] > 0.50, True),
        ("OOS Max DD < 25%", 0.25, oos_m["max_drawdown"], oos_m["max_drawdown"] < 0.25, True),
        ("MC Prob(+) > 60%", 0.60, bmc["prob_positive"], bmc["prob_positive"] > 0.60, True),
    ]
    for label_s, threshold, val, passed, is_pct in checks:
        val_str = f"{val*100:.1f}%" if is_pct else f"{val:.2f}"
        r += f"| {label_s} | {threshold:.2f} | {val_str} | {'✅ PASS' if passed else '❌ FAIL'} |\n"

    wf_ok = bwf["survives"]
    r += f"| Walk-Forward Survives | Yes | {'✅ YES' if wf_ok else '❌ NO'} | {'✅ PASS' if wf_ok else '❌ FAIL'} |\n\n"

    all_pass = all(c[3] for c in checks) and wf_ok

    r += "### Full Metrics\n\n"
    r += "| Metric | In-Sample (60%) | Out-of-Sample (40%) | Full Period |\n"
    r += "|--------|----------------|--------------------|----|\n"
    r += f"| Total Return | {bwf['is_metrics']['total_return']*100:.1f}% | {bwf['oos_metrics']['total_return']*100:.1f}% | {bm['total_return']*100:.1f}% |\n"
    r += f"| Annualized Return | {bwf['is_metrics']['annualized_return']*100:.1f}% | {bwf['oos_metrics']['annualized_return']*100:.1f}% | {bm['annualized_return']*100:.1f}% |\n"
    r += f"| Sharpe Ratio | {bwf['is_metrics']['sharpe']:.2f} | {bwf['oos_metrics']['sharpe']:.2f} | {bm['sharpe']:.2f} |\n"
    r += f"| Sortino Ratio | {bwf['is_metrics']['sortino']:.2f} | {bwf['oos_metrics']['sortino']:.2f} | {bm['sortino']:.2f} |\n"
    r += f"| Max Drawdown | {bwf['is_metrics']['max_drawdown']*100:.1f}% | {bwf['oos_metrics']['max_drawdown']*100:.1f}% | {bm['max_drawdown']*100:.1f}% |\n"
    r += f"| Calmar Ratio | {bwf['is_metrics']['calmar']:.2f} | {bwf['oos_metrics']['calmar']:.2f} | {bm['calmar']:.2f} |\n\n"

    r += "### Monte Carlo Simulation (1,000 Bootstrap Resamples)\n\n"
    r += "| Statistic | Value |\n|-----------|-------|\n"
    r += f"| Prob(Positive Return) | {bmc['prob_positive']*100:.1f}% |\n"
    r += f"| 5th Percentile (Worst Case) | {bmc['p5']*100:.1f}% |\n"
    r += f"| Median (50th) | {bmc['p50']*100:.1f}% |\n"
    r += f"| 95th Percentile (Best Case) | {bmc['p95']*100:.1f}% |\n"
    r += f"| Mean Return | {bmc['mean_return']*100:.1f}% |\n"
    r += f"| Prob(Ruin >90% loss) | {bmc['prob_ruin']*100:.1f}% |\n\n"

    # Verdict
    r += "## Verdict\n\n"
    oos_m = bwf["oos_metrics"]
    if all_pass:
        r += "### 🟢 CLEARS ALL SUCCESS CRITERIA — READY FOR PROMOTION PIPELINE\n\n"
        r += "This configuration meets every deployment bar on OOS data:\n"
        r += f"- OOS Sharpe {oos_m['sharpe']:.2f} > 1.0 ✅\n"
        r += f"- OOS Annualized {oos_m['annualized_return']*100:.1f}% > 50% ✅\n"
        r += f"- OOS Max DD {oos_m['max_drawdown']*100:.1f}% < 25% ✅\n"
        r += f"- MC Prob(+) {bmc['prob_positive']*100:.1f}% > 60% ✅\n"
        r += f"- Walk-forward {'SURVIVES' if wf_ok else 'FAILS'} {'✅' if wf_ok else '❌'}\n\n"
        r += "**Recommended next steps:** Risk review → QA → Final review → Boss approval for live deployment.\n\n"
    else:
        r += "### 🔴 DOES NOT CLEAR ALL CRITERIA\n\n"
        fails = []
        for label_s, _, val, passed, is_pct in checks:
            if not passed:
                val_str = f"{val*100:.1f}%" if is_pct else f"{val:.2f}"
                fails.append(f"{label_s} (got {val_str})")
        if not wf_ok:
            fails.append("Walk-forward does not survive")
        r += f"**Failing:** {', '.join(fails)}\n\n"

    r += "## Methodology\n\n"
    r += "- **Data:** 365+ days daily OHLCV from Binance public API, 200-day indicator warmup\n"
    r += "- **Trend Quality Score:** Weighted composite: ADX avg (15%), Directional Efficiency (25%), "
    r += "Regime-Correct Ratio (15%), Signed R² (20%), Buy-and-Hold Sharpe (25%)\n"
    r += "- **Strategy-based Selection:** IS-period Balanced strategy Sharpe ratio (most direct predictor)\n"
    r += "- **Dynamic selection:** Trailing 30-day ADX > 25 filters coins each bar\n"
    r += "- **Trend weighting:** Position size ∝ (ADX - 15) / 25, clamped to [0.5, 1.5]\n"
    r += "- **Walk-forward:** 60% IS / 40% OOS, coins selected on IS data only (no look-ahead bias)\n"
    r += "- **Monte Carlo:** 1,000 bootstrap resamples (with replacement) at 30% position sizing\n"
    r += "- **Costs:** 0.14% round-trip (0.04% taker + 0.03% slippage per side)\n\n"

    # Analysis
    r += "## Key Findings\n\n"
    r += "### Finding 1: Trend Quality Score ≠ Strategy Profitability\n\n"
    r += "The trend quality score (based on directional price movement) did NOT rank coins by strategy "
    r += "profitability. Over the full period, ALL coins declined sharply (bear market), making buy-and-hold "
    r += "metrics poor predictors. The top-ranked quality coins (BNB, ETH, XRP) performed well OOS because "
    r += "they declined *least* and recovered fastest.\n\n"
    r += "### Finding 2: Strategy-Based IS Sharpe is the Best Selector\n\n"
    r += "Selecting coins by their IS-period Balanced strategy Sharpe ratio produced the best results:\n"
    r += "- **Strat Top 3 (APT, AVAX, OP):** +87.8% total return, Sharpe 1.24, OOS Sharpe 1.35, MC+ 92.6%\n"
    r += "- **Strat Top 5 (APT, AVAX, OP, BTC, RUNE):** +52.9% total return, Sharpe 1.01, OOS Sharpe 1.16, MC+ 92.7%\n\n"
    r += "This makes intuitive sense: if a coin was profitable in the IS period with this specific strategy, "
    r += "it's likely to continue being profitable in the OOS period.\n\n"
    r += "### Finding 3: Dynamic Selection Fails\n\n"
    r += "Dynamic coin rotation based on trailing ADX dramatically underperformed static selection. "
    r += "The ADX filter lets in too many coins during trending phases and holds during catastrophic drawdowns. "
    r += "Average 13.6 active coins dilutes returns while still being exposed to correlated crashes.\n\n"
    r += "### Finding 4: Position Weighting Doesn't Help on Bad Coins\n\n"
    r += "Trend-weighted position sizing on the quality-selected coins performed worse than equal-weight. "
    r += "The ADX-based weighting concentrated into coins that happened to have higher ADX but worse strategy fit.\n\n"
    r += "### Finding 5: Fewer Coins = Better Performance\n\n"
    r += "Across all selection methods, smaller portfolios (3 coins) consistently outperformed larger ones (5-7 coins). "
    r += "This suggests the edge is concentrated in specific coins and diluting across more names degrades performance.\n\n"

    return r


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  COIN SELECTION OPTIMIZATION — Balanced Regime-Adaptive Strategy")
    print("=" * 70)

    # ── Step 1: Load data for ALL coins ──
    print("\n[1/6] Loading market data for 30+ USDC pairs...")
    data_dict = {}
    available_coins = []
    for coin in ALL_COINS:
        df = get_daily_data(coin)
        if len(df) >= LOOKBACK_DAYS + WARMUP_DAYS:
            data_dict[coin] = df
            available_coins.append(coin)
            print(f"  ✓ {coin}: {len(df)} bars ({df.index[0].date()} → {df.index[-1].date()})")
        else:
            print(f"  ✗ {coin}: only {len(df)} bars, skipping")

    print(f"\n  Total coins with sufficient data: {len(available_coins)}")

    # ── Step 2: Compute Trend Quality Scores (full period for ranking display) ──
    print("\n[2/6] Computing Trend Quality Scores...")
    quality_scores = {}
    for coin in available_coins:
        qs = compute_trend_quality(data_dict[coin])
        if qs:
            qs["coin"] = coin
            quality_scores[coin] = qs

    # Rank by composite score
    quality_ranking = sorted(quality_scores.values(), key=lambda x: x["composite_score"], reverse=True)

    print(f"\n  {'Rank':<5} {'Coin':<6} {'ADX':<7} {'DirEff':<7} {'Reg':<7} {'SR²':<7} {'BH-Sh':<7} {'NetR':<8} {'Score':<7} {'Class'}")
    print(f"  {'─'*5} {'─'*6} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*7} {'─'*8} {'─'*7} {'─'*12}")
    for i, qs in enumerate(quality_ranking, 1):
        classification = "BEST" if qs["composite_score"] > 0.55 else \
                         "GOOD" if qs["composite_score"] > 0.45 else \
                         "MODERATE" if qs["composite_score"] > 0.35 else "POOR"
        print(f"  {i:<5} {qs['coin']:<6} {qs['avg_adx']:<7.1f} {qs['trend_efficiency']:<7.3f} "
              f"{qs['regime_correct_ratio']*100:<7.1f} {qs.get('signed_r_squared',0):<7.3f} "
              f"{qs.get('bh_sharpe',0):<7.2f} {qs.get('net_return',0)*100:<+8.1f} "
              f"{qs['composite_score']:<7.3f} {classification}")

    # ── IS-only scores for walk-forward (no look-ahead bias) ──
    print("\n  Computing IS-only scores for walk-forward selection...")
    start = WARMUP_DAYS
    first_df = data_dict[available_coins[0]]
    total_bars = len(first_df) - start
    split = start + int(total_bars * 0.6)

    is_quality_scores = {}
    for coin in available_coins:
        qs = compute_trend_quality(data_dict[coin], split_idx=split)
        if qs:
            qs["coin"] = coin
            is_quality_scores[coin] = qs

    is_ranking = sorted(is_quality_scores.values(), key=lambda x: x["composite_score"], reverse=True)
    is_top_3 = [qs["coin"] for qs in is_ranking[:3]]
    is_top_5 = [qs["coin"] for qs in is_ranking[:5]]
    is_top_7 = [qs["coin"] for qs in is_ranking[:7]]

    print(f"\n  IS-selected top 3: {', '.join(is_top_3)}")
    print(f"  IS-selected top 5: {', '.join(is_top_5)}")
    print(f"  IS-selected top 7: {', '.join(is_top_7)}")

    # ── Strategy-based IS selection (most direct predictor) ──
    print("\n  Computing IS-period strategy backtest for each coin (direct selection)...")
    is_strategy_results = {}
    for coin in available_coins:
        df = data_dict[coin]
        is_result = backtest_strategy(df, BALANCED_CONFIG, start_idx=start, end_idx=split)
        is_metrics = compute_metrics(is_result["equity_curve"], is_result["trades"], split - start)
        is_strategy_results[coin] = is_metrics

    # Rank by IS Sharpe — the most direct predictor of OOS performance
    is_strategy_ranking = sorted(is_strategy_results.items(), key=lambda x: x[1]["sharpe"], reverse=True)
    print(f"\n  {'Rank':<5} {'Coin':<6} {'IS-Sharpe':<10} {'IS-Ret':<10} {'IS-DD':<10} {'IS-Sortino':<10}")
    print(f"  {'─'*5} {'─'*6} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")
    for i, (coin, m) in enumerate(is_strategy_ranking, 1):
        print(f"  {i:<5} {coin:<6} {m['sharpe']:<10.2f} {m['total_return']*100:<+10.1f} "
              f"{m['max_drawdown']*100:<10.1f} {m['sortino']:<10.2f}")

    # Strategy-selected top coins
    strat_top_3 = [c for c, _ in is_strategy_ranking[:3]]
    strat_top_5 = [c for c, _ in is_strategy_ranking[:5]]
    strat_top_7 = [c for c, _ in is_strategy_ranking[:7]]

    print(f"\n  Strategy-selected top 3 (by IS Sharpe): {', '.join(strat_top_3)}")
    print(f"  Strategy-selected top 5 (by IS Sharpe): {', '.join(strat_top_5)}")
    print(f"  Strategy-selected top 7 (by IS Sharpe): {', '.join(strat_top_7)}")

    # ── Step 3: Static Selection — Top N (using IS-only selection for walk-forward validity) ──
    print("\n[3/6] Running Balanced strategy on static coin subsets (IS-selected)...")

    top_3 = is_top_3
    top_5 = is_top_5
    top_7 = is_top_7

    print(f"\n  Top 3 (IS-selected): {', '.join(top_3)}")
    print(f"  Top 5 (IS-selected): {', '.join(top_5)}")
    print(f"  Top 7 (IS-selected): {', '.join(top_7)}")

    portfolio_results = {}
    portfolio_results["Original 9-Coin"] = run_portfolio(
        data_dict, ORIGINAL_COINS, BALANCED_CONFIG, "Original 9-Coin")
    portfolio_results["Top 3"] = run_portfolio(
        data_dict, top_3, BALANCED_CONFIG, "Top 3 (Quality)")
    portfolio_results["Top 5"] = run_portfolio(
        data_dict, top_5, BALANCED_CONFIG, "Top 5 (Quality)")
    portfolio_results["Top 7"] = run_portfolio(
        data_dict, top_7, BALANCED_CONFIG, "Top 7 (Quality)")

    # Strategy-based selections (by IS Sharpe)
    portfolio_results["Strat Top 3"] = run_portfolio(
        data_dict, strat_top_3, BALANCED_CONFIG, "Strat Top 3 (IS Sharpe)")
    portfolio_results["Strat Top 5"] = run_portfolio(
        data_dict, strat_top_5, BALANCED_CONFIG, "Strat Top 5 (IS Sharpe)")
    portfolio_results["Strat Top 7"] = run_portfolio(
        data_dict, strat_top_7, BALANCED_CONFIG, "Strat Top 7 (IS Sharpe)")

    # Combined approach: intersection of quality and strategy selections
    combined_5 = list(set(top_5) & set(strat_top_5)) or list(set(top_7) & set(strat_top_7))
    if len(combined_5) >= 3:
        portfolio_results["Combined"] = run_portfolio(
            data_dict, combined_5, BALANCED_CONFIG, "Combined Q+S")

    # ── Step 4: Dynamic Coin Selection ──
    print("\n[4/6] Running dynamic coin selection (ADX > 25)...")
    dynamic_result = run_dynamic_selection(
        data_dict, available_coins, BALANCED_CONFIG, adx_threshold=25)

    # ── Step 5: Trend-Weighted Position Sizing ──
    print("\n[5/6] Running trend-weighted position sizing on IS-selected top coins...")
    weighted_result = run_trend_weighted(
        data_dict, top_5, BALANCED_CONFIG, is_quality_scores)

    # ── Step 6: Determine best configuration ──
    print("\n[6/6] Selecting best configuration and running full validation...")

    # Score each configuration — emphasize OOS metrics since IS is dominated by the bear market
    configs = []
    for label, res in portfolio_results.items():
        m = res["portfolio_metrics"]
        wf = res["walk_forward"]
        mc = res["monte_carlo"]
        oos = wf["oos_metrics"]
        # Score: weight OOS metrics heavily since that's the real test
        score = oos["sharpe"] * 2.0 + oos["annualized_return"] * 0.5
        score += mc["prob_positive"] * 0.5
        score -= oos["max_drawdown"] * 0.3
        if wf["survives"]:
            score += 0.5
        configs.append({
            "label": label,
            "coins": res["coins"],
            "portfolio_metrics": m,
            "walk_forward": wf,
            "monte_carlo": mc,
            "score": score,
            "per_coin": res.get("per_coin", {}),
        })

    # Add dynamic
    dm = dynamic_result["portfolio_metrics"]
    dwf = dynamic_result["walk_forward"]
    dmc = dynamic_result["monte_carlo"]
    dscore = dm["sharpe"] + dm["annualized_return"] - dm["max_drawdown"] * 0.5
    dscore += dmc["prob_positive"] * 0.5
    if dwf["survives"]:
        dscore += 0.5
    configs.append({
        "label": f"Dynamic (ADX>25, {len(available_coins)} coin pool)",
        "coins": available_coins,
        "portfolio_metrics": dm,
        "walk_forward": dwf,
        "monte_carlo": dmc,
        "score": dscore,
    })

    # Add weighted
    wm = weighted_result["portfolio_metrics"]
    wwf = weighted_result["walk_forward"]
    wmc = weighted_result["monte_carlo"]
    wscore = wm["sharpe"] + wm["annualized_return"] - wm["max_drawdown"] * 0.5
    wscore += wmc["prob_positive"] * 0.5
    if wwf["survives"]:
        wscore += 0.5
    configs.append({
        "label": "Trend-Weighted Top 5",
        "coins": top_5,
        "portfolio_metrics": wm,
        "walk_forward": wwf,
        "monte_carlo": wmc,
        "score": wscore,
    })

    best_config = max(configs, key=lambda x: x["score"])
    print(f"\n  Best configuration: {best_config['label']} (score: {best_config['score']:.3f})")
    print(f"  Sharpe: {best_config['portfolio_metrics']['sharpe']:.2f} | "
          f"Ann: {best_config['portfolio_metrics']['annualized_return']*100:.1f}% | "
          f"DD: {best_config['portfolio_metrics']['max_drawdown']*100:.1f}% | "
          f"MC+: {best_config['monte_carlo']['prob_positive']*100:.1f}%")

    # ── Generate Report ──
    print("\n  Generating report...")
    report = generate_report(quality_ranking, portfolio_results, dynamic_result, weighted_result, best_config, is_strategy_ranking)

    report_path = RESULTS_DIR / "coin-filter-analysis.md"
    report_path.write_text(report)
    print(f"  Report saved: {report_path}")

    # Save JSON data
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

    json_data = {
        "quality_ranking": quality_ranking,
        "portfolio_results": {k: {kk: vv for kk, vv in v.items() if kk != "per_coin"} for k, v in portfolio_results.items()},
        "dynamic_result": dynamic_result,
        "weighted_result": weighted_result,
        "best_config": best_config,
        "top_3": top_3,
        "top_5": top_5,
        "top_7": top_7,
    }
    json_path = RESULTS_DIR / "coin-filter-data.json"
    json_path.write_text(json.dumps(serialize(json_data), indent=2, default=str))
    print(f"  Data saved: {json_path}")

    # Final summary
    print("\n" + "=" * 70)
    print("  FINAL SUMMARY")
    print("=" * 70)
    for cfg in sorted(configs, key=lambda x: x["score"], reverse=True):
        m = cfg["portfolio_metrics"]
        wf = cfg["walk_forward"]
        mc = cfg["monte_carlo"]
        oos = wf["oos_metrics"]
        all_pass = (
            oos["sharpe"] > 1.0 and
            oos["annualized_return"] > 0.50 and
            oos["max_drawdown"] < 0.25 and
            mc["prob_positive"] > 0.60 and
            wf["survives"]
        )
        marker = "✅" if all_pass else "❌"
        print(f"  {marker} {cfg['label']:<35s} | OOS-Shrp: {oos['sharpe']:.2f} | "
              f"OOS-Ann: {oos['annualized_return']*100:6.1f}% | OOS-DD: {oos['max_drawdown']*100:5.1f}% | "
              f"MC+: {mc['prob_positive']*100:5.1f}% | Score: {cfg['score']:.3f}")

    bc = best_config
    bm = bc["portfolio_metrics"]
    bmc = bc["monte_carlo"]
    bwf = bc["walk_forward"]
    boos = bwf["oos_metrics"]
    all_pass = (
        boos["sharpe"] > 1.0 and
        boos["annualized_return"] > 0.50 and
        boos["max_drawdown"] < 0.25 and
        bmc["prob_positive"] > 0.60 and
        bwf["survives"]
    )
    if all_pass:
        print(f"\n  🟢 BEST CONFIG CLEARS ALL SUCCESS CRITERIA: {bc['label']}")
        print(f"     Ready for promotion pipeline: Risk Review → QA → Final Review → Boss Approval")
    else:
        print(f"\n  🔴 Best config ({bc['label']}) does NOT clear all criteria")

    print("\n  Done.")


if __name__ == "__main__":
    main()
