#!/usr/bin/env python3
"""Research-only volatility breakout (squeeze + expansion) strategy analysis.

Uses the PUBLIC Binance SPOT API to fetch hourly klines for top 10 USDC pairs
and evaluates the classic Bollinger Band squeeze breakout pattern.

This script is intentionally research-only: it never places orders, reads
private endpoints, or modifies any live config.
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BINANCE_API = "https://api.binance.com"
KLINES_URL = f"{BINANCE_API}/api/v3/klines"
SYMBOLS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC",
    "DOGEUSDC", "ADAUSDC", "AVAXUSDC", "LINKUSDC", "DOTUSDC",
]
INTERVAL = "1h"
LIMIT = 720  # 30 days of hourly data
BB_PERIOD = 20
BB_STD_MULT = 2.0
ATR_PERIOD = 20
BB_WIDTH_LOOKBACK = 100  # rolling lookback for percentile ranking
SQUEEZE_PCTL = 20  # BB width below this percentile = squeeze
EXPANSION_PCTL = 80  # BB width above this percentile = expansion
EXPANSION_WINDOW = 24  # hours after squeeze to detect expansion
STOP_LOSS_PCT = 0.02  # 2%
TAKE_PROFIT_PCT = 0.04  # 4%
CAPITAL = 500  # simulated capital ($)
REQUEST_TIMEOUT = 15
RATE_LIMIT_PAUSE = 0.35  # seconds between requests


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class VolatilityMetrics:
    close: float
    atr: float
    atr_norm: float  # ATR / close
    bb_upper: float
    bb_lower: float
    bb_mid: float
    bb_width: float  # (upper - lower) / mid
    bb_width_pctl: float  # percentile rank over lookback


@dataclass(slots=True)
class SqueezeEvent:
    symbol: str
    squeeze_time: datetime
    squeeze_price: float
    expansion_time: datetime | None
    expansion_price: float | None
    bb_width_at_squeeze: float
    bb_width_at_expansion: float | None
    breakout_direction: str  # "up" or "down"
    atr_norm_at_squeeze: float


@dataclass(slots=True)
class TradeResult:
    symbol: str
    entry_time: datetime
    entry_price: float
    direction: str  # "long" or "short"
    exit_price: float | None
    exit_time: datetime | None
    pnl_pct: float | None  # % of capital
    pnl_dollars: float | None
    exit_reason: str | None
    hold_hours: int | None


@dataclass(slots=True)
class StrategyResult:
    symbol: str
    n_squeezes: int
    n_breakouts: int
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_return_pct: float
    total_pnl_pct: float
    total_pnl_dollars: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_hold_hours: float
    buy_and_hold_return_pct: float
    alpha_vs_bnh_pct: float


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_klines(symbol: str) -> list[Kline]:
    """Fetch hourly klines from Binance public API."""
    params = {
        "symbol": symbol,
        "interval": INTERVAL,
        "limit": LIMIT,
    }
    resp = requests.get(KLINES_URL, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    raw = resp.json()
    klines = []
    for row in raw:
        klines.append(Kline(
            open_time=row[0],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        ))
    return klines


# ---------------------------------------------------------------------------
# Indicator computations
# ---------------------------------------------------------------------------
def true_range(k: Kline, prev_close: float) -> float:
    return max(
        k.high - k.low,
        abs(k.high - prev_close),
        abs(k.low - prev_close),
    )


def compute_atr(closes: list[float], highs: list[float], lows: list[float], period: int) -> list[float | None]:
    """Compute simple moving average True Range."""
    n = len(closes)
    result = [None] * n
    if n < period + 1:
        return result
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = true_range(
            Kline(0, 0, highs[i], lows[i], closes[i], 0),
            closes[i - 1],
        )
    for i in range(period, n):
        result[i] = sum(trs[i - period + 1 : i + 1]) / period
    return result


def compute_bollinger(closes: list[float], period: int, std_mult: float):
    """Return (upper, lower, mid, width) lists."""
    n = len(closes)
    upper = [None] * n
    lower = [None] * n
    mid = [None] * n
    width = [None] * n
    if n < period:
        return upper, lower, mid, width
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        std = math.sqrt(var)
        mid[i] = m
        upper[i] = m + std_mult * std
        lower[i] = m - std_mult * std
        if m > 0:
            width[i] = (upper[i] - lower[i]) / m
    return upper, lower, mid, width


def rolling_percentile(values: list[float | None], lookback: int) -> list[float | None]:
    """Compute percentile rank of each value vs its lookback window."""
    n = len(values)
    result = [None] * n
    for i in range(n):
        if values[i] is None:
            continue
        start = max(0, i - lookback + 1)
        window = [v for v in values[start : i + 1] if v is not None]
        if len(window) < lookback // 2:
            continue
        # Percentile rank: fraction of window values <= current value
        below_or_eq = sum(1 for v in window if v <= values[i])
        result[i] = (below_or_eq / len(window)) * 100.0
    return result


# ---------------------------------------------------------------------------
# Squeeze & breakout detection
# ---------------------------------------------------------------------------
def detect_squeezes_and_breakouts(
    symbol: str,
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> list[SqueezeEvent]:
    """Detect squeeze events and subsequent expansions."""
    atrs = compute_atr(closes, highs, lows, ATR_PERIOD)
    bb_upper, bb_lower, bb_mid, bb_width = compute_bollinger(closes, BB_PERIOD, BB_STD_MULT)
    bb_width_pctl = rolling_percentile(bb_width, BB_WIDTH_LOOKBACK)

    events: list[SqueezeEvent] = []
    squeeze_candidates: list[int] = []  # indices where squeeze detected

    n = len(closes)
    for i in range(n):
        if bb_width[i] is None or bb_width_pctl[i] is None or atrs[i] is None:
            continue
        # Detect squeeze: BB width percentile drops below threshold
        if bb_width_pctl[i] < SQUEEZE_PCTL:
            squeeze_candidates.append(i)
            continue

        # Detect expansion from a prior squeeze
        if bb_width_pctl[i] > EXPANSION_PCTL and squeeze_candidates:
            sq_idx = squeeze_candidates[-1]  # most recent squeeze
            # Must be within expansion window
            if i - sq_idx <= EXPANSION_WINDOW:
                # Determine direction: did price break above upper BB or below lower BB?
                price = closes[i]
                if bb_upper[i] is not None and bb_lower[i] is not None:
                    if price > bb_upper[sq_idx] if bb_upper[sq_idx] else price > bb_mid[sq_idx]:
                        direction = "up"
                    elif price < bb_lower[sq_idx] if bb_lower[sq_idx] else price < bb_mid[sq_idx]:
                        direction = "down"
                    else:
                        direction = "up" if closes[i] > closes[sq_idx] else "down"

                    events.append(SqueezeEvent(
                        symbol=symbol,
                        squeeze_time=datetime.fromtimestamp(
                            closes_meta := (i,), tz=timezone.utc
                        )[0] if False else _open_time_to_dt(i),
                        squeeze_price=closes[sq_idx],
                        expansion_time=_open_time_to_dt(i),
                        expansion_price=closes[i],
                        bb_width_at_squeeze=bb_width[sq_idx],
                        bb_width_at_expansion=bb_width[i],
                        breakout_direction=direction,
                        atr_norm_at_squeeze=atrs[sq_idx] / closes[sq_idx] if closes[sq_idx] > 0 and atrs[sq_idx] else 0,
                    ))
                # Clear used squeeze
                squeeze_candidates.pop()

    return events


def _open_time_to_dt(idx: int) -> datetime:
    """Placeholder — real open_time comes from kline data."""
    # We'll handle this properly in the main function with actual kline data
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------
def simulate_trades(
    symbol: str,
    klines: list[Kline],
    squeezes: list[SqueezeEvent],
) -> list[TradeResult]:
    """Simulate trades from squeeze breakout events."""
    trades: list[TradeResult] = []

    # Build index from open_time to kline position for fast lookup
    time_to_idx = {k.open_time: i for i, k in enumerate(klines)}

    for sq in squeezes:
        if sq.expansion_time is None or sq.expansion_price is None:
            continue

        # Find the expansion kline index
        # We stored squeeze_time / expansion_time as open_time timestamps in the real flow
        # For now, locate by matching prices + direction logic in klines
        exp_idx = None
        for i, k in enumerate(klines):
            if abs(k.close - sq.expansion_price) < 0.001 * sq.expansion_price:
                exp_idx = i
                break

        if exp_idx is None:
            continue

        entry_price = klines[exp_idx].close
        direction = "long" if sq.breakout_direction == "up" else "short"

        # Simulate forward until TP or SL hit
        tp_price = entry_price * (1 + TAKE_PROFIT_PCT) if direction == "long" else entry_price * (1 - TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 - STOP_LOSS_PCT) if direction == "long" else entry_price * (1 + STOP_LOSS_PCT)

        exit_price = None
        exit_reason = None
        hold_hours = 0

        for j in range(exp_idx + 1, min(exp_idx + 96, len(klines))):  # max 96 hours
            k = klines[j]
            hold_hours = j - exp_idx

            if direction == "long":
                if k.high >= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                    break
                if k.low <= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                    break
            else:
                if k.low <= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                    break
                if k.high >= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                    break

        # If no exit triggered, use last close
        if exit_price is None:
            last_idx = min(exp_idx + 96, len(klines) - 1)
            exit_price = klines[last_idx].close
            exit_reason = "timeout"
            hold_hours = last_idx - exp_idx

        # Calculate P&L
        if direction == "long":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        pnl_dollars = pnl_pct / 100 * CAPITAL

        trades.append(TradeResult(
            symbol=symbol,
            entry_time=datetime.fromtimestamp(exp_idx * 3600, tz=timezone.utc),  # approximate
            entry_price=entry_price,
            direction=direction,
            exit_price=exit_price,
            exit_time=datetime.fromtimestamp((exp_idx + hold_hours) * 3600, tz=timezone.utc),
            pnl_pct=pnl_pct,
            pnl_dollars=pnl_dollars,
            exit_reason=exit_reason,
            hold_hours=hold_hours,
        ))

    return trades


def compute_strategy_stats(
    symbol: str,
    klines: list[Kline],
    squeezes: list[SqueezeEvent],
    trades: list[TradeResult],
) -> StrategyResult:
    """Compute aggregate strategy statistics."""
    n_trades = len(trades)
    wins = sum(1 for t in trades if t.pnl_pct is not None and t.pnl_pct > 0)
    losses = n_trades - wins

    # Total P&L
    total_pnl_pct = sum(t.pnl_pct for t in trades if t.pnl_pct is not None)
    total_pnl_dollars = sum(t.pnl_dollars for t in trades if t.pnl_dollars is not None)

    # Win rate
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0

    # Average return per trade
    avg_return = total_pnl_pct / n_trades if n_trades > 0 else 0

    # Max drawdown (peak-to-trough in cumulative P&L)
    cum_pnl = 0
    peak = 0
    max_dd = 0
    returns_list = []
    for t in trades:
        if t.pnl_pct is not None:
            cum_pnl += t.pnl_pct
            returns_list.append(t.pnl_pct)
            if cum_pnl > peak:
                peak = cum_pnl
            dd = peak - cum_pnl
            if dd > max_dd:
                max_dd = dd

    # Sharpe ratio (annualized, assuming ~252 trading days, 24 trades/day avg)
    if len(returns_list) > 1:
        mean_r = sum(returns_list) / len(returns_list)
        var_r = sum((r - mean_r) ** 2 for r in returns_list) / len(returns_list)
        std_r = math.sqrt(var_r) if var_r > 0 else 0.001
        # Each trade ≈ some number of hours; annualize assuming 252*24 hourly periods
        sharpe = (mean_r / std_r) * math.sqrt(252 * 24 / max(1, sum(t.hold_hours or 1 for t in trades) / len(returns_list)))
    else:
        sharpe = 0

    # Average hold hours
    avg_hold = sum(t.hold_hours or 0 for t in trades) / n_trades if n_trades > 0 else 0

    # Buy and hold return
    if len(klines) >= 2:
        bnh_return = (klines[-1].close - klines[0].close) / klines[0].close * 100
    else:
        bnh_return = 0

    alpha = total_pnl_pct - bnh_return

    return StrategyResult(
        symbol=symbol,
        n_squeezes=len(squeezes),
        n_breakouts=sum(1 for s in squeezes if s.expansion_time is not None),
        n_trades=n_trades,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 1),
        avg_return_pct=round(avg_return, 2),
        total_pnl_pct=round(total_pnl_pct, 2),
        total_pnl_dollars=round(total_pnl_dollars, 2),
        max_drawdown_pct=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        avg_hold_hours=round(avg_hold, 1),
        buy_and_hold_return_pct=round(bnh_return, 2),
        alpha_vs_bnh_pct=round(alpha, 2),
    )


# ---------------------------------------------------------------------------
# Proper squeeze detection with kline timestamps
# ---------------------------------------------------------------------------
def detect_squeezes(
    symbol: str,
    klines: list[Kline],
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> list[SqueezeEvent]:
    """Detect volatility squeezes and subsequent expansions using actual kline data."""
    atrs = compute_atr(closes, highs, lows, ATR_PERIOD)
    bb_upper, bb_lower, bb_mid, bb_width = compute_bollinger(closes, BB_PERIOD, BB_STD_MULT)
    bb_width_pctl = rolling_percentile(bb_width, BB_WIDTH_LOOKBACK)

    events: list[SqueezeEvent] = []
    squeeze_candidates: list[tuple[int, float, float, float]] = []  # (idx, bb_width, atr, close)

    n = len(closes)
    for i in range(n):
        if bb_width[i] is None or bb_width_pctl[i] is None or atrs[i] is None:
            continue
        if closes[i] == 0:
            continue

        # Detect squeeze
        if bb_width_pctl[i] < SQUEEZE_PCTL:
            squeeze_candidates.append((i, bb_width[i], atrs[i], closes[i]))
            continue

        # Detect expansion from most recent squeeze
        if bb_width_pctl[i] > EXPANSION_PCTL and squeeze_candidates:
            sq_idx, sq_bb_w, sq_atr, sq_close = squeeze_candidates[-1]
            if i - sq_idx <= EXPANSION_WINDOW:
                # Determine breakout direction by price relative to BB at squeeze point
                if bb_upper[sq_idx] is not None and bb_lower[sq_idx] is not None and bb_mid[sq_idx] is not None:
                    cur_close = closes[i]
                    if cur_close > bb_upper[sq_idx]:
                        direction = "up"
                    elif cur_close < bb_lower[sq_idx]:
                        direction = "down"
                    else:
                        # Default: direction of price movement from squeeze to now
                        direction = "up" if cur_close > sq_close else "down"
                else:
                    direction = "up" if closes[i] > sq_close else "down"

                events.append(SqueezeEvent(
                    symbol=symbol,
                    squeeze_time=datetime.fromtimestamp(
                        klines[sq_idx].open_time / 1000, tz=timezone.utc
                    ),
                    squeeze_price=sq_close,
                    expansion_time=datetime.fromtimestamp(
                        klines[i].open_time / 1000, tz=timezone.utc
                    ),
                    expansion_price=closes[i],
                    bb_width_at_squeeze=sq_bb_w,
                    bb_width_at_expansion=bb_width[i] if bb_width[i] else 0,
                    breakout_direction=direction,
                    atr_norm_at_squeeze=sq_atr / sq_close,
                ))
                squeeze_candidates.pop()

    return events


def simulate_trades_from_events(
    symbol: str,
    klines: list[Kline],
    squeezes: list[SqueezeEvent],
) -> list[TradeResult]:
    """Simulate trades from squeeze breakout events."""
    trades: list[TradeResult] = []

    for sq in squeezes:
        if sq.expansion_time is None or sq.expansion_price is None:
            continue

        # Find the expansion kline by timestamp
        exp_open_time = int(sq.expansion_time.timestamp() * 1000)
        exp_idx = None
        for i, k in enumerate(klines):
            if k.open_time == exp_open_time:
                exp_idx = i
                break

        if exp_idx is None:
            # Find closest match
            for i, k in enumerate(klines):
                if abs(k.open_time - exp_open_time) < 3600000:
                    exp_idx = i
                    break

        if exp_idx is None:
            continue

        entry_price = klines[exp_idx].close
        direction = "long" if sq.breakout_direction == "up" else "short"

        # TP and SL prices
        if direction == "long":
            tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
            sl_price = entry_price * (1 - STOP_LOSS_PCT)
        else:
            tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
            sl_price = entry_price * (1 + STOP_LOSS_PCT)

        exit_price = None
        exit_reason = None
        hold_hours = 0
        exit_idx = None

        for j in range(exp_idx + 1, min(exp_idx + 96, len(klines))):
            k = klines[j]
            hold_hours = j - exp_idx

            if direction == "long":
                if k.high >= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                    exit_idx = j
                    break
                if k.low <= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                    exit_idx = j
                    break
            else:
                if k.low <= tp_price:
                    exit_price = tp_price
                    exit_reason = "take_profit"
                    exit_idx = j
                    break
                if k.high >= sl_price:
                    exit_price = sl_price
                    exit_reason = "stop_loss"
                    exit_idx = j
                    break

        # No exit triggered → timeout, use last available close
        if exit_price is None:
            last_idx = min(exp_idx + 96, len(klines) - 1)
            exit_price = klines[last_idx].close
            exit_reason = "timeout"
            hold_hours = last_idx - exp_idx
            exit_idx = last_idx

        # P&L
        if direction == "long":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        pnl_dollars = pnl_pct / 100 * CAPITAL

        trades.append(TradeResult(
            symbol=symbol,
            entry_time=datetime.fromtimestamp(
                klines[exp_idx].open_time / 1000, tz=timezone.utc
            ),
            entry_price=entry_price,
            direction=direction,
            exit_price=exit_price,
            exit_time=datetime.fromtimestamp(
                klines[exit_idx].open_time / 1000, tz=timezone.utc
            ),
            pnl_pct=round(pnl_pct, 4),
            pnl_dollars=round(pnl_dollars, 4),
            exit_reason=exit_reason,
            hold_hours=hold_hours,
        ))

    return trades


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_markdown_report(
    all_results: list[StrategyResult],
    all_trades: list[TradeResult],
    all_squeezes: list[SqueezeEvent],
    start_time: datetime,
    end_time: datetime,
    capital: float,
) -> str:
    """Generate a markdown analysis report."""
    lines = []

    # Header
    lines.append("# Volatility Breakout Strategy Analysis")
    lines.append("")
    lines.append(f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC*")
    lines.append(f"*Data source: Public Binance SPOT API — {len(SYMBOLS)} USDC pairs, hourly klines*")
    lines.append(f"*Period: {start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')} UTC*")
    lines.append(f"*Simulated capital: ${capital}*")
    lines.append("")

    # Summary stats
    total_trades = sum(r.n_trades for r in all_results)
    total_wins = sum(r.wins for r in all_results)
    total_losses = sum(r.losses for r in all_results)
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_pnl = sum(r.total_pnl_dollars for r in all_results)
    total_pnl_pct = sum(r.total_pnl_pct for r in all_results)
    avg_max_dd = sum(r.max_drawdown_pct for r in all_results) / len(all_results) if all_results else 0
    avg_sharpe = sum(r.sharpe_ratio for r in all_results) / len(all_results) if all_results else 0
    avg_bnh = sum(r.buy_and_hold_return_pct for r in all_results) / len(all_results) if all_results else 0
    avg_alpha = sum(r.alpha_vs_bnh_pct for r in all_results) / len(all_results) if all_results else 0

    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- **Total signals (squeezes detected)**: {sum(r.n_squeezes for r in all_results)}")
    lines.append(f"- **Total breakouts**: {sum(r.n_breakouts for r in all_results)}")
    lines.append(f"- **Total trades executed**: {total_trades}")
    lines.append(f"- **Overall win rate**: {overall_wr:.1f}%")
    lines.append(f"- **Total P&L**: ${total_pnl:.2f} ({total_pnl_pct:.2f}%)")
    lines.append(f"- **Average max drawdown**: {avg_max_dd:.2f}%")
    lines.append(f"- **Average Sharpe ratio**: {avg_sharpe:.2f}")
    lines.append(f"- **Average buy-and-hold return**: {avg_bnh:.2f}%")
    lines.append(f"- **Average alpha vs buy-and-hold**: {avg_alpha:.2f}%")
    lines.append("")

    # Per-pair results
    lines.append("## Per-Pair Results")
    lines.append("")
    lines.append("| Pair | Squeezes | Breakouts | Trades | Win Rate | Avg Return | Total P&L ($) | Max DD | Sharpe | BnH Return | Alpha |")
    lines.append("|------|----------|-----------|--------|----------|------------|---------------|--------|--------|------------|-------|")
    for r in sorted(all_results, key=lambda x: x.total_pnl_pct, reverse=True):
        lines.append(
            f"| {r.symbol} | {r.n_squeezes} | {r.n_breakouts} | {r.n_trades} "
            f"| {r.win_rate}% | {r.avg_return_pct}% | ${r.total_pnl_dollars:.2f} "
            f"| {r.max_drawdown_pct}% | {r.sharpe_ratio} | {r.buy_and_hold_return_pct}% "
            f"| {r.alpha_vs_bnh_pct}% |"
        )
    lines.append("")

    # Direction analysis
    long_trades = [t for t in all_trades if t.direction == "long"]
    short_trades = [t for t in all_trades if t.direction == "short"]
    long_wins = sum(1 for t in long_trades if t.pnl_pct and t.pnl_pct > 0)
    short_wins = sum(1 for t in short_trades if t.pnl_pct and t.pnl_pct > 0)
    long_wr = (long_wins / len(long_trades) * 100) if long_trades else 0
    short_wr = (short_wins / len(short_trades) * 100) if short_trades else 0
    long_pnl = sum(t.pnl_dollars for t in long_trades if t.pnl_dollars)
    short_pnl = sum(t.pnl_dollars for t in short_trades if t.pnl_dollars)

    lines.append("## Direction Analysis")
    lines.append("")
    lines.append("| Direction | Trades | Win Rate | Total P&L ($) |")
    lines.append("|-----------|--------|----------|---------------|")
    lines.append(f"| Long | {len(long_trades)} | {long_wr:.1f}% | ${long_pnl:.2f} |")
    lines.append(f"| Short | {len(short_trades)} | {short_wr:.1f}% | ${short_pnl:.2f} |")
    lines.append("")

    # Exit reason analysis
    tp_trades = [t for t in all_trades if t.exit_reason == "take_profit"]
    sl_trades = [t for t in all_trades if t.exit_reason == "stop_loss"]
    to_trades = [t for t in all_trades if t.exit_reason == "timeout"]

    lines.append("## Exit Reason Breakdown")
    lines.append("")
    lines.append("| Exit Reason | Count | Pct of Trades | Avg P&L (%) |")
    lines.append("|-------------|-------|----------------|-------------|")
    for label, group in [("Take Profit", tp_trades), ("Stop Loss", sl_trades), ("Timeout", to_trades)]:
        cnt = len(group)
        pct = (cnt / total_trades * 100) if total_trades > 0 else 0
        avg_p = (sum(t.pnl_pct for t in group if t.pnl_pct) / cnt) if cnt > 0 else 0
        lines.append(f"| {label} | {cnt} | {pct:.1f}% | {avg_p:.2f}% |")
    lines.append("")

    # Hold time analysis
    avg_hold = sum(t.hold_hours for t in all_trades if t.hold_hours) / total_trades if total_trades > 0 else 0
    lines.append("## Trade Duration")
    lines.append("")
    lines.append(f"- **Average hold time**: {avg_hold:.1f} hours ({avg_hold / 24:.1f} days)")
    lines.append(f"- **TP hit average**: {sum(t.hold_hours for t in tp_trades if t.hold_hours) / len(tp_trades):.1f} hours" if tp_trades else "- No TP exits")
    lines.append(f"- **SL hit average**: {sum(t.hold_hours for t in sl_trades if t.hold_hours) / len(sl_trades):.1f} hours" if sl_trades else "- No SL exits")
    lines.append("")

    # Risk/Reward assessment
    lines.append("## Risk/Reward Assessment")
    lines.append("")
    if total_trades > 0:
        # Risk per trade = 2% SL
        risk_per_trade = STOP_LOSS_PCT * 100
        reward_per_trade = TAKE_PROFIT_PCT * 100
        theoretical_rr = reward_per_trade / risk_per_trade

        # Realized R:R considering win rate
        if overall_wr > 0:
            expectancy = (overall_wr / 100 * reward_per_trade) - ((100 - overall_wr) / 100 * risk_per_trade)
        else:
            expectancy = 0

        lines.append(f"- **Theoretical R:R**: 1:{theoretical_rr:.1f} (4% TP / 2% SL)")
        lines.append(f"- **Realized expectancy per trade**: {expectancy:.2f}%")
        lines.append(f"- **Profit factor**: {(sum(t.pnl_dollars for t in all_trades if t.pnl_dollars and t.pnl_dollars > 0) / abs(sum(t.pnl_dollars for t in all_trades if t.pnl_dollars and t.pnl_dollars < 0))):.2f}" if any(t.pnl_dollars and t.pnl_dollars < 0 for t in all_trades) else "N/A (no losing trades)")
    lines.append("")

    # Scale projection
    lines.append("## Scale Projections ($100–$1000)")
    lines.append("")
    for scale in [100, 250, 500, 1000]:
        scaled_pnl = total_pnl_pct / 100 * scale
        scaled_dd = avg_max_dd / 100 * scale
        lines.append(f"### ${scale} Scale")
        lines.append(f"- **Projected P&L**: ${scaled_pnl:.2f}")
        lines.append(f"- **Projected max drawdown**: ${scaled_dd:.2f}")
        lines.append("")

    # Conclusion
    lines.append("## Conclusion")
    lines.append("")
    if total_trades == 0:
        lines.append("**No trades were generated in the 30-day sample.** The strategy parameters (20-period BB, 100-period lookback, 20th/80th percentile thresholds) may be too strict for hourly data, or the market regime during this period did not produce sufficient squeeze events.")
        lines.append("")
        lines.append("### Recommendations:")
        lines.append("1. **Expand the lookback period** to 90+ days to capture more squeeze events")
        lines.append("2. **Relax thresholds** — try 25th/75th percentile instead of 20th/80th")
        lines.append("3. **Try 4h candles** instead of 1h for smoother volatility signals")
        lines.append("4. **Add volume confirmation** — require volume spike on breakout")
        lines.append("5. **Test on futures** — the 2%/4% SL/TP may not map well to spot without leverage")
    elif overall_wr > 55 and total_pnl > 0:
        lines.append(f"**The strategy shows a potential edge.** With a {overall_wr:.1f}% win rate and ${total_pnl:.2f} total P&L on ${capital} capital over 30 days:")
        lines.append(f"- Risk/reward is favorable with a 1:2 theoretical ratio")
        lines.append(f"- Alpha of {avg_alpha:.2f}% vs buy-and-hold suggests genuine outperformance")
        lines.append(f"- Max drawdown of {avg_max_dd:.2f}% is manageable at small scale")
        lines.append("")
        lines.append("### Caveats:")
        lines.append("- **Slippage not modeled** — real execution will have worse fills")
        lines.append("- **30-day sample is small** — need 90+ days for statistical significance")
        lines.append("- **Correlation between pairs** — BTC/ETH squeezes often coincide")
        lines.append("- **Regime dependent** — works best in ranging markets that transition to trending")
    else:
        lines.append(f"**No clear edge detected.** Win rate of {overall_wr:.1f}% is below the {100 * TAKE_PROFIT_PCT / (STOP_LOSS_PCT + TAKE_PROFIT_PCT):.0f}% breakeven threshold for a 2%/4% SL/TP system.")
        if total_pnl < 0:
            lines.append(f"- Total P&L of ${total_pnl:.2f} is negative")
        lines.append(f"- Average alpha of {avg_alpha:.2f}% vs buy-and-hold suggests the strategy underperforms simply holding")
        lines.append("")
        lines.append("### Why it might not work:")
        lines.append("- **BB squeeze is a lagging indicator** — by the time expansion is confirmed, the move may already be priced in")
        lines.append("- **1h timeframe too noisy** — false breakouts are common on hourly data")
        lines.append("- **Fixed SL/TP doesn't adapt** — volatility-dependent stops would perform better")
        lines.append("- **Small capital impact** — at $62–$500 scale, the strategy doesn't compound meaningfully")

    lines.append("")
    lines.append("---")
    lines.append("*Research only — no live trades were placed. Data from Binance public API.*")

    return "\n".join(lines)


def generate_json_data(
    all_results: list[StrategyResult],
    all_trades: list[TradeResult],
    all_squeezes: list[SqueezeEvent],
    meta: dict,
) -> dict:
    """Generate structured JSON data for further analysis."""
    # Convert dataclasses to dicts
    results_data = []
    for r in all_results:
        results_data.append({
            "symbol": r.symbol,
            "n_squeezes": r.n_squeezes,
            "n_breakouts": r.n_breakouts,
            "n_trades": r.n_trades,
            "wins": r.wins,
            "losses": r.losses,
            "win_rate": r.win_rate,
            "avg_return_pct": r.avg_return_pct,
            "total_pnl_pct": r.total_pnl_pct,
            "total_pnl_dollars": r.total_pnl_dollars,
            "max_drawdown_pct": r.max_drawdown_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "avg_hold_hours": r.avg_hold_hours,
            "buy_and_hold_return_pct": r.buy_and_hold_return_pct,
            "alpha_vs_bnh_pct": r.alpha_vs_bnh_pct,
        })

    trades_data = []
    for t in all_trades:
        trades_data.append({
            "symbol": t.symbol,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "entry_price": t.entry_price,
            "direction": t.direction,
            "exit_price": t.exit_price,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            "pnl_pct": t.pnl_pct,
            "pnl_dollars": t.pnl_dollars,
            "exit_reason": t.exit_reason,
            "hold_hours": t.hold_hours,
        })

    squeezes_data = []
    for s in all_squeezes:
        squeezes_data.append({
            "symbol": s.symbol,
            "squeeze_time": s.squeeze_time.isoformat() if s.squeeze_time else None,
            "squeeze_price": s.squeeze_price,
            "expansion_time": s.expansion_time.isoformat() if s.expansion_time else None,
            "expansion_price": s.expansion_price,
            "bb_width_at_squeeze": s.bb_width_at_squeeze,
            "bb_width_at_expansion": s.bb_width_at_expansion,
            "breakout_direction": s.breakout_direction,
            "atr_norm_at_squeeze": s.atr_norm_at_squeeze,
        })

    return {
        "meta": meta,
        "parameters": {
            "bb_period": BB_PERIOD,
            "bb_std_mult": BB_STD_MULT,
            "atr_period": ATR_PERIOD,
            "bb_width_lookback": BB_WIDTH_LOOKBACK,
            "squeeze_percentile": SQUEEZE_PCTL,
            "expansion_percentile": EXPANSION_PCTL,
            "expansion_window_hours": EXPANSION_WINDOW,
            "stop_loss_pct": STOP_LOSS_PCT * 100,
            "take_profit_pct": TAKE_PROFIT_PCT * 100,
            "capital": CAPITAL,
        },
        "results": results_data,
        "trades": trades_data,
        "squeezes": squeezes_data,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print("=" * 70)
    print("VOLATILITY BREAKOUT STRATEGY RESEARCH")
    print(f"Pairs: {len(SYMBOLS)} | Interval: {INTERVAL} | Limit: {LIMIT} candles")
    print("=" * 70)

    all_results: list[StrategyResult] = []
    all_trades: list[TradeResult] = []
    all_squeezes: list[SqueezeEvent] = []
    earliest_time = None
    latest_time = None

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")

        try:
            klines = fetch_klines(symbol)
        except requests.RequestException as e:
            print(f"  ERROR fetching {symbol}: {e}")
            continue

        if not klines:
            print(f"  No kline data returned for {symbol}")
            continue

        # Track time range
        sym_start = datetime.fromtimestamp(klines[0].open_time / 1000, tz=timezone.utc)
        sym_end = datetime.fromtimestamp(klines[-1].open_time / 1000, tz=timezone.utc)
        if earliest_time is None or sym_start < earliest_time:
            earliest_time = sym_start
        if latest_time is None or sym_end > latest_time:
            latest_time = sym_end

        print(f"  Fetched {len(klines)} candles: {sym_start} to {sym_end}")
        print(f"  Price range: ${klines[0].close:.2f} → ${klines[-1].close:.2f}")

        # Extract OHLCV arrays
        closes = [k.close for k in klines]
        highs = [k.high for k in klines]
        lows = [k.low for k in klines]

        # Detect squeezes
        squeezes = detect_squeezes(symbol, klines, closes, highs, lows)
        print(f"  Squeeze events: {len(squeezes)}")
        all_squeezes.extend(squeezes)

        for sq in squeezes:
            print(
                f"    Squeeze @ {sq.squeeze_time.strftime('%m-%d %H:%M')} "
                f"(BB width {sq.bb_width_at_squeeze:.4f}) → "
                f"Expansion @ {sq.expansion_time.strftime('%m-%d %H:%M') if sq.expansion_time else 'N/A'} "
                f"(direction: {sq.breakout_direction})"
            )

        # Simulate trades
        trades = simulate_trades_from_events(symbol, klines, squeezes)
        print(f"  Trades executed: {len(trades)}")
        for t in trades:
            print(
                f"    {t.direction.upper()} @ ${t.entry_price:.4f} → ${t.exit_price:.4f} "
                f"({t.exit_reason}) P&L: {t.pnl_pct:+.2f}% (${t.pnl_dollars:+.2f}) [{t.hold_hours}h]"
            )
        all_trades.extend(trades)

        # Compute stats
        stats = compute_strategy_stats(symbol, klines, squeezes, trades)
        all_results.append(stats)
        print(
            f"  WR: {stats.win_rate}% | P&L: ${stats.total_pnl_dollars:.2f} | "
            f"DD: {stats.max_drawdown_pct}% | Sharpe: {stats.sharpe_ratio}"
        )

        time.sleep(RATE_LIMIT_PAUSE)

    # --- Aggregate report ---
    print("\n" + "=" * 70)
    print("AGGREGATE RESULTS")
    print("=" * 70)

    if not all_results:
        print("No results to report. Exiting.")
        return

    total_trades = sum(r.n_trades for r in all_results)
    total_pnl = sum(r.total_pnl_dollars for r in all_results)
    total_wins = sum(r.wins for r in all_results)
    overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

    print(f"Total trades: {total_trades}")
    print(f"Total P&L: ${total_pnl:.2f}")
    print(f"Overall win rate: {overall_wr:.1f}%")

    # Generate reports
    md_report = generate_markdown_report(
        all_results, all_trades, all_squeezes,
        earliest_time or datetime.now(timezone.utc),
        latest_time or datetime.now(timezone.utc),
        CAPITAL,
    )

    json_data = generate_json_data(
        all_results, all_trades, all_squeezes,
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": "Binance Public SPOT API",
            "symbols": SYMBOLS,
            "interval": INTERVAL,
            "candles_per_symbol": LIMIT,
            "period_start": earliest_time.isoformat() if earliest_time else None,
            "period_end": latest_time.isoformat() if latest_time else None,
        },
    )

    # Save outputs
    docs_dir = REPO_ROOT / "docs" / "research"
    docs_dir.mkdir(parents=True, exist_ok=True)

    md_path = docs_dir / "volatility-breakout-analysis.md"
    md_path.write_text(md_report, encoding="utf-8")
    print(f"\n✓ Markdown report saved: {md_path}")

    json_path = docs_dir / "volatility-breakout-data.json"
    json_path.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
    print(f"✓ JSON data saved: {json_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
