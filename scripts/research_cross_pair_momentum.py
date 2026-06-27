#!/usr/bin/env python3
"""Cross-pair momentum research: basket vs single-coin rotation.

Research-only. No live trading, no config changes.

Fetches 30-day 4h klines for 20+ USDC pairs, computes momentum, simulates:
  1. Top-3 momentum basket (equal weight, rebalance every 4h)
  2. Single best pair (hindsight — cheat)
  3. Buy-and-hold BTC
  4. Current bot's single-coin momentum rotation (lookback=18h, min_edge=8%)

Key question: Is the problem the implementation (single-coin rotation) or is
momentum itself broken? Would a basket approach diversify risk?
"""

from __future__ import annotations

import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BINANCE_API = "https://api.binance.com"
BRIDGE = "USDC"

# 25 liquid USDC pairs on Binance
PAIRS = [
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK",
    "DOT", "MATIC", "UNI", "AAVE", "NEAR", "SUI", "INJ", "JUP",
    "PEPE", "ENA", "TIA", "APT", "RENDER", "WIF", "PYTH", "FET", "SEI",
]

INTERVAL = "4h"
LIMIT = 180  # 30 days of 4h candles


def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = LIMIT) -> list[dict]:
    """Fetch klines from Binance public API."""
    params = {"symbol": f"{symbol}{BRIDGE}", "interval": interval, "limit": limit}
    r = requests.get(f"{BINANCE_API}/api/v3/klines", params=params, timeout=30)
    if r.status_code == 451:
        print(f"  {symbol}: unavailable in region, skipping")
        return []
    r.raise_for_status()
    data = r.json()
    return [
        {
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in data
    ]


def compute_momentum(closes: list[float], lookback: int) -> list[float | None]:
    """Compute simple return momentum over lookback periods."""
    n = len(closes)
    mom = [None] * n
    for i in range(lookback, n):
        if closes[i - lookback] > 0:
            mom[i] = (closes[i] / closes[i - lookback] - 1.0) * 100.0
    return mom


def rank_pairs(
    all_closes: dict[str, list[float]],
    all_mom7: dict[str, list[float | None]],
    all_mom14: dict[str, list[float | None]],
    idx: int,
    n: int,
) -> list[tuple[str, float, float, float]]:
    """Rank pairs by combined momentum score at index idx.

    Score = 0.6 * mom7 + 0.4 * mom14 (weight recent more).
    Returns list of (symbol, score, mom7, mom14) sorted descending.
    """
    scores = []
    for sym in all_closes:
        m7 = all_mom7[sym][idx] if idx < len(all_mom7[sym]) else None
        m14 = all_mom14[sym][idx] if idx < len(all_mom14[sym]) else None
        if m7 is not None and m14 is not None:
            score = 0.6 * m7 + 0.4 * m14
            scores.append((sym, score, m7, m14))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


def correlation_coefficient(a: list[float], b: list[float]) -> float:
    """Pearson correlation between two equal-length lists."""
    n = len(a)
    if n < 2:
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = math.sqrt(sum((a[i] - mean_a) ** 2 for i in range(n)))
    den_b = math.sqrt(sum((b[i] - mean_b) ** 2 for i in range(n)))
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


def simulate_basket(
    all_closes: dict[str, list[float]],
    all_mom7: dict[str, list[float | None]],
    all_mom14: dict[str, list[float | None]],
    n: int,
    top_k: int = 3,
    fee_pct: float = 0.1,
) -> dict[str, Any]:
    """Simulate top-K momentum basket strategy.

    Equal weight, rebalance every 4h. Tracks turnover, drawdown, returns.
    """
    # Skip first 42 candles (need 42 periods for 7-day momentum)
    warmup = 42
    if n <= warmup:
        return {"total_return_pct": 0, "trades_per_week": 0, "max_dd_pct": 0,
                "sharpe": 0, "turnover": 0}

    portfolio = [0.0] * n  # cumulative equity
    holdings = {sym: 0.0 for sym in all_closes}  # weight per symbol
    trades = 0
    fees_paid = 0.0

    # Start at warmup with all cash
    portfolio[warmup - 1] = 1.0

    for i in range(warmup, n):
        prev_close_idx = i - 1
        # Mark-to-market: apply price changes to holdings
        mtm = 0.0
        for sym in all_closes:
            if holdings.get(sym, 0) > 0:
                closes = all_closes[sym]
                if closes[prev_close_idx] > 0 and i < len(closes):
                    ret = closes[i] / closes[prev_close_idx] - 1.0
                    mtm += holdings[sym] * (1.0 + ret)
            else:
                mtm += 0.0

        # Add cash portion (unallocated)
        cash = portfolio[i - 1] - sum(holdings.values())
        if cash < 0:
            cash = 0
        equity = mtm + cash
        portfolio[i] = equity

        # Rebalance: rank by momentum
        ranked = rank_pairs(all_closes, all_mom7, all_mom14, i, n)
        if not ranked:
            continue

        # Target top-K
        target = {sym: equity / top_k for sym, *_ in ranked[:top_k]}

        # Compute trades needed
        for sym in all_closes:
            current_w = holdings.get(sym, 0)
            desired_w = target.get(sym, 0)
            delta = abs(desired_w - current_w)
            if delta > equity * 0.001:  # 0.1% threshold
                fee = delta * (fee_pct / 100.0)
                fees_paid += fee
                trades += 1
                holdings[sym] = desired_w

        # Zero out non-target
        for sym in all_closes:
            if sym not in target:
                holdings[sym] = 0.0

    # Metrics
    returns = []
    for i in range(warmup + 1, n):
        if portfolio[i - 1] > 0:
            r = portfolio[i] / portfolio[i - 1] - 1.0
            returns.append(r)

    total_ret = (portfolio[-1] / portfolio[warmup] - 1.0) * 100 if portfolio[warmup] > 0 else 0
    hours = (n - warmup) * 4  # 4h candles
    weeks = hours / 168
    trades_per_week = trades / weeks if weeks > 0 else 0

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for i in range(warmup, n):
        peak = max(peak, portfolio[i])
        if peak > 0:
            dd = (peak - portfolio[i]) / peak * 100
            max_dd = max(max_dd, dd)

    # Sharpe ratio (annualized, assuming 4h bars)
    if len(returns) > 1 and sum(returns) != 0:
        mean_ret = sum(returns) / len(returns)
        bars_per_year = 365.25 * 6  # 4h bars per year
        ann_ret = mean_ret * bars_per_year
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        ann_vol = math.sqrt(variance * bars_per_year) if variance > 0 else 0.001
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
    else:
        sharpe = 0

    return {
        "total_return_pct": total_ret,
        "net_return_after_fees": total_ret - fees_paid * 100 / portfolio[warmup] if portfolio[warmup] > 0 else 0,
        "fees_total_pct": fees_paid * 100 / portfolio[warmup] if portfolio[warmup] > 0 else 0,
        "trades": trades,
        "trades_per_week": trades_per_week,
        "max_dd_pct": max_dd,
        "sharpe": sharpe,
        "portfolio_values": portfolio,
    }


def simulate_single_coin_rotation(
    all_closes: dict[str, list[float]],
    all_mom7: dict[str, list[float | None]],
    all_mom14: dict[str, list[float | None]],
    n: int,
    lookback_candles: int = 18,  # ~3 days of 4h candles (matches bot's 18h lookback)
    min_edge: float = 8.0,
    fee_pct: float = 0.1,
) -> dict[str, Any]:
    """Simulate current bot's single-coin momentum rotation.

    Uses same logic: find best performer, rotate only if edge > min_edge.
    This approximates the bot's behavior on 4h instead of 1h.
    """
    warmup = lookback_candles + 2
    if n <= warmup:
        return {"total_return_pct": 0, "trades": 0, "max_dd_pct": 0, "sharpe": 0}

    equity = 1.0
    current_coin = None
    entry_idx = None
    trades = []
    trade_fees = fee_pct / 100.0

    for i in range(warmup, n):
        # Compute performance for each coin over lookback
        perfs = {}
        for sym in all_closes:
            closes = all_closes[sym]
            if i >= lookback_candles and closes[i - lookback_candles] > 0:
                perfs[sym] = (closes[i] / closes[i - lookback_candles] - 1.0) * 100.0

        if not perfs:
            continue

        # If not holding, buy best
        if current_coin is None:
            best_sym = max(perfs, key=perfs.get)
            current_coin = best_sym
            entry_idx = i
        else:
            # Check if another coin has better edge
            cur_perf = perfs.get(current_coin)
            if cur_perf is None:
                continue

            best_edge = -float("inf")
            best_sym = None
            for sym, p in perfs.items():
                if sym != current_coin:
                    edge = p - cur_perf
                    if edge > best_edge:
                        best_edge = edge
                        best_sym = sym

            if best_sym and best_edge > min_edge:
                # Rotate
                closes_cur = all_closes[current_coin]
                if entry_idx is not None and entry_idx < len(closes_cur) and closes_cur[entry_idx] > 0:
                    hold_ret = closes_cur[i] / closes_cur[entry_idx] - 1.0
                    equity *= (1.0 + hold_ret) * (1.0 - trade_fees)
                    trades.append({
                        "from": current_coin, "to": best_sym,
                        "hold_ret": hold_ret * 100,
                        "edge": best_edge,
                    })
                current_coin = best_sym
                entry_idx = i

    # Close final position
    if current_coin and entry_idx is not None:
        closes = all_closes[current_coin]
        if entry_idx < len(closes) and closes[entry_idx] > 0:
            hold_ret = closes[-1] / closes[entry_idx] - 1.0
            equity *= (1.0 + hold_ret) * (1.0 - trade_fees)
            trades.append({
                "from": current_coin, "to": "CASH",
                "hold_ret": hold_ret * 100,
                "edge": 0,
            })

    # Max drawdown
    peak = 1.0
    max_dd = 0.0
    eq = 1.0
    for t in trades:
        eq *= (1.0 + t["hold_ret"] / 100.0) * (1.0 - trade_fees)
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

    # Sharpe
    if len(trades) > 1:
        rets = [(1.0 + t["hold_ret"] / 100.0) * (1.0 - trade_fees) - 1.0 for t in trades]
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0
        sharpe = mean_r / math.sqrt(var_r) * math.sqrt(len(rets)) if var_r > 0 else 0
    else:
        sharpe = 0

    hours = (n - warmup) * 4
    weeks = hours / 168

    return {
        "total_return_pct": (equity - 1.0) * 100,
        "trades": len(trades),
        "trades_per_week": len(trades) / weeks if weeks > 0 else 0,
        "max_dd_pct": max_dd,
        "sharpe": sharpe,
        "win_rate": sum(1 for t in trades if t["hold_ret"] > 0) / len(trades) * 100 if trades else 0,
    }


def simulate_hindsight_best(
    all_closes: dict[str, list[float]], n: int, warmup: int = 42
) -> dict[str, Any]:
    """Cheat: at start, pick the pair with the best total return over the period."""
    best_sym = None
    best_ret = -float("inf")
    for sym, closes in all_closes.items():
        if len(closes) >= n and closes[warmup] > 0:
            ret = (closes[-1] / closes[warmup] - 1.0) * 100
            if ret > best_ret:
                best_ret = ret
                best_sym = sym
    return {"symbol": best_sym or "N/A", "total_return_pct": best_ret}


def simulate_buy_hold_btc(
    all_closes: dict[str, list[float]], n: int, warmup: int = 42
) -> dict[str, Any]:
    """Simple BTC buy and hold."""
    closes = all_closes.get("BTC", [])
    if len(closes) < n or closes[warmup] <= 0:
        return {"total_return_pct": 0}
    ret = (closes[-1] / closes[warmup] - 1.0) * 100

    # Max drawdown
    peak = closes[warmup]
    max_dd = 0.0
    for i in range(warmup, n):
        peak = max(peak, closes[i])
        if peak > 0:
            dd = (peak - closes[i]) / peak * 100
            max_dd = max(max_dd, dd)

    # Daily returns for Sharpe
    returns_4h = []
    for i in range(warmup + 1, n):
        if closes[i - 1] > 0:
            returns_4h.append(closes[i] / closes[i - 1] - 1.0)
    if len(returns_4h) > 1:
        mean_r = sum(returns_4h) / len(returns_4h)
        var_r = sum((r - mean_r) ** 2 for r in returns_4h) / len(returns_4h)
        bars_per_year = 365.25 * 6
        sharpe = (mean_r * bars_per_year) / (math.sqrt(var_r * bars_per_year) if var_r > 0 else 0.001)
    else:
        sharpe = 0

    return {"total_return_pct": ret, "max_dd_pct": max_dd, "sharpe": sharpe}


def compute_avg_pair_correlation(
    all_closes: dict[str, list[float]],
    all_mom7: dict[str, list[float | None]],
    top3_history: list[list[str]],
    n: int,
    warmup: int = 42,
) -> dict[str, Any]:
    """Average correlation between the top-3 picks at each rebalance point."""
    # Use 4h returns for correlation
    corr_values = []
    count = 0
    for step, top3 in enumerate(top3_history):
        if len(top3) < 2:
            continue
        # Get 4h returns over the last 42 periods for each of the top 3
        ret_maps = {}
        for sym in top3:
            closes = all_closes[sym]
            rets = []
            for i in range(warmup, n):
                if closes[i - 1] > 0:
                    rets.append(closes[i] / closes[i - 1] - 1.0)
            ret_maps[sym] = rets

        # Pairwise correlations among top 3
        for i_s, s1 in enumerate(top3):
            for s2 in top3[i_s + 1:]:
                r1 = ret_maps.get(s1, [])
                r2 = ret_maps.get(s2, [])
                min_len = min(len(r1), len(r2))
                if min_len > 10:
                    c = correlation_coefficient(r1[:min_len], r2[:min_len])
                    corr_values.append(c)
                    count += 1

    avg_corr = sum(corr_values) / len(corr_values) if corr_values else 0
    return {
        "avg_correlation": avg_corr,
        "n_pairs": count,
        "correlation_range": (
            f"{min(corr_values):.3f} to {max(corr_values):.3f}" if corr_values else "N/A"
        ),
    }


def main():
    print("=" * 70)
    print("CROSS-PAIR MOMENTUM RESEARCH")
    print(f"Date: {datetime.now(timezone.utc).isoformat()}")
    print(f"Pairs: {len(PAIRS)}, Interval: {INTERVAL}, Limit: {LIMIT} candles")
    print("=" * 70)

    # 1. Fetch data
    print("\n[1/5] Fetching 30-day 4h klines...")
    all_closes: dict[str, list[float]] = {}
    all_klines: dict[str, list[dict]] = {}
    for sym in PAIRS:
        try:
            klines = fetch_klines(sym)
            if klines:
                all_klines[sym] = klines
                all_closes[sym] = [k["close"] for k in klines]
                print(f"  {sym:8s}: {len(klines)} candles, "
                      f"${klines[-1]['close']:.4f}")
            time.sleep(0.15)  # Rate limit
        except Exception as e:
            print(f"  {sym:8s}: FAILED ({e})")

    n = min(len(c) for c in all_closes.values()) if all_closes else 0
    print(f"\n  {len(all_closes)} pairs loaded, aligned to {n} candles")
    if n < 50:
        print("❌ Not enough data, aborting")
        return

    # 2. Compute momentum
    print("\n[2/5] Computing 7-day and 14-day momentum...")
    all_mom7: dict[str, list[float | None]] = {}
    all_mom14: dict[str, list[float | None]] = {}
    # 7 days * 6 bars/day = 42 candles, 14 days = 84 candles
    for sym in all_closes:
        all_mom7[sym] = compute_momentum(all_closes[sym], 42)
        all_mom14[sym] = compute_momentum(all_closes[sym], 84)
        latest_m7 = all_mom7[sym][-1]
        latest_m14 = all_mom14[sym][-1]
        print(f"  {sym:8s}: 7d mom={latest_m7:+.2f}% 14d mom={latest_m14:+.2f}%")

    # 3. Rank and show top/bottom
    print("\n[3/5] Current momentum rankings (combined score = 0.6*7d + 0.4*14d):")
    ranked = rank_pairs(all_closes, all_mom7, all_mom14, n - 1, n)
    print(f"  {'#':>3} {'Pair':>8} {'Score':>8} {'7d Mom':>8} {'14d Mom':>8}")
    for i, (sym, score, m7, m14) in enumerate(ranked[:10]):
        marker = "🏆" if i < 3 else "  "
        print(f"  {marker}{i+1:>2} {sym:>8} {score:>+7.2f}% {m7:>+7.2f}% {m14:>+7.2f}%")
    print("  ...")
    for i, (sym, score, m7, m14) in enumerate(ranked[-5:]):
        print(f"     {len(ranked)-4+i:>2} {sym:>8} {score:>+7.2f}% {m7:>+7.2f}% {m14:>+7.2f}%")

    # 4. Simulate strategies
    print("\n[4/5] Running strategy simulations...")

    # Track top-3 history for correlation analysis
    warmup = 42
    top3_history = []
    for i in range(warmup, n):
        ranked = rank_pairs(all_closes, all_mom7, all_mom14, i, n)
        top3_history.append([sym for sym, *_ in ranked[:3]])

    # Strategy A: Top-3 basket
    print("  Simulating top-3 momentum basket...")
    basket = simulate_basket(all_closes, all_mom7, all_mom14, n, top_k=3)

    # Strategy B: Top-5 basket (extra comparison)
    print("  Simulating top-5 momentum basket...")
    basket5 = simulate_basket(all_closes, all_mom7, all_mom14, n, top_k=5)

    # Strategy C: Single best hindsight
    print("  Simulating hindsight best pair...")
    hindsight = simulate_hindsight_best(all_closes, n)

    # Strategy D: Buy-and-hold BTC
    print("  Simulating buy-and-hold BTC...")
    btc = simulate_buy_hold_btc(all_closes, n)

    # Strategy E: Current bot's single-coin rotation
    print("  Simulating current bot's single-coin rotation...")
    bot_single = simulate_single_coin_rotation(
        all_closes, all_mom7, all_mom14, n,
        lookback_candles=18, min_edge=8.0
    )

    # 5. Correlation analysis
    print("\n[5/5] Correlation analysis...")
    corr = compute_avg_pair_correlation(
        all_closes, all_mom7, top3_history, n, warmup
    )

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    fmt_ret = lambda x: f"{x:+.2f}%"
    fmt_dd = lambda x: f"{x:.2f}%"

    print(f"\n{'Strategy':<35} {'Return':>8} {'Max DD':>8} {'Sharpe':>8} {'Trades/wk':>10}")
    print("-" * 75)
    print(f"{'Hindsight best pair':<35} {fmt_ret(hindsight['total_return_pct']):>8} {'N/A':>8} {'N/A':>8} {'—':>10}")
    print(f"{'BTC buy-and-hold':<35} {fmt_ret(btc['total_return_pct']):>8} {fmt_dd(btc.get('max_dd_pct', 0)):>8} {btc.get('sharpe', 0):>8.2f} {'—':>10}")
    print(f"{'Top-3 basket (this research)':<35} {fmt_ret(basket['total_return_pct']):>8} {fmt_dd(basket['max_dd_pct']):>8} {basket['sharpe']:>8.2f} {basket['trades_per_week']:>10.1f}")
    print(f"{'Top-5 basket (extra)':<35} {fmt_ret(basket5['total_return_pct']):>8} {fmt_dd(basket5['max_dd_pct']):>8} {basket5['sharpe']:>8.2f} {basket5['trades_per_week']:>10.1f}")
    print(f"{'Bot single-coin rotation (curr)':<35} {fmt_ret(bot_single['total_return_pct']):>8} {fmt_dd(bot_single['max_dd_pct']):>8} {bot_single['sharpe']:>8.2f} {bot_single['trades_per_week']:>10.1f}")

    print(f"\n{'Fee impact (basket)':<35} {basket['fees_total_pct']:.2f}% of starting capital")
    print(f"{'Top-3 avg correlation':<35} {corr['avg_correlation']:.3f} ({corr['correlation_range']})")
    print(f"{'Bot win rate':<35} {bot_single['win_rate']:.1f}%")

    # ── Verdict ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("VERDICT")
    print("=" * 70)

    basket_beats_bot = basket["total_return_pct"] > bot_single["total_return_pct"]
    basket_beats_btc = basket["total_return_pct"] > btc["total_return_pct"]
    lower_dd = basket["max_dd_pct"] < bot_single["max_dd_pct"]
    lower_turnover = basket["trades_per_week"] < bot_single["trades_per_week"]

    if basket_beats_bot and basket_beats_btc:
        verdict = "✅ BASKET WINS"
    elif basket_beats_bot and not basket_beats_btc:
        verdict = "🟡 BASKET BEATS BOT, NOT BTC"
    elif not basket_beats_bot and basket_beats_btc:
        verdict = "🟡 MIXED SIGNALS"
    else:
        verdict = "❌ MOMENTUM IS BROKEN"

    print(f"\n  {verdict}")
    print()
    print(f"  Basket vs Bot single-coin:")
    print(f"    Return: {fmt_ret(basket['total_return_pct'])} vs {fmt_ret(bot_single['total_return_pct'])} "
          f"({'✅ basket better' if basket_beats_bot else '❌ bot better'})")
    print(f"    Max DD: {fmt_dd(basket['max_dd_pct'])} vs {fmt_dd(bot_single['max_dd_pct'])} "
          f"({'✅ basket lower' if lower_dd else '❌ bot lower'})")
    print(f"    Sharpe: {basket['sharpe']:.2f} vs {bot_single['sharpe']:.2f}")
    print(f"    Turnover: {basket['trades_per_week']:.1f}/wk vs {bot_single['trades_per_week']:.1f}/wk "
          f"({'✅ basket fewer' if lower_turnover else '❌ basket more'})")
    print()
    print(f"  Basket vs BTC buy-and-hold:")
    print(f"    Return: {fmt_ret(basket['total_return_pct'])} vs {fmt_ret(btc['total_return_pct'])} "
          f"({'✅ basket better' if basket_beats_btc else '❌ BTC better'})")
    print()
    print(f"  Diversification metrics:")
    print(f"    Top-3 avg correlation: {corr['avg_correlation']:.3f} "
          f"({'✅ low — diversification works' if corr['avg_correlation'] < 0.7 else '❌ high — correlated'})")
    print()
    print(f"  Key question answered:")
    if corr['avg_correlation'] < 0.7 and basket_beats_bot:
        print(f"    🟢 The problem IS the single-coin implementation, not momentum itself.")
        print(f"       A basket approach reduces whipsaw and diversifies risk.")
        print(f"       Average correlation {corr['avg_correlation']:.2f} is low enough for benefit.")
    elif basket_beats_bot:
        print(f"    🟡 Basket beats bot, but correlation is high ({corr['avg_correlation']:.2f}).")
        print(f"       Diversification benefit is limited. Gains may be from lower turnover.")
    elif basket_beats_btc:
        print(f"    🟡 Momentum works (beats BTC), but neither basket nor single-coin dominates.")
        print(f"       Consider hybrid: momentum basket + regime filter.")
    else:
        print(f"    🔴 Momentum itself appears broken in this period.")
        print(f"       Both implementations lose. Consider sitting in USDC.")
        print(f"       The market may be too choppy/mean-reverting for trend following.")

    # ── Save markdown report ────────────────────────────────────────────────
    md_lines = [
        "# Cross-Pair Momentum Analysis",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Pairs:** {len(all_closes)} USDC pairs",
        f"**Period:** 30 days, 4h candles ({n} bars)",
        "",
        "## Methodology",
        "",
        "- Fetched 4h klines for 25 USDC pairs from Binance public API",
        "- Computed 7-day (42-bar) and 14-day (84-bar) momentum as simple returns",
        "- Combined score: `0.6 * mom7 + 0.4 * mom14`",
        "",
        "### Strategies compared",
        "",
        "| Strategy | Description |",
        "|----------|-------------|",
        "| Top-3 Basket | Equal-weight top-3 momentum pairs, rebalance every 4h |",
        "| Top-5 Basket | Equal-weight top-5 momentum pairs, rebalance every 4h |",
        "| Bot single-coin | Current bot logic: best performer, rotate on >8% edge |",
        "| BTC buy-and-hold | Hold BTC for entire period |",
        "| Hindsight best | Best single pair chosen with perfect foresight |",
        "",
        "### Metrics",
        "",
        "- Fee: 0.1% per trade (Binance spot maker/taker)",
        "- Max drawdown: largest peak-to-trough decline",
        "- Sharpe: annualized (4h bars → 2190/year)",
        "- Turnover: number of trades per week",
        "",
        "## Results",
        "",
        "| Strategy | Return | Max DD | Sharpe | Trades/wk |",
        "|----------|--------|--------|--------|-----------|",
        f"| Hindsight best ({hindsight['symbol']}) | {hindsight['total_return_pct']:+.2f}% | — | — | — |",
        f"| BTC buy-and-hold | {btc['total_return_pct']:+.2f}% | {btc.get('max_dd_pct', 0):.2f}% | {btc.get('sharpe', 0):.2f} | — |",
        f"| **Top-3 basket** | **{basket['total_return_pct']:+.2f}%** | {basket['max_dd_pct']:.2f}% | {basket['sharpe']:.2f} | {basket['trades_per_week']:.1f} |",
        f"| Top-5 basket | {basket5['total_return_pct']:+.2f}% | {basket5['max_dd_pct']:.2f}% | {basket5['sharpe']:.2f} | {basket5['trades_per_week']:.1f} |",
        f"| Bot single-coin | {bot_single['total_return_pct']:+.2f}% | {bot_single['max_dd_pct']:.2f}% | {bot_single['sharpe']:.2f} | {bot_single['trades_per_week']:.1f} |",
        "",
        "## Diversification",
        "",
        f"- **Average correlation among top-3 picks:** {corr['avg_correlation']:.3f}",
        f"- **Range:** {corr['correlation_range']}",
        f"- **Interpretation:** {'Low — diversification works' if corr['avg_correlation'] < 0.7 else 'High — limited diversification benefit'}",
        "",
        "## Fee Impact",
        "",
        f"- Total fees paid (basket): {basket['fees_total_pct']:.2f}% of starting capital",
        f"- Total trades (basket): {basket['trades']}",
        "",
        "## Current Momentum Rankings (snapshot)",
        "",
        "| Rank | Pair | Score | 7d Mom | 14d Mom |",
        "|------|------|-------|--------|---------|",
    ]
    for i, (sym, score, m7, m14) in enumerate(ranked[:10]):
        md_lines.append(f"| {i+1} | {sym} | {score:+.2f}% | {m7:+.2f}% | {m14:+.2f}% |")
    md_lines.extend([
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        f"**Basket vs Bot single-coin:**",
        f"- Return: {basket['total_return_pct']:+.2f}% vs {bot_single['total_return_pct']:+.2f} ({'basket better' if basket_beats_bot else 'bot better'})",
        f"- Max DD: {basket['max_dd_pct']:.2f}% vs {bot_single['max_dd_pct']:.2f}% ({'basket lower' if lower_dd else 'bot lower'})",
        f"- Sharpe: {basket['sharpe']:.2f} vs {bot_single['sharpe']:.2f}",
        f"- Turnover: {basket['trades_per_week']:.1f}/wk vs {bot_single['trades_per_week']:.1f}/wk",
        "",
        f"**Key finding:**",
    ])
    if corr['avg_correlation'] < 0.7 and basket_beats_bot:
        md_lines.extend([
            "The problem IS the single-coin implementation, not momentum itself.",
            "A basket approach reduces whipsaw and diversifies risk.",
            f"Average correlation ({corr['avg_correlation']:.2f}) is low enough for diversification benefit.",
        ])
    elif basket_beats_bot:
        md_lines.extend([
            "Basket beats bot, but correlation is high. Diversification benefit is limited.",
            "Gains may primarily come from lower turnover rather than diversification.",
        ])
    elif basket_beats_btc:
        md_lines.extend([
            "Momentum works (beats BTC buy-and-hold), but neither implementation dominates.",
            "Consider hybrid approach: momentum basket + regime filter.",
        ])
    else:
        md_lines.extend([
            "Momentum itself appears broken in this period.",
            "Both implementations lose to buy-and-hold BTC.",
            "Market may be too choppy/mean-reverting for trend following.",
        ])

    md_lines.extend([
        "",
        "## Recommendation",
        "",
    ])

    if basket_beats_bot and basket_beats_btc:
        md_lines.extend([
            "1. **Implement top-3 basket rotation** as the primary strategy",
            "2. Keep regime filter — only trade in BULL regime (avoid BEAR/SIDEWAYS churn)",
            "3. Rebalance every 4h with 0.1% fee budget",
            "4. Monitor: if top-3 correlation exceeds 0.8, reduce to 2 pairs or go to cash",
        ])
    elif basket_beats_bot:
        md_lines.extend([
            "1. **Try basket approach** but expect modest improvement over single-coin",
            "2. The real alpha may come from regime filtering, not momentum itself",
            "3. Consider: momentum basket in BULL only, cash in SIDEWAYS/BEAR",
        ])
    else:
        md_lines.extend([
            "1. **Do NOT deploy momentum rotation at current $62 scale**",
            "2. Losses from fees + whipsaw are destructive at small capital",
            "3. Wait for clear trend regime (strong ADX) before deploying",
            "4. Consider: paper trade basket approach for 2-4 weeks before committing real capital",
        ])

    md_content = "\n".join(md_lines)

    # Save
    out_dir = Path(__file__).resolve().parents[1] / "docs" / "research"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cross-pair-momentum-analysis.md"
    out_path.write_text(md_content)
    print(f"\n📄 Report saved to: {out_path}")

    # Return the markdown for GitHub comment
    return md_content


if __name__ == "__main__":
    result = main()
    # Print the markdown for easy copy-paste
    print("\n" + "=" * 70)
    print("MARKDOWN FOR GITHUB COMMENT:")
    print("=" * 70)
    print(result)
