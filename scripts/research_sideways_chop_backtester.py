#!/usr/bin/env python3
"""Research-only SIDEWAYS/chop strategy backtester.

Tests conservative spot mean reversion in sideways regimes against the true
baseline: staying in USDC. This script is research-only; it never places orders
or reads private account data.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimize_momentum import BRIDGE, COINS, HOUR_MS, run_momrot  # noqa: E402
from scripts.strategy_acceptance_gates import build_research_output  # noqa: E402

BINANCE_API = "https://api.binance.com/api/v3"
DAY_MS = 86400 * 1000
DEFAULT_CURRENT_MOMENTUM_PARAMS = {
    "momentum_lookback": 18,
    "momentum_min_edge": 8.0,
    "cooldown_hours": 2,
    "anti_churn_hours": 24,
    "trailing_stop_pct": 15,
    "use_regime_filter": True,
}


@dataclass
class SidewaysConfig:
    initial_balance: float = 1000.0
    fee_rate: float = 0.00075
    slippage_pct: float = 0.0005
    lookback_hours: int = 24
    entry_z: float = 1.25
    exit_z: float = 0.10
    min_range_pct: float = 2.0
    max_range_pct: float = 12.0
    max_abs_trend_pct: float = 8.0
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 4.0
    max_hold_hours: int = 36
    max_drawdown_gate_pct: float = 15.0
    max_fee_gate_pct: float = 8.0

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def parse_klines(raw_klines: list[list[Any]]) -> list[dict[str, float | int]]:
    return [
        {
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw_klines
    ]


def fetch_klines(symbol: str, *, interval: str = "1h", days: int = 90) -> list[dict[str, float | int]]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list[Any]] = []
    cur = start_ms
    while cur < end_ms:
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        cur = int(data[-1][0]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.12)
    return parse_klines(rows)


def load_market_data(coins: list[str], *, days: int, interval: str = "1h", bridge: str = BRIDGE) -> dict[str, list[dict[str, float | int]]]:
    data: dict[str, list[dict[str, float | int]]] = {}
    for coin in coins:
        data[coin] = fetch_klines(f"{coin}{bridge}", interval=interval, days=days)
        time.sleep(0.1)
    return data


def _mean(values: list[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _stdev(values: list[float]) -> float:
    return float(statistics.pstdev(values)) if len(values) > 1 else 0.0


def _iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def build_oos_windows(
    timestamps: list[int],
    *,
    lookback_hours: int,
    test_hours: int,
    count: int,
) -> list[dict[str, Any]]:
    """Build newest-N non-overlapping OOS windows with warmup candles."""
    if lookback_hours <= 0 or test_hours <= 0 or count <= 0:
        return []
    ts = sorted({int(value) for value in timestamps})
    if not ts:
        return []
    first_ts = ts[0]
    cursor_end = ts[-1]
    built: list[dict[str, Any]] = []
    while len(built) < count:
        test_end = cursor_end
        test_start = test_end - (test_hours - 1) * HOUR_MS
        warmup_start = test_start - lookback_hours * HOUR_MS
        if warmup_start < first_ts:
            break
        built.append(
            {
                "warmup_start": warmup_start,
                "test_start": test_start,
                "test_end": test_end,
                "warmup_start_iso": _iso(warmup_start),
                "test_start_iso": _iso(test_start),
                "test_end_iso": _iso(test_end),
            }
        )
        cursor_end = test_start - HOUR_MS
    windows = list(reversed(built))
    for idx, window in enumerate(windows, start=1):
        window["label"] = f"w{idx}"
    return windows


def classify_chop_window(candles: list[dict[str, Any]], config: SidewaysConfig) -> dict[str, Any]:
    """Diagnose whether a completed window is sideways/choppy enough to trade."""
    window = candles[-config.lookback_hours :]
    if len(window) < config.lookback_hours:
        return {"eligible": False, "reason": "insufficient_window"}

    closes = [float(row["close"]) for row in window]
    highs = [float(row["high"]) for row in window]
    lows = [float(row["low"]) for row in window]
    low = min(lows)
    high = max(highs)
    mean = _mean(closes)
    stdev = _stdev(closes)
    trend_pct = ((closes[-1] / closes[0]) - 1.0) * 100.0 if closes[0] else 0.0
    range_pct = ((high / low) - 1.0) * 100.0 if low else 0.0
    zscore = (closes[-1] - mean) / stdev if stdev else 0.0

    failures: list[str] = []
    if stdev == 0:
        failures.append("flat_window")
    if range_pct < config.min_range_pct:
        failures.append("range_too_tight")
    if range_pct > config.max_range_pct:
        failures.append("range_too_wide")
    if abs(trend_pct) > config.max_abs_trend_pct:
        failures.append("trend_too_strong")

    return {
        "eligible": not failures,
        "failures": failures,
        "range_pct": range_pct,
        "trend_pct": trend_pct,
        "mean": mean,
        "stdev": stdev,
        "zscore": zscore,
        "start_ts": int(window[0]["ts"]),
        "end_ts": int(window[-1]["ts"]),
        "start": _iso(int(window[0]["ts"])),
        "end": _iso(int(window[-1]["ts"])),
    }


def _sell_position(
    *,
    balance_coin: float,
    exit_price: float,
    config: SidewaysConfig,
) -> tuple[float, float, float]:
    exit_fill = exit_price * (1.0 - config.slippage_pct)
    gross = balance_coin * exit_fill
    fee = gross * config.fee_rate
    return gross - fee, fee, exit_fill


def _update_drawdown(balance_usdc: float, balance_coin: float, mark_price: float, peak: float, max_dd: float) -> tuple[float, float]:
    equity = balance_usdc + balance_coin * mark_price
    peak = max(peak, equity)
    if peak > 0:
        max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    return peak, max_dd


def simulate_sideways_mean_reversion(symbol: str, candles: list[dict[str, Any]], config: SidewaysConfig) -> dict[str, Any]:
    """Simulate long-only mean reversion using completed signals only.

    Signal windows end at candle ``i-1`` and entries/exits happen at candle
    ``i`` open, except hard stop/take-profit checks that use candle ``i``'s
    intrabar high/low. This prevents entering on information from the same
    candle being traded.
    """
    balance_usdc = config.initial_balance
    balance_coin = 0.0
    fees = 0.0
    peak_equity = config.initial_balance
    max_dd = 0.0
    trades: list[dict[str, Any]] = []
    open_trade: dict[str, Any] | None = None

    if len(candles) <= config.lookback_hours:
        return {
            "symbol": symbol,
            "final": config.initial_balance,
            "pnl_pct": 0.0,
            "fees": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
            "trades_detail": [],
        }

    for idx in range(config.lookback_hours, len(candles)):
        exec_candle = candles[idx]
        signal_window = candles[idx - config.lookback_hours : idx]
        signal_candle = signal_window[-1]
        diag = classify_chop_window(signal_window, config)
        open_price = float(exec_candle["open"])
        close_price = float(exec_candle["close"])

        if open_trade is None:
            if diag["eligible"] and float(diag["zscore"]) <= -config.entry_z and balance_usdc > 0:
                entry_fill = open_price * (1.0 + config.slippage_pct)
                buy_fee = balance_usdc * config.fee_rate
                investable = balance_usdc - buy_fee
                balance_coin = investable / entry_fill if entry_fill else 0.0
                fees += buy_fee
                open_trade = {
                    "symbol": symbol,
                    "signal_ts": int(signal_candle["ts"]),
                    "signal_time": _iso(int(signal_candle["ts"])),
                    "entry_ts": int(exec_candle["ts"]),
                    "entry_time": _iso(int(exec_candle["ts"])),
                    "entry": entry_fill,
                    "qty": balance_coin,
                    "entry_zscore": diag["zscore"],
                    "entry_range_pct": diag["range_pct"],
                    "buy_fee": buy_fee,
                }
                balance_usdc = 0.0
        else:
            entry = float(open_trade["entry"])
            held_hours = (int(exec_candle["ts"]) - int(open_trade["entry_ts"])) / HOUR_MS
            exit_price = None
            exit_reason = None

            stop_price = entry * (1.0 - config.stop_loss_pct / 100.0)
            take_profit_price = entry * (1.0 + config.take_profit_pct / 100.0)
            if float(exec_candle["low"]) <= stop_price:
                exit_price = stop_price
                exit_reason = "stop_loss"
            elif float(exec_candle["high"]) >= take_profit_price:
                exit_price = take_profit_price
                exit_reason = "take_profit"
            elif diag["eligible"] and float(diag["zscore"]) >= -config.exit_z:
                exit_price = open_price
                exit_reason = "mean_reversion"
            elif held_hours >= config.max_hold_hours:
                exit_price = open_price
                exit_reason = "max_hold"

            if exit_price is not None:
                balance_usdc, sell_fee, exit_fill = _sell_position(
                    balance_coin=balance_coin,
                    exit_price=exit_price,
                    config=config,
                )
                fees += sell_fee
                pnl = balance_usdc - config.initial_balance if len(trades) == 0 else None
                trade = {
                    **open_trade,
                    "exit_ts": int(exec_candle["ts"]),
                    "exit_time": _iso(int(exec_candle["ts"])),
                    "exit": exit_fill,
                    "exit_reason": exit_reason,
                    "sell_fee": sell_fee,
                    "gross_return_pct": ((exit_fill / entry) - 1.0) * 100.0 if entry else 0.0,
                    "portfolio_pnl_after_trade": pnl,
                }
                trades.append(trade)
                balance_coin = 0.0
                open_trade = None

        mark = close_price if close_price else open_price
        peak_equity, max_dd = _update_drawdown(balance_usdc, balance_coin, mark, peak_equity, max_dd)

    if open_trade is not None:
        last = candles[-1]
        balance_usdc, sell_fee, exit_fill = _sell_position(
            balance_coin=balance_coin,
            exit_price=float(last["close"]),
            config=config,
        )
        fees += sell_fee
        trades.append(
            {
                **open_trade,
                "exit_ts": int(last["ts"]),
                "exit_time": _iso(int(last["ts"])),
                "exit": exit_fill,
                "exit_reason": "end_of_data",
                "sell_fee": sell_fee,
                "gross_return_pct": ((exit_fill / float(open_trade["entry"])) - 1.0) * 100.0
                if float(open_trade["entry"])
                else 0.0,
                "portfolio_pnl_after_trade": balance_usdc - config.initial_balance,
            }
        )
        balance_coin = 0.0

    final_value = balance_usdc + balance_coin * float(candles[-1]["close"])
    pnl_pct = ((final_value / config.initial_balance) - 1.0) * 100.0 if config.initial_balance else 0.0
    return {
        "symbol": symbol,
        "final": final_value,
        "pnl_pct": pnl_pct,
        "fees": fees,
        "max_drawdown_pct": max_dd,
        "trade_count": len(trades),
        "trades_detail": trades,
    }


def make_acceptance_record(symbol: str, result: dict[str, Any], config: SidewaysConfig) -> dict[str, Any]:
    pnl_pct = float(result.get("pnl_pct", 0.0))
    fees = float(result.get("fees", 0.0))
    fee_pct = fees / config.initial_balance * 100.0 if config.initial_balance else 0.0
    return {
        "name": f"sideways_mean_reversion_{symbol}",
        "strategy": "sideways_mean_reversion",
        "regime": "sideways",
        "symbol": symbol,
        "params": config.to_dict(),
        "oos_pnl": pnl_pct,
        "pnl_pct": pnl_pct,
        "baseline_pnl": 0.0,
        "baseline_pnl_pct": 0.0,
        "vs_baseline_pct": pnl_pct,
        "final": float(result.get("final", config.initial_balance)),
        "trades": int(result.get("trade_count", 0)),
        "trade_count": int(result.get("trade_count", 0)),
        "fees": fees,
        "total_fees": fees,
        "fee_pct": fee_pct,
        "max_drawdown": float(result.get("max_drawdown_pct", 0.0)),
        "max_drawdown_pct": float(result.get("max_drawdown_pct", 0.0)),
        "sharpe": 0.0,
        "initial_balance": config.initial_balance,
        "trades_detail": result.get("trades_detail", []),
    }


def make_windowed_acceptance_record(
    symbol: str,
    window_results: list[dict[str, Any]],
    config: SidewaysConfig,
) -> dict[str, Any]:
    """Aggregate per-window OOS results into one gated SIDEWAYS record."""
    pnl_values = [float(row.get("result", {}).get("pnl_pct", 0.0)) for row in window_results]
    baselines = [float(row.get("baseline_pnl", 0.0)) for row in window_results]
    vs_baseline = [pnl - baseline for pnl, baseline in zip(pnl_values, baselines)]
    drawdowns = [float(row.get("result", {}).get("max_drawdown_pct", 0.0)) for row in window_results]
    trade_counts = [int(row.get("result", {}).get("trade_count", 0)) for row in window_results]
    fees = [float(row.get("result", {}).get("fees", 0.0)) for row in window_results]
    pass_count = sum(1 for row in window_results if row.get("passed"))
    window_count = len(window_results)
    total_fees = sum(fees)
    fee_pct = total_fees / (config.initial_balance * max(1, window_count)) * 100.0 if config.initial_balance else 0.0
    median_pnl = _median(pnl_values)
    median_baseline = _median(baselines)
    median_vs_baseline = _median(vs_baseline)
    max_dd = max(drawdowns) if drawdowns else 0.0
    total_trades = sum(trade_counts)

    return {
        "name": f"sideways_mean_reversion_{symbol}",
        "strategy": "sideways_mean_reversion",
        "regime": "sideways",
        "symbol": symbol,
        "params": config.to_dict(),
        "oos_pnl": median_pnl,
        "pnl_pct": median_pnl,
        "baseline_pnl": median_baseline,
        "baseline_pnl_pct": median_baseline,
        "vs_baseline_pct": median_vs_baseline,
        "final": config.initial_balance * (1.0 + median_pnl / 100.0),
        "trades": total_trades,
        "trade_count": total_trades,
        "fees": total_fees,
        "total_fees": total_fees,
        "fee_pct": fee_pct,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd,
        "sharpe": _mean(pnl_values) / _stdev(pnl_values) if _stdev(pnl_values) else 0.0,
        "initial_balance": config.initial_balance,
        "robustness": {
            "window_count": window_count,
            "passing_windows": pass_count,
            "pass_rate_pct": pass_count / window_count * 100.0 if window_count else 0.0,
            "median_oos_pnl": median_pnl,
            "worst_oos_pnl": min(pnl_values) if pnl_values else 0.0,
            "median_baseline_pnl": median_baseline,
            "median_vs_baseline_pct": median_vs_baseline,
            "worst_vs_baseline_pct": min(vs_baseline) if vs_baseline else 0.0,
            "worst_max_drawdown_pct": max_dd,
            "total_trades": total_trades,
            "fee_pct": fee_pct,
        },
        "windows": window_results,
    }


def apply_comparison_baseline(
    records: list[dict[str, Any]],
    comparison_baselines: dict[str, float],
) -> list[dict[str, Any]]:
    """Require SIDEWAYS candidates to beat cash and current momentum behavior."""
    baseline = max(comparison_baselines.values()) if comparison_baselines else 0.0
    adjusted: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        pnl_pct = float(item.get("pnl_pct", item.get("oos_pnl", 0.0)))
        item["baseline_pnl"] = baseline
        item["baseline_pnl_pct"] = baseline
        item["vs_baseline_pct"] = pnl_pct - baseline
        item["comparison_baselines"] = dict(comparison_baselines)
        adjusted.append(item)
    return adjusted


def current_momentum_baseline_pnl(
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    config: SidewaysConfig,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> float:
    """Approximate current momentum behavior over the same dataset.

    This is only a comparator for research artifacts. If the data is too small
    or missing the SOL reference series, fall back to cash-equivalent 0%.
    """
    sol = ohlcv_by_coin.get("SOL") or []
    if len(sol) <= config.lookback_hours:
        return 0.0
    starting_coin = "TIA" if ohlcv_by_coin.get("TIA") else sorted(ohlcv_by_coin)[0]
    try:
        result = run_momrot(
            DEFAULT_CURRENT_MOMENTUM_PARAMS,
            start_ts=start_ts,
            end_ts=end_ts,
            ohlcv_by_coin=ohlcv_by_coin,
            btc_data=[],
            all_timestamps=[int(row["ts"]) for row in sol],
            initial_balance=config.initial_balance,
            starting_coin=starting_coin,
        )
    except Exception:
        return 0.0
    return float(result.get("pnl", 0.0))


def _recommendation(payload: dict[str, Any]) -> dict[str, Any]:
    passed = [row for row in payload["leaderboard"]["overall"] if row.get("passed")]
    if not passed:
        return {
            "action": "cash_standby",
            "reason": "No SIDEWAYS candidate beat cash after fees/slippage and risk gates.",
            "deployable": False,
        }
    best = passed[0]
    return {
        "action": "shadow_candidate_only",
        "strategy": best["name"],
        "reason": "At least one SIDEWAYS candidate passed research gates; observe in shadow before live routing.",
        "deployable": False,
    }


def build_sideways_research_output(
    records: list[dict[str, Any]],
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    config: SidewaysConfig,
    *,
    days: int | None = None,
    interval: str = "1h",
) -> dict[str, Any]:
    window_count = max(
        [int((record.get("robustness") or {}).get("window_count", 0)) for record in records] or [0]
    )
    gates = {
        "min_oos_pnl_pct": 0.0,
        "min_vs_baseline_pct": 0.0,
        "max_drawdown_pct": config.max_drawdown_gate_pct,
        "min_trades": 1,
        "max_fee_pct": config.max_fee_gate_pct,
        "min_sharpe": 0.0,
    }
    if window_count:
        gates.update(
            {
                "min_passing_windows": 1 if window_count == 1 else max(2, (window_count + 1) // 2),
                "min_window_pass_rate_pct": 50.0,
            }
        )
    payload = build_research_output(
        records,
        ohlcv_by_coin=ohlcv_by_coin,
        interval=interval,
        bridge=BRIDGE,
        assumptions={
            "data_source": "Binance public spot klines",
            "research_scope": "SIDEWAYS/chop mean reversion vs cash baseline; research only",
            "baseline": "cash/USDC standby at 0% P&L",
            "days": days,
            **config.to_dict(),
        },
        gates=gates,
    )
    return payload | {
        "candidates": records,
        "recommendation": _recommendation(payload),
    }


def _slice_candles(candles: list[dict[str, Any]], start_ts: int, end_ts: int) -> list[dict[str, Any]]:
    return [row for row in candles if start_ts <= int(row["ts"]) <= end_ts]


def _window_passed(result: dict[str, Any], baseline: float, config: SidewaysConfig) -> bool:
    pnl = float(result.get("pnl_pct", 0.0))
    return (
        pnl > 0.0
        and pnl - baseline >= 0.0
        and float(result.get("max_drawdown_pct", 100.0)) <= config.max_drawdown_gate_pct
        and int(result.get("trade_count", 0)) >= 1
        and (float(result.get("fees", 0.0)) / config.initial_balance * 100.0 if config.initial_balance else 0.0)
        <= config.max_fee_gate_pct
    )


def run_backtest(
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    config: SidewaysConfig,
    *,
    days: int | None = None,
    interval: str = "1h",
    windows: int = 3,
    test_days: int = 14,
) -> dict[str, Any]:
    reference = ohlcv_by_coin.get("SOL") or next(iter(ohlcv_by_coin.values()), [])
    wf_windows = build_oos_windows(
        [int(row["ts"]) for row in reference],
        lookback_hours=config.lookback_hours,
        test_hours=test_days * 24,
        count=windows,
    )

    records = []
    if wf_windows:
        window_baselines: dict[str, float] = {}
        for window in wf_windows:
            window_market = {
                coin: _slice_candles(candles, int(window["warmup_start"]), int(window["test_end"]))
                for coin, candles in ohlcv_by_coin.items()
            }
            momentum = current_momentum_baseline_pnl(
                window_market,
                config,
                start_ts=int(window["test_start"]),
                end_ts=int(window["test_end"]),
            )
            window_baselines[str(window["label"])] = max(0.0, momentum)

        for coin, candles in sorted(ohlcv_by_coin.items()):
            if len(candles) < config.lookback_hours + 1:
                continue
            per_window = []
            for window in wf_windows:
                window_candles = _slice_candles(candles, int(window["warmup_start"]), int(window["test_end"]))
                if len(window_candles) < config.lookback_hours + 1:
                    continue
                result = simulate_sideways_mean_reversion(coin, window_candles, config)
                baseline = window_baselines[str(window["label"])]
                per_window.append(
                    {
                        "window": window,
                        "result": result,
                        "baseline_pnl": baseline,
                        "comparison_baselines": {
                            "cash": 0.0,
                            "current_momentum": baseline,
                        },
                        "passed": _window_passed(result, baseline, config),
                    }
                )
            if per_window:
                records.append(make_windowed_acceptance_record(coin, per_window, config))
    else:
        for coin, candles in sorted(ohlcv_by_coin.items()):
            if len(candles) < config.lookback_hours + 1:
                continue
            result = simulate_sideways_mean_reversion(coin, candles, config)
            records.append(make_acceptance_record(coin, result, config))
        comparison_baselines = {
            "cash": 0.0,
            "current_momentum": current_momentum_baseline_pnl(ohlcv_by_coin, config),
        }
        records = apply_comparison_baseline(records, comparison_baselines)

    payload = build_sideways_research_output(records, ohlcv_by_coin, config, days=days, interval=interval)
    payload["windows"] = wf_windows
    return payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--coins", default=",".join(COINS))
    parser.add_argument("--windows", type=int, default=3, help="Number of non-overlapping OOS windows")
    parser.add_argument("--test-days", type=int, default=14, help="OOS days per window")
    parser.add_argument("--output", default="sideways_chop_backtest.json")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--entry-z", type=float, default=1.25)
    parser.add_argument("--exit-z", type=float, default=0.10)
    parser.add_argument("--min-range-pct", type=float, default=2.0)
    parser.add_argument("--max-range-pct", type=float, default=12.0)
    parser.add_argument("--max-abs-trend-pct", type=float, default=8.0)
    parser.add_argument("--stop-loss-pct", type=float, default=5.0)
    parser.add_argument("--take-profit-pct", type=float, default=4.0)
    parser.add_argument("--max-hold-hours", type=int, default=36)
    parser.add_argument("--fee-rate", type=float, default=0.00075)
    parser.add_argument("--slippage-pct", type=float, default=0.0005)
    parser.add_argument("--no-output", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    config = SidewaysConfig(
        initial_balance=args.initial_balance,
        fee_rate=args.fee_rate,
        slippage_pct=args.slippage_pct,
        lookback_hours=args.lookback_hours,
        entry_z=args.entry_z,
        exit_z=args.exit_z,
        min_range_pct=args.min_range_pct,
        max_range_pct=args.max_range_pct,
        max_abs_trend_pct=args.max_abs_trend_pct,
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        max_hold_hours=args.max_hold_hours,
    )
    print(f"Fetching {args.days}d public spot data for {len(coins)} coins...")
    ohlcv_by_coin = load_market_data(coins, days=args.days, interval=args.interval)
    payload = run_backtest(
        ohlcv_by_coin,
        config,
        days=args.days,
        interval=args.interval,
        windows=args.windows,
        test_days=args.test_days,
    )

    print("\nSIDEWAYS/chop candidates:")
    for row in payload["leaderboard"]["overall"][:10]:
        print(
            f"  {row['name']:<34} pass={row['passed']} "
            f"pnl={row['metrics']['pnl_pct']:+.2f}% "
            f"baseline_edge={row['metrics']['vs_baseline_pct']:+.2f}% "
            f"dd={row['metrics']['max_drawdown_pct']:.2f}% "
            f"trades={row['metrics']['trades']}"
        )
    rec = payload["recommendation"]
    print(f"\nRecommendation: {rec['action']} — {rec['reason']}")

    if not args.no_output:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"Saved to {args.output}")
    return payload


if __name__ == "__main__":
    main()
