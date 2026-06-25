#!/usr/bin/env python3
"""Research-only BULL momentum robustness optimizer.

This script does not change live trading. It reuses the import-safe focused
momentum backtester, then evaluates candidate parameter sets across multiple
walk-forward OOS windows so one lucky bull slice cannot dominate the result.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from optimize_momentum import (  # noqa: E402
    BRIDGE,
    COINS,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_STARTING_COIN,
    HOUR_MS,
    build_param_grid,
    build_price_index,
    buy_hold_pnl,
    load_market_data,
    run_momrot,
)
from scripts.strategy_acceptance_gates import build_research_output  # noqa: E402

DEFAULT_BENCHMARKS = ("cash", "BTC", "SOL", DEFAULT_STARTING_COIN)


def candidate_param_grid(*, allow_no_regime_filter: bool = False) -> list[dict[str, Any]]:
    """Return momentum params, preserving the BEAR skip by default.

    ``use_regime_filter=False`` can look attractive in a short BULL-only sample,
    but it weakens the live strategy's core protection: stand aside in BEAR. Keep
    it out of default robustness ranking unless explicitly requested for
    exploratory diagnostics.
    """
    combos = build_param_grid()
    if allow_no_regime_filter:
        return combos
    return [params for params in combos if params.get("use_regime_filter") is True]


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _mean(values: list[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def _stdev(values: list[float]) -> float:
    return float(statistics.pstdev(values)) if len(values) > 1 else 0.0


def _percentile(values: list[float], pct: float) -> float:
    """Small deterministic percentile helper without numpy/pandas dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def build_walk_forward_windows(
    timestamps: list[int],
    *,
    train_hours: int,
    test_hours: int,
    count: int,
) -> list[dict[str, Any]]:
    """Build newest-N rolling train/OOS windows.

    Test windows are non-overlapping and each train window ends exactly one hour
    before its OOS window starts. Training windows may overlap previous OOS
    periods, which is normal for rolling walk-forward diagnostics; the ranking
    score below only uses OOS robustness, not train headline P&L.
    """
    if train_hours <= 0 or test_hours <= 0 or count <= 0:
        return []

    ts = sorted({int(value) for value in timestamps})
    if not ts:
        return []

    first_ts = ts[0]
    cursor_test_end = ts[-1]
    built: list[dict[str, Any]] = []

    while len(built) < count:
        test_end = cursor_test_end
        test_start = test_end - (test_hours - 1) * HOUR_MS
        train_end = test_start - HOUR_MS
        train_start = train_end - (train_hours - 1) * HOUR_MS
        if train_start < first_ts:
            break
        built.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
                "train_start_iso": _iso(train_start),
                "train_end_iso": _iso(train_end),
                "test_start_iso": _iso(test_start),
                "test_end_iso": _iso(test_end),
            }
        )
        # Step back by one OOS span, keeping test windows non-overlapping.
        cursor_test_end = test_start - HOUR_MS

    windows = list(reversed(built))
    for idx, window in enumerate(windows, start=1):
        window["label"] = f"w{idx}"
    return windows


def benchmark_pnl(
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    btc_data: list[dict[str, Any]],
    window: dict[str, Any],
    *,
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS,
) -> tuple[float, dict[str, float]]:
    """Return the best configured benchmark P&L for a test window.

    The default benchmark is intentionally risk-on for BULL research: cash, BTC,
    SOL, and the live starting coin. The artifact keeps per-benchmark values so
    we can see whether a candidate merely rode beta or added alpha.
    """
    values: dict[str, float] = {}
    start_ts = int(window["test_start"])
    end_ts = int(window["test_end"])
    for symbol in benchmarks:
        label = symbol.upper()
        if label == "CASH":
            values["cash"] = 0.0
        elif label == "BTC":
            values["BTC"] = buy_hold_pnl(btc_data, start_ts, end_ts)
        else:
            candles = ohlcv_by_coin.get(label, [])
            if candles:
                values[label] = buy_hold_pnl(candles, start_ts, end_ts)
    if not values:
        values["cash"] = 0.0
    return max(values.values()), values


def evaluate_params_on_windows(
    params: dict[str, Any],
    windows: list[dict[str, Any]],
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    btc_data: list[dict[str, Any]],
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    max_window_drawdown_pct: float = 35.0,
    min_window_trades: int = 1,
) -> list[dict[str, Any]]:
    """Run one parameter set across every walk-forward window."""
    price_index = build_price_index(ohlcv_by_coin)
    ts_list = [row["ts"] for row in ohlcv_by_coin.get("SOL", [])]
    results: list[dict[str, Any]] = []
    for window in windows:
        train = run_momrot(
            params,
            start_ts=int(window["train_start"]),
            end_ts=int(window["train_end"]),
            ohlcv_by_coin=ohlcv_by_coin,
            btc_data=btc_data,
            price_index=price_index,
            all_timestamps=ts_list,
            initial_balance=initial_balance,
        )
        train["initial_balance"] = initial_balance
        oos = run_momrot(
            params,
            start_ts=int(window["test_start"]),
            end_ts=int(window["test_end"]),
            ohlcv_by_coin=ohlcv_by_coin,
            btc_data=btc_data,
            price_index=price_index,
            all_timestamps=ts_list,
            initial_balance=initial_balance,
        )
        oos["initial_balance"] = initial_balance
        baseline, benchmark_values = benchmark_pnl(ohlcv_by_coin, btc_data, window)
        vs_baseline = float(oos.get("pnl", 0.0)) - baseline
        passed = (
            float(oos.get("pnl", 0.0)) > 0.0
            and vs_baseline >= 0.0
            and float(oos.get("max_dd", 100.0)) <= max_window_drawdown_pct
            and int(oos.get("trades", 0)) >= min_window_trades
        )
        results.append(
            {
                "window": window,
                "train": train,
                "oos": oos,
                "baseline_pnl": baseline,
                "benchmark_pnls": benchmark_values,
                "vs_baseline_pct": vs_baseline,
                "passed": passed,
            }
        )
    return results


def score_robustness(window_results: list[dict[str, Any]]) -> float:
    """Drawdown/variance-aware score for ranking BULL momentum params."""
    if not window_results:
        return -1_000_000.0
    oos = [float(row.get("oos", {}).get("pnl", 0.0)) for row in window_results]
    baselines = [float(row.get("baseline_pnl", 0.0)) for row in window_results]
    vs_baseline = [float(row.get("vs_baseline_pct", p - b)) for row, p, b in zip(window_results, oos, baselines)]
    drawdowns = [float(row.get("oos", {}).get("max_dd", 100.0)) for row in window_results]
    fees_pct = [
        float(row.get("oos", {}).get("fees", 0.0))
        / max(float(row.get("oos", {}).get("initial_balance", DEFAULT_INITIAL_BALANCE)), DEFAULT_INITIAL_BALANCE)
        * 100.0
        for row in window_results
    ]
    pass_rate = sum(1 for row in window_results if row.get("passed")) / len(window_results) * 100.0
    variability = _stdev(oos)
    score = (
        _median(oos)
        + _mean(oos) * 0.20
        + min(oos) * 0.50
        + _median(vs_baseline) * 0.50
        + pass_rate * 0.05
        - max(drawdowns) * 0.25
        - variability * 0.25
        - _mean(fees_pct) * 0.50
    )
    return round(score, 6)


def _window_sharpe(oos_values: list[float]) -> float:
    if not oos_values:
        return 0.0
    std = _stdev(oos_values)
    if std == 0:
        return 0.0 if oos_values[0] == 0 else (1.0 if oos_values[0] > 0 else -1.0)
    return _mean(oos_values) / std


def make_robust_acceptance_record(
    rank: int,
    params: dict[str, Any],
    window_results: list[dict[str, Any]],
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
) -> dict[str, Any]:
    """Convert multi-window diagnostics into one gated research record."""
    oos_values = [float(row.get("oos", {}).get("pnl", 0.0)) for row in window_results]
    train_values = [float(row.get("train", {}).get("pnl", 0.0)) for row in window_results]
    baseline_values = [float(row.get("baseline_pnl", 0.0)) for row in window_results]
    vs_baseline = [float(row.get("vs_baseline_pct", 0.0)) for row in window_results]
    drawdowns = [float(row.get("oos", {}).get("max_dd", 0.0)) for row in window_results]
    trades = [int(row.get("oos", {}).get("trades", 0)) for row in window_results]
    fees = [float(row.get("oos", {}).get("fees", 0.0)) for row in window_results]
    pass_count = sum(1 for row in window_results if row.get("passed"))
    window_count = len(window_results)
    fee_pct = _mean([(fee / initial_balance * 100.0) if initial_balance else 0.0 for fee in fees])
    robustness_score = score_robustness(window_results)

    return {
        "name": f"bull_momentum_robust_rank_{rank}",
        "strategy": "bull_momentum_rotation",
        "regime": "bull",
        "params": dict(params),
        "train_pnl": _median(train_values),
        "oos_pnl": _median(oos_values),
        "pnl_pct": _median(oos_values),
        "baseline_pnl": _median(baseline_values),
        "baseline_pnl_pct": _median(baseline_values),
        "vs_baseline_pct": _median(vs_baseline),
        "max_drawdown": max(drawdowns) if drawdowns else 0.0,
        "max_drawdown_pct": max(drawdowns) if drawdowns else 0.0,
        "trade_count": sum(trades),
        "trades": sum(trades),
        "fees": sum(fees),
        "total_fees": sum(fees),
        "fee_pct": fee_pct,
        "sharpe": _window_sharpe(oos_values),
        "initial_balance": initial_balance,
        "robustness_score": robustness_score,
        "robustness": {
            "window_count": window_count,
            "passing_windows": pass_count,
            "pass_rate_pct": (pass_count / window_count * 100.0) if window_count else 0.0,
            "median_train_pnl": _median(train_values),
            "median_oos_pnl": _median(oos_values),
            "mean_oos_pnl": _mean(oos_values),
            "worst_oos_pnl": min(oos_values) if oos_values else 0.0,
            "p10_oos_pnl": _percentile(oos_values, 0.10),
            "median_baseline_pnl": _median(baseline_values),
            "median_vs_baseline_pct": _median(vs_baseline),
            "worst_vs_baseline_pct": min(vs_baseline) if vs_baseline else 0.0,
            "worst_max_drawdown_pct": max(drawdowns) if drawdowns else 0.0,
            "mean_fee_pct": fee_pct,
            "oos_stdev_pct": _stdev(oos_values),
            "score": robustness_score,
        },
        "windows": window_results,
    }


def build_bull_momentum_research_output(
    records: list[dict[str, Any]],
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    btc_data: list[dict[str, Any]],
    *,
    assumptions: dict[str, Any] | None = None,
    gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    manifest_data = {**ohlcv_by_coin, "BTC": btc_data}
    gate_overrides = {
        "min_trades": 1,
        "max_drawdown_pct": 35.0,
        **(gates or {}),
    }
    payload = build_research_output(
        records,
        ohlcv_by_coin=manifest_data,
        interval=str((assumptions or {}).get("interval", "1h")),
        bridge=BRIDGE,
        assumptions={
            "data_source": "Binance public spot klines",
            "research_scope": "BULL momentum robustness; shadow/research only",
            "ranking": "median/worst OOS, pass-rate, drawdown, variance, and fee adjusted",
            **(assumptions or {}),
        },
        gates=gate_overrides,
    )
    return payload | {"candidates": records}


def run_robust_optimization(
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    btc_data: list[dict[str, Any]],
    *,
    max_combos: int = 200,
    top_n: int = 10,
    windows: int = 3,
    train_days: int = 60,
    test_days: int = 30,
    seed: int = 42,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    output_path: str | None = "bull_momentum_robustness.json",
    interval: str = "1h",
    allow_no_regime_filter: bool = False,
) -> dict[str, Any]:
    timestamps = [int(row["ts"]) for row in ohlcv_by_coin.get("SOL", [])]
    wf_windows = build_walk_forward_windows(
        timestamps,
        train_hours=train_days * 24,
        test_hours=test_days * 24,
        count=windows,
    )
    if not wf_windows:
        raise ValueError("Not enough SOL candles to build requested walk-forward windows")

    combos = candidate_param_grid(allow_no_regime_filter=allow_no_regime_filter)
    random.seed(seed)
    if max_combos and len(combos) > max_combos:
        combos = random.sample(combos, max_combos)

    print(
        f"Evaluating {len(combos)} parameter sets across {len(wf_windows)} "
        f"walk-forward windows ({train_days}d train / {test_days}d OOS)..."
    )
    records: list[dict[str, Any]] = []
    for idx, params in enumerate(combos, start=1):
        window_results = evaluate_params_on_windows(
            params,
            wf_windows,
            ohlcv_by_coin,
            btc_data,
            initial_balance=initial_balance,
        )
        record = make_robust_acceptance_record(
            idx,
            params,
            window_results,
            initial_balance=initial_balance,
        )
        records.append(record)
        if idx % 50 == 0:
            best = max(row["robustness_score"] for row in records)
            print(f"  {idx}/{len(combos)}... best robust score {best:+.2f}")

    records.sort(key=lambda row: row["robustness_score"], reverse=True)
    for rank, record in enumerate(records, start=1):
        record["name"] = f"bull_momentum_robust_rank_{rank}"

    kept = records[:top_n]
    assumptions = {
        "initial_balance": initial_balance,
        "interval": interval,
        "max_combos": max_combos,
        "sampled_combos": len(combos),
        "seed": seed,
        "top_n": top_n,
        "windows": len(wf_windows),
        "requested_windows": windows,
        "train_days": train_days,
        "test_days": test_days,
        "benchmarks": list(DEFAULT_BENCHMARKS),
        "allow_no_regime_filter": allow_no_regime_filter,
        "fee_rate": 0.00075,
        "slippage": 0.0005,
        "tested_params": sorted(combos[0].keys()) if combos else [],
    }
    required_passing_windows = 1 if len(wf_windows) == 1 else max(2, (len(wf_windows) + 1) // 2)
    payload = build_bull_momentum_research_output(
        kept,
        ohlcv_by_coin,
        btc_data,
        assumptions=assumptions,
        gates={
            "min_trades": max(1, len(wf_windows)),
            "min_passing_windows": required_passing_windows,
            "min_window_pass_rate_pct": 50.0,
        },
    )
    payload["windows"] = wf_windows
    payload["evaluation_summary"] = {
        "total_candidates_evaluated": len(records),
        "top_candidate_score": kept[0]["robustness_score"] if kept else None,
        "top_candidate_params": kept[0]["params"] if kept else None,
    }

    if output_path:
        Path(output_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"Saved to {output_path}")
    return payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180, help="History length in days")
    parser.add_argument("--interval", default="1h", help="Binance kline interval")
    parser.add_argument("--coins", default=",".join(COINS), help="Comma-separated spot base assets")
    parser.add_argument("--max-combos", type=int, default=200, help="Maximum sampled parameter combos")
    parser.add_argument("--top-n", type=int, default=10, help="Top robust candidates to save")
    parser.add_argument("--windows", type=int, default=3, help="Number of OOS windows")
    parser.add_argument("--train-days", type=int, default=60, help="Training days per walk-forward window")
    parser.add_argument("--test-days", type=int, default=30, help="OOS days per walk-forward window")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for combo sampling")
    parser.add_argument("--initial-balance", type=float, default=DEFAULT_INITIAL_BALANCE)
    parser.add_argument("--output", default="bull_momentum_robustness.json", help="JSON output path")
    parser.add_argument("--no-output", action="store_true", help="Do not write a JSON output file")
    parser.add_argument(
        "--allow-no-regime-filter",
        action="store_true",
        help="Exploratory only: include params that disable the BEAR regime skip",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    output_path = None if args.no_output else args.output
    print(f"Fetching {args.days}d public spot data for {len(coins)} coins...")
    ohlcv_by_coin, btc_data = load_market_data(days=args.days, interval=args.interval, coins=coins)
    payload = run_robust_optimization(
        ohlcv_by_coin,
        btc_data,
        max_combos=args.max_combos,
        top_n=args.top_n,
        windows=args.windows,
        train_days=args.train_days,
        test_days=args.test_days,
        seed=args.seed,
        initial_balance=args.initial_balance,
        output_path=output_path,
        interval=args.interval,
        allow_no_regime_filter=args.allow_no_regime_filter,
    )

    print("\nTop BULL momentum robust candidates:")
    for row in payload["records"][:10]:
        robust = row["robustness"]
        print(
            f"  {row['name']:<28} score={row['robustness_score']:+7.2f} "
            f"median_oos={robust['median_oos_pnl']:+6.2f}% "
            f"worst={robust['worst_oos_pnl']:+6.2f}% "
            f"pass={robust['passing_windows']}/{robust['window_count']} "
            f"dd={robust['worst_max_drawdown_pct']:.1f}% "
            f"params={row['params']}"
        )
    print(f"\nLeaderboard summary: {payload['leaderboard']['summary']}")
    return payload


if __name__ == "__main__":
    main()
