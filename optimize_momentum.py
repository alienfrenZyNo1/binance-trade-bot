#!/usr/bin/env python3
"""Focused momentum strategy optimization + forward validation."""
import sys, math, time, json, random, itertools, requests
from datetime import datetime, timezone
from collections import defaultdict
import importlib.util

_spec = importlib.util.spec_from_file_location("indicators", "REDACTED_PATHbinance_trade_bot/indicators.py")
_indicators_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_indicators_mod)
_ema = _indicators_mod.compute_ema
_adx = _indicators_mod.compute_adx
_rsi = _indicators_mod.compute_rsi

BINANCE_API = "https://api.binance.com/api/v3"
COINS = ["SOL","SUI","XRP","ADA","DOGE","NEAR","LINK","AAVE","AVAX","APT","INJ","TIA","ENA","PEPE","JUP"]
BRIDGE = "USDC"
REF_COIN = "SOL"
BULL, BEAR, SIDEWAYS = "bull","bear","sideways"

def fetch_klines(symbol, interval="1h", days=180):
    end_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    start_ms = end_ms - days*86400*1000
    all_data = []
    cur = start_ms
    while cur < end_ms:
        resp = requests.get(f"{BINANCE_API}/klines", params={"symbol":symbol,"interval":interval,"startTime":cur,"endTime":end_ms,"limit":1000}, timeout=30)
        data = resp.json()
        if not data: break
        all_data.extend(data)
        cur = data[-1][0]+1
        if len(data)<1000: break
        time.sleep(0.12)
    return all_data

def parse(raw):
    return [{"ts":k[0],"open":float(k[1]),"high":float(k[2]),"low":float(k[3]),"close":float(k[4]),"volume":float(k[5])} for k in raw]

# Fetch
print("Fetching 6 months data...")
ohlcv = {}
for coin in COINS:
    raw = fetch_klines(f"{coin}{BRIDGE}", "1h", 180)
    ohlcv[coin] = parse(raw)
    time.sleep(0.1)
btc_ohlcv = parse(fetch_klines(f"BTC{BRIDGE}", "1h", 180))
print(f"Done: {len(ohlcv[REF_COIN])} candles each")

# Pre-build indexes
price_idx = {c:{x["ts"]:x["close"] for x in ohlcv[c]} for c in COINS}
timestamps = [x["ts"] for x in ohlcv[REF_COIN]]

def run_momrot(p, start_ts=None, end_ts=None):
    """Run momentum rotation backtest. Returns dict of results."""
    fee = 0.00075
    slip = 0.0005
    lookback = p.get("momentum_lookback", 24)
    min_edge = p.get("momentum_min_edge", 5.0)
    cooldown = p.get("cooldown_hours", 4)
    anti_churn = p.get("anti_churn_hours", 12)
    trail_pct = p.get("trailing_stop_pct", 15)
    use_regime = p.get("use_regime_filter", False)
    reentry_delay = p.get("reentry_delay_hours", 2)

    first_price = ohlcv["TIA"][0]["close"]
    balance = 62.0 / first_price
    reserve = 0.0
    coin = "TIA"
    last_trade = None
    trades = 0
    fees = 0.0
    peak_val = 62.0
    max_dd = 0.0
    peak_price = None
    recent = {}
    regime = SIDEWAYS

    for ts in timestamps:
        if start_ts and ts < start_ts: continue
        if end_ts and ts > end_ts: break

        # Portfolio value
        if coin == BRIDGE:
            v = balance + reserve
        else:
            pr = price_idx.get(coin, {}).get(ts)
            v = (balance * pr + reserve) if pr else reserve

        peak_val = max(peak_val, v)
        if v < peak_val:
            dd = ((peak_val - v) / peak_val) * 100
            max_dd = max(max_dd, dd)

        # Regime detection
        ref = [c for c in ohlcv[REF_COIN] if c["ts"] <= ts][-60:]
        if len(ref) >= 30:
            highs = [c["high"] for c in ref]
            lows = [c["low"] for c in ref]
            closes = [c["close"] for c in ref]
            adx, pdi, mdi = _adx(highs, lows, closes, 14)
            es = _ema(closes, 20)
            el = _ema(closes, 50)
            cp = closes[-1]
            if adx >= 25:
                if cp > el and pdi > mdi: regime = BULL
                elif cp < el and mdi > pdi: regime = BEAR
                else: regime = SIDEWAYS
            else: regime = SIDEWAYS

        # Trailing stop
        if coin != BRIDGE and trail_pct < 100:
            pr = price_idx.get(coin, {}).get(ts)
            if pr is not None:
                if peak_price is None: peak_price = pr
                if pr > peak_price: peak_price = pr
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
            if last_trade and (ts - last_trade) / (3600*1000) < reentry_delay:
                continue
            total = balance
            if total < 5: continue
            ts_lb = ts - lookback * 3600 * 1000
            best_c = None; best_p = -float("inf")
            for c in COINS:
                sold = recent.get(c)
                if sold and (ts - sold) / (3600*1000) < anti_churn: continue
                cn = price_idx.get(c, {}).get(ts)
                cp2 = price_idx.get(c, {}).get(ts_lb)
                if cn and cp2 and cp2 > 0:
                    perf = (cn / cp2 - 1) * 100
                    if perf > best_p: best_p = perf; best_c = c
            if best_c:
                pr = price_idx.get(best_c, {}).get(ts)
                if pr:
                    cost = total * (fee + slip)
                    fees += cost
                    balance = (total - cost) / pr
                    coin = best_c
                    last_trade = ts
                    trades += 1
                    peak_price = pr
            continue

        # Cooldown
        if last_trade and (ts - last_trade) / (3600*1000) < cooldown:
            continue

        # Regime filter
        if use_regime and regime == BEAR:
            continue

        # Momentum check
        cur_pr = price_idx.get(coin, {}).get(ts)
        if cur_pr is None: continue
        ts_lb = ts - lookback * 3600 * 1000
        cur_prev = price_idx.get(coin, {}).get(ts_lb)
        if not cur_prev: continue
        cur_perf = (cur_pr / cur_prev - 1) * 100

        best_c = None; best_s = -float("inf")
        for c in COINS:
            if c == coin: continue
            sold = recent.get(c)
            if sold and (ts - sold) / (3600*1000) < anti_churn: continue
            cn = price_idx.get(c, {}).get(ts)
            cp2 = price_idx.get(c, {}).get(ts_lb)
            if not cn or not cp2 or cp2 <= 0: continue
            perf = (cn / cp2 - 1) * 100
            edge = perf - cur_perf
            if edge < min_edge: continue
            # Skip very overbought
            candles = [x for x in ohlcv[c] if x["ts"] <= ts][-16:]
            if len(candles) >= 16:
                closes = [x["close"] for x in candles]
                r = _rsi(closes, 14)
                if r and r > 75: continue
            score = edge - (fee + slip) * 2 * 100
            if score > best_s:
                best_s = score; best_c = c

        if best_c:
            tgt_pr = price_idx.get(best_c, {}).get(ts)
            if tgt_pr is None: continue
            coin_val = balance * cur_pr
            total_val = coin_val + reserve
            sell_cost = coin_val * (fee + slip)
            bridge = coin_val - sell_cost + reserve
            buy_cost = bridge * (fee + slip)
            investable = bridge - buy_cost
            fees += sell_cost + buy_cost
            recent[coin] = ts
            balance = investable / tgt_pr
            reserve = 0.0
            coin = best_c
            last_trade = ts
            trades += 1
            peak_price = tgt_pr

    # Final value
    last_ts = timestamps[-1]
    if coin == BRIDGE:
        fv = balance
    else:
        fp = price_idx.get(coin, {}).get(last_ts, 0)
        fv = balance * fp + reserve

    return {"pnl": ((fv/62.0)-1)*100, "final": fv, "trades": trades, "fees": fees, "max_dd": max_dd, "coin": coin}

# ── Grid search ──
end_ts = timestamps[-1]
test_start = end_ts - 60*86400*1000
train_start = test_start - 120*86400*1000

print(f"\nTrain: {datetime.fromtimestamp(train_start/1000,tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(test_start/1000,tz=timezone.utc).strftime('%Y-%m-%d')}")
print(f"Test:  {datetime.fromtimestamp(test_start/1000,tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(end_ts/1000,tz=timezone.utc).strftime('%Y-%m-%d')}")

grid = {
    "momentum_lookback": [12, 18, 24, 36, 48],
    "momentum_min_edge": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
    "cooldown_hours": [2, 4, 6, 8, 12],
    "anti_churn_hours": [3, 6, 12, 18, 24],
    "trailing_stop_pct": [10, 12, 15, 18, 20, 25, 100],
    "use_regime_filter": [True, False],
}

combos = []
keys = list(grid.keys())
for vals in itertools.product(*[grid[k] for k in keys]):
    combos.append(dict(zip(keys, vals)))

print(f"\nTotal combos: {len(combos)}")
random.seed(42)
if len(combos) > 500:
    combos = random.sample(combos, 500)
print(f"Sampling {len(combos)} for train phase...")

train_results = []
for i, combo in enumerate(combos):
    r = run_momrot(combo, start_ts=train_start, end_ts=test_start)
    r["params"] = combo
    train_results.append(r)
    if (i+1) % 100 == 0:
        best = max(x["pnl"] for x in train_results)
        print(f"  {i+1}/{len(combos)}... best train: {best:+.1f}%")

train_results.sort(key=lambda x: x["pnl"], reverse=True)

# OOS test
print(f"\nTop 10 → OOS validation:")
print(f"{'#':<3} {'Train':>8} {'OOS':>8} {'Trades':>7} {'MaxDD':>7} {'Look':>5} {'Edge':>5} {'Cool':>5} {'Churn':>5} {'Trail':>5} {'RegF':>5}")
print("-"*80)

best_oos = None
for i in range(min(10, len(train_results))):
    r = run_momrot(train_results[i]["params"], start_ts=test_start, end_ts=end_ts)
    p = train_results[i]["params"]
    print(f"{i+1:<3} {train_results[i]['pnl']:>+7.1f}% {r['pnl']:>+7.1f}% {r['trades']:>7} {r['max_dd']:>6.1f}% {p['momentum_lookback']:>5} {p['momentum_min_edge']:>5.1f} {p['cooldown_hours']:>5} {p['anti_churn_hours']:>5} {p['trailing_stop_pct']:>5} {str(p['use_regime_filter']):>5}")
    r["params"] = train_results[i]["params"]
    r["train_pnl"] = train_results[i]["pnl"]
    if best_oos is None or r["pnl"] > best_oos["pnl"]:
        best_oos = r

# Full 6-month
print(f"\n{'='*60}")
print(f"FULL 6-MONTH RUN:")
bt_p = best_oos["params"]
full = run_momrot(bt_p)
print(f"  P&L:    {full['pnl']:+.1f}% (${full['final']:.2f} from $62)")
print(f"  Trades: {full['trades']}")
print(f"  Fees:   ${full['fees']:.2f}")
print(f"  Max DD: {full['max_dd']:.1f}%")
print(f"  Params: {json.dumps(bt_p)}")

# Buy & hold
print(f"\nBuy & Hold:")
for coin in ["TIA","SOL","BTC"]:
    d = btc_ohlcv if coin=="BTC" else ohlcv.get(coin,[])
    if d: print(f"  {coin}: {((d[-1]['close']/d[0]['close'])-1)*100:+.1f}%")

# Save best
with open("REDACTED_PATHbest_momentum.json", "w") as f:
    json.dump({"params": bt_p, "full_6mo": full, "train_pnl": best_oos["train_pnl"], "oos_pnl": best_oos["pnl"]}, f, indent=2)
print("\nSaved to best_momentum.json")
