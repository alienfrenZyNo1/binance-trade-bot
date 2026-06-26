#!/usr/bin/env python3
"""Per-regime parameter sensitivity study.

Tests different momentum_lookback and min_edge combinations for each regime
to find whether regime-specific parameters beat the current one-size-fits-all
approach (lookback=18h, min_edge=8%).

Uses the same ADX(14) + EMA(12/26) regime classification as the live bot.
Research-only — no live changes.
"""

from __future__ import annotations

import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

BINANCE_API = "https://api.binance.com"
BRIDGE = "USDC"
HOUR_MS = 3_600_000

COINS = [
    "SOL", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE",
    "AVAX", "SUI", "TIA", "ENA", "PEPE", "JUP", "INJ", "APT",
]

LOOKBACK_GRID = [4, 6, 8, 10, 12, 18, 24, 36, 48]
MIN_EDGE_GRID = [3.0, 5.0, 8.0, 10.0, 12.0]


def build_param_grid(
    lookbacks: list[int] | None = None,
    min_edges: list[float] | None = None,
) -> list[tuple[int, float]]:
    """Return the full sensitivity surface, not a hand-picked subset."""
    lbs = lookbacks or LOOKBACK_GRID
    edges = min_edges or MIN_EDGE_GRID
    return [(int(lb), float(edge)) for lb in lbs for edge in edges]


PARAM_GRID = build_param_grid()


def _result_return_pct(value: dict[str, Any] | float | int | None) -> float:
    if isinstance(value, dict):
        return float(value.get("return_pct", 0.0))
    if value is None:
        return 0.0
    return float(value)


def _adjacent_param_keys(lookback_hours: int, min_edge: float) -> list[tuple[int, float]]:
    """Return immediate grid neighbors for plateau checks."""
    neighbors: list[tuple[int, float]] = []
    if lookback_hours in LOOKBACK_GRID:
        idx = LOOKBACK_GRID.index(lookback_hours)
        for adj_idx in (idx - 1, idx + 1):
            if 0 <= adj_idx < len(LOOKBACK_GRID):
                neighbors.append((LOOKBACK_GRID[adj_idx], float(min_edge)))
    if float(min_edge) in MIN_EDGE_GRID:
        idx = MIN_EDGE_GRID.index(float(min_edge))
        for adj_idx in (idx - 1, idx + 1):
            if 0 <= adj_idx < len(MIN_EDGE_GRID):
                neighbors.append((int(lookback_hours), MIN_EDGE_GRID[adj_idx]))
    return neighbors


def assess_parameter_plateau(
    results: dict[tuple[int, float], dict[str, Any] | float],
    lookback_hours: int,
    min_edge: float,
    *,
    min_neighbor_ratio: float = 0.70,
    min_robust_neighbors: int = 2,
) -> dict[str, Any]:
    """Assess whether a candidate is a robust plateau or a sharp spike.

    A top candidate is suspect if adjacent grid cells collapse. This helper
    requires nearby lookback/edge combinations to retain at least a fraction of
    the candidate return before we consider it robust.
    """
    candidate_key = (int(lookback_hours), float(min_edge))
    candidate_return = _result_return_pct(results.get(candidate_key))
    neighbor_keys = [key for key in _adjacent_param_keys(*candidate_key) if key in results]
    neighbor_returns = [_result_return_pct(results[key]) for key in neighbor_keys]
    if candidate_return <= 0:
        robust_neighbor_count = 0
        threshold = 0.0
    else:
        threshold = candidate_return * min_neighbor_ratio
        robust_neighbor_count = sum(1 for value in neighbor_returns if value >= threshold)
    return {
        "lookback_hours": candidate_key[0],
        "min_edge": candidate_key[1],
        "candidate_return_pct": candidate_return,
        "neighbor_count": len(neighbor_returns),
        "robust_neighbor_count": robust_neighbor_count,
        "worst_neighbor_return_pct": min(neighbor_returns) if neighbor_returns else 0.0,
        "median_neighbor_return_pct": sorted(neighbor_returns)[len(neighbor_returns) // 2] if neighbor_returns else 0.0,
        "min_neighbor_ratio": min_neighbor_ratio,
        "robust": robust_neighbor_count >= min_robust_neighbors,
    }


def build_walk_forward_windows(
    timestamps: list[int],
    *,
    train_hours: int,
    test_hours: int,
    step_hours: int,
    count: int,
) -> list[dict[str, Any]]:
    """Build newest-N non-overlapping OOS walk-forward windows."""
    if train_hours <= 0 or test_hours <= 0 or step_hours <= 0 or count <= 0:
        return []
    ts = sorted({int(value) for value in timestamps})
    if not ts:
        return []
    first_ts = ts[0]
    cursor_test_end = ts[-1]
    windows: list[dict[str, Any]] = []
    while len(windows) < count:
        test_end = cursor_test_end
        test_start = test_end - (test_hours - 1) * HOUR_MS
        train_end = test_start - HOUR_MS
        train_start = train_end - (train_hours - 1) * HOUR_MS
        if train_start < first_ts:
            break
        windows.append({
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
        })
        next_test_start = test_start - step_hours * HOUR_MS
        cursor_test_end = next_test_start + (test_hours - 1) * HOUR_MS
    windows = list(reversed(windows))
    for idx, window in enumerate(windows, start=1):
        window["label"] = f"w{idx}"
    return windows


def format_best_line(
    best: tuple[int, float, dict[str, Any]],
    plateau: dict[str, Any],
    *,
    min_trades: int = 15,
) -> str:
    """Format a best-result line that cannot hide weak sample/spike status."""
    lookback, min_edge, result = best
    trades = int(result.get("trades", 0))
    verdict = "PLATEAU" if plateau.get("robust") else "SPIKE"
    warnings = []
    if trades < min_trades:
        warnings.append("BELOW_MIN_TRADES")
    if not plateau.get("robust"):
        warnings.append("SPIKE")
    suffix = f" [{' '.join(warnings)}]" if warnings else ""
    return (
        f"Best: lookback={lookback}h min_edge={min_edge}% → "
        f"{result['return_pct']:+.1f}% trades={trades} {verdict}{suffix}"
    )


def select_best_result(
    results: list[tuple[int, float, dict[str, Any]]],
    *,
    min_trades: int = 15,
) -> tuple[int, float, dict[str, Any]]:
    """Return best result after a minimum-trade reliability floor.

    If no result reaches the floor, fall back to the highest-return cell while
    making the caller's printed trade count expose the weak sample size.
    """
    eligible = [row for row in results if int(row[2].get("trades", 0)) >= min_trades]
    pool = eligible or results
    return max(pool, key=lambda x: x[2]["return_pct"])


def fetch_klines(symbol: str, interval: str = "1h", days: int = 90) -> list[dict]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows = []
    cur = start_ms
    while cur < end_ms:
        params = {
            "symbol": f"{symbol}{BRIDGE}", "interval": interval,
            "startTime": cur, "endTime": end_ms, "limit": 1000,
        }
        r = requests.get(f"{BINANCE_API}/api/v3/klines", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        cur = int(data[-1][0]) + 1
        if len(data) < 1000:
            break
        time.sleep(0.1)
    return [
        {"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        for k in rows
    ]


def ema(values: list[float], period: int) -> list[float]:
    result = []
    k = 2.0 / (period + 1)
    prev = values[0] if values else 0
    for v in values:
        prev = v * k + prev * (1 - k)
        result.append(prev)
    return result


def adx(highs, lows, closes, period=14):
    n = len(closes)
    if n < period * 2:
        return [None] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        hc = max(highs[i] - closes[i - 1], 0)
        lc = max(closes[i - 1] - lows[i], 0)
        tr[i] = max(highs[i] - lows[i], hc, lc)
    atr = [0.0] * n
    apdm = [0.0] * n
    amdm = [0.0] * n
    atr[period] = sum(tr[1:period + 1])
    apdm[period] = sum(plus_dm[1:period + 1])
    amdm[period] = sum(minus_dm[1:period + 1])
    for i in range(period + 1, n):
        atr[i] = atr[i - 1] - atr[i - 1] / period + tr[i]
        apdm[i] = apdm[i - 1] - apdm[i - 1] / period + plus_dm[i]
        amdm[i] = amdm[i - 1] - amdm[i - 1] / period + minus_dm[i]
    dx = [0.0] * n
    for i in range(period, n):
        if atr[i] > 0:
            pdi = 100 * apdm[i] / atr[i]
            mdi = 100 * amdm[i] / atr[i]
            s = pdi + mdi
            dx[i] = 100 * abs(pdi - mdi) / s if s > 0 else 0
    adx_vals = [None] * n
    if n >= 2 * period:
        first = sum(dx[period:2 * period]) / period
        adx_vals[2 * period - 1] = first
        for i in range(2 * period, n):
            adx_vals[i] = (adx_vals[i - 1] * (period - 1) + dx[i]) / period
    return adx_vals


def classify_regime(candles):
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    es = ema(closes, 12)
    el = ema(closes, 26)
    ax = adx(highs, lows, closes, 14)
    regimes = []
    for i in range(len(candles)):
        if ax[i] is None or i < 26:
            regimes.append("unknown")
        elif ax[i] < 25.0:
            regimes.append("sideways")
        elif es[i] > el[i]:
            regimes.append("bull")
        else:
            regimes.append("bear")
    return regimes


def evaluate_walk_forward(
    coin_candles: dict[str, list[dict]],
    regimes: list[str],
    target_regime: str,
    windows: list[dict[str, Any]],
    *,
    param_grid: list[tuple[int, float]] | None = None,
    runner=None,
    min_trades: int = 15,
) -> list[dict[str, Any]]:
    """Select params on each train window, then score only selected params OOS.

    This avoids peeking at test-window performance when choosing parameters.
    """
    grid = param_grid or PARAM_GRID
    if runner is None:
        runner = run_momentum
    output: list[dict[str, Any]] = []
    for window in windows:
        train_results = []
        grid_results = {}
        for lookback, min_edge in grid:
            train = runner(
                coin_candles,
                regimes,
                target_regime,
                lookback,
                min_edge,
                start_ts=int(window["train_start"]),
                end_ts=int(window["train_end"]),
            )
            train_results.append((lookback, min_edge, train))
            grid_results[(lookback, min_edge)] = train
        selected = select_best_result(train_results, min_trades=min_trades)
        plateau = assess_parameter_plateau(grid_results, selected[0], selected[1])
        oos = runner(
            coin_candles,
            regimes,
            target_regime,
            selected[0],
            selected[1],
            start_ts=int(window["test_start"]),
            end_ts=int(window["test_end"]),
        )
        output.append({
            "window": window,
            "selected_params": {"lookback_hours": selected[0], "min_edge": selected[1]},
            "train": selected[2],
            "train_plateau": plateau,
            "oos": oos,
            "passed": (
                bool(plateau.get("robust"))
                and int(selected[2].get("trades", 0)) >= min_trades
                and float(oos.get("return_pct", 0.0)) > 0.0
                and int(oos.get("trades", 0)) >= 1
            ),
        })
    return output


def run_momentum(
    coin_candles: dict[str, list[dict]],
    regimes: list[str],
    target_regime: str,
    lookback_hours: int,
    min_edge: float,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> dict[str, Any]:
    """Run momentum rotation with specific params in target regime."""
    ref = coin_candles.get("SOL", coin_candles[list(coin_candles.keys())[0]])
    n = len(ref)
    trade_fees = (fee_pct + slippage_pct) / 100.0

    # Pre-compute performance
    perf = {}
    for coin, candles in coin_candles.items():
        perf[coin] = [None] * n
        for i in range(lookback_hours, n):
            sp = candles[i - lookback_hours]["close"]
            if sp > 0:
                perf[coin][i] = (candles[i]["close"] / sp - 1.0) * 100.0

    equity = 1.0
    current_coin = None
    entry_price = 0.0
    trades = 0
    wins = 0

    active_indices = [
        i for i, candle in enumerate(ref)
        if (start_ts is None or int(candle["ts"]) >= start_ts)
        and (end_ts is None or int(candle["ts"]) <= end_ts)
    ]
    if not active_indices:
        return {"return_pct": 0.0, "trades": 0, "win_rate": 0.0}

    for i in active_indices:
        if regimes[i] != target_regime:
            continue

        if current_coin is None:
            best = None
            best_perf = -float("inf")
            for coin in coin_candles:
                p = perf[coin][i]
                if p is not None and p > best_perf:
                    best_perf = p
                    best = coin
            if best:
                current_coin = best
                entry_price = coin_candles[best][i]["close"]
        elif current_coin:
            cur_perf = perf[current_coin][i]
            if cur_perf is None:
                continue
            best = None
            best_edge = min_edge
            for coin in coin_candles:
                if coin == current_coin:
                    continue
                p = perf[coin][i]
                if p is None:
                    continue
                edge = p - cur_perf
                if edge > best_edge:
                    best_edge = edge
                    best = coin
            if best:
                exit_price = coin_candles[current_coin][i]["close"]
                hold_ret = (exit_price / entry_price - 1.0) * 100.0
                equity *= (1.0 + hold_ret / 100.0) * (1.0 - trade_fees)
                if hold_ret > 0:
                    wins += 1
                trades += 1
                current_coin = best
                entry_price = coin_candles[best][i]["close"]

    # Close final
    if current_coin:
        exit_idx = active_indices[-1]
        exit_price = coin_candles[current_coin][exit_idx]["close"]
        hold_ret = (exit_price / entry_price - 1.0) * 100.0
        equity *= (1.0 + hold_ret / 100.0) * (1.0 - trade_fees)
        if hold_ret > 0:
            wins += 1
        trades += 1

    return {
        "return_pct": (equity - 1.0) * 100.0,
        "trades": trades,
        "win_rate": wins / trades * 100 if trades else 0,
    }


def main():
    days = 90
    print(f"Fetching {days}d spot data for {len(COINS)} coins...")
    coin_candles = {}
    for coin in COINS:
        try:
            coin_candles[coin] = fetch_klines(coin, "1h", days)
        except Exception as e:
            print(f"  {coin}: FAILED ({e})")
        time.sleep(0.05)
    print(f"  Loaded {len(coin_candles)} coins")

    ref = coin_candles["SOL"]
    regimes = classify_regime(ref)

    # Count regime hours
    counts = defaultdict(int)
    for r in regimes:
        counts[r] += 1
    print(f"\nRegime distribution: {dict(counts)}")

    print(f"\n{'='*80}")
    print(f"PER-REGIME PARAMETER SENSITIVITY ({days}d)")
    print(f"{'='*80}")

    for regime in ["bull", "sideways", "bear"]:
        print(f"\n── {regime.upper()} ({counts[regime]}h) ──")
        print(f"{'Lookback':>8} {'MinEdge':>8} {'Return':>10} {'Trades':>8} {'WinRate':>8}")
        print("-" * 50)

        results = []
        grid_results = {}
        for lookback, min_edge in PARAM_GRID:
            r = run_momentum(coin_candles, regimes, regime, lookback, min_edge)
            results.append((lookback, min_edge, r))
            grid_results[(lookback, min_edge)] = r
            marker = " ← DEFAULT" if lookback == 18 and min_edge == 8.0 else ""
            print(
                f"{lookback:>8}h {min_edge:>7.1f}% {r['return_pct']:>+9.1f}% "
                f"{r['trades']:>8} {r['win_rate']:>7.0f}%{marker}"
            )

        # Find best after minimum-trade reliability floor
        best = select_best_result(results, min_trades=15)
        plateau = assess_parameter_plateau(grid_results, best[0], best[1])
        default = next((r for r in results if r[0] == 18 and r[1] == 8.0), None)
        improvement = best[2]["return_pct"] - (default[2]["return_pct"] if default else 0)

        print(f"\n  {format_best_line(best, plateau, min_trades=15)}")
        plateau_label = "ROBUST PLATEAU" if plateau["robust"] else "SHARP/SUSPECT SPIKE"
        print(
            f"  Plateau check: {plateau_label} "
            f"({plateau['robust_neighbor_count']}/{plateau['neighbor_count']} neighbors >= "
            f"{plateau['min_neighbor_ratio']:.0%} of best; "
            f"worst neighbor {plateau['worst_neighbor_return_pct']:+.1f}%)"
        )
        if default and improvement > 1:
            print(f"  Improvement vs current default: +{improvement:.1f}%")
        elif default and improvement < -1:
            print(f"  Current default is better by {-improvement:.1f}%")
        else:
            print(f"  Similar to current default")

    # Summary: what would optimal per-regime config achieve?
    print(f"\n{'='*80}")
    print("OPTIMAL PER-REGIME CONFIG vs ONE-SIZE-FITS-ALL")
    print(f"{'='*80}")

    regime_best = {}
    regime_default = {}
    regime_plateau = {}
    for regime in ["bull", "sideways", "bear"]:
        results = []
        grid_results = {}
        for lookback, min_edge in PARAM_GRID:
            r = run_momentum(coin_candles, regimes, regime, lookback, min_edge)
            results.append((lookback, min_edge, r))
            grid_results[(lookback, min_edge)] = r
        best = select_best_result(results, min_trades=15)
        default = next((r for r in results if r[0] == 18 and r[1] == 8.0))
        regime_best[regime] = best
        regime_default[regime] = default
        regime_plateau[regime] = assess_parameter_plateau(grid_results, best[0], best[1])

    print(f"\n{'Regime':>10} {'Default Return':>15} {'Best Return':>15} {'Best Params':>20} {'Verdict':>10} {'Improvement':>12}")
    print("-" * 88)
    for regime in ["bull", "sideways", "bear"]:
        d = regime_default[regime]
        b = regime_best[regime]
        imp = b[2]["return_pct"] - d[2]["return_pct"]
        params = f"lb={b[0]}h edge={b[1]}%"
        verdict = "✓ plateau" if regime_plateau[regime]["robust"] else "⚠ spike"
        print(
            f"{regime:>10} {d[2]['return_pct']:>+14.1f}% {b[2]['return_pct']:>+14.1f}% "
            f"{params:>20} {verdict:>10} {imp:>+11.1f}%"
        )

    total_default = sum(regime_default[r][2]["return_pct"] for r in regime_default)
    total_best = sum(regime_best[r][2]["return_pct"] for r in regime_best)
    print(f"\n{'TOTAL (diagnostic sum)':>24} {total_default:>+14.1f}% {total_best:>+14.1f}% {'':>20} {total_best - total_default:>+11.1f}%")

    print(
        "\nConclusion: This is an in-sample sensitivity scan, not a live enablement signal. "
        "Any best cell flagged as SHARP/SUSPECT SPIKE or supported by too few trades "
        "must be rejected until true walk-forward OOS validation passes. "
        "The TOTAL row is a diagnostic sum of regime slices, not a realizable compounded return."
    )

    print(f"\n{'='*80}")
    print("WALK-FORWARD TRAIN→OOS VALIDATION")
    print(f"{'='*80}")
    timestamps = [row["ts"] for row in ref]
    windows = build_walk_forward_windows(
        timestamps,
        train_hours=45 * 24,
        test_hours=15 * 24,
        step_hours=15 * 24,
        count=3,
    )
    if not windows:
        print("Not enough data for walk-forward windows.")
    else:
        for regime in ["bull", "sideways", "bear"]:
            wf = evaluate_walk_forward(
                coin_candles,
                regimes,
                regime,
                windows,
                min_trades=15,
            )
            print(f"\n── {regime.upper()} WALK-FORWARD ──")
            print(f"{'Window':>6} {'Params':>18} {'Train':>10} {'OOS':>10} {'Trades':>8} {'Verdict':>10}")
            print("-" * 70)
            for row in wf:
                p = row["selected_params"]
                params = f"{p['lookback_hours']}h/{p['min_edge']}%"
                train_ret = float(row["train"].get("return_pct", 0.0))
                oos_ret = float(row["oos"].get("return_pct", 0.0))
                trades = int(row["oos"].get("trades", 0))
                verdict = "PASS" if row["passed"] else "FAIL"
                print(
                    f"{row['window']['label']:>6} {params:>18} "
                    f"{train_ret:>+9.1f}% {oos_ret:>+9.1f}% {trades:>8} {verdict:>10}"
                )
            pass_count = sum(1 for row in wf if row["passed"])
            print(f"  Result: {pass_count}/{len(wf)} windows passed")


if __name__ == "__main__":
    main()
