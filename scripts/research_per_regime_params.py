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

COINS = [
    "SOL", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE",
    "AVAX", "SUI", "TIA", "ENA", "PEPE", "JUP", "INJ", "APT",
]

PARAM_GRID = [
    # (lookback_hours, min_edge_pct)
    (6, 3.0),   # Fast, sensitive
    (6, 5.0),
    (12, 3.0),  # Medium-fast
    (12, 5.0),
    (12, 8.0),
    (18, 5.0),  # Current lookback, looser
    (18, 8.0),  # CURRENT DEFAULT
    (18, 12.0), # Current lookback, tighter
    (24, 5.0),  # Slow
    (24, 8.0),
    (24, 12.0),
    (36, 8.0),  # Very slow
]


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


def run_momentum(
    coin_candles: dict[str, list[dict]],
    regimes: list[str],
    target_regime: str,
    lookback_hours: int,
    min_edge: float,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
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

    for i in range(n):
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
        exit_price = coin_candles[current_coin][n - 1]["close"]
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
        for lookback, min_edge in PARAM_GRID:
            r = run_momentum(coin_candles, regimes, regime, lookback, min_edge)
            results.append((lookback, min_edge, r))
            marker = " ← DEFAULT" if lookback == 18 and min_edge == 8.0 else ""
            print(
                f"{lookback:>8}h {min_edge:>7.1f}% {r['return_pct']:>+9.1f}% "
                f"{r['trades']:>8} {r['win_rate']:>7.0f}%{marker}"
            )

        # Find best
        best = max(results, key=lambda x: x[2]["return_pct"])
        default = next((r for r in results if r[0] == 18 and r[1] == 8.0), None)
        improvement = best[2]["return_pct"] - (default[2]["return_pct"] if default else 0)

        print(f"\n  Best: lookback={best[0]}h min_edge={best[1]}% → {best[2]['return_pct']:+.1f}%")
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
    for regime in ["bull", "sideways", "bear"]:
        results = []
        for lookback, min_edge in PARAM_GRID:
            r = run_momentum(coin_candles, regimes, regime, lookback, min_edge)
            results.append((lookback, min_edge, r))
        best = max(results, key=lambda x: x[2]["return_pct"])
        default = next((r for r in results if r[0] == 18 and r[1] == 8.0))
        regime_best[regime] = best
        regime_default[regime] = default

    print(f"\n{'Regime':>10} {'Default Return':>15} {'Best Return':>15} {'Best Params':>20} {'Improvement':>12}")
    print("-" * 75)
    for regime in ["bull", "sideways", "bear"]:
        d = regime_default[regime]
        b = regime_best[regime]
        imp = b[2]["return_pct"] - d[2]["return_pct"]
        params = f"lb={b[0]}h edge={b[1]}%"
        print(
            f"{regime:>10} {d[2]['return_pct']:>+14.1f}% {b[2]['return_pct']:>+14.1f}% "
            f"{params:>20} {imp:>+11.1f}%"
        )

    total_default = sum(regime_default[r][2]["return_pct"] for r in regime_default)
    total_best = sum(regime_best[r][2]["return_pct"] for r in regime_best)
    print(f"\n{'TOTAL':>10} {total_default:>+14.1f}% {total_best:>+14.1f}% {'':>20} {total_best - total_default:>+11.1f}%")

    print(f"\nConclusion: Per-regime parameter tuning could improve returns by "
          f"{total_best - total_default:+.1f}% over {days}d.")


if __name__ == "__main__":
    main()
