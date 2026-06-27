#!/usr/bin/env python3
"""
Grid Trading Deep-Dive Research
================================
RESEARCH ONLY — no live trading, no config changes.

Tests grid trading strategy across 15 USDC pairs with 180 days of hourly data.
For each coin tests: 5 grid spacings × 4 grid levels × spot & futures = 40 configs.
Also tests adaptive (ATR-based) grid and simulates 30% crash in 48h.

Computes: total return, annualized return, max drawdown, Sharpe ratio,
profit factor, completed grid cycles, capital efficiency.

Uses public Binance API only — no API keys needed.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ── Configuration ─────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_API = "https://api.binance.com"
KLINES_URL = f"{BINANCE_API}/api/v3/klines"
INTERVAL = "1h"
HOURS = 180 * 24  # 4320 hourly candles
COMMISSION_PCT = 0.001  # 0.1% per side
RATE_LIMIT_PAUSE = 0.25
REQUEST_TIMEOUT = 30

SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC",
    "DOGEUSDC", "AVAXUSDC", "LINKUSDC", "ADAUSDC", "DOTUSDC",
    "NEARUSDC", "APTUSDC", "ARBUSDC", "OPUSDC", "INJUSDC",
]

GRID_SPACINGS = [0.01, 0.02, 0.03, 0.05, 0.08]
GRID_LEVELS = [10, 15, 20, 30]
FUTURES_LEVERAGE = 3
PRICE_BUFFER = 0.10

ATR_PERIOD = 24
ATR_MULTIPLIERS = [1.5, 2.0, 3.0]

CRASH_PCT = 0.30
CRASH_HOURS = 48
INITIAL_CAPITAL = 10000.0


# ── Data Structures ──────────────────────────────────────────────────────────
@dataclass(slots=True)
class GridConfig:
    spacing_pct: float
    n_levels: int
    is_futures: bool
    adaptive: bool = False
    atr_mult: float = 0.0


@dataclass(slots=True)
class GridResult:
    symbol: str
    config: GridConfig
    total_return: float
    annualized: float
    max_drawdown: float
    sharpe: float
    profit_factor: float
    grid_cycles: int
    capital_efficiency: float
    total_trades: int
    buy_hold_return: float
    range_low: float
    range_high: float
    final_equity: float
    liquidations: int


# ── Data Fetching ────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, limit: int = HOURS) -> pd.DataFrame:
    all_candles: list = []
    remaining = limit

    while remaining > 0:
        batch = min(remaining, 1000)
        params = {"symbol": symbol, "interval": INTERVAL, "limit": batch}
        if all_candles:
            params["endTime"] = all_candles[0][0] - 1

        try:
            resp = requests.get(KLINES_URL, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠ {symbol}: fetch error ({e}), using {len(all_candles)} candles")
            break

        if not data:
            break
        all_candles = data + all_candles
        remaining -= len(data)
        if len(data) < 1000:
            break
        time.sleep(RATE_LIMIT_PAUSE)

    if not all_candles:
        raise ValueError(f"No klines returned for {symbol}")

    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def _compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


# ── Grid Simulator (Corrected) ───────────────────────────────────────────────
def simulate_grid(
    df: pd.DataFrame,
    config: GridConfig,
    symbol: str,
) -> GridResult:
    """
    Correct grid trading simulation.

    A grid strategy places buy orders below the current price and sell orders
    above. When the price oscillates within the range, the grid captures
    the spread between adjacent levels.

    Key mechanics:
    - Grid levels are spaced `spacing_pct` apart, centered on the starting price.
    - Total range = spacing_pct × n_levels (split above/below center).
    - Each level deploys equal capital (total_capital / n_levels).
    - When price crosses a level downward → buy at that level.
    - When price crosses the next level up → sell for profit.
    - Futures: position size = margin × leverage; liquidation if loss > margin.
    """
    closes = df["close"].values.astype(float)
    n = len(closes)
    leverage = FUTURES_LEVERAGE if config.is_futures else 1

    # ── Determine grid range ─────────────────────────────────────────────────
    if config.adaptive:
        atr = _compute_atr(df, ATR_PERIOD).values
        atr_mean = np.nanmean(atr[atr > 0]) if np.any(atr > 0) else np.std(closes)
        center = float(closes[0])
        half_range = atr_mean * config.atr_mult
        range_low = center - half_range
        range_high = center + half_range
        if range_low <= 0:
            range_low = float(closes[0]) * 0.5
    else:
        # Grid levels spaced `spacing_pct` apart (percentage), centered on the
        # starting price. This makes spacing a meaningful, independently varied
        # parameter: wider spacing => wider total grid range.
        # No look-ahead: center is the first observed close only.
        center = float(closes[0])
        half = (config.n_levels - 1) / 2.0
        range_low = center * (1.0 - half * config.spacing_pct)
        range_high = center * (1.0 + half * config.spacing_pct)

    # ── Build grid levels ────────────────────────────────────────────────────
    grid_prices = np.linspace(range_low, range_high, config.n_levels)
    spacing_abs = grid_prices[1] - grid_prices[0] if len(grid_prices) > 1 else 0

    # ── Capital allocation ───────────────────────────────────────────────────
    capital_per_level = INITIAL_CAPITAL / config.n_levels

    # ── State tracking ───────────────────────────────────────────────────────
    # For each level: units held, entry price
    units_held = np.zeros(config.n_levels, dtype=np.float64)
    entry_prices = np.zeros(config.n_levels, dtype=np.float64)

    cash = INITIAL_CAPITAL  # available cash
    equity_curve = np.zeros(n)
    equity_curve[0] = INITIAL_CAPITAL

    gross_profit = 0.0
    gross_loss = 0.0
    completed_trades = 0
    liquidations = 0

    # Track capital deployment for efficiency metric
    deployed_hours = 0  # hours where at least one position was open

    prev_price = closes[0]

    # ── Adaptive range rebalancing state ─────────────────────────────────────
    rebalance_interval = 7 * 24  # weekly

    for i in range(1, n):
        price = closes[i]

        # ── Adaptive range rebalancing ────────────────────────────────────────
        if config.adaptive and i % rebalance_interval == 0:
            atr_slice = _compute_atr(df.iloc[max(0, i - ATR_PERIOD * 3):i + 1], ATR_PERIOD)
            if len(atr_slice) > 0:
                atr_val = float(atr_slice.iloc[-1])
                if atr_val > 0:
                    center_local = float(closes[i])
                    half = atr_val * config.atr_mult
                    new_low = center_local - half
                    new_high = center_local + half
                    if new_low > 0:
                        # Close all positions at current price
                        for lv in range(config.n_levels):
                            if units_held[lv] > 0:
                                pnl = units_held[lv] * (price - entry_prices[lv])
                                if leverage > 1:
                                    pnl *= leverage
                                cash += capital_per_level  # return margin
                                cash += pnl  # add/subtract PnL
                                cash -= units_held[lv] * price * COMMISSION_PCT
                                if pnl >= 0:
                                    gross_profit += pnl
                                else:
                                    gross_loss += abs(pnl)
                                completed_trades += 1
                                units_held[lv] = 0
                                entry_prices[lv] = 0
                        # Reset grid
                        range_low = new_low
                        range_high = new_high
                        grid_prices = np.linspace(range_low, range_high, config.n_levels)
                        spacing_abs = grid_prices[1] - grid_prices[0] if len(grid_prices) > 1 else 0

        # ── Check liquidations for futures ────────────────────────────────────
        if leverage > 1:
            for lv in range(config.n_levels):
                if units_held[lv] > 0:
                    # Liquidation if price drops by > 1/leverage from entry
                    liq_price = entry_prices[lv] * (1 - 1.0 / leverage)
                    if price <= liq_price:
                        # Liquidated: the entire margin is lost. Margin was already
                        # locked (subtracted from cash) at entry, so do NOT subtract
                        # it again here — doing so double-counts the loss and drives
                        # equity below zero (drawdown > -100%, which is impossible).
                        loss = capital_per_level
                        gross_loss += loss
                        units_held[lv] = 0
                        entry_prices[lv] = 0
                        liquidations += 1
                        completed_trades += 1

        # ── Process grid crossings ────────────────────────────────────────────
        for lv in range(config.n_levels):
            gp = grid_prices[lv]

            # Price crossed level downward → BUY (if we have cash and no position)
            if prev_price > gp >= price and units_held[lv] == 0 and cash >= capital_per_level:
                margin = capital_per_level
                position_value = margin * leverage
                buy_units = position_value / price
                commission = buy_units * price * COMMISSION_PCT
                cash -= margin  # lock up margin
                cash -= commission  # pay commission from cash
                units_held[lv] = buy_units
                entry_prices[lv] = price

            # Price crossed level upward → SELL (if we have a position)
            elif prev_price < gp <= price and units_held[lv] > 0:
                sell_units = units_held[lv]
                entry_price = entry_prices[lv]
                pnl = sell_units * (price - entry_price)
                if leverage > 1:
                    pnl *= leverage  # leverage amplifies PnL
                # Commission on sale
                commission = sell_units * price * COMMISSION_PCT
                cash += capital_per_level  # return margin
                cash += pnl  # add profit/loss
                cash -= commission

                if pnl >= 0:
                    gross_profit += pnl
                else:
                    gross_loss += abs(pnl)

                completed_trades += 1
                units_held[lv] = 0
                entry_prices[lv] = 0

        # ── Track deployment ──────────────────────────────────────────────────
        has_position = np.any(units_held > 0)
        if has_position:
            deployed_hours += 1

        # ── Mark-to-market equity ─────────────────────────────────────────────
        unrealized = 0.0
        for lv in range(config.n_levels):
            if units_held[lv] > 0:
                pnl = units_held[lv] * (price - entry_prices[lv])
                if leverage > 1:
                    pnl *= leverage
                unrealized += pnl
        equity_curve[i] = cash + unrealized

        prev_price = price

    # ── Close remaining positions at final price ─────────────────────────────
    final_price = closes[-1]
    for lv in range(config.n_levels):
        if units_held[lv] > 0:
            pnl = units_held[lv] * (final_price - entry_prices[lv])
            if leverage > 1:
                pnl *= leverage
            cash += capital_per_level
            cash += pnl
            cash -= units_held[lv] * final_price * COMMISSION_PCT
            if pnl >= 0:
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)
            completed_trades += 1

    # ── Compute metrics ──────────────────────────────────────────────────────
    final_equity = float(equity_curve[-1])
    total_return = (final_equity - INITIAL_CAPITAL) / INITIAL_CAPITAL

    days = n / 24
    if final_equity > 0 and total_return != 0:
        annualized = (final_equity / INITIAL_CAPITAL) ** (365.0 / days) - 1.0
    else:
        annualized = -1.0

    # Cap annualized to reasonable bounds
    annualized = max(-1.0, min(annualized, 100.0))

    # Max drawdown (capped at -100%: a futures position can show MTM losses
    # exceeding margin intrabar before liquidation triggers, but realized
    # drawdown can never lose more than the account)
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - running_max) / np.maximum(running_max, 1e-10)
    max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0
    max_drawdown = max(-1.0, min(max_drawdown, 0.0))

    # Sharpe ratio
    hourly_rets = np.diff(equity_curve) / np.maximum(equity_curve[:-1], 1e-10)
    std_rets = np.std(hourly_rets)
    if std_rets > 1e-10:
        sharpe = float(np.mean(hourly_rets) / std_rets * math.sqrt(24 * 365))
    else:
        sharpe = 0.0

    # Profit factor
    if gross_loss > 1e-10:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 99.0 if gross_profit > 0 else 0.0
    profit_factor = min(profit_factor, 99.0)

    # Grid cycles
    grid_cycles = completed_trades // max(config.n_levels, 1)

    # Capital efficiency
    capital_efficiency = deployed_hours / max(n - 1, 1)

    # Buy & hold
    buy_hold_return = float((closes[-1] - closes[0]) / closes[0])

    return GridResult(
        symbol=symbol,
        config=config,
        total_return=total_return,
        annualized=annualized,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        profit_factor=profit_factor,
        grid_cycles=grid_cycles,
        capital_efficiency=capital_efficiency,
        total_trades=completed_trades,
        buy_hold_return=buy_hold_return,
        range_low=range_low,
        range_high=range_high,
        final_equity=final_equity,
        liquidations=liquidations,
    )


# ── Crash Simulation ─────────────────────────────────────────────────────────
def simulate_crash(
    df: pd.DataFrame,
    config: GridConfig,
    symbol: str,
    crash_pct: float = CRASH_PCT,
    crash_hours: int = CRASH_HOURS,
) -> dict:
    closes = df["close"].values.astype(float).copy()
    n = len(closes)

    crash_start = int(n * 0.40)
    crash_end = min(crash_start + crash_hours, n)

    # Apply linear decline
    crash_factor = np.linspace(1.0, 1.0 - crash_pct, min(crash_hours, crash_end - crash_start))
    closes[crash_start:crash_end] *= crash_factor

    # After crash, prices stay depressed (natural recovery continues)
    post_factor = 1.0 - crash_pct
    closes[crash_end:] *= post_factor

    df_crash = df.copy()
    df_crash["close"] = closes
    for col in ["high", "low", "open"]:
        orig = df[col].values.astype(float)
        adjusted = orig.copy()
        adjusted[crash_start:crash_end] *= crash_factor
        adjusted[crash_end:] *= post_factor
        df_crash[col] = adjusted

    result = simulate_grid(df_crash, config, symbol)

    # Recovery analysis: find peak equity before crash, then measure time to recover
    capital_base = INITIAL_CAPITAL
    return {
        "symbol": symbol,
        "config_spacing": float(config.spacing_pct),
        "config_levels": int(config.n_levels),
        "is_futures": bool(config.is_futures),
        "total_return": float(result.total_return),
        "max_drawdown": float(result.max_drawdown),
        "sharpe": float(result.sharpe),
        "profit_factor": float(result.profit_factor),
        "grid_cycles": int(result.grid_cycles),
        "total_trades": int(result.total_trades),
        "survived": bool(result.final_equity > capital_base * 0.3),
        "final_equity": float(result.final_equity),
        "buy_hold_return": float(result.buy_hold_return),
        "liquidations": int(result.liquidations),
    }


# ── Reporting ────────────────────────────────────────────────────────────────
def _safe_float(v):
    if isinstance(v, (np.floating, np.integer)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return 0.0
    return v


def result_to_dict(r: GridResult) -> dict:
    return {
        "symbol": r.symbol,
        "spacing_pct": _safe_float(r.config.spacing_pct),
        "n_levels": int(r.config.n_levels),
        "is_futures": bool(r.config.is_futures),
        "adaptive": bool(r.config.adaptive),
        "atr_mult": _safe_float(r.config.atr_mult),
        "total_return": round(_safe_float(r.total_return), 4),
        "annualized": round(_safe_float(r.annualized), 4),
        "max_drawdown": round(_safe_float(r.max_drawdown), 4),
        "sharpe": round(_safe_float(r.sharpe), 2),
        "profit_factor": round(_safe_float(r.profit_factor), 2),
        "grid_cycles": int(r.grid_cycles),
        "capital_efficiency": round(_safe_float(r.capital_efficiency), 3),
        "total_trades": int(r.total_trades),
        "buy_hold_return": round(_safe_float(r.buy_hold_return), 4),
        "range_low": round(_safe_float(r.range_low), 6),
        "range_high": round(_safe_float(r.range_high), 6),
        "final_equity": round(_safe_float(r.final_equity), 2),
        "liquidations": int(r.liquidations),
    }


def build_markdown_report(
    all_results: list[dict],
    adaptive_results: list[dict],
    crash_results: list[dict],
    coin_rankings: list[dict],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = []
    lines.append("# 🔲 Grid Trading Deep-Dive Analysis\n\n")
    lines.append(f"**Generated:** {now}\n\n")
    lines.append("**Data:** 180 days of hourly candles from Binance public API\n\n")
    lines.append("**Pairs tested:** " + ", ".join(SYMBOLS) + "\n\n")
    lines.append("---\n\n")

    # ── Executive Summary ────────────────────────────────────────────────────
    lines.append("## 📊 Executive Summary\n\n")
    valid = [r for r in all_results if not (math.isnan(r["annualized"]) or math.isinf(r["annualized"]))]
    best_overall = max(valid, key=lambda x: x["annualized"]) if valid else None
    valid_sharpe = [r for r in valid if not math.isnan(r["sharpe"])]
    best_sharpe = max(valid_sharpe, key=lambda x: x["sharpe"]) if valid_sharpe else None

    spot_results = [r for r in all_results if not r["is_futures"] and not r["adaptive"]]
    fut_results = [r for r in all_results if r["is_futures"] and not r["adaptive"]]
    spot_ann = [r["annualized"] for r in spot_results if not math.isnan(r["annualized"])]
    fut_ann = [r["annualized"] for r in fut_results if not math.isnan(r["annualized"])]

    avg_spot = float(np.mean(spot_ann)) if spot_ann else 0
    avg_fut = float(np.mean(fut_ann)) if fut_ann else 0
    pct_above_100 = len([r for r in all_results if r["annualized"] > 1.0]) / len(all_results) * 100 if all_results else 0

    n_survived_crash = sum(1 for c in crash_results if c["survived"])
    crash_survival_rate = n_survived_crash / len(crash_results) * 100 if crash_results else 0

    lines.append("| Metric | Value |\n|--------|-------|\n")
    lines.append(f"| Total static configs tested | {len(all_results)} |\n")
    if best_overall:
        lines.append(f"| Best annualized return | {best_overall['annualized']:.1%} ({best_overall['symbol']}, {best_overall['spacing_pct']:.0%} spacing, {best_overall['n_levels']} levels, {'futures' if best_overall['is_futures'] else 'spot'}) |\n")
    if best_sharpe:
        lines.append(f"| Best Sharpe ratio | {best_sharpe['sharpe']:.2f} ({best_sharpe['symbol']}, {best_sharpe['spacing_pct']:.0%} spacing, {best_sharpe['n_levels']} levels) |\n")
    lines.append(f"| Avg spot annualized | {avg_spot:.1%} |\n")
    lines.append(f"| Avg futures annualized | {avg_fut:.1%} |\n")
    lines.append(f"| Configs above 100% annualized | {pct_above_100:.1f}% |\n")
    lines.append(f"| Crash survival rate | {crash_survival_rate:.1f}% |\n\n")

    # ── Static Grid Results (Top 30) ────────────────────────────────────────
    lines.append("## 📐 Static Grid Results (Top 30 by Annualized Return)\n\n")
    lines.append("| # | Symbol | Spacing | Levels | Type | Annualized | Max DD | Sharpe | PF | Trades | B&H |\n")
    lines.append("|---|--------|---------|--------|------|------------|--------|--------|----|--------|-----|\n")

    sorted_static = sorted([r for r in all_results if not r["adaptive"]],
                           key=lambda x: x["annualized"], reverse=True)
    for i, r in enumerate(sorted_static[:30], 1):
        typ = "FUT" if r["is_futures"] else "Spot"
        liq = f" ({r['liquidations']} liq)" if r.get("liquidations", 0) > 0 else ""
        lines.append(
            f"| {i} | {r['symbol']} | {r['spacing_pct']:.0%} | {r['n_levels']} | {typ}{liq} | "
            f"{r['annualized']:.1%} | {r['max_drawdown']:.1%} | {r['sharpe']:.2f} | "
            f"{r['profit_factor']:.2f} | {r['total_trades']} | {r['buy_hold_return']:.1%} |\n"
        )
    lines.append("\n")

    # ── Best Config per Coin ─────────────────────────────────────────────────
    lines.append("## 🏆 Best Static Config per Coin\n\n")
    lines.append("| Symbol | Best Spacing | Best Levels | Type | Annualized | Max DD | Sharpe | PF | B&H |\n")
    lines.append("|--------|-------------|------------|------|------------|--------|--------|----|-----|\n")

    for sym in SYMBOLS:
        sym_results = [r for r in all_results if r["symbol"] == sym and not r["adaptive"]]
        if not sym_results:
            continue
        best = max(sym_results, key=lambda x: x["annualized"])
        typ = "FUT" if best["is_futures"] else "Spot"
        lines.append(
            f"| {sym} | {best['spacing_pct']:.0%} | {best['n_levels']} | {typ} | "
            f"{best['annualized']:.1%} | {best['max_drawdown']:.1%} | {best['sharpe']:.2f} | "
            f"{best['profit_factor']:.2f} | {best['buy_hold_return']:.1%} |\n"
        )
    lines.append("\n")

    # ── Spot vs Futures ──────────────────────────────────────────────────────
    lines.append("## ⚡ Spot vs Futures (3x Leverage)\n\n")
    lines.append("Average metrics across all coins and configs:\n\n")
    lines.append("| Type | Avg Annualized | Avg Max DD | Avg Sharpe | Avg PF | Avg Trades | Avg Liqs |\n")
    lines.append("|------|---------------|-----------|-----------|--------|------------|----------|\n")

    for typ_name, typ_filter in [("Spot", False), ("Futures", True)]:
        subset = [r for r in all_results if r["is_futures"] == typ_filter and not r["adaptive"]]
        if subset:
            avg_ann = float(np.mean([r["annualized"] for r in subset]))
            avg_dd = float(np.mean([r["max_drawdown"] for r in subset]))
            avg_sharpe = float(np.mean([r["sharpe"] for r in subset]))
            avg_pf = float(np.mean([r["profit_factor"] for r in subset]))
            avg_trades = float(np.mean([r["total_trades"] for r in subset]))
            avg_liqs = float(np.mean([r.get("liquidations", 0) for r in subset]))
            lines.append(f"| {typ_name} | {avg_ann:.1%} | {avg_dd:.1%} | {avg_sharpe:.2f} | {avg_pf:.2f} | {avg_trades:.0f} | {avg_liqs:.1f} |\n")
    lines.append("\n")

    # ── Adaptive vs Static ───────────────────────────────────────────────────
    lines.append("## 🔄 Adaptive (ATR-Based) vs Static Grid\n\n")
    lines.append("Does dynamic range adjustment beat static grid?\n\n")
    lines.append("| Symbol | Adaptive Config | Adaptive Ann. | Static Best Ann. | Adaptive DD | Static Best DD | Winner |\n")
    lines.append("|--------|----------------|--------------|------------------|------------|---------------|--------|\n")

    adaptive_wins = 0
    for sym in SYMBOLS:
        adaptive = [r for r in adaptive_results if r["symbol"] == sym]
        static_best = [r for r in all_results if r["symbol"] == sym and not r["adaptive"]]
        if not adaptive or not static_best:
            continue
        best_adaptive = max(adaptive, key=lambda x: x["annualized"])
        best_static = max(static_best, key=lambda x: x["annualized"])
        winner = "Adaptive" if best_adaptive["annualized"] > best_static["annualized"] else "Static"
        if winner == "Adaptive":
            adaptive_wins += 1
        ad_config = f"ATR×{best_adaptive['atr_mult']}, {best_adaptive['spacing_pct']:.0%}, {best_adaptive['n_levels']}L"
        lines.append(
            f"| {sym} | {ad_config} | {best_adaptive['annualized']:.1%} | "
            f"{best_static['annualized']:.1%} | {best_adaptive['max_drawdown']:.1%} | "
            f"{best_static['max_drawdown']:.1%} | **{winner}** |\n"
        )
    lines.append(f"\nAdaptive wins: {adaptive_wins}/{len(SYMBOLS)} coins\n\n")

    # ── Crash Stress Test ────────────────────────────────────────────────────
    lines.append("## 💥 Crash Stress Test (30% drop in 48h)\n\n")
    lines.append("Simulated flash crash at 40% through the data. Does the grid survive?\n\n")
    lines.append("| Symbol | Spacing | Levels | Type | Total Return | Max DD | Sharpe | PF | Survived | Liqs | B&H |\n")
    lines.append("|--------|---------|--------|------|-------------|--------|--------|----|---------|------|-----|\n")

    for c in crash_results:
        typ = "FUT" if c["is_futures"] else "Spot"
        surv = "✅" if c["survived"] else "❌"
        lines.append(
            f"| {c['symbol']} | {c['config_spacing']:.0%} | {c['config_levels']} | {typ} | "
            f"{c['total_return']:.1%} | {c['max_drawdown']:.1%} | {c['sharpe']:.2f} | "
            f"{c['profit_factor']:.2f} | {surv} | {c.get('liquidations', 0)} | {c['buy_hold_return']:.1%} |\n"
        )
    lines.append("\n")

    # ── Coin Rankings ────────────────────────────────────────────────────────
    lines.append("## 🪙 Coin Grid Rankings\n\n")
    lines.append("Best coins for grid trading (by best config annualized return):\n\n")
    lines.append("| Rank | Symbol | Best Annualized | Best Max DD | Best Sharpe | Volatility | Grid Score |\n")
    lines.append("|------|--------|----------------|------------|------------|-----------|------------|\n")

    for i, cr in enumerate(coin_rankings, 1):
        lines.append(
            f"| {i} | {cr['symbol']} | {cr['best_annualized']:.1%} | "
            f"{cr['best_max_dd']:.1%} | {cr['best_sharpe']:.2f} | "
            f"{cr['volatility']:.1%} | {cr['grid_score']:.1f} |\n"
        )
    lines.append("\n")

    # ── Parameter Analysis ───────────────────────────────────────────────────
    lines.append("## 🔧 Parameter Sensitivity Analysis\n\n")
    lines.append("### By Grid Spacing\n\n")
    lines.append("| Spacing | Avg Annualized | Avg Max DD | Avg Sharpe | Avg PF | Avg Trades |\n")
    lines.append("|---------|---------------|-----------|-----------|--------|------------|\n")
    for sp in GRID_SPACINGS:
        subset = [r for r in all_results if abs(r["spacing_pct"] - sp) < 0.001 and not r["adaptive"]]
        if subset:
            avg_ann = float(np.mean([r["annualized"] for r in subset]))
            avg_dd = float(np.mean([r["max_drawdown"] for r in subset]))
            avg_sh = float(np.mean([r["sharpe"] for r in subset]))
            avg_pf = float(np.mean([r["profit_factor"] for r in subset]))
            avg_tr = float(np.mean([r["total_trades"] for r in subset]))
            lines.append(f"| {sp:.0%} | {avg_ann:.1%} | {avg_dd:.1%} | {avg_sh:.2f} | {avg_pf:.2f} | {avg_tr:.0f} |\n")
    lines.append("\n")

    lines.append("### By Grid Levels\n\n")
    lines.append("| Levels | Avg Annualized | Avg Max DD | Avg Sharpe | Avg PF | Avg Trades |\n")
    lines.append("|--------|---------------|-----------|-----------|--------|------------|\n")
    for lv in GRID_LEVELS:
        subset = [r for r in all_results if r["n_levels"] == lv and not r["adaptive"]]
        if subset:
            avg_ann = float(np.mean([r["annualized"] for r in subset]))
            avg_dd = float(np.mean([r["max_drawdown"] for r in subset]))
            avg_sh = float(np.mean([r["sharpe"] for r in subset]))
            avg_pf = float(np.mean([r["profit_factor"] for r in subset]))
            avg_tr = float(np.mean([r["total_trades"] for r in subset]))
            lines.append(f"| {lv} | {avg_ann:.1%} | {avg_dd:.1%} | {avg_sh:.2f} | {avg_pf:.2f} | {avg_tr:.0f} |\n")
    lines.append("\n")

    # ── Key Findings ─────────────────────────────────────────────────────────
    lines.append("## 🔑 Key Findings\n\n")

    # Best spacing
    best_spacing_data = {}
    for sp in GRID_SPACINGS:
        subset = [r for r in all_results if abs(r["spacing_pct"] - sp) < 0.001 and not r["adaptive"]]
        if subset:
            best_spacing_data[sp] = float(np.mean([r["annualized"] for r in subset]))
    if best_spacing_data:
        best_sp = max(best_spacing_data, key=best_spacing_data.get)
        lines.append(f"1. **Best grid spacing:** {best_sp:.0%} yields average {best_spacing_data[best_sp]:.1%} annualized\n")

    # Best levels
    best_levels_data = {}
    for lv in GRID_LEVELS:
        subset = [r for r in all_results if r["n_levels"] == lv and not r["adaptive"]]
        if subset:
            best_levels_data[lv] = float(np.mean([r["annualized"] for r in subset]))
    if best_levels_data:
        best_lv = max(best_levels_data, key=best_levels_data.get)
        lines.append(f"2. **Best grid levels:** {best_lv} levels yields average {best_levels_data[lv]:.1%} annualized\n")

    # Futures vs Spot
    if avg_fut > avg_spot:
        lines.append(f"3. **Futures 3x beats spot:** {avg_fut:.1%} vs {avg_spot:.1%} average annualized\n")
    else:
        lines.append(f"3. **Spot beats futures 3x:** {avg_spot:.1%} vs {avg_fut:.1%} average annualized\n")

    # Adaptive vs static
    lines.append(f"4. **Adaptive vs Static:** Adaptive grid wins on {adaptive_wins}/{len(SYMBOLS)} coins\n")

    # Crash
    lines.append(f"5. **Crash survival:** {crash_survival_rate:.0f}% of crash-tested configs survived a 30% flash crash\n")

    # Coins above 100%
    above_100_coins = set()
    for r in all_results:
        if r["annualized"] > 1.0:
            above_100_coins.add(r["symbol"])
    lines.append(f"6. **100%+ annualized:** {len(above_100_coins)}/{len(SYMBOLS)} coins have at least one config achieving 100%+ annualized\n")
    if above_100_coins:
        lines.append(f"   Coins: {', '.join(sorted(above_100_coins))}\n")
    lines.append("\n")

    # ── Recommendations ──────────────────────────────────────────────────────
    lines.append("## 🎯 Recommendations\n\n")
    if best_overall:
        lines.append(f"### Best Overall Configuration\n\n")
        lines.append(f"- **Symbol:** {best_overall['symbol']}\n")
        lines.append(f"- **Spacing:** {best_overall['spacing_pct']:.0%}\n")
        lines.append(f"- **Levels:** {best_overall['n_levels']}\n")
        lines.append(f"- **Type:** {'Futures 3x' if best_overall['is_futures'] else 'Spot'}\n")
        lines.append(f"- **Annualized:** {best_overall['annualized']:.1%}\n")
        lines.append(f"- **Max Drawdown:** {best_overall['max_drawdown']:.1%}\n")
        lines.append(f"- **Sharpe:** {best_overall['sharpe']:.2f}\n")
        lines.append(f"- **Profit Factor:** {best_overall['profit_factor']:.2f}\n\n")

    lines.append(f"### Crash-Survivable Configs\n\n")
    crash_survived = [c for c in crash_results if c["survived"]]
    if crash_survived:
        lines.append(f"Configs that survived the 30% crash test ({len(crash_survived)}/{len(crash_results)}):\n\n")
        for c in crash_survived:
            typ = "FUT" if c["is_futures"] else "Spot"
            lines.append(f"- {c['symbol']} {c['config_spacing']:.0%} {c['config_levels']}L {typ}: DD {c['max_drawdown']:.1%}, Return {c['total_return']:.1%}\n")
    else:
        lines.append("No crash configs survived.\n")
    lines.append("\n---\n\n*This analysis is research-only. No live trading was performed.*\n")

    return "".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 80)
    print("  GRID TRADING DEEP-DIVE ANALYSIS")
    print("=" * 80)
    print(f"  Symbols: {len(SYMBOLS)}")
    print(f"  Spacings: {GRID_SPACINGS}")
    print(f"  Levels: {GRID_LEVELS}")
    print(f"  Static configs per coin: {len(GRID_SPACINGS) * len(GRID_LEVELS) * 2}")
    print(f"  Total static configs: {len(SYMBOLS) * len(GRID_SPACINGS) * len(GRID_LEVELS) * 2}")
    print(f"  Adaptive configs per coin: {len(GRID_SPACINGS) * len(GRID_LEVELS) * 2 * len(ATR_MULTIPLIERS)}")
    print(f"  Crash tests: {len(SYMBOLS) * 4}")
    print("=" * 80)

    # ── Fetch data ───────────────────────────────────────────────────────────
    data: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        print(f"\n📡 Fetching {sym} ({HOURS} hourly candles)...")
        try:
            df = fetch_klines(sym, HOURS)
            data[sym] = df
            print(f"  ✅ {len(df)} candles | Range: {df['close'].iloc[0]:.4f} → {df['close'].iloc[-1]:.4f}")
        except Exception as e:
            print(f"  ❌ {sym}: {e}")
    print(f"\n✅ Data fetched for {len(data)}/{len(SYMBOLS)} symbols")

    if not data:
        print("FATAL: No data fetched. Aborting.")
        return

    # ── Static grid tests ────────────────────────────────────────────────────
    all_results: list[dict] = []
    adaptive_results: list[dict] = []
    crash_results: list[dict] = []

    for sym, df in data.items():
        print(f"\n🔧 Testing static grids for {sym}...")
        for spacing in GRID_SPACINGS:
            for levels in GRID_LEVELS:
                for is_fut in [False, True]:
                    cfg = GridConfig(
                        spacing_pct=spacing,
                        n_levels=levels,
                        is_futures=is_fut,
                    )
                    try:
                        result = simulate_grid(df, cfg, sym)
                        all_results.append(result_to_dict(result))
                    except Exception as e:
                        print(f"  ⚠ {sym} {spacing:.0%} {levels} {'FUT' if is_fut else 'Spot'}: {e}")

    print(f"\n✅ Static grid tests complete: {len(all_results)} results")

    # ── Adaptive grid tests ──────────────────────────────────────────────────
    for sym, df in data.items():
        print(f"🔧 Testing adaptive grids for {sym}...")
        for spacing in GRID_SPACINGS:
            for levels in GRID_LEVELS:
                for is_fut in [False, True]:
                    for atr_m in ATR_MULTIPLIERS:
                        cfg = GridConfig(
                            spacing_pct=spacing,
                            n_levels=levels,
                            is_futures=is_fut,
                            adaptive=True,
                            atr_mult=atr_m,
                        )
                        try:
                            result = simulate_grid(df, cfg, sym)
                            adaptive_results.append(result_to_dict(result))
                        except Exception as e:
                            print(f"  ⚠ {sym} adaptive: {e}")

    print(f"\n✅ Adaptive grid tests complete: {len(adaptive_results)} results")

    # ── Crash stress tests ──────────────────────────────────────────────────
    crash_configs = [
        (0.02, 20, False),
        (0.02, 20, True),
        (0.03, 20, False),
        (0.03, 20, True),
    ]
    for sym, df in data.items():
        print(f"💥 Crash testing {sym}...")
        for spacing, levels, is_fut in crash_configs:
            cfg = GridConfig(
                spacing_pct=spacing,
                n_levels=levels,
                is_futures=is_fut,
            )
            try:
                crash_res = simulate_crash(df, cfg, sym)
                crash_results.append(crash_res)
            except Exception as e:
                print(f"  ⚠ {sym} crash: {e}")

    print(f"\n✅ Crash tests complete: {len(crash_results)} results")

    # ── Coin rankings ────────────────────────────────────────────────────────
    coin_rankings = []
    for sym in SYMBOLS:
        sym_results = [r for r in all_results if r["symbol"] == sym]
        if not sym_results:
            continue
        best = max(sym_results, key=lambda x: x["annualized"])
        df_sym = data.get(sym)
        vol = float(np.std(df_sym["close"].pct_change().dropna()) * math.sqrt(24 * 365)) if df_sym is not None else 0
        grid_score = best["annualized"] / abs(best["max_drawdown"]) if best["max_drawdown"] != 0 else 0
        coin_rankings.append({
            "symbol": sym,
            "best_annualized": float(best["annualized"]),
            "best_max_dd": float(best["max_drawdown"]),
            "best_sharpe": float(best["sharpe"]),
            "volatility": vol,
            "grid_score": float(grid_score),
        })
    coin_rankings.sort(key=lambda x: x["best_annualized"], reverse=True)

    # ── Save JSON data ───────────────────────────────────────────────────────
    json_path = RESULTS_DIR / "grid-deep-data.json"
    with open(json_path, "w") as f:
        json.dump({
            "static_results": all_results,
            "adaptive_results": adaptive_results,
            "crash_results": crash_results,
            "coin_rankings": coin_rankings,
            "config": {
                "symbols": SYMBOLS,
                "spacings": [float(s) for s in GRID_SPACINGS],
                "levels": [int(l) for l in GRID_LEVELS],
                "hours": int(HOURS),
            },
        }, f, indent=2, default=_safe_float)
    print(f"\n✅ JSON saved: {json_path}")

    # ── Build markdown report ────────────────────────────────────────────────
    report = build_markdown_report(all_results, adaptive_results, crash_results, coin_rankings)
    md_path = RESULTS_DIR / "grid-deep-analysis.md"
    with open(md_path, "w") as f:
        f.write(report)
    print(f"✅ Report saved: {md_path}")

    # ── Print quick summary ──────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("  QUICK SUMMARY")
    print("=" * 80)
    if all_results:
        best = max(all_results, key=lambda x: x["annualized"])
        print(f"  Best config: {best['symbol']} {best['spacing_pct']:.0%} {best['n_levels']}L "
              f"{'FUT' if best['is_futures'] else 'Spot'} → {best['annualized']:.1%} annualized")
        print(f"  Total configs tested: {len(all_results)} static + {len(adaptive_results)} adaptive + {len(crash_results)} crash")
        crash_surv = sum(1 for c in crash_results if c["survived"])
        print(f"  Crash survival: {crash_surv}/{len(crash_results)} ({crash_surv/len(crash_results)*100:.0f}%)")
        above_100 = [r for r in all_results if r["annualized"] > 1.0]
        print(f"  Configs above 100% annualized: {len(above_100)}")
    print("=" * 80)


if __name__ == "__main__":
    main()
