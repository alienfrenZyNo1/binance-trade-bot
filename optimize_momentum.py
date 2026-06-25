#!/usr/bin/env python3
"""Focused momentum strategy optimization + forward validation.

This module is intentionally import-safe: importing it must not fetch market data
or overwrite ``best_momentum.json``. Run it as a script to fetch Binance public
OHLCV data and execute the optimizer.
"""

import argparse
import itertools
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import importlib.util
import requests

from scripts.strategy_acceptance_gates import build_research_output

_REPO_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "indicators", _REPO_ROOT / "binance_trade_bot" / "indicators.py"
)
_indicators_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_indicators_mod)
_ema = _indicators_mod.compute_ema
_adx = _indicators_mod.compute_adx
_rsi = _indicators_mod.compute_rsi

BINANCE_API = "https://api.binance.com/api/v3"
COINS = [
    "SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
    "APT", "INJ", "TIA", "ENA", "PEPE", "JUP",
]
BRIDGE = "USDC"
REF_COIN = "SOL"
BULL, BEAR, SIDEWAYS = "bull", "bear", "sideways"
DEFAULT_INITIAL_BALANCE = 62.0
DEFAULT_STARTING_COIN = "TIA"
HOUR_MS = 3600 * 1000
DAY_MS = 86400 * 1000

# Backward-compatible globals. They remain empty until main()/load_market_data()
# populates them, so importing this module has no network or file side effects.
ohlcv = {}
btc_ohlcv = []
price_idx = {}
timestamps = []


def fetch_klines(symbol, interval="1h", days=180):
    """Fetch historical OHLCV klines from Binance public API."""
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
    """Parse Binance kline arrays into compact OHLCV dicts."""
    return [
        {
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw
    ]


def build_price_index(ohlcv_by_coin):
    return {coin: {row["ts"]: row["close"] for row in rows} for coin, rows in ohlcv_by_coin.items()}


def load_market_data(days=180, interval="1h", coins=None, bridge=BRIDGE):
    """Fetch and parse public Binance spot data for optimizer runs."""
    selected = list(coins or COINS)
    data = {}
    for coin in selected:
        raw = fetch_klines(f"{coin}{bridge}", interval, days)
        data[coin] = parse(raw)
        time.sleep(0.1)
    btc = parse(fetch_klines(f"BTC{bridge}", interval, days))
    return data, btc


def _timestamps_for(ohlcv_by_coin, ref_coin=REF_COIN):
    return [row["ts"] for row in ohlcv_by_coin.get(ref_coin, [])]


def _price_at_or_before(index, coin, ts, ohlcv_by_coin=None):
    exact = index.get(coin, {}).get(ts)
    if exact is not None:
        return exact
    if not ohlcv_by_coin:
        return None
    prior = [row["close"] for row in ohlcv_by_coin.get(coin, []) if row["ts"] <= ts]
    return prior[-1] if prior else None


def run_momrot(
    p,
    start_ts=None,
    end_ts=None,
    *,
    ohlcv_by_coin=None,
    btc_data=None,
    price_index=None,
    all_timestamps=None,
    initial_balance=DEFAULT_INITIAL_BALANCE,
    starting_coin=DEFAULT_STARTING_COIN,
):
    """Run momentum rotation backtest. Returns dict of results.

    Windowed runs deliberately size the opening position at the first processed
    candle >= ``start_ts`` and value final holdings at the last processed candle
    <= ``end_ts``. This prevents train/OOS leakage from the full dataset.
    """
    data = ohlcv_by_coin if ohlcv_by_coin is not None else ohlcv
    idx = price_index if price_index is not None else (price_idx or build_price_index(data))
    ts_list = list(all_timestamps if all_timestamps is not None else (timestamps or _timestamps_for(data)))

    if not data or not ts_list:
        raise ValueError("No OHLCV data supplied. Call load_market_data() or pass ohlcv_by_coin.")

    run_timestamps = [
        ts for ts in ts_list
        if (start_ts is None or ts >= start_ts) and (end_ts is None or ts <= end_ts)
    ]
    if not run_timestamps:
        return {
            "pnl": 0.0,
            "final": initial_balance,
            "trades": 0,
            "fees": 0.0,
            "max_dd": 0.0,
            "coin": starting_coin,
        }

    fee = p.get("fee_rate", 0.00075)
    slip = p.get("slippage", 0.0005)
    lookback = p.get("momentum_lookback", 24)
    min_edge = p.get("momentum_min_edge", 5.0)
    cooldown = p.get("cooldown_hours", 4)
    anti_churn = p.get("anti_churn_hours", 12)
    trail_pct = p.get("trailing_stop_pct", 15)
    use_regime = p.get("use_regime_filter", False)
    reentry_delay = p.get("reentry_delay_hours", 2)

    first_ts = run_timestamps[0]
    first_price = _price_at_or_before(idx, starting_coin, first_ts, data)
    if not first_price:
        raise ValueError(f"No starting price for {starting_coin} at/before {first_ts}")

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

    for ts in run_timestamps:
        last_processed_ts = ts

        # Portfolio value
        if coin == BRIDGE:
            value = balance + reserve
        else:
            pr = idx.get(coin, {}).get(ts)
            value = (balance * pr + reserve) if pr else reserve

        peak_val = max(peak_val, value)
        if value < peak_val:
            dd = ((peak_val - value) / peak_val) * 100
            max_dd = max(max_dd, dd)

        # Regime detection
        ref = [row for row in data.get(REF_COIN, []) if row["ts"] <= ts][-60:]
        if len(ref) >= 30:
            highs = [row["high"] for row in ref]
            lows = [row["low"] for row in ref]
            closes = [row["close"] for row in ref]
            adx, pdi, mdi = _adx(highs, lows, closes, 14)
            ema_long = _ema(closes, 50)
            current_price = closes[-1]
            if adx >= 25:
                if current_price > ema_long and pdi > mdi:
                    regime = BULL
                elif current_price < ema_long and mdi > pdi:
                    regime = BEAR
                else:
                    regime = SIDEWAYS
            else:
                regime = SIDEWAYS

        # Trailing stop
        if coin != BRIDGE and trail_pct < 100:
            pr = idx.get(coin, {}).get(ts)
            if pr is not None:
                if peak_price is None:
                    peak_price = pr
                if pr > peak_price:
                    peak_price = pr
                drop = ((peak_price - pr) / peak_price) * 100 if peak_price > 0 else 0
                if drop >= trail_pct:
                    coin_val = balance * pr
                    cost = coin_val * (fee + slip)
                    fees += cost
                    reserve += coin_val - cost
                    recent[coin] = ts
                    coin = BRIDGE
                    balance = reserve
                    reserve = 0.0
                    trades += 1
                    last_trade = ts
                    peak_price = None
                    continue

        # Reentry
        if coin == BRIDGE:
            if last_trade and (ts - last_trade) / HOUR_MS < reentry_delay:
                continue
            total = balance
            if total < 5:
                continue
            ts_lb = ts - lookback * HOUR_MS
            best_coin = None
            best_perf = -float("inf")
            for candidate in COINS:
                sold = recent.get(candidate)
                if sold and (ts - sold) / HOUR_MS < anti_churn:
                    continue
                current = idx.get(candidate, {}).get(ts)
                previous = idx.get(candidate, {}).get(ts_lb)
                if current and previous and previous > 0:
                    perf = (current / previous - 1) * 100
                    if perf > best_perf:
                        best_perf = perf
                        best_coin = candidate
            if best_coin:
                pr = idx.get(best_coin, {}).get(ts)
                if pr:
                    cost = total * (fee + slip)
                    fees += cost
                    balance = (total - cost) / pr
                    coin = best_coin
                    last_trade = ts
                    trades += 1
                    peak_price = pr
            continue

        # Cooldown
        if last_trade and (ts - last_trade) / HOUR_MS < cooldown:
            continue

        # Regime filter
        if use_regime and regime == BEAR:
            continue

        # Momentum check
        cur_pr = idx.get(coin, {}).get(ts)
        if cur_pr is None:
            continue
        ts_lb = ts - lookback * HOUR_MS
        cur_prev = idx.get(coin, {}).get(ts_lb)
        if not cur_prev:
            continue
        cur_perf = (cur_pr / cur_prev - 1) * 100

        best_coin = None
        best_score = -float("inf")
        for candidate in COINS:
            if candidate == coin:
                continue
            sold = recent.get(candidate)
            if sold and (ts - sold) / HOUR_MS < anti_churn:
                continue
            current = idx.get(candidate, {}).get(ts)
            previous = idx.get(candidate, {}).get(ts_lb)
            if not current or not previous or previous <= 0:
                continue
            perf = (current / previous - 1) * 100
            edge = perf - cur_perf
            if edge < min_edge:
                continue
            # Skip very overbought candidates.
            candles = [row for row in data.get(candidate, []) if row["ts"] <= ts][-16:]
            if len(candles) >= 16:
                closes = [row["close"] for row in candles]
                rsi = _rsi(closes, 14)
                if rsi and rsi > 75:
                    continue
            score = edge - (fee + slip) * 2 * 100
            if score > best_score:
                best_score = score
                best_coin = candidate

        if best_coin:
            tgt_pr = idx.get(best_coin, {}).get(ts)
            if tgt_pr is None:
                continue
            coin_val = balance * cur_pr
            sell_cost = coin_val * (fee + slip)
            bridge_value = coin_val - sell_cost + reserve
            buy_cost = bridge_value * (fee + slip)
            investable = bridge_value - buy_cost
            fees += sell_cost + buy_cost
            recent[coin] = ts
            balance = investable / tgt_pr
            reserve = 0.0
            coin = best_coin
            last_trade = ts
            trades += 1
            peak_price = tgt_pr

    # Final value is measured inside the requested run window, not at the full
    # dataset end.
    if coin == BRIDGE:
        final_value = balance + reserve
    else:
        final_price = idx.get(coin, {}).get(last_processed_ts, 0)
        final_value = balance * final_price + reserve

    return {
        "pnl": ((final_value / initial_balance) - 1) * 100,
        "final": final_value,
        "trades": trades,
        "fees": fees,
        "max_dd": max_dd,
        "coin": coin,
    }


def build_param_grid():
    grid = {
        "momentum_lookback": [12, 18, 24, 36, 48],
        "momentum_min_edge": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
        "cooldown_hours": [2, 4, 6, 8, 12],
        "anti_churn_hours": [3, 6, 12, 18, 24],
        "trailing_stop_pct": [10, 12, 15, 18, 20, 25, 100],
        "use_regime_filter": [True, False],
    }
    keys = list(grid.keys())
    return [dict(zip(keys, vals)) for vals in itertools.product(*[grid[k] for k in keys])]


def buy_hold_pnl(candles, start_ts=None, end_ts=None):
    """Return buy-and-hold P&L for candles inside a requested window."""
    window = [
        row for row in candles
        if (start_ts is None or row["ts"] >= start_ts) and (end_ts is None or row["ts"] <= end_ts)
    ]
    if len(window) < 2 or window[0]["close"] == 0:
        return 0.0
    return (window[-1]["close"] / window[0]["close"] - 1.0) * 100.0


def optimizer_assumptions(initial_balance, *, interval="1h", days=None, max_combos=None, seed=None):
    return {
        "initial_balance": initial_balance,
        "bridge": BRIDGE,
        "interval": interval,
        "days": days,
        "max_combos": max_combos,
        "seed": seed,
        "fee_rate": 0.00075,
        "slippage": 0.0005,
        "strategy": "momentum_rotation",
        "train_days": 120,
        "test_days": 60,
    }


def make_acceptance_record(rank, result, params, *, train_pnl, baseline_pnl, initial_balance):
    fee_pct = (result.get("fees", 0.0) / initial_balance * 100.0) if initial_balance else 0.0
    return {
        "name": f"momentum_rotation_rank_{rank}",
        "strategy": "momentum_rotation",
        "regime": "bull_sideways",
        "params": params,
        "train_pnl": train_pnl,
        "oos_pnl": result["pnl"],
        "pnl_pct": result["pnl"],
        "baseline_pnl": baseline_pnl,
        "baseline_pnl_pct": baseline_pnl,
        "final": result["final"],
        "trades": result["trades"],
        "trade_count": result["trades"],
        "fees": result["fees"],
        "total_fees": result["fees"],
        "fee_pct": fee_pct,
        "max_dd": result["max_dd"],
        "max_drawdown": result["max_dd"],
        "sharpe": result.get("sharpe", 0.0),
        "initial_balance": initial_balance,
    }


def run_optimization(
    ohlcv_by_coin,
    btc_data,
    *,
    max_combos=500,
    output_path: str | None = "best_momentum.json",
    seed=42,
    initial_balance=DEFAULT_INITIAL_BALANCE,
    days=None,
    interval="1h",
):
    idx = build_price_index(ohlcv_by_coin)
    ts_list = _timestamps_for(ohlcv_by_coin)
    if not ts_list:
        raise ValueError(f"No candles for reference coin {REF_COIN}")

    end_ts = ts_list[-1]
    test_start = end_ts - 60 * DAY_MS
    train_start = test_start - 120 * DAY_MS

    print(
        f"\nTrain: {datetime.fromtimestamp(train_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')} "
        f"→ {datetime.fromtimestamp(test_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')}"
    )
    print(
        f"Test:  {datetime.fromtimestamp(test_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')} "
        f"→ {datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}"
    )

    combos = build_param_grid()
    print(f"\nTotal combos: {len(combos)}")
    random.seed(seed)
    if len(combos) > max_combos:
        combos = random.sample(combos, max_combos)
    print(f"Sampling {len(combos)} for train phase...")

    train_results = []
    for i, combo in enumerate(combos):
        result = run_momrot(
            combo,
            start_ts=train_start,
            end_ts=test_start,
            ohlcv_by_coin=ohlcv_by_coin,
            btc_data=btc_data,
            price_index=idx,
            all_timestamps=ts_list,
            initial_balance=initial_balance,
        )
        result["params"] = combo
        train_results.append(result)
        if (i + 1) % 100 == 0:
            best = max(item["pnl"] for item in train_results)
            print(f"  {i+1}/{len(combos)}... best train: {best:+.1f}%")

    train_results.sort(key=lambda item: item["pnl"], reverse=True)

    print(f"\nTop 10 → OOS validation:")
    print(f"{'#':<3} {'Train':>8} {'OOS':>8} {'Trades':>7} {'MaxDD':>7} {'Look':>5} {'Edge':>5} {'Cool':>5} {'Churn':>5} {'Trail':>5} {'RegF':>5}")
    print("-" * 80)

    best_oos = None
    acceptance_records = []
    baseline_pnl = buy_hold_pnl(ohlcv_by_coin.get(DEFAULT_STARTING_COIN, []), test_start, end_ts)
    for i in range(min(10, len(train_results))):
        result = run_momrot(
            train_results[i]["params"],
            start_ts=test_start,
            end_ts=end_ts,
            ohlcv_by_coin=ohlcv_by_coin,
            btc_data=btc_data,
            price_index=idx,
            all_timestamps=ts_list,
            initial_balance=initial_balance,
        )
        params = train_results[i]["params"]
        print(
            f"{i+1:<3} {train_results[i]['pnl']:>+7.1f}% {result['pnl']:>+7.1f}% "
            f"{result['trades']:>7} {result['max_dd']:>6.1f}% "
            f"{params['momentum_lookback']:>5} {params['momentum_min_edge']:>5.1f} "
            f"{params['cooldown_hours']:>5} {params['anti_churn_hours']:>5} "
            f"{params['trailing_stop_pct']:>5} {str(params['use_regime_filter']):>5}"
        )
        result["params"] = params
        result["train_pnl"] = train_results[i]["pnl"]
        result["oos_pnl"] = result["pnl"]
        acceptance_records.append(
            make_acceptance_record(
                i + 1,
                result,
                params,
                train_pnl=train_results[i]["pnl"],
                baseline_pnl=baseline_pnl,
                initial_balance=initial_balance,
            )
        )
        if best_oos is None or result["pnl"] > best_oos["pnl"]:
            best_oos = result

    if best_oos is None:
        raise RuntimeError("No optimization results produced")

    print(f"\n{'=' * 60}")
    print("FULL RUN:")
    best_params = best_oos["params"]
    full = run_momrot(
        best_params,
        ohlcv_by_coin=ohlcv_by_coin,
        btc_data=btc_data,
        price_index=idx,
        all_timestamps=ts_list,
        initial_balance=initial_balance,
    )
    print(f"  P&L:    {full['pnl']:+.1f}% (${full['final']:.2f} from ${initial_balance:.0f})")
    print(f"  Trades: {full['trades']}")
    print(f"  Fees:   ${full['fees']:.2f}")
    print(f"  Max DD: {full['max_dd']:.1f}%")
    print(f"  Params: {json.dumps(best_params)}")

    print("\nBuy & Hold:")
    for coin in [starting for starting in [DEFAULT_STARTING_COIN, REF_COIN, "BTC"] if starting]:
        data = btc_data if coin == "BTC" else ohlcv_by_coin.get(coin, [])
        if data:
            print(f"  {coin}: {((data[-1]['close'] / data[0]['close']) - 1) * 100:+.1f}%")

    payload = {
        "params": best_params,
        "full_run": full,
        "train_pnl": best_oos["train_pnl"],
        "oos_pnl": best_oos["pnl"],
    }
    research_output = build_research_output(
        acceptance_records,
        ohlcv_by_coin=ohlcv_by_coin,
        interval=interval,
        bridge=BRIDGE,
        assumptions=optimizer_assumptions(
            initial_balance,
            interval=interval,
            days=days,
            max_combos=max_combos,
            seed=seed,
        ),
    )
    payload.update(research_output)
    if output_path:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nSaved to {output_path}")
    return payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=6, help="History length in 30-day months")
    parser.add_argument("--days", type=int, default=None, help="Override history length in days")
    parser.add_argument("--interval", default="1h", help="Binance kline interval")
    parser.add_argument("--max-combos", type=int, default=500, help="Maximum sampled parameter combos")
    parser.add_argument("--output", default="best_momentum.json", help="JSON output path")
    parser.add_argument("--no-output", action="store_true", help="Do not write a JSON result file")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for combo sampling")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    days = args.days if args.days is not None else args.months * 30
    output_path = None if args.no_output else args.output

    print(f"Fetching {days} days data...")
    global ohlcv, btc_ohlcv, price_idx, timestamps
    ohlcv, btc_ohlcv = load_market_data(days=days, interval=args.interval)
    price_idx = build_price_index(ohlcv)
    timestamps = _timestamps_for(ohlcv)
    print(f"Done: {len(timestamps)} {REF_COIN} candles")

    return run_optimization(
        ohlcv,
        btc_ohlcv,
        max_combos=args.max_combos,
        output_path=output_path,
        seed=args.seed,
        days=days,
        interval=args.interval,
    )


if __name__ == "__main__":
    main()
