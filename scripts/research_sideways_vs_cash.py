#!/usr/bin/env python3
"""SIDEWAYS regime study: does momentum rotation beat cash?

Classifies the last N days into BULL/SIDEWAYS/BEAR using the same ADX(14) +
EMA(12/26) logic as the live bot, then runs momentum rotation only during
SIDEWAYS periods to see if it beats sitting in USDC.

Research-only. Uses free public Binance spot data.
"""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

BINANCE_API = "https://api.binance.com"
HOUR_MS = 3600 * 1000
BRIDGE = "USDC"

COINS = [
    "SOL", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE",
    "AVAX", "SUI", "TIA", "ENA", "PEPE", "JUP", "INJ", "APT",
]


def fetch_klines(symbol: str, interval: str = "1h", days: int = 90) -> list[dict]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows = []
    cur = start_ms
    while cur < end_ms:
        params = {
            "symbol": f"{symbol}{BRIDGE}",
            "interval": interval,
            "startTime": cur,
            "endTime": end_ms,
            "limit": 1000,
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


# ── Indicators ───────────────────────────────────────────────────────────────

def ema(values: list[float], period: int) -> list[float]:
    result = []
    k = 2.0 / (period + 1)
    prev = values[0] if values else 0
    for v in values:
        prev = v * k + prev * (1 - k)
        result.append(prev)
    return result


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's ADX."""
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
        hl = highs[i] - lows[i]
        tr[i] = max(hl, hc, lc)

    # Wilder smoothing
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
        first_adx = sum(dx[period:2 * period]) / period
        for j in range(2 * period - 1):
            adx_vals[j] = None
        adx_vals[2 * period - 1] = first_adx
        for i in range(2 * period, n):
            adx_vals[i] = (adx_vals[i - 1] * (period - 1) + dx[i]) / period

    return adx_vals


def classify_regime(candles: list[dict]) -> list[str]:
    """Classify each candle as bull/sideways/bear using ADX + EMA."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    ema_short = ema(closes, 12)
    ema_long = ema(closes, 26)
    adx_vals = adx(highs, lows, closes, 14)

    regimes = []
    for i in range(len(candles)):
        if adx_vals[i] is None or i < 26:
            regimes.append("unknown")
        elif adx_vals[i] < 25.0:
            regimes.append("sideways")
        elif ema_short[i] > ema_long[i]:
            regimes.append("bull")
        else:
            regimes.append("bear")
    return regimes


# ── Momentum rotation backtest ───────────────────────────────────────────────

def momentum_backtest(
    coin_candles: dict[str, list[dict]],
    regimes: list[str],
    target_regime: str,
    *,
    lookback_hours: int = 18,
    min_edge: float = 8.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
) -> dict[str, Any]:
    """Run momentum rotation during target_regime periods only.

    Returns total return %, trade count, and per-trade details.
    """
    # Align all coins to the same timestamps (use SOL as reference)
    ref = coin_candles.get("SOL", coin_candles[list(coin_candles.keys())[0]])
    n = len(ref)

    # Pre-compute rolling performance for each coin at each timestamp
    # perf[coin][i] = % change over last lookback_hours
    perf = {}
    for coin, candles in coin_candles.items():
        perf[coin] = [None] * n
        for i in range(lookback_hours, n):
            start_price = candles[i - lookback_hours]["close"]
            if start_price > 0:
                perf[coin][i] = (candles[i]["close"] / start_price - 1.0) * 100.0

    # Simulate: at each hour during target_regime, find best rotation
    equity = 1.0
    current_coin = None
    trades = []
    trade_fees = (fee_pct + slippage_pct) / 100.0

    for i in range(n):
        if regimes[i] != target_regime:
            continue

        # If not holding, buy the best performing coin
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
                entry_ts = i

        # If holding, check for rotation
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
                # Rotate
                exit_price = coin_candles[current_coin][i]["close"]
                hold_return = (exit_price / entry_price - 1.0) * 100.0
                equity *= (1.0 + hold_return / 100.0) * (1.0 - trade_fees)

                trades.append({
                    "from": current_coin,
                    "to": best,
                    "hold_return_pct": hold_return,
                    "edge": best_edge,
                    "ts_idx": i,
                })

                current_coin = best
                entry_price = coin_candles[best][i]["close"]
                entry_ts = i

    # Close final position
    if current_coin and entry_price:
        last_idx = n - 1
        exit_price = coin_candles[current_coin][last_idx]["close"]
        hold_return = (exit_price / entry_price - 1.0) * 100.0
        equity *= (1.0 + hold_return / 100.0) * (1.0 - trade_fees)
        trades.append({
            "from": current_coin,
            "to": "CASH",
            "hold_return_pct": hold_return,
            "edge": 0,
            "ts_idx": last_idx,
        })

    total_return = (equity - 1.0) * 100.0
    winning = [t for t in trades if t["hold_return_pct"] > 0]
    losing = [t for t in trades if t["hold_return_pct"] <= 0]

    # Max drawdown
    peak = 1.0
    max_dd = 0.0
    eq = 1.0
    for t in trades:
        eq *= (1.0 + t["hold_return_pct"] / 100.0) * (1.0 - trade_fees)
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "regime": target_regime,
        "total_return_pct": total_return,
        "trade_count": len(trades),
        "win_rate": len(winning) / len(trades) * 100 if trades else 0,
        "avg_hold_return": sum(t["hold_return_pct"] for t in trades) / len(trades) if trades else 0,
        "max_drawdown_pct": max_dd,
        "cash_return_pct": 0.0,
        "edge_vs_cash": total_return,
        "trades": trades[-10:],  # Last 10 for inspection
    }


def main():
    days = 90
    print(f"Fetching {days}d spot data for {len(COINS)} coins...")
    coin_candles = {}
    for coin in COINS:
        try:
            coin_candles[coin] = fetch_klines(coin, "1h", days)
            print(f"  {coin}: {len(coin_candles[coin])} candles")
        except Exception as e:
            print(f"  {coin}: FAILED ({e})")
        time.sleep(0.1)

    # Classify regime using SOL (same as live bot)
    print("\nClassifying regime using SOL/USDC 1h...")
    ref_candles = coin_candles.get("SOL")
    if not ref_candles:
        print("❌ No SOL data")
        return

    regimes = classify_regime(ref_candles)

    # Count regime distribution
    from collections import Counter
    counts = Counter(regimes)
    total = len(regimes)
    print(f"\nRegime distribution ({total}h):")
    for r in ["bull", "sideways", "bear", "unknown"]:
        c = counts.get(r, 0)
        hrs = c
        days_r = c / 24
        print(f"  {r:10s}: {hrs:5d}h ({days_r:.1f}d) — {c/total*100:.0f}%")

    # Run momentum rotation per regime
    print(f"\n{'='*60}")
    print(f"MOMENTUM ROTATION vs CASH (per regime)")
    print(f"{'='*60}")
    print(f"Config: lookback=18h, min_edge=8%, fee=0.1%, slippage=0.05%")
    print()

    for regime in ["bull", "sideways", "bear"]:
        result = momentum_backtest(coin_candles, regimes, regime)
        tag = "✅" if result["edge_vs_cash"] > 0 else "❌"
        print(f"{tag} {regime.upper():10s}: {result['total_return_pct']:+.1f}% "
              f"({result['trade_count']} trades, WR={result['win_rate']:.0f}%, "
              f"maxDD={result['max_drawdown_pct']:.1f}%)")
        print(f"   vs cash (0%): edge={result['edge_vs_cash']:+.1f}%")

    # Summary verdict for SIDEWAYS
    side = momentum_backtest(coin_candles, regimes, "sideways")
    print(f"\n{'='*60}")
    print(f"VERDICT: SIDEWAYS regime")
    print(f"{'='*60}")
    if side["trade_count"] == 0:
        print("⚠️ No sideways periods detected — inconclusive")
    elif side["edge_vs_cash"] > 5:
        print(f"✅ Momentum rotation BEATS cash by {side['edge_vs_cash']:+.1f}%")
        print(f"   Strategy is justified during sideways markets.")
    elif side["edge_vs_cash"] > 0:
        print(f"🟡 Momentum rotation slightly beats cash by {side['edge_vs_cash']:+.1f}%")
        print(f"   Edge is thin — consider cash-only during sideways.")
    else:
        print(f"❌ Momentum rotation LOSES to cash by {side['edge_vs_cash']:+.1f}%")
        print(f"   Bot should sit in USDC during sideways markets.")
        print(f"   Churning fees ({side['trade_count']} trades) destroy value.")


if __name__ == "__main__":
    main()
