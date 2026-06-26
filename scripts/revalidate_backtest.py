#!/usr/bin/env python3
"""Revalidation backtest for issue #92.

Reproduces the original (flawed) methodology and then applies corrected
methodologies to determine whether the momentum-rotation edge survives:

  FIX-1  Next-bar-open execution  (eliminates same-bar-close lookahead)
  FIX-2  Rolling walk-forward      (multiple OOS windows, no train/OOS inversion)
  FIX-3  Realistic slippage 0.1%   (per side; audit said 0.05% understated)
  FIX-4  Benchmark comparison      (buy & hold + random rotation)
  FIX-5  Dynamic-fee/slip stress   (vary costs to find break-even)

Read-only: does NOT modify any live config, DB, or live trading behavior.
Outputs JSON to research_results/revalidation_results.json and prints a table.
"""

import argparse
import itertools
import json
import random
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "indicators", REPO_ROOT / "binance_trade_bot" / "indicators.py"
)
_ind = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ind)
_ema = _ind.compute_ema
_adx = _ind.compute_adx
_rsi = _ind.compute_rsi

BINANCE_API = "https://api.binance.com/api/v3"
COINS = [
    "SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
    "APT", "INJ", "TIA", "ENA", "PEPE", "JUP",
]
BRIDGE = "USDC"
REF_COIN = "SOL"
BULL, BEAR, SIDEWAYS = "bull", "bear", "sideways"
HOUR_MS = 3600 * 1000
DAY_MS = 86400 * 1000

# Default starting state mirrors live deployment.
DEFAULT_INITIAL_BALANCE = 62.0
DEFAULT_STARTING_COIN = "TIA"


# ---------------------------------------------------------------------------
# Data loading (mirrors optimize_momentum.fetch_klines but caches to a pickle)
# ---------------------------------------------------------------------------
def fetch_klines(symbol, interval="1h", days=180):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    all_data = []
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
        all_data.extend(data)
        cur = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.12)
    return all_data


def parse(raw):
    return [
        {
            "ts": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
        }
        for k in raw
    ]


def load_market_data(days=180, interval="1h", coins=None):
    selected = list(coins or COINS)
    data = {}
    for coin in selected:
        raw = fetch_klines(f"{coin}{BRIDGE}", interval, days)
        data[coin] = parse(raw)
        print(f"  {coin}{BRIDGE}: {len(data[coin])} candles", flush=True)
        time.sleep(0.1)
    return data


def build_indices(ohlcv_by_coin):
    """Build close and open price indices keyed by (coin -> ts -> price)."""
    close_idx = {c: {r["ts"]: r["close"] for r in rows} for c, rows in ohlcv_by_coin.items()}
    open_idx = {c: {r["ts"]: r["open"] for r in rows} for c, rows in ohlcv_by_coin.items()}
    ts_list = [r["ts"] for r in ohlcv_by_coin.get(REF_COIN, [])]
    return close_idx, open_idx, ts_list


# ---------------------------------------------------------------------------
# Corrected backtest engine
#
# Key fixes vs optimize_momentum.run_momrot:
#   - exec_mode: "same_bar_close" (legacy) | "next_bar_open" (corrected)
#     In next_bar_open mode, a signal generated at bar ts is executed at the
#     OPEN of bar ts+1. We track a pending rotation and resolve it on the
#     following bar.
# ---------------------------------------------------------------------------
def run_rotation(
    p,
    start_ts=None,
    end_ts=None,
    *,
    ohlcv_by_coin,
    close_idx,
    open_idx,
    ts_list,
    initial_balance=DEFAULT_INITIAL_BALANCE,
    starting_coin=DEFAULT_STARTING_COIN,
    exec_mode="next_bar_open",
    fee=0.00075,
    slip=0.0005,
):
    run_ts = [
        ts for ts in ts_list
        if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts)
    ]
    if not run_ts:
        return {"pnl": 0.0, "final": initial_balance, "trades": 0,
                "fees": 0.0, "max_dd": 0.0, "coin": starting_coin, "ret_series": []}

    lookback = p.get("momentum_lookback", 24)
    min_edge = p.get("momentum_min_edge", 5.0)
    cooldown = p.get("cooldown_hours", 4)
    anti_churn = p.get("anti_churn_hours", 12)
    trail_pct = p.get("trailing_stop_pct", 15)
    use_regime = p.get("use_regime_filter", False)
    reentry_delay = p.get("reentry_delay_hours", 2)
    cost = fee + slip  # per side

    first_ts = run_ts[0]
    first_price = close_idx.get(starting_coin, {}).get(first_ts)
    if not first_price:
        return {"pnl": 0.0, "final": initial_balance, "trades": 0,
                "fees": 0.0, "max_dd": 0.0, "coin": starting_coin, "ret_series": []}

    balance = initial_balance / first_price
    reserve = 0.0
    coin = starting_coin
    last_trade = None
    trades = 0
    fees = 0.0
    peak_val = initial_balance
    max_dd = 0.0
    peak_price = first_price
    recent = {}
    regime = SIDEWAYS
    last_processed_ts = first_ts
    prev_value = initial_balance
    ret_series = []
    pending_signal = None  # (action, target_coin) to execute on next bar

    def _price(idx, c, t):
        return idx.get(c, {}).get(t)

    def _value(ts):
        if coin == BRIDGE:
            return balance + reserve
        pr = _price(close_idx, coin, ts)
        return (balance * pr + reserve) if pr else reserve

    def _resolve_pending(ts):
        """Execute a pending rotation signal at THIS bar's open price."""
        nonlocal coin, balance, reserve, fees, trades, last_trade, peak_price, recent
        if not pending_signal:
            return
        action, target = pending_signal
        if action == "rotate":
            tgt_open = _price(open_idx, target, ts)
            if tgt_open is None or tgt_open <= 0:
                return
            if coin == BRIDGE:
                total = balance
                c = total * cost
                fees += c
                balance = (total - c) / tgt_open
                reserve = 0.0
            else:
                cur_close = _price(close_idx, coin, ts - HOUR_MS)
                cur_price = cur_close if cur_close else _price(open_idx, coin, ts)
                coin_val = balance * cur_price
                sell_c = coin_val * cost
                bridge_val = coin_val - sell_c + reserve
                buy_c = bridge_val * cost
                fees += sell_c + buy_c
                recent[coin] = ts
                balance = (bridge_val - buy_c) / tgt_open
                reserve = 0.0
            coin = target
            last_trade = ts
            trades += 1
            peak_price = tgt_open

    for ts in run_ts:
        last_processed_ts = ts
        # First, resolve any pending signal at this bar's OPEN.
        if pending_signal:
            _resolve_pending(ts)
            pending_signal = None

        value = _value(ts)
        if prev_value > 0:
            ret_series.append(value / prev_value - 1.0)
        prev_value = value
        peak_val = max(peak_val, value)
        if value < peak_val:
            dd = (peak_val - value) / peak_val * 100
            max_dd = max(max_dd, dd)

        # Regime detection (uses data <= ts, no leak)
        ref = [r for r in ohlcv_by_coin.get(REF_COIN, []) if r["ts"] <= ts][-60:]
        if len(ref) >= 30:
            highs = [r["high"] for r in ref]
            lows = [r["low"] for r in ref]
            closes = [r["close"] for r in ref]
            adx, pdi, mdi = _adx(highs, lows, closes, 14)
            ema_long = _ema(closes, 50)
            cur = closes[-1]
            if adx >= 25:
                if cur > ema_long and pdi > mdi:
                    regime = BULL
                elif cur < ema_long and mdi > pdi:
                    regime = BEAR
                else:
                    regime = SIDEWAYS
            else:
                regime = SIDEWAYS

        # --- Trailing stop: signal at close, execute at NEXT bar open ---
        if coin != BRIDGE and trail_pct < 100:
            pr = _price(close_idx, coin, ts)
            if pr is not None:
                if peak_price is None:
                    peak_price = pr
                if pr > peak_price:
                    peak_price = pr
                drop = (peak_price - pr) / peak_price * 100 if peak_price > 0 else 0
                if drop >= trail_pct:
                    pending_signal = ("rotate", BRIDGE)
                    peak_price = None
                    continue

        # --- Reentry from cash ---
        if coin == BRIDGE:
            if last_trade and (ts - last_trade) / HOUR_MS < reentry_delay:
                continue
            total = balance
            if total < 5:
                continue
            ts_lb = ts - lookback * HOUR_MS
            best_coin, best_perf = None, -float("inf")
            for cand in COINS:
                sold = recent.get(cand)
                if sold and (ts - sold) / HOUR_MS < anti_churn:
                    continue
                cur = _price(close_idx, cand, ts)
                prev = _price(close_idx, cand, ts_lb)
                if cur and prev and prev > 0:
                    perf = (cur / prev - 1) * 100
                    if perf > best_perf:
                        best_perf, best_coin = perf, cand
            if best_coin and best_perf > 0:
                pending_signal = ("rotate", best_coin)
            continue

        # --- Cooldown ---
        if last_trade and (ts - last_trade) / HOUR_MS < cooldown:
            continue
        if use_regime and regime == BEAR:
            continue

        # --- Momentum rotation ---
        cur_pr = _price(close_idx, coin, ts)
        if cur_pr is None:
            continue
        ts_lb = ts - lookback * HOUR_MS
        cur_prev = _price(close_idx, coin, ts_lb)
        if not cur_prev:
            continue
        cur_perf = (cur_pr / cur_prev - 1) * 100

        best_coin, best_score = None, -float("inf")
        for cand in COINS:
            if cand == coin:
                continue
            sold = recent.get(cand)
            if sold and (ts - sold) / HOUR_MS < anti_churn:
                continue
            cur = _price(close_idx, cand, ts)
            prev = _price(close_idx, cand, ts_lb)
            if not cur or not prev or prev <= 0:
                continue
            perf = (cur / prev - 1) * 100
            edge = perf - cur_perf
            if edge < min_edge:
                continue
            candles = [r for r in ohlcv_by_coin.get(cand, []) if r["ts"] <= ts][-16:]
            if len(candles) >= 16:
                closes = [r["close"] for r in candles]
                rsi = _rsi(closes, 14)
                if rsi and rsi > 75:
                    continue
            score = edge - cost * 2 * 100
            if score > best_score:
                best_score, best_coin = score, cand

        if best_coin:
            pending_signal = ("rotate", best_coin)

    if coin == BRIDGE:
        final_value = balance + reserve
    else:
        final_price = _price(close_idx, coin, last_processed_ts) or 0
        final_value = balance * final_price + reserve

    pnl = (final_value / initial_balance - 1) * 100
    sharpe = 0.0
    if len(ret_series) > 1:
        mean_r = statistics.mean(ret_series)
        std_r = statistics.pstdev(ret_series)
        if std_r > 0:
            sharpe = mean_r / std_r * (24 * 365) ** 0.5

    return {
        "pnl": pnl, "final": final_value, "trades": trades,
        "fees": fees, "max_dd": max_dd, "coin": coin,
        "sharpe": sharpe, "ret_series_len": len(ret_series),
    }


# ---------------------------------------------------------------------------
# Legacy same-bar-close run (to reproduce the +79% baseline faithfully)
# Implemented via run_rotation with exec_mode handled by a parallel helper.
# ---------------------------------------------------------------------------
def run_legacy_same_bar(
    p, start_ts=None, end_ts=None, *,
    ohlcv_by_coin, close_idx, open_idx, ts_list,
    initial_balance=DEFAULT_INITIAL_BALANCE,
    starting_coin=DEFAULT_STARTING_COIN, fee=0.00075, slip=0.0005,
):
    """Reproduce optimize_momentum.run_momrot (same-bar-close execution)."""
    run_ts = [ts for ts in ts_list
              if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts)]
    if not run_ts:
        return {"pnl": 0.0, "final": initial_balance, "trades": 0, "fees": 0.0,
                "max_dd": 0.0, "coin": starting_coin, "sharpe": 0.0}

    lookback = p.get("momentum_lookback", 24)
    min_edge = p.get("momentum_min_edge", 5.0)
    cooldown = p.get("cooldown_hours", 4)
    anti_churn = p.get("anti_churn_hours", 12)
    trail_pct = p.get("trailing_stop_pct", 15)
    use_regime = p.get("use_regime_filter", False)
    reentry_delay = p.get("reentry_delay_hours", 2)
    cost = fee + slip
    first_ts = run_ts[0]
    first_price = close_idx.get(starting_coin, {}).get(first_ts)
    if not first_price:
        return {"pnl": 0.0, "final": initial_balance, "trades": 0, "fees": 0.0,
                "max_dd": 0.0, "coin": starting_coin, "sharpe": 0.0}

    balance = initial_balance / first_price
    reserve = 0.0
    coin = starting_coin
    last_trade = None
    trades = 0
    fees = 0.0
    peak_val = initial_balance
    max_dd = 0.0
    peak_price = first_price
    recent = {}
    regime = SIDEWAYS
    last_processed_ts = first_ts
    ret_series = []
    prev_value = initial_balance

    def _value(ts):
        if coin == BRIDGE:
            return balance + reserve
        pr = close_idx.get(coin, {}).get(ts)
        return (balance * pr + reserve) if pr else reserve

    for ts in run_ts:
        last_processed_ts = ts
        value = _value(ts)
        if prev_value > 0:
            ret_series.append(value / prev_value - 1.0)
        prev_value = value
        peak_val = max(peak_val, value)
        if value < peak_val:
            max_dd = max(max_dd, (peak_val - value) / peak_val * 100)

        ref = [r for r in ohlcv_by_coin.get(REF_COIN, []) if r["ts"] <= ts][-60:]
        if len(ref) >= 30:
            highs = [r["high"] for r in ref]
            lows = [r["low"] for r in ref]
            closes = [r["close"] for r in ref]
            adx, pdi, mdi = _adx(highs, lows, closes, 14)
            ema_long = _ema(closes, 50)
            cur = closes[-1]
            if adx >= 25:
                if cur > ema_long and pdi > mdi:
                    regime = BULL
                elif cur < ema_long and mdi > pdi:
                    regime = BEAR
                else:
                    regime = SIDEWAYS
            else:
                regime = SIDEWAYS

        if coin != BRIDGE and trail_pct < 100:
            pr = close_idx.get(coin, {}).get(ts)
            if pr is not None:
                if peak_price is None or pr > peak_price:
                    peak_price = pr
                drop = (peak_price - pr) / peak_price * 100 if peak_price > 0 else 0
                if drop >= trail_pct:
                    coin_val = balance * pr
                    c = coin_val * cost
                    fees += c
                    reserve += coin_val - c
                    recent[coin] = ts
                    coin = BRIDGE
                    balance = reserve
                    reserve = 0.0
                    trades += 1
                    last_trade = ts
                    peak_price = None
                    continue

        if coin == BRIDGE:
            if last_trade and (ts - last_trade) / HOUR_MS < reentry_delay:
                continue
            total = balance
            if total < 5:
                continue
            ts_lb = ts - lookback * HOUR_MS
            best_coin, best_perf = None, -float("inf")
            for cand in COINS:
                sold = recent.get(cand)
                if sold and (ts - sold) / HOUR_MS < anti_churn:
                    continue
                cur = close_idx.get(cand, {}).get(ts)
                prev = close_idx.get(cand, {}).get(ts_lb)
                if cur and prev and prev > 0:
                    perf = (cur / prev - 1) * 100
                    if perf > best_perf:
                        best_perf, best_coin = perf, cand
            if best_coin:
                pr = close_idx.get(best_coin, {}).get(ts)
                if pr:
                    c = total * cost
                    fees += c
                    balance = (total - c) / pr
                    coin = best_coin
                    last_trade = ts
                    trades += 1
                    peak_price = pr
            continue

        if last_trade and (ts - last_trade) / HOUR_MS < cooldown:
            continue
        if use_regime and regime == BEAR:
            continue

        cur_pr = close_idx.get(coin, {}).get(ts)
        if cur_pr is None:
            continue
        ts_lb = ts - lookback * HOUR_MS
        cur_prev = close_idx.get(coin, {}).get(ts_lb)
        if not cur_prev:
            continue
        cur_perf = (cur_pr / cur_prev - 1) * 100

        best_coin, best_score = None, -float("inf")
        for cand in COINS:
            if cand == coin:
                continue
            sold = recent.get(cand)
            if sold and (ts - sold) / HOUR_MS < anti_churn:
                continue
            cur = close_idx.get(cand, {}).get(ts)
            prev = close_idx.get(cand, {}).get(ts_lb)
            if not cur or not prev or prev <= 0:
                continue
            perf = (cur / prev - 1) * 100
            edge = perf - cur_perf
            if edge < min_edge:
                continue
            candles = [r for r in ohlcv_by_coin.get(cand, []) if r["ts"] <= ts][-16:]
            if len(candles) >= 16:
                closes = [r["close"] for r in candles]
                rsi = _rsi(closes, 14)
                if rsi and rsi > 75:
                    continue
            score = edge - cost * 2 * 100
            if score > best_score:
                best_score, best_coin = score, cand

        if best_coin:
            tgt_pr = close_idx.get(best_coin, {}).get(ts)
            if tgt_pr is None:
                continue
            coin_val = balance * cur_pr
            sell_c = coin_val * cost
            bridge_val = coin_val - sell_c + reserve
            buy_c = bridge_val * cost
            fees += sell_c + buy_c
            recent[coin] = ts
            balance = (bridge_val - buy_c) / tgt_pr
            reserve = 0.0
            coin = best_coin
            last_trade = ts
            trades += 1
            peak_price = tgt_pr

    if coin == BRIDGE:
        final_value = balance + reserve
    else:
        final_price = close_idx.get(coin, {}).get(last_processed_ts, 0)
        final_value = balance * final_price + reserve

    pnl = (final_value / initial_balance - 1) * 100
    sharpe = 0.0
    if len(ret_series) > 1:
        mean_r = statistics.mean(ret_series)
        std_r = statistics.pstdev(ret_series)
        if std_r > 0:
            sharpe = mean_r / std_r * (24 * 365) ** 0.5
    return {"pnl": pnl, "final": final_value, "trades": trades, "fees": fees,
            "max_dd": max_dd, "coin": coin, "sharpe": sharpe}


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------
def buy_hold(candles, start_ts=None, end_ts=None):
    win = [r for r in candles
           if (start_ts is None or r["ts"] >= start_ts) and (end_ts is None or r["ts"] <= end_ts)]
    if len(win) < 2 or win[0]["close"] == 0:
        return 0.0
    return (win[-1]["close"] / win[0]["close"] - 1) * 100


def random_rotation_baseline(ohlcv_by_coin, close_idx, ts_list, *, start_ts, end_ts,
                             initial_balance=DEFAULT_INITIAL_BALANCE, n_runs=50, seed=0,
                             fee=0.00075, slip=0.0001, cooldown_h=24):
    """Random uniform coin rotation as a null hypothesis benchmark."""
    rng = random.Random(seed)
    run_ts = [ts for ts in ts_list if start_ts <= ts <= end_ts]
    if not run_ts:
        return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "runs": 0}
    cost = fee + slip
    pnls = []
    tradable = [c for c in COINS if c in close_idx]
    for run_i in range(n_runs):
        # hold each random coin for cooldown_h hours
        coin = tradable[rng.randrange(len(tradable))]
        bal = initial_balance / close_idx[coin].get(run_ts[0], 1.0)
        peak = initial_balance
        max_dd = 0.0
        last_switch = run_ts[0]
        trades = 0
        for ts in run_ts:
            pr = close_idx[coin].get(ts)
            if pr is None:
                continue
            val = bal * pr
            peak = max(peak, val)
            if val < peak:
                max_dd = max(max_dd, (peak - val) / peak * 100)
            if (ts - last_switch) / HOUR_MS >= cooldown_h and rng.random() < 0.3:
                newc = tradable[rng.randrange(len(tradable))]
                if newc != coin:
                    coin_val = bal * pr
                    c = coin_val * cost * 2  # sell+buy
                    new_pr = close_idx[newc].get(ts, pr)
                    bal = (coin_val - c) / new_pr
                    coin = newc
                    last_switch = ts
                    trades += 1
        final_pr = close_idx[coin].get(run_ts[-1], 1.0)
        final = bal * final_pr
        pnls.append((final / initial_balance - 1) * 100)
    return {"mean": statistics.mean(pnls), "median": statistics.median(pnls),
            "min": min(pnls), "max": max(pnls), "runs": n_runs,
            "std": statistics.pstdev(pnls) if len(pnls) > 1 else 0.0}


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
def rolling_walk_forward(ohlcv_by_coin, close_idx, open_idx, ts_list, *,
                         param_grid_sample, n_windows=5, oos_days=30,
                         train_days=120, exec_mode="next_bar_open",
                         fee=0.00075, slip=0.0005, seed=42):
    """Rolling walk-forward: each window optimizes on train, evaluates on OOS.

    Each OOS window is DISJOINT and forward in time from its train window.
    """
    end_ts = ts_list[-1]
    windows = []
    cur_end = end_ts
    for i in range(n_windows):
        oos_end = cur_end
        oos_start = oos_end - oos_days * DAY_MS
        train_end = oos_start
        train_start = train_end - train_days * DAY_MS
        windows.append({
            "idx": i, "train_start": train_start, "train_end": train_end,
            "oos_start": oos_start, "oos_end": oos_end,
        })
        cur_end = oos_start  # next older window ends where this OOS begins
        if train_start < ts_list[0]:
            break

    results = []
    for w in windows:
        # Optimize on train
        best = None
        for combo in param_grid_sample:
            r = run_rotation(
                combo, start_ts=w["train_start"], end_ts=w["train_end"],
                ohlcv_by_coin=ohlcv_by_coin, close_idx=close_idx, open_idx=open_idx,
                ts_list=ts_list, exec_mode=exec_mode, fee=fee, slip=slip,
            )
            if best is None or r["pnl"] > best["pnl"]:
                best = {**r, "params": combo}
        # Evaluate best on OOS
        oos = run_rotation(
            best["params"], start_ts=w["oos_start"], end_ts=w["oos_end"],
            ohlcv_by_coin=ohlcv_by_coin, close_idx=close_idx, open_idx=open_idx,
            ts_list=ts_list, exec_mode=exec_mode, fee=fee, slip=slip,
        )
        # Benchmark: buy & hold TIA over OOS window
        bh_tia = buy_hold(ohlcv_by_coin.get(DEFAULT_STARTING_COIN, []),
                          w["oos_start"], w["oos_end"])
        bh_sol = buy_hold(ohlcv_by_coin.get(REF_COIN, []),
                          w["oos_start"], w["oos_end"])
        results.append({
            "window": w["idx"],
            "train_start": datetime.fromtimestamp(w["train_start"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "oos_start": datetime.fromtimestamp(w["oos_start"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "oos_end": datetime.fromtimestamp(w["oos_end"]/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "train_pnl": best["pnl"],
            "oos_pnl": oos["pnl"],
            "oos_trades": oos["trades"],
            "oos_max_dd": oos["max_dd"],
            "bh_tia": bh_tia,
            "bh_sol": bh_sol,
            "beat_tia": oos["pnl"] > bh_tia,
            "beat_sol": oos["pnl"] > bh_sol,
            "params": best["params"],
        })
    return results


def build_param_grid_sample(max_combos=400, seed=42):
    grid = {
        "momentum_lookback": [12, 18, 24, 36, 48],
        "momentum_min_edge": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
        "cooldown_hours": [2, 4, 6, 8, 12],
        "anti_churn_hours": [3, 6, 12, 18, 24],
        "trailing_stop_pct": [10, 12, 15, 18, 20, 25, 100],
        "use_regime_filter": [True, False],
    }
    keys = list(grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]
    random.Random(seed).shuffle(combos)
    return combos[:max_combos]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cache", default="research_results/reval_data.json")
    parser.add_argument("--out", default="research_results/revalidation_results.json")
    parser.add_argument("--max-combos", type=int, default=400)
    parser.add_argument("--windows", type=int, default=5)
    args = parser.parse_args()

    cache = Path(args.cache)
    if cache.exists():
        print(f"Loading cached data from {cache}")
        with open(cache) as fh:
            bundle = json.load(fh)
        ohlcv = bundle["ohlcv"]
    else:
        print(f"Fetching {args.days} days from Binance API...")
        ohlcv = load_market_data(days=args.days)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w") as fh:
            json.dump({"ohlcv": ohlcv}, fh)
        print(f"Cached to {cache}")

    close_idx, open_idx, ts_list = build_indices(ohlcv)
    print(f"\n{len(ts_list)} {REF_COIN} candles: "
          f"{datetime.fromtimestamp(ts_list[0]/1000, tz=timezone.utc).date()} → "
          f"{datetime.fromtimestamp(ts_list[-1]/1000, tz=timezone.utc).date()}")

    end_ts = ts_list[-1]
    test_start = end_ts - 60 * DAY_MS
    train_start = test_start - 120 * DAY_MS
    grid = build_param_grid_sample(max_combos=args.max_combos)

    # The deployed params from best_momentum.json
    deployed = {
        "momentum_lookback": 18, "momentum_min_edge": 8.0, "cooldown_hours": 2,
        "anti_churn_hours": 24, "trailing_stop_pct": 15, "use_regime_filter": True,
    }

    report = {"date": datetime.now(timezone.utc).isoformat(), "deployed_params": deployed}

    # =====================================================================
    # SECTION 1 — Reproduce original +79% (legacy same-bar-close, 0.05% slip)
    # =====================================================================
    print("\n" + "=" * 72)
    print("SECTION 1: Reproduce original baseline (same-bar-close, slip=0.05%)")
    print("=" * 72)
    legacy_full = run_legacy_same_bar(
        deployed, ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, fee=0.00075, slip=0.0005,
    )
    legacy_train = run_legacy_same_bar(
        deployed, start_ts=train_start, end_ts=test_start,
        ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, fee=0.00075, slip=0.0005,
    )
    legacy_oos = run_legacy_same_bar(
        deployed, start_ts=test_start, end_ts=end_ts,
        ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, fee=0.00075, slip=0.0005,
    )
    print(f"  Full 6mo: P&L {legacy_full['pnl']:+.2f}% | trades {legacy_full['trades']} "
          f"| maxDD {legacy_full['max_dd']:.1f}% | sharpe {legacy_full['sharpe']:.2f}")
    print(f"  Train:    P&L {legacy_train['pnl']:+.2f}%")
    print(f"  OOS:      P&L {legacy_oos['pnl']:+.2f}%")
    report["s1_legacy_baseline"] = {
        "full_pnl": legacy_full["pnl"], "full_trades": legacy_full["trades"],
        "full_max_dd": legacy_full["max_dd"], "full_sharpe": legacy_full["sharpe"],
        "train_pnl": legacy_train["pnl"], "oos_pnl": legacy_oos["pnl"],
        "slip": 0.0005,
    }

    # =====================================================================
    # SECTION 2 — FIX-1: next-bar-open execution (slip 0.05% to isolate)
    # =====================================================================
    print("\n" + "=" * 72)
    print("SECTION 2: FIX-1 next-bar-open execution (slip=0.05%, isolate effect)")
    print("=" * 72)
    nbo_full = run_rotation(
        deployed, ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.0005,
    )
    nbo_train = run_rotation(
        deployed, start_ts=train_start, end_ts=test_start,
        ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.0005,
    )
    nbo_oos = run_rotation(
        deployed, start_ts=test_start, end_ts=end_ts,
        ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.0005,
    )
    print(f"  Full 6mo: P&L {nbo_full['pnl']:+.2f}% | trades {nbo_full['trades']} "
          f"| maxDD {nbo_full['max_dd']:.1f}% | sharpe {nbo_full['sharpe']:.2f}")
    print(f"  Train:    P&L {nbo_train['pnl']:+.2f}%")
    print(f"  OOS:      P&L {nbo_oos['pnl']:+.2f}%")
    print(f"  Δ vs legacy full: {nbo_full['pnl'] - legacy_full['pnl']:+.2f} pp")
    report["s2_next_bar_open"] = {
        "full_pnl": nbo_full["pnl"], "full_trades": nbo_full["trades"],
        "full_max_dd": nbo_full["max_dd"], "full_sharpe": nbo_full["sharpe"],
        "train_pnl": nbo_train["pnl"], "oos_pnl": nbo_oos["pnl"],
        "delta_vs_legacy": nbo_full["pnl"] - legacy_full["pnl"],
    }

    # =====================================================================
    # SECTION 3 — FIX-3: realistic slippage 0.1% per side (next-bar-open)
    # =====================================================================
    print("\n" + "=" * 72)
    print("SECTION 3: FIX-3 realistic slip=0.1% per side (next-bar-open)")
    print("=" * 72)
    real_full = run_rotation(
        deployed, ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.001,
    )
    real_oos = run_rotation(
        deployed, start_ts=test_start, end_ts=end_ts,
        ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
        ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=0.001,
    )
    print(f"  Full 6mo: P&L {real_full['pnl']:+.2f}% | trades {real_full['trades']} "
          f"| maxDD {real_full['max_dd']:.1f}% | sharpe {real_full['sharpe']:.2f}")
    print(f"  OOS:      P&L {real_oos['pnl']:+.2f}%")
    report["s3_realistic_costs"] = {
        "full_pnl": real_full["pnl"], "full_trades": real_full["trades"],
        "full_max_dd": real_full["max_dd"], "full_sharpe": real_full["sharpe"],
        "oos_pnl": real_oos["pnl"], "slip": 0.001,
    }

    # =====================================================================
    # SECTION 4 — Cost stress (find break-even slippage)
    # =====================================================================
    print("\n" + "=" * 72)
    print("SECTION 4: Cost stress — break-even slippage sweep")
    print("=" * 72)
    sweep = []
    for slip in [0.0, 0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005]:
        r = run_rotation(
            deployed, ohlcv_by_coin=ohlcv, close_idx=close_idx, open_idx=open_idx,
            ts_list=ts_list, exec_mode="next_bar_open", fee=0.00075, slip=slip,
        )
        sweep.append({"slip": slip, "pnl": r["pnl"], "trades": r["trades"]})
        print(f"  slip={slip*100:.2f}%: P&L {r['pnl']:+.2f}% ({r['trades']} trades)")
    report["s4_cost_sweep"] = sweep

    # =====================================================================
    # SECTION 5 — FIX-2: Rolling walk-forward (next-bar-open, slip 0.1%)
    # =====================================================================
    print("\n" + "=" * 72)
    print(f"SECTION 5: FIX-2 rolling walk-forward ({args.windows} windows, "
          "next-bar-open, slip=0.1%)")
    print("=" * 72)
    wf = rolling_walk_forward(
        ohlcv, close_idx, open_idx, ts_list, param_grid_sample=grid,
        n_windows=args.windows, oos_days=30, train_days=120,
        exec_mode="next_bar_open", fee=0.00075, slip=0.001,
    )
    print(f"\n{'W':>2} {'OOS start':>12} {'Train':>8} {'OOS':>8} {'B&H TIA':>8} "
          f"{'B&H SOL':>8} {'beatTIA':>8} {'beatSOL':>8} {'OOStr':>6} {'maxDD':>6}")
    print("-" * 90)
    oos_pnls = []
    beat_tia = 0
    beat_sol = 0
    for w in wf:
        oos_pnls.append(w["oos_pnl"])
        if w["beat_tia"]:
            beat_tia += 1
        if w["beat_sol"]:
            beat_sol += 1
        print(f"{w['window']:>2} {w['oos_start']:>12} {w['train_pnl']:>+7.1f}% "
              f"{w['oos_pnl']:>+7.1f}% {w['bh_tia']:>+7.1f}% {w['bh_sol']:>+7.1f}% "
              f"{str(w['beat_tia']):>8} {str(w['beat_sol']):>8} "
              f"{w['oos_trades']:>6} {w['oos_max_dd']:>5.1f}%")
    print("-" * 90)
    wf_mean = statistics.mean(oos_pnls) if oos_pnls else 0.0
    wf_median = statistics.median(oos_pnls) if oos_pnls else 0.0
    n_win = len(oos_pnls)
    n_pos = sum(1 for p in oos_pnls if p > 0)
    print(f"\n  OOS mean: {wf_mean:+.2f}% | median: {wf_median:+.2f}% | "
          f"positive windows: {n_pos}/{n_win} | beat TIA: {beat_tia}/{n_win} | "
          f"beat SOL: {beat_sol}/{n_win}")
    report["s5_walk_forward"] = {
        "windows": wf,
        "oos_mean": wf_mean, "oos_median": wf_median,
        "positive_windows": n_pos, "total_windows": n_win,
        "beat_tia_count": beat_tia, "beat_sol_count": beat_sol,
    }

    # =====================================================================
    # SECTION 6 — Benchmarks over full period
    # =====================================================================
    print("\n" + "=" * 72)
    print("SECTION 6: Benchmarks (full 6 months)")
    print("=" * 72)
    bh = {}
    for c in [DEFAULT_STARTING_COIN, REF_COIN, "BTC"] + COINS:
        if c in ohlcv:
            bh[c] = buy_hold(ohlcv[c])
    bh_top = sorted(bh.items(), key=lambda x: -x[1])[:5]
    print("  Buy & Hold top performers:")
    for c, p in bh_top:
        print(f"    {c}: {p:+.2f}%")
    print(f"  Equal-weight 15-coin basket (avg): {statistics.mean(bh[c] for c in COINS if c in bh):+.2f}%")
    rand = random_rotation_baseline(
        ohlcv, close_idx, ts_list, start_ts=ts_list[0], end_ts=end_ts,
        n_runs=50, fee=0.00075, slip=0.001,
    )
    print(f"  Random rotation (50 runs): mean {rand['mean']:+.2f}% "
          f"| median {rand['median']:+.2f}% | range [{rand['min']:+.1f}%, {rand['max']:+.1f}%]")
    report["s6_benchmarks"] = {
        "bh_tia": bh.get(DEFAULT_STARTING_COIN, 0.0),
        "bh_sol": bh.get(REF_COIN, 0.0),
        "bh_btc": bh.get("BTC", 0.0),
        "equal_weight_avg": statistics.mean(bh[c] for c in COINS if c in bh),
        "random_rotation": rand,
    }

    # =====================================================================
    # VERDICT
    # =====================================================================
    print("\n" + "=" * 72)
    print("VERDICT SUMMARY")
    print("=" * 72)
    verdict = {
        "legacy_full_pnl": legacy_full["pnl"],
        "corrected_full_pnl_nbo_010": real_full["pnl"],
        "walk_forward_oos_mean": wf_mean,
        "walk_forward_positive_rate": f"{n_pos}/{n_win}",
        "beat_tia_rate": f"{beat_tia}/{n_win}",
        "beat_sol_rate": f"{beat_sol}/{n_win}",
        "random_rotation_mean": rand["mean"],
    }
    edge_survives = (
        real_full["pnl"] > 0
        and n_pos >= n_win / 2
        and wf_mean > report["s6_benchmarks"]["equal_weight_avg"]
        and wf_mean > rand["mean"]
    )
    verdict["edge_survives_corrected_methodology"] = bool(edge_survives)
    for k, v in verdict.items():
        print(f"  {k}: {v}")
    report["verdict"] = verdict

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
