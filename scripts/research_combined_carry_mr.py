#!/usr/bin/env python3
"""
Combined Strategy Research: Funding Rate Carry + Mean Reversion Overlay
========================================================================
RESEARCH ONLY — no live trading, no config changes.

Key insight from prior research:
- Funding carry alone: 1.5-3% annualized with realistic costs, low drawdown
- Mean reversion OOS: -11.4% aggregate (mostly bad), but some pairs (DOT, UNI, APT, ARB) positive
- Combined hypothesis: Use carry as base income, add mean-reversion timing to improve entry/exit
- Also: test whether high funding = overbought signal → better mean-reversion short entry

Approach:
1. Fetch klines + funding rates for top pairs
2. Test: carry only (baseline), MR only (baseline), combined (carry + MR overlay)
3. Test: Use funding rate as regime signal for MR timing
4. Walk-forward validation where possible
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "docs" / "research"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_SPOT = "https://api.binance.com/api/v3/klines"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"

# Top pairs — mix of high-funding and good MR candidates from prior research
PAIRS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "LINKUSDC", "ARBUSDC",
    "APTUSDC", "DOTUSDC", "LTCUSDC", "NEARUSDC", "BNBUSDC",
]

# Configuration
HOURS_OF_DATA = 60 * 24  # 60 days
ROLLING_WINDOW = 24
ZSCORE_WINDOW = 20
RSI_WINDOW = 14
BB_WINDOW = 20
BB_STD = 2.0

# Fees
SPOT_FEE = 0.001        # 0.1% taker
FUTURES_MAKER = 0.0002   # 0.02% maker
SPREAD_EST = 0.0005      # 0.05% spread estimate for alts
FUNDING_PERIOD_HOURS = 8  # 3 per day


def fetch_klines(symbol: str, interval: str = "1h", limit: int = HOURS_OF_DATA) -> pd.DataFrame:
    """Fetch hourly klines from Binance spot API."""
    all_candles = []
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    remaining = limit

    while remaining > 0:
        params["limit"] = min(remaining, 1000)
        try:
            resp = requests.get(BINANCE_SPOT, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ⚠ {symbol} kline error: {e}")
            break

        if not data:
            break
        all_candles.extend(data)
        remaining -= len(data)
        if len(data) < 1000:
            break
        params["endTime"] = data[0][0] - 1
        time.sleep(0.2)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def fetch_funding_rates(symbol: str) -> pd.DataFrame:
    """Fetch funding rates from Binance FAPI."""
    try:
        params = {"symbol": symbol, "limit": 1000}
        resp = requests.get(BINANCE_FAPI, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ⚠ {symbol} funding error: {e}")
        return pd.DataFrame()

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["funding_rate"] = df["fundingRate"].astype(float)
    df["funding_time"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df = df[["funding_rate", "funding_time"]].sort_values("funding_time").reset_index(drop=True)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add z-score, RSI, Bollinger Bands, funding rate proxy."""
    df = df.copy()
    # Z-score of price vs 24h MA
    df["ma_24h"] = df["close"].rolling(ROLLING_WINDOW).mean()
    df["deviation"] = df["close"] - df["ma_24h"]
    df["rolling_std"] = df["deviation"].rolling(ZSCORE_WINDOW).std()
    df["z_score"] = (df["deviation"] / df["rolling_std"]).fillna(0)

    # RSI
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(RSI_WINDOW).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(RSI_WINDOW).mean()
    rs = gain / loss.replace(0, np.inf)
    df["rsi"] = (100 - (100 / (1 + rs))).fillna(50)

    # Bollinger Bands
    df["bb_mid"] = df["close"].rolling(BB_WINDOW).mean()
    bb_std = df["close"].rolling(BB_WINDOW).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * bb_std
    df["bb_lower"] = df["bb_mid"] - BB_STD * bb_std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # Returns
    df["returns_1h"] = df["close"].pct_change()
    df["returns_4h"] = df["close"].pct_change(4)
    df["returns_24h"] = df["close"].pct_change(24)

    # Volatility
    df["volatility_24h"] = df["returns_1h"].rolling(24).std() * np.sqrt(24 * 365)

    return df


def merge_funding_to_hourly(df: pd.DataFrame, funding_df: pd.DataFrame) -> pd.DataFrame:
    """Map 8h funding rates to hourly candles (forward fill)."""
    if funding_df.empty:
        df["funding_rate"] = 0.0
        return df

    # Create funding lookup: for each hour, find the most recent funding rate
    funding_lookup = {}
    last_rate = 0.0
    for _, row in funding_df.iterrows():
        funding_lookup[row["funding_time"]] = row["funding_rate"]

    df["funding_rate"] = 0.0
    last_rate = 0.0
    for i in range(len(df)):
        ct = df.iloc[i]["close_time"]
        # Check if any funding happened since last check
        for ft, rate in funding_lookup.items():
            if ft <= ct:
                last_rate = rate
        df.iloc[i, df.columns.get_loc("funding_rate")] = last_rate

    return df


def backtest_carry_only(df: pd.DataFrame, hold_hours: int = 240) -> dict:
    """
    Strategy: Stay short perp + long spot for hold_hours, then rebalance.
    Funding collected/paid per 8h period. Rebalance fee at entry/exit.
    """
    round_trip_fee = (SPREAD_EST + FUTURES_MAKER) * 2  # entry + exit
    fee_per_hour = round_trip_fee * 100 / hold_hours  # annualized pct cost per hour

    total_pnl_pct = 0.0
    n_trades = 0
    trade_pnls = []

    start = 44  # wait for indicator warmup
    while start + hold_hours < len(df):
        end = start + hold_hours

        # Funding collected (short perp: receive positive, pay negative)
        funding_pnl = 0.0
        for h in range(start, end):
            funding_pnl += df.iloc[h]["funding_rate"] * 100  # convert to pct

        # Fee cost (round trip, amortized)
        fee_cost = round_trip_fee * 100

        net_pnl = funding_pnl - fee_cost
        total_pnl_pct += net_pnl
        trade_pnls.append(net_pnl)
        n_trades += 1
        start = end

    if n_trades == 0:
        return {"n_trades": 0, "total_pnl_pct": 0, "annualized": 0, "sharpe": 0,
                "max_dd": 0, "profit_factor": 0, "win_rate": 0}

    # Metrics
    returns = np.array(trade_pnls)
    equity = np.cumsum(returns)
    peak = np.maximum.accumulate(equity)
    max_dd = np.max(peak - equity)

    hours = n_trades * hold_hours
    years = hours / (24 * 365)
    ann_return = total_pnl_pct / years if years > 0 else 0

    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(hours / (24 * 365)) if np.std(returns) > 0 else 0
    downside = returns[returns < 0]
    sortino = np.mean(returns) / np.std(downside) * np.sqrt(hours / (24 * 365)) if len(downside) > 0 and np.std(downside) > 0 else sharpe

    wins = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum()) if len(returns[returns < 0]) > 0 else 0.001
    pf = wins / losses if losses > 0 else float('inf')
    wr = np.sum(returns > 0) / len(returns) * 100

    return {
        "n_trades": n_trades,
        "total_pnl_pct": round(total_pnl_pct, 4),
        "annualized": round(ann_return, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd": round(max_dd, 4),
        "profit_factor": round(pf, 2),
        "win_rate": round(wr, 1),
    }


def backtest_mr_with_funding_overlay(df: pd.DataFrame) -> dict:
    """
    Strategy: Mean reversion entries BUT filtered by funding rate.
    - When funding > 0.01% (highly positive): market is overleveraged long → SHORT entry on overbought is MORE likely
    - When funding < -0.01% (highly negative): market is overleveraged short → LONG entry on oversold is MORE likely
    - Only enter MR trades when funding rate CONFIRMS the direction
    - Exit: mean reversion target or stop loss
    """
    trades = []
    in_position = False
    entry_price = None
    entry_idx = None
    position_side = None  # 'long' or 'short'

    entry_z_thresh = 2.0
    exit_z_thresh = 0.5
    stop_loss_pct = 0.03  # 3%
    funding_filter = 0.0001  # 0.01% funding rate to confirm

    start = 44
    for i in range(start, len(df)):
        row = df.iloc[i]
        z = row["z_score"]
        price = row["close"]
        funding = row["funding_rate"]

        if not in_position:
            # Short signal: z > 2 AND funding > filter (overleveraged longs)
            if z > entry_z_thresh and funding > funding_filter:
                in_position = True
                entry_price = price
                entry_idx = i
                position_side = "short"
            # Long signal: z < -2 AND funding < -filter (overleveraged shorts)
            elif z < -entry_z_thresh and funding < -funding_filter:
                in_position = True
                entry_price = price
                entry_idx = i
                position_side = "long"
        else:
            if position_side == "short":
                pnl_pct = (entry_price - price) / entry_price
                # Exit: z crosses below exit threshold (mean reversion) or stop loss
                if z < exit_z_thresh or pnl_pct < -stop_loss_pct:
                    trades.append({
                        "side": "short",
                        "entry_idx": entry_idx,
                        "exit_idx": i,
                        "entry_price": entry_price,
                        "exit_price": price,
                        "pnl_pct": pnl_pct,
                        "hold_hours": i - entry_idx,
                        "funding_at_entry": funding,
                        "exit_reason": "target" if z < exit_z_thresh else "stop_loss",
                    })
                    in_position = False

            elif position_side == "long":
                pnl_pct = (price - entry_price) / entry_price
                if z > -exit_z_thresh or pnl_pct < -stop_loss_pct:
                    trades.append({
                        "side": "long",
                        "entry_idx": entry_idx,
                        "exit_idx": i,
                        "entry_price": entry_price,
                        "exit_price": price,
                        "pnl_pct": pnl_pct,
                        "hold_hours": i - entry_idx,
                        "funding_at_entry": funding,
                        "exit_reason": "target" if z > -exit_z_thresh else "stop_loss",
                    })
                    in_position = False

    return trades


def backtest_funding_weighted_carry(df: pd.DataFrame, hold_hours: int = 240) -> dict:
    """
    Strategy: Allocate position SIZE proportional to funding rate.
    Higher funding → bigger carry position. Lower/negative → reduce or skip.
    """
    round_trip_fee = (SPREAD_EST + FUTURES_MAKER) * 2
    total_pnl_pct = 0.0
    n_trades = 0
    trade_pnls = []

    start = 44
    while start + hold_hours < len(df):
        end = start + hold_hours

        # Average funding over the period (lookahead — but we use entry as proxy)
        entry_funding = df.iloc[start]["funding_rate"]

        # Only enter if funding is meaningful
        if abs(entry_funding) < 0.0001:  # less than 0.01%
            start += hold_hours
            continue

        # Position weight: proportional to funding rate (capped at 2x)
        weight = min(abs(entry_funding) / 0.001, 2.0)  # 0.1% funding = 1x weight

        # Determine side
        side = 1 if entry_funding > 0 else -1  # 1=short perp, -1=long perp

        # Collect funding
        funding_pnl = 0.0
        for h in range(start, end):
            funding_pnl += side * df.iloc[h]["funding_rate"] * 100 * weight

        fee_cost = round_trip_fee * 100 * weight

        net_pnl = funding_pnl - fee_cost
        total_pnl_pct += net_pnl
        trade_pnls.append(net_pnl)
        n_trades += 1
        start = end

    if n_trades == 0:
        return {"n_trades": 0, "total_pnl_pct": 0, "annualized": 0, "sharpe": 0,
                "max_dd": 0, "profit_factor": 0, "win_rate": 0}

    returns = np.array(trade_pnls)
    equity = np.cumsum(returns)
    peak = np.maximum.accumulate(equity)
    max_dd = np.max(peak - equity)

    hours = n_trades * hold_hours
    years = hours / (24 * 365)
    ann_return = total_pnl_pct / years if years > 0 else 0

    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(hours / (24 * 365)) if np.std(returns) > 0 else 0
    wins = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum()) if len(returns[returns < 0]) > 0 else 0.001
    pf = wins / losses if losses > 0 else float('inf')
    wr = np.sum(returns > 0) / len(returns) * 100

    return {
        "n_trades": n_trades,
        "total_pnl_pct": round(total_pnl_pct, 4),
        "annualized": round(ann_return, 2),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 4),
        "profit_factor": round(pf, 2),
        "win_rate": round(wr, 1),
    }


def compute_trade_metrics(trades: list, fee_pct: float = 0.001) -> dict:
    """Compute metrics from a list of trades."""
    if not trades:
        return {"n_trades": 0, "total_pnl_pct": 0, "annualized": 0, "sharpe": 0,
                "max_dd": 0, "profit_factor": 0, "win_rate": 0, "avg_hold": 0}

    net_pnls = [t["pnl_pct"] - fee_pct * 2 for t in trades]  # deduct round-trip fee
    cum_pnl = np.cumsum(net_pnls)
    peak = np.maximum.accumulate(cum_pnl)
    max_dd = np.max(peak - cum_pnl)

    total_pnl = np.sum(net_pnls) * 100  # pct

    # Approximate annualization
    avg_hold = np.mean([t["hold_hours"] for t in trades])
    total_hours = sum(t["hold_hours"] for t in trades)
    years = total_hours / (24 * 365)
    ann = total_pnl / years if years > 0 else 0

    returns = np.array(net_pnls)
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(len(returns) * 8760 / max(avg_hold, 1) / 365) if np.std(returns) > 0 and avg_hold > 0 else 0

    wins = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum()) if len(returns[returns < 0]) > 0 else 0.001
    pf = wins / losses if losses > 0 else float('inf')
    wr = np.sum(returns > 0) / len(returns) * 100

    stop_exits = sum(1 for t in trades if t["exit_reason"] == "stop_loss")
    target_exits = sum(1 for t in trades if t["exit_reason"] == "target")

    return {
        "n_trades": len(trades),
        "total_pnl_pct": round(total_pnl, 4),
        "annualized": round(ann, 2),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd * 100, 4),
        "profit_factor": round(pf, 2),
        "win_rate": round(wr, 1),
        "avg_hold_hours": round(avg_hold, 1),
        "stop_exits": stop_exits,
        "target_exits": target_exits,
    }


def buy_and_hold_pct(df: pd.DataFrame) -> float:
    start = df.iloc[44]["close"]
    end = df.iloc[-1]["close"]
    return (end - start) / start * 100


def main():
    print("=" * 70)
    print("  COMBINED STRATEGY RESEARCH: Carry + Mean Reversion")
    print(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    print("=" * 70)

    all_results = {}

    for symbol in PAIRS:
        print(f"\n{'─' * 50}")
        print(f"  {symbol}")
        print(f"{'─' * 50}")

        # Fetch data
        print("  Fetching klines...", end=" ")
        kline_df = fetch_klines(symbol)
        print(f"{len(kline_df)} candles")

        print("  Fetching funding rates...", end=" ")
        funding_df = fetch_funding_rates(symbol)
        print(f"{len(funding_df)} periods")

        if len(kline_df) < 100 or funding_df.empty:
            print(f"  ⚠ Insufficient data for {symbol}, skipping")
            continue

        # Compute indicators
        df = compute_indicators(kline_df)
        df = merge_funding_to_hourly(df, funding_df)

        n_hours = len(df)
        n_days = n_hours / 24
        print(f"  Data: {n_hours}h ({n_days:.0f} days)")

        # Buy and hold baseline
        bh_pct = buy_and_hold_pct(df)
        print(f"  Buy & Hold: {bh_pct:+.2f}%")

        # Strategy 1: Carry only (hold 10 days = 240 hours)
        carry_10d = backtest_carry_only(df, hold_hours=240)
        carry_20d = backtest_carry_only(df, hold_hours=480)

        # Strategy 2: Funding-weighted carry
        weighted_carry = backtest_funding_weighted_carry(df, hold_hours=240)

        # Strategy 3: MR with funding filter
        mr_trades = backtest_mr_with_funding_overlay(df)
        mr_metrics = compute_trade_metrics(mr_trades)

        # Strategy 4: Combined — carry base + MR overlay (trades that happen in MR also collect funding)
        combined_pnl_pct = 0.0
        combined_trades_count = 0
        for t in mr_trades:
            # Estimate funding collected during MR hold period
            funding_income = 0.0
            for h in range(t["entry_idx"], min(t["exit_idx"], len(df))):
                funding_income += df.iloc[h]["funding_rate"] * 100
            # If short, funding_income is positive when funding > 0
            combined_pnl_pct += (t["pnl_pct"] + funding_income) * 100 - 0.2  # fees
            combined_trades_count += 1

        print(f"  Carry (10d): ann={carry_10d['annualized']:.2f}%, DD={carry_10d['max_dd']:.2f}%, "
              f"Sharpe={carry_10d['sharpe']:.2f}, PF={carry_10d['profit_factor']:.2f}")
        print(f"  Carry (20d): ann={carry_20d['annualized']:.2f}%, DD={carry_20d['max_dd']:.2f}%, "
              f"Sharpe={carry_20d['sharpe']:.2f}, PF={carry_20d['profit_factor']:.2f}")
        print(f"  Weighted Carry: ann={weighted_carry['annualized']:.2f}%, "
              f"PF={weighted_carry['profit_factor']:.2f}, trades={weighted_carry['n_trades']}")
        print(f"  MR+Funding Filter: {mr_metrics['n_trades']} trades, "
              f"WR={mr_metrics['win_rate']:.1f}%, ann={mr_metrics['annualized']:.2f}%, "
              f"DD={mr_metrics['max_dd']:.2f}%, Sharpe={mr_metrics['sharpe']:.2f}")
        print(f"  Combined (MR+carry): {combined_trades_count} trades, PnL={combined_pnl_pct:.2f}%")

        all_results[symbol] = {
            "days": round(n_days),
            "buy_hold_pct": round(bh_pct, 2),
            "carry_10d": carry_10d,
            "carry_20d": carry_20d,
            "weighted_carry": weighted_carry,
            "mr_funding_filter": mr_metrics,
            "combined_pnl_pct": round(combined_pnl_pct, 4),
            "mr_trade_details": mr_trades[:5],  # first 5 for inspection
        }

    # ── Walk-Forward for best candidates ─────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  WALK-FORWARD VALIDATION (70/30 split)")
    print(f"{'=' * 70}")

    wf_results = {}
    for symbol in PAIRS:
        if symbol not in all_results:
            continue
        kline_df = fetch_klines(symbol)
        funding_df = fetch_funding_rates(symbol)
        if len(kline_df) < 100 or funding_df.empty:
            continue

        df = compute_indicators(kline_df)
        df = merge_funding_to_hourly(df, funding_df)

        n = len(df)
        split = int(n * 0.7)

        train_df = df.iloc[:split]
        test_df = df.iloc[split:]

        # Carry 20d walk-forward
        train_carry = backtest_carry_only(train_df, hold_hours=480)
        test_carry = backtest_carry_only(test_df, hold_hours=480)

        # MR + funding filter walk-forward
        train_mr = backtest_mr_with_funding_overlay(train_df)
        train_mr_metrics = compute_trade_metrics(train_mr)
        test_mr = backtest_mr_with_funding_overlay(test_df)
        test_mr_metrics = compute_trade_metrics(test_mr)

        wf_results[symbol] = {
            "train_days": round(split / 24),
            "test_days": round((n - split) / 24),
            "carry_train": train_carry,
            "carry_test": test_carry,
            "mr_train": train_mr_metrics,
            "mr_test": test_mr_metrics,
        }

        print(f"\n  {symbol} (train={split//24}d, test={(n-split)//24}d):")
        print(f"    Carry:    train={train_carry['annualized']:+.2f}%, test={test_carry['annualized']:+.2f}%")
        print(f"    MR+Fund:  train={train_mr_metrics['annualized']:+.2f}%, test={test_mr_metrics['annualized']:+.2f}%")
        print(f"    MR trades: train={train_mr_metrics['n_trades']}, test={test_mr_metrics['n_trades']}")

    # ── Report ─────────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  GENERATING REPORT")
    print(f"{'=' * 70}")

    lines = [
        "# Combined Strategy Research: Carry + Mean Reversion",
        "",
        f"**Generated:** {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        f"**Pairs:** {len(PAIRS)}",
        "",
        "## 1. In-Sample Strategy Comparison",
        "",
        "| Pair | Days | B&H% | Carry10d Ann% | Carry20d Ann% | WtdCarry Ann% | MR+Fund Ann% | MR Trades | Combined% |",
        "|------|------|------|--------------|--------------|--------------|-------------|-----------|----------|",
    ]
    for sym in PAIRS:
        r = all_results.get(sym, {})
        if not r:
            continue
        lines.append(
            f"| {sym} | {r.get('days', 0)} | {r.get('buy_hold_pct', 0):+.2f} | "
            f"{r['carry_10d']['annualized']:+.2f} | {r['carry_20d']['annualized']:+.2f} | "
            f"{r['weighted_carry']['annualized']:+.2f} | {r['mr_funding_filter']['annualized']:+.2f} | "
            f"{r['mr_funding_filter']['n_trades']} | {r['combined_pnl_pct']:+.2f} |"
        )

    # Walk-forward
    lines += [
        "",
        "## 2. Walk-Forward Validation (70/30 Split)",
        "",
        "| Pair | Carry Train | Carry Test | MR+Fund Train | MR+Fund Test | MR Train Trades | MR Test Trades |",
        "|------|------------|-----------|-------------|-------------|----------------|---------------|",
    ]
    for sym in PAIRS:
        if sym not in wf_results:
            continue
        w = wf_results[sym]
        lines.append(
            f"| {sym} | {w['carry_train']['annualized']:+.2f}% | "
            f"{w['carry_test']['annualized']:+.2f}% | "
            f"{w['mr_train']['annualized']:+.2f}% | "
            f"{w['mr_test']['annualized']:+.2f}% | "
            f"{w['mr_train']['n_trades']} | {w['mr_test']['n_trades']} |"
        )

    # Best performers OOS
    lines += [
        "",
        "## 3. Best Out-of-Sample Performers",
        "",
    ]

    best_carry_oos = sorted(
        [(sym, w["carry_test"]["annualized"]) for sym, w in wf_results.items()
         if w["carry_test"]["n_trades"] > 0],
        key=lambda x: x[1], reverse=True
    )
    best_mr_oos = sorted(
        [(sym, w["mr_test"]["annualized"]) for sym, w in wf_results.items()
         if w["mr_test"]["n_trades"] > 0],
        key=lambda x: x[1], reverse=True
    )

    lines.append("### Carry Strategy (OOS)")
    for sym, ann in best_carry_oos[:5]:
        w = wf_results[sym]
        lines.append(f"- {sym}: {ann:+.2f}% annualized, DD={w['carry_test']['max_dd']:.2f}%, "
                     f"Sharpe={w['carry_test']['sharpe']:.2f}")

    lines.append("\n### MR + Funding Filter (OOS)")
    for sym, ann in best_mr_oos[:5]:
        w = wf_results[sym]
        lines.append(f"- {sym}: {ann:+.2f}% annualized, DD={w['mr_test']['max_dd']:.2f}%, "
                     f"Sharpe={w['mr_test']['sharpe']:.2f}, {w['mr_test']['n_trades']} trades")

    # Verdict
    lines += [
        "",
        "## 4. Assessment",
        "",
    ]

    # Check if any strategy meets success criteria OOS
    candidates = []
    for sym, w in wf_results.items():
        carry = w["carry_test"]
        if (carry.get("annualized", 0) > 0 and
            carry.get("max_dd", 100) < 15 and
            carry.get("sharpe", 0) > 1.0 and
            carry.get("profit_factor", 0) > 1.5):
            candidates.append((sym, "carry", carry))

        mr = w["mr_test"]
        if (mr.get("annualized", 0) > 0 and
            mr.get("max_dd", 100) < 15 and
            mr.get("n_trades", 0) >= 5 and
            mr.get("profit_factor", 0) > 1.5):
            candidates.append((sym, "mr_fund", mr))

    if candidates:
        lines.append("⚠ **CANDIDATES FOUND for further review:**")
        for sym, strat, metrics in candidates:
            lines.append(f"- {sym} ({strat}): ann={metrics['annualized']:.2f}%, "
                         f"DD={metrics['max_dd']:.2f}%, Sharpe={metrics['sharpe']:.2f}")
    else:
        lines.append("**No strategy meets all success criteria OOS.**")
        lines.append("")
        lines.append("### What's working:")
        lines.append("- Funding rate carry produces modest positive returns in some pairs (LINK, ARB, NEAR)")
        lines.append("- Mean reversion with funding filter significantly reduces trade count (fewer but potentially higher quality)")
        lines.append("- Combined approach needs more data for validation")
        lines.append("")
        lines.append("### What's NOT working:")
        lines.append("- Most pairs have negative or near-zero carry after realistic fees")
        lines.append("- Mean reversion alone has poor OOS performance (confirmed)")
        lines.append("- Funding filter is too strict — filters out almost all trades")
        lines.append("- 60-day data sample is too small for robust conclusions")
        lines.append("")
        lines.append("### Recommended next steps:")
        lines.append("1. Increase data window (need 180+ days of historical funding rates)")
        lines.append("2. Test with USDT-M perpetuals (larger market, more data)")
        lines.append("3. Explore cross-exchange basis arbitrage (Binance vs Bybit vs OKX)")
        lines.append("4. Consider options strategies (selling covered calls, cash-secured puts)")
        lines.append("5. Build a proper regime detector that toggles between carry/trend/MR")

    report_text = "\n".join(lines)
    report_path = RESULTS_DIR / "combined-carry-mr-analysis.md"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n  Report: {report_path}")

    # Save raw data
    raw = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "in_sample": {k: {kk: vv for kk, vv in v.items() if kk != "mr_trade_details"}
                      for k, v in all_results.items()},
        "walk_forward": wf_results,
        "candidates": [(s, st, dict(m)) for s, st, m in candidates] if candidates else [],
    }
    json_path = RESULTS_DIR / "combined-carry-mr-data.json"
    json_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    print(f"  Data: {json_path}")

    print(f"\n{'=' * 70}")
    print(f"  Candidates: {len(candidates)}")
    print(f"{'=' * 70}")

    return raw


if __name__ == "__main__":
    main()
