#!/usr/bin/env python3
"""
Historical backtest for the adaptive trading strategy.
Fetches real Binance price data and simulates the mean-reversion strategy
with realistic fees.

Usage: python backtest_strategy.py [--months 6]
"""

import math
import time
import argparse
import requests
from datetime import datetime, timedelta, timezone


BINANCE_API = "https://api.binance.com/api/v3"
COINS = ["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
         "APT", "INJ", "TIA", "ENA", "PEPE", "JUP"]
BRIDGE = "USDC"
FEE_RATE = 0.00075  # 0.075% per side (BNB discount, taker)
MAKER_FEE = 0.00025  # 0.025% per side (maker)


def fetch_klines(symbol, interval="1h", days=180):
    """Fetch historical klines from Binance."""
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * 86400 * 1000
    url = f"{BINANCE_API}/klines"
    params = {"symbol": symbol, "interval": interval,
              "startTime": start, "endTime": end, "limit": 1000}
    all_data = []
    while start < end:
        params["startTime"] = start
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if not data:
            break
        all_data.extend(data)
        start = data[-1][0] + 1
        time.sleep(0.1)
    return all_data


def simulate_strategy(prices_by_coin, initial_balance=62.0,
                      scout_multiplier=6, min_profit=0.015,
                      cooldown_hours=2, fee_rate=FEE_RATE):
    """Simulate the mean-reversion strategy.

    prices_by_coin: {coin: [(timestamp, close_price), ...]}
    """
    # Build aligned price timeline
    all_timestamps = sorted(set(ts for prices in prices_by_coin.values()
                                for ts, _ in prices))

    # Index prices by timestamp
    price_lookup = {}
    for coin, prices in prices_by_coin.items():
        price_lookup[coin] = {ts: p for ts, p in prices}

    balance = initial_balance
    current_coin = "TIA"
    last_trade_time = None
    trade_count = 0
    fee_total = 0.0
    trade_log = []
    peak_value = initial_balance

    # Track ratio history for z-score (simplified EMA)
    ratio_history = {}  # (coin_a, coin_b) -> [ratios]

    for ts in all_timestamps:
        # Get current prices
        prices = {}
        for coin in COINS:
            if ts in price_lookup.get(coin, {}):
                prices[coin] = price_lookup[coin][ts]

        if current_coin not in prices:
            continue

        current_price = prices[current_coin]
        portfolio_value = balance if current_coin == BRIDGE else balance * current_price
        peak_value = max(peak_value, portfolio_value)

        # Check cooldown
        if last_trade_time and (ts - last_trade_time) < cooldown_hours * 3600000:
            continue

        # Evaluate each candidate
        best_candidate = None
        best_score = min_profit  # Must exceed minimum profit threshold

        for target_coin in COINS:
            if target_coin == current_coin:
                continue
            if target_coin not in prices:
                continue

            target_price = prices[target_coin]
            ratio = current_price / target_price

            # Update ratio history
            pair_key = (current_coin, target_coin)
            if pair_key not in ratio_history:
                ratio_history[pair_key] = []
            ratio_history[pair_key].append(ratio)

            # Need at least 20 samples for baseline
            history = ratio_history[pair_key]
            if len(history) < 20:
                continue

            # Compute EMA baseline
            period = min(20, len(history))
            alpha = 2.0 / (period + 1)
            ema = history[0]
            for h in history[1:]:
                ema = alpha * h + (1 - alpha) * ema

            # Score: how far is current ratio above EMA?
            pct_gain = (ratio / ema) - 1.0
            fee_hurdle = fee_rate * scout_multiplier
            score = pct_gain - fee_hurdle

            if score > best_score:
                best_score = score
                best_candidate = target_coin

        # Execute trade
        if best_candidate:
            # Sell current coin → buy target coin through bridge
            fee = portfolio_value * fee_rate * 2  # round trip fee
            fee_total += fee

            old_value = portfolio_value
            balance = portfolio_value - fee  # Bridge balance after fees

            # Buy target coin
            target_price = prices[best_candidate]
            balance = balance / target_price  # Convert to target coin units

            trade_log.append({
                "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "from": current_coin,
                "to": best_candidate,
                "value": old_value,
                "fee": fee,
                "score": best_score,
            })

            current_coin = best_candidate
            last_trade_time = ts
            trade_count += 1

    # Final portfolio value
    final_prices = {}
    for coin in COINS:
        if price_lookup.get(coin):
            last_ts = sorted(price_lookup[coin].keys())[-1]
            final_prices[coin] = price_lookup[coin][last_ts]

    if current_coin in final_prices:
        final_value = balance * final_prices[current_coin]
    elif current_coin == BRIDGE:
        final_value = balance
    else:
        final_value = balance

    return {
        "initial_balance": initial_balance,
        "final_value": final_value,
        "pnl_pct": ((final_value / initial_balance) - 1) * 100,
        "trade_count": trade_count,
        "total_fees": fee_total,
        "fee_pct_of_initial": (fee_total / initial_balance) * 100,
        "max_value": peak_value,
        "trade_log": trade_log,
    }


def run_backtest(months=6):
    print(f"=== FETCHING {months} MONTHS OF HISTORICAL DATA ===")
    prices_by_coin = {}
    for coin in COINS:
        symbol = f"{coin}{BRIDGE}"
        print(f"  Fetching {symbol}...", end=" ")
        try:
            klines = fetch_klines(symbol, "1h", months * 30)
            prices = [(k[0], float(k[4])) for k in klines]  # (timestamp, close)
            prices_by_coin[coin] = prices
            print(f"{len(prices)} candles")
        except Exception as e:
            print(f"FAILED: {e}")
        time.sleep(0.2)

    print(f"\n=== BACKTEST: {months} months, ${62} initial ===\n")

    # Test different parameter sets
    configs = [
        {"name": "Current (conservative)", "scout_multiplier": 6, "min_profit": 0.015, "cooldown_hours": 2},
        {"name": "Aggressive", "scout_multiplier": 3, "min_profit": 0.005, "cooldown_hours": 0.5},
        {"name": "Very conservative", "scout_multiplier": 10, "min_profit": 0.02, "cooldown_hours": 4},
        {"name": "Balanced", "scout_multiplier": 5, "min_profit": 0.01, "cooldown_hours": 1},
    ]

    results = []
    for cfg in configs:
        print(f"--- {cfg['name']} ---")
        result = simulate_strategy(
            prices_by_coin,
            scout_multiplier=cfg["scout_multiplier"],
            min_profit=cfg["min_profit"],
            cooldown_hours=cfg["cooldown_hours"],
        )
        result["config"] = cfg["name"]
        results.append(result)

        pnl = result["pnl_pct"]
        trades = result["trade_count"]
        fees = result["fee_pct_of_initial"]
        print(f"  P&L: {pnl:+.2f}% (${result['final_value']:.2f} from ${result['initial_balance']:.2f})")
        print(f"  Trades: {trades} ({trades / (months * 30):.1f}/day avg)")
        print(f"  Fees: ${result['total_fees']:.2f} ({fees:.2f}% of initial)")
        if trades > 0:
            avg_per_trade = pnl / trades
            print(f"  Avg P&L per trade: {avg_per_trade:+.3f}%")
        print()

    # Summary
    print("=== SUMMARY ===")
    print(f"{'Config':<25} {'P&L':>8} {'Trades':>8} {'Fees%':>8} {'$/Trade':>8}")
    print("-" * 60)
    for r in results:
        pnl = r["pnl_pct"]
        trades = r["trade_count"]
        fees = r["fee_pct_of_initial"]
        per_trade = pnl / trades if trades > 0 else 0
        print(f"{r['config']:<25} {pnl:>+7.2f}% {trades:>8} {fees:>7.2f}% {per_trade:>+7.3f}%")

    # Buy & hold comparison
    print("\n=== BUY & HOLD COMPARISON ===")
    if "TIA" in prices_by_coin and prices_by_coin["TIA"]:
        tia_prices = prices_by_coin["TIA"]
        bh_pnl = ((tia_prices[-1][1] / tia_prices[0][1]) - 1) * 100
        print(f"TIA buy & hold: {bh_pnl:+.2f}%")
    if "SOL" in prices_by_coin and prices_by_coin["SOL"]:
        sol_prices = prices_by_coin["SOL"]
        bh_pnl = ((sol_prices[-1][1] / sol_prices[0][1]) - 1) * 100
        print(f"SOL buy & hold: {bh_pnl:+.2f}%")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=6, help="Months of history to test")
    args = parser.parse_args()
    run_backtest(args.months)
