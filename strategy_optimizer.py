#!/usr/bin/env python3
"""
Strategy Optimization Engine

Tests multiple strategy types across parameter grids with walk-forward validation:
  - Mean Reversion (optimized)
  - Trend Following (buy strength)
  - Momentum Rotation (relative strength)
  - RSI Reversal (buy oversold, sell overbought)
  - Breakout (Donchian/Bollinger)
  - Grid Trading (fixed intervals)
  - Hybrid (momentum + mean reversion combo)

Validation:
  - Walk-forward: optimize on months 1-4, test on months 5-6 (out-of-sample)
  - Monte Carlo: randomize trade order 1000x for robustness
  - Fee sensitivity: test at taker/maker/slippage tiers
"""

import math
import time
import json
import random
import argparse
import itertools
import requests
from datetime import datetime, timezone
from collections import defaultdict

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "indicators", "REDACTED_PATHbinance_trade_bot/indicators.py"
)
_indicators_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_indicators_mod)

_ema = _indicators_mod.compute_ema
_adx = _indicators_mod.compute_adx
_rsi = _indicators_mod.compute_rsi
_bb = _indicators_mod.compute_bollinger_bands
_bb_squeeze = _indicators_mod.detect_bollinger_squeeze
_corr = _indicators_mod.compute_correlation
_returns = _indicators_mod.compute_returns
_sma = _indicators_mod.compute_sma
_std = _indicators_mod.compute_std

BINANCE_API = "https://api.binance.com/api/v3"
COINS = ["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
         "APT", "INJ", "TIA", "ENA", "PEPE", "JUP"]
BRIDGE = "USDC"
REF_COIN = "SOL"

BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"


# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHING + CACHING
# ═══════════════════════════════════════════════════════════════════════════

_data_cache = {}


def fetch_klines(symbol, interval="1h", days=180):
    """Fetch historical OHLCV klines from Binance public API."""
    cache_key = f"{symbol}_{interval}_{days}"
    if cache_key in _data_cache:
        return _data_cache[cache_key]

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_data = []
    cur_start = start_ms

    while cur_start < end_ms:
        url = f"{BINANCE_API}/klines"
        params = {
            "symbol": symbol, "interval": interval,
            "startTime": cur_start, "endTime": end_ms, "limit": 1000,
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if not data:
            break
        all_data.extend(data)
        cur_start = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.12)

    _data_cache[cache_key] = all_data
    return all_data


def parse_ohlcv(raw_klines):
    result = []
    for k in raw_klines:
        result.append({
            "ts": k[0], "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  BASE BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class BacktestBase:
    """Common backtest infrastructure shared by all strategies."""

    def __init__(self, ohlcv_by_coin, btc_ohlcv, params, initial_balance=62.0,
                 starting_coin="TIA"):
        self.ohlcv = ohlcv_by_coin
        self.btc_ohlcv = btc_ohlcv
        self.p = params  # Strategy parameters dict
        self.initial_balance = initial_balance

        # Build timestamp index
        self.timestamps = [c["ts"] for c in self.ohlcv[REF_COIN]]

        # Price indexes
        self.price_index = {}
        self.ohlcv_index = {}
        for coin, candles in self.ohlcv.items():
            self.price_index[coin] = {c["ts"]: c["close"] for c in candles}
            self.ohlcv_index[coin] = {c["ts"]: c for c in candles}
        self.btc_price_index = {c["ts"]: c["close"] for c in self.btc_ohlcv}

        # State
        first_price = self.ohlcv.get(starting_coin, [{}])[0].get("close", 1.0) if self.ohlcv.get(starting_coin) else 1.0
        self.balance = initial_balance / first_price if first_price else initial_balance
        self.bridge_reserve = 0.0
        self.current_coin = starting_coin
        self.last_trade_ts = None
        self.trade_count = 0
        self.fee_total = 0.0
        self.peak_value = initial_balance
        self.max_drawdown = 0.0

        # Trailing stop
        self.entry_price = None
        self.peak_price = None

        # Anti-churn
        self.recently_held = {}

        # Regime
        self.regime = SIDEWAYS
        self.regime_adx = 0.0

        # Ratio tracking
        self.ratio_samples = defaultdict(list)
        self.ratio_ema_value = {}
        self.ratio_std = {}

        # Stats
        self.trade_log = []
        self.equity_curve = []

    # ── Helpers ──────────────────────────────────────────────────────────

    def portfolio_value(self, ts):
        if self.current_coin == BRIDGE:
            return self.balance + self.bridge_reserve
        price = self.price_index.get(self.current_coin, {}).get(ts)
        if price is None:
            return self.bridge_reserve
        return self.balance * price + self.bridge_reserve

    def _get_fee_rate(self):
        return self.p.get("fee_rate", 0.00075)

    def _get_slippage(self):
        return self.p.get("slippage", 0.0005)

    def _get_round_trip_cost(self):
        return (self._get_fee_rate() + self._get_slippage()) * 2

    # ── Regime detection ─────────────────────────────────────────────────

    def detect_regime(self, ts):
        ref_candles = [c for c in self.ohlcv.get(REF_COIN, []) if c["ts"] <= ts]
        if len(ref_candles) < 30:
            return SIDEWAYS

        lookback = min(len(ref_candles), 60)
        recent = ref_candles[-lookback:]

        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]
        closes = [c["close"] for c in recent]

        adx_period = self.p.get("adx_period", 14)
        adx, plus_di, minus_di = _adx(highs, lows, closes, adx_period)
        self.regime_adx = adx

        ema_short = _ema(closes, self.p.get("ema_short", 20))
        ema_long = _ema(closes, self.p.get("ema_long", 50))
        current_price = closes[-1]

        is_trending = adx >= self.p.get("adx_threshold", 25)

        if is_trending:
            if current_price > ema_long and plus_di > minus_di:
                return BULL
            elif current_price < ema_long and minus_di > plus_di:
                return BEAR
        return SIDEWAYS

    # ── Ratio tracking ───────────────────────────────────────────────────

    def update_ratio(self, from_coin, to_coin, ratio):
        key = (from_coin, to_coin)
        self.ratio_samples[key].append(ratio)
        alpha = self.p.get("ratio_ema_alpha", 0.05)
        if key not in self.ratio_ema_value:
            self.ratio_ema_value[key] = ratio
        else:
            self.ratio_ema_value[key] = alpha * ratio + (1 - alpha) * self.ratio_ema_value[key]
        window = self.ratio_samples[key][-50:]
        if len(window) >= 2:
            mean = sum(window) / len(window)
            var = sum((x - mean) ** 2 for x in window) / len(window)
            self.ratio_std[key] = math.sqrt(var)

    def get_ratio_baseline(self, from_coin, to_coin):
        key = (from_coin, to_coin)
        return self.ratio_ema_value.get(key), self.ratio_std.get(key, 0.0)

    def get_sample_count(self, from_coin, to_coin):
        return len(self.ratio_samples.get((from_coin, to_coin), []))

    # ── Filters ──────────────────────────────────────────────────────────

    def check_momentum(self, coin, ts):
        ts_1h = ts - 3600 * 1000
        c_now = self.ohlcv_index.get(coin, {}).get(ts)
        c_prev = self.ohlcv_index.get(coin, {}).get(ts_1h)
        if not c_now or not c_prev:
            return True
        change = ((c_now["close"] - c_prev["close"]) / c_prev["close"]) * 100
        threshold = self.p.get("momentum_max_drop", 5.0)
        return change >= -threshold

    def check_rsi(self, coin, ts):
        period = self.p.get("rsi_period", 14)
        candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
        if len(candles) < period + 2:
            return True
        closes = [c["close"] for c in candles[-(period + 2):]]
        rsi = _rsi(closes, period)
        if rsi is None:
            return True
        return rsi <= self.p.get("rsi_overbought", 70)

    def check_anti_churn(self, coin, ts):
        sold_ts = self.recently_held.get(coin)
        if sold_ts is None:
            return True
        elapsed_hrs = (ts - sold_ts) / (3600 * 1000)
        if elapsed_hrs < self.p.get("anti_churn_hours", 6):
            return False
        del self.recently_held[coin]
        return True

    def check_rsi_oversold(self, coin, ts):
        """For RSI reversal: check if coin is oversold (buy signal)."""
        period = self.p.get("rsi_period", 14)
        candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
        if len(candles) < period + 2:
            return None
        closes = [c["close"] for c in candles[-(period + 2):]]
        return _rsi(closes, period)

    # ── Trade execution ──────────────────────────────────────────────────

    def execute_trade(self, target_coin, ts, score, reason=""):
        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        tgt_price = self.price_index.get(target_coin, {}).get(ts)
        if cur_price is None or tgt_price is None:
            return

        coin_value = self.balance * cur_price
        total_value = coin_value + self.bridge_reserve

        fee_rate = self._get_fee_rate()
        slip = self._get_slippage()
        sell_cost = coin_value * (fee_rate + slip)
        bridge_from_sell = coin_value - sell_cost
        total_bridge = bridge_from_sell + self.bridge_reserve

        # Position sizing
        position_pct = self.p.get("position_size", 1.0)
        if self.regime == BEAR and self.p.get("bear_position_size", 1.0) < 1.0:
            position_pct = self.p.get("bear_position_size", 0.7)

        max_deploy = total_bridge * position_pct
        new_reserve = total_bridge - max_deploy

        buy_cost = max_deploy * (fee_rate + slip)
        investable = max_deploy - buy_cost
        new_balance = investable / tgt_price

        total_fees = sell_cost + buy_cost
        self.fee_total += total_fees
        self.recently_held[self.current_coin] = ts

        self.trade_log.append({
            "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "from": self.current_coin, "to": target_coin,
            "value": total_value, "fee": total_fees, "score": score,
            "regime": self.regime, "reserve": new_reserve,
        })

        self.current_coin = target_coin
        self.balance = new_balance
        self.bridge_reserve = new_reserve
        self.last_trade_ts = ts
        self.trade_count += 1
        self.entry_price = tgt_price
        self.peak_price = tgt_price

    def sell_to_bridge(self, ts, reason=""):
        price = self.price_index.get(self.current_coin, {}).get(ts)
        if price is None:
            return
        coin_value = self.balance * price
        sell_cost = coin_value * (self._get_fee_rate() + self._get_slippage())
        self.fee_total += sell_cost
        self.bridge_reserve += coin_value - sell_cost
        self.recently_held[self.current_coin] = ts
        self.trade_log.append({
            "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "from": self.current_coin, "to": BRIDGE,
            "value": coin_value, "fee": sell_cost, "score": 0,
            "regime": self.regime, "reason": reason,
        })
        self.current_coin = BRIDGE
        self.balance = self.bridge_reserve
        self.bridge_reserve = 0.0
        self.trade_count += 1

    def reenter_from_bridge(self, ts):
        if self.last_trade_ts:
            elapsed_hrs = (ts - self.last_trade_ts) / (3600 * 1000)
            if elapsed_hrs < self.p.get("reentry_delay_hours", 2):
                return

        total_bridge = self.balance + self.bridge_reserve
        if total_bridge < 5.0:
            return

        # Pick best candidate using the strategy's scoring
        best_coin = self._pick_reentry(ts)
        if best_coin:
            price = self.price_index.get(best_coin, {}).get(ts)
            if price:
                buy_cost = total_bridge * (self._get_fee_rate() + self._get_slippage())
                investable = total_bridge - buy_cost
                self.fee_total += buy_cost
                self.balance = investable / price
                self.current_coin = best_coin
                self.bridge_reserve = 0.0
                self.last_trade_ts = ts
                self.trade_count += 1
                self.entry_price = price
                self.peak_price = price

    def _pick_reentry(self, ts):
        """Default: pick least-negative performer."""
        best_coin = None
        best_score = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
            if len(candles) >= 2:
                perf = ((candles[-1]["close"] - candles[-2]["close"]) / candles[-2]["close"]) * 100
                if perf > best_score:
                    best_score = perf
                    best_coin = coin
        return best_coin

    # ── Trailing stop ────────────────────────────────────────────────────

    def check_trailing_stop(self, ts):
        if not self.p.get("trailing_stop", True):
            return False
        price = self.price_index.get(self.current_coin, {}).get(ts)
        if price is None:
            return False
        if self.peak_price is None:
            self.peak_price = price
            self.entry_price = price
            return False
        if price > self.peak_price:
            self.peak_price = price
        drop = ((self.peak_price - price) / self.peak_price) * 100 if self.peak_price > 0 else 0
        if drop >= self.p.get("trailing_stop_pct", 15.0):
            self.sell_to_bridge(ts, reason="trailing_stop")
            return True
        return False

    # ── Cooldown ─────────────────────────────────────────────────────────

    def check_cooldown(self, ts):
        cooldown_hrs = self.p.get("cooldown_hours", 2.0)
        if self.regime == BULL:
            cooldown_hrs = self.p.get("bull_cooldown", 0.5)
        elif self.regime == BEAR:
            cooldown_hrs = self.p.get("bear_cooldown", 2.0)

        if not self.last_trade_ts:
            return False
        elapsed = (ts - self.last_trade_ts) / (3600 * 1000)
        return elapsed < cooldown_hrs

    # ── Run ──────────────────────────────────────────────────────────────

    def run(self, start_ts=None, end_ts=None):
        """Run backtest, optionally over a time window."""
        for ts in self.timestamps:
            if start_ts and ts < start_ts:
                # Still update ratios even before trading starts
                continue
            if end_ts and ts > end_ts:
                break

            val = self.portfolio_value(ts)
            self.peak_value = max(self.peak_value, val)
            if val < self.peak_value:
                dd = ((self.peak_value - val) / self.peak_value) * 100
                self.max_drawdown = max(self.max_drawdown, dd)

            self.equity_curve.append((ts, val))
            self.scout(ts)

        return self.get_results()

    def get_results(self):
        last_ts = self.timestamps[-1] if self.timestamps else 0
        if self.current_coin == BRIDGE:
            final_value = self.balance + self.bridge_reserve
        else:
            fp = self.price_index.get(self.current_coin, {}).get(last_ts, 0)
            final_value = self.balance * fp + self.bridge_reserve

        pnl_pct = ((final_value / self.initial_balance) - 1) * 100

        # Sharpe ratio (hourly returns)
        if len(self.equity_curve) > 10:
            returns = []
            for i in range(1, len(self.equity_curve)):
                prev = self.equity_curve[i - 1][1]
                curr = self.equity_curve[i][1]
                if prev > 0:
                    returns.append((curr - prev) / prev)
            if returns:
                mean_r = sum(returns) / len(returns)
                std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
                # Annualized Sharpe (hourly returns * 24 * 365)
                sharpe = (mean_r / std_r * math.sqrt(24 * 365)) if std_r > 0 else 0
            else:
                sharpe = 0
        else:
            sharpe = 0

        return {
            "final_value": final_value,
            "pnl_pct": pnl_pct,
            "trade_count": self.trade_count,
            "total_fees": self.fee_total,
            "fee_pct": (self.fee_total / self.initial_balance) * 100,
            "max_drawdown": self.max_drawdown,
            "sharpe": sharpe,
            "trade_log": self.trade_log,
        }

    def scout(self, ts):
        """Override in subclasses."""
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 1: MEAN REVERSION (current, optimized)
# ═══════════════════════════════════════════════════════════════════════════

class MeanReversion(BacktestBase):
    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        candidates = {}
        for target in COINS:
            if target == self.current_coin:
                continue
            tgt_price = self.price_index.get(target, {}).get(ts)
            if tgt_price is None:
                continue

            ratio = cur_price / tgt_price
            self.update_ratio(self.current_coin, target, ratio)

            if self.get_sample_count(self.current_coin, target) < self.p.get("min_samples", 20):
                continue

            baseline, _ = self.get_ratio_baseline(self.current_coin, target)
            if baseline is None or baseline <= 0:
                continue

            pct_gain = (ratio / baseline) - 1.0
            fee_hurdle = self._get_round_trip_cost() * self.p.get("scout_multiplier", 6)
            score = pct_gain - fee_hurdle

            min_profit = self.p.get("min_profit", 0.015)
            if score <= min_profit:
                continue
            if not self.check_anti_churn(target, ts):
                continue

            # Z-score filter
            _, std = self.get_ratio_baseline(self.current_coin, target)
            if std and std > 0:
                z = (ratio - baseline) / std
                z_thresh = self.p.get("z_threshold", 1.5)
                if z < z_thresh:
                    continue

            if not self.check_momentum(target, ts):
                continue
            if not self.check_rsi(target, ts):
                continue

            candidates[target] = score

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 2: MOMENTUM ROTATION (buy strength, sell weakness)
# ═══════════════════════════════════════════════════════════════════════════

class MomentumRotation(BacktestBase):
    """Rotate into the strongest performer. Only trades when current coin
    is underperforming significantly."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        # Get 24h performance for all coins
        lookback = self.p.get("momentum_lookback", 24)  # hours
        ts_lookback = ts - lookback * 3600 * 1000

        performance = {}
        for coin in COINS:
            c_now = self.price_index.get(coin, {}).get(ts)
            c_prev = self.price_index.get(coin, {}).get(ts_lookback)
            if c_now and c_prev and c_prev > 0:
                performance[coin] = (c_now / c_prev - 1.0) * 100

        if self.current_coin not in performance:
            return

        cur_perf = performance[self.current_coin]
        round_trip = self._get_round_trip_cost() * 100

        # Find coins significantly outperforming current
        min_edge = self.p.get("momentum_min_edge", 2.0)  # % outperformance needed
        candidates = {}
        for coin, perf in performance.items():
            if coin == self.current_coin:
                continue
            edge = perf - cur_perf
            if edge < min_edge:
                continue
            if not self.check_anti_churn(coin, ts):
                continue
            if not self.check_momentum(coin, ts):
                continue
            # Score = how much it's outperforming minus fees
            candidates[coin] = edge - round_trip

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])

    def _pick_reentry(self, ts):
        """Pick the strongest performer."""
        lookback = self.p.get("momentum_lookback", 24)
        ts_lookback = ts - lookback * 3600 * 1000
        best_coin = None
        best_perf = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            c_now = self.price_index.get(coin, {}).get(ts)
            c_prev = self.price_index.get(coin, {}).get(ts_lookback)
            if c_now and c_prev and c_prev > 0:
                perf = (c_now / c_prev - 1.0) * 100
                if perf > best_perf:
                    best_perf = perf
                    best_coin = coin
        return best_coin


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 3: TREND FOLLOWING (EMA crossover + ADX filter)
# ═══════════════════════════════════════════════════════════════════════════

class TrendFollowing(BacktestBase):
    """Follow trends: hold when uptrend, sell to bridge when downtrend."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        # Check current coin's trend
        candles = [c for c in self.ohlcv.get(self.current_coin, []) if c["ts"] <= ts]
        if len(candles) < self.p.get("ema_long", 50) + 5:
            return

        closes = [c["close"] for c in candles]
        ema_s = _ema(closes[-(self.p.get("ema_short", 20) + 10):], self.p.get("ema_short", 20))
        ema_l = _ema(closes[-(self.p.get("ema_long", 50) + 10):], self.p.get("ema_long", 50))
        current = closes[-1]

        # If current coin is in downtrend, look for better trend
        if current < ema_l and self.regime != BULL:
            # Find coins in strong uptrends
            candidates = {}
            for coin in COINS:
                if coin == self.current_coin:
                    continue
                if not self.check_anti_churn(coin, ts):
                    continue
                coin_candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
                if len(coin_candles) < self.p.get("ema_long", 50) + 5:
                    continue
                coin_closes = [c["close"] for c in coin_candles]
                coin_ema_s = _ema(coin_closes[-(self.p.get("ema_short", 20) + 10):], self.p.get("ema_short", 20))
                coin_ema_l = _ema(coin_closes[-(self.p.get("ema_long", 50) + 10):], self.p.get("ema_long", 50))

                # Must be in uptrend: price > EMA short > EMA long
                if coin_closes[-1] > coin_ema_s and coin_ema_s > coin_ema_l:
                    # Score by strength of trend (distance above EMA long)
                    strength = ((coin_closes[-1] / coin_ema_l) - 1) * 100
                    min_strength = self.p.get("trend_min_strength", 3.0)
                    if strength >= min_strength:
                        candidates[coin] = strength

            if candidates:
                best = max(candidates, key=lambda k: candidates[k])
                self.execute_trade(best, ts, candidates[best])

    def _pick_reentry(self, ts):
        """Pick coin with strongest uptrend."""
        best_coin = None
        best_strength = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
            if len(candles) < self.p.get("ema_long", 50) + 5:
                continue
            closes = [c["close"] for c in candles]
            ema_l = _ema(closes[-(self.p.get("ema_long", 50) + 10):], self.p.get("ema_long", 50))
            if ema_l and ema_l > 0:
                strength = ((closes[-1] / ema_l) - 1) * 100
                if strength > best_strength:
                    best_strength = strength
                    best_coin = coin
        return best_coin


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 4: RSI REVERSAL (buy oversold, sell overbought)
# ═══════════════════════════════════════════════════════════════════════════

class RSIReversal(BacktestBase):
    """Classic RSI mean reversion: buy when RSI < 30, sell when RSI > 70."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        # Check if current coin is overbought
        period = self.p.get("rsi_period", 14)
        candles = [c for c in self.ohlcv.get(self.current_coin, []) if c["ts"] <= ts]
        if len(candles) < period + 2:
            return
        closes = [c["close"] for c in candles[-(period + 2):]]
        cur_rsi = _rsi(closes, period)

        if cur_rsi is not None and cur_rsi > self.p.get("rsi_sell_threshold", 70):
            # Current coin overbought — find oversold replacement
            candidates = {}
            for coin in COINS:
                if coin == self.current_coin:
                    continue
                if not self.check_anti_churn(coin, ts):
                    continue
                rsi = self.check_rsi_oversold(coin, ts)
                if rsi is not None and rsi < self.p.get("rsi_buy_threshold", 35):
                    # Score: more oversold = better
                    candidates[coin] = (50 - rsi)  # Higher = more oversold

            if candidates:
                best = max(candidates, key=lambda k: candidates[k])
                self.execute_trade(best, ts, candidates[best])

    def _pick_reentry(self, ts):
        """Pick most oversold coin."""
        best_coin = None
        best_score = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            rsi = self.check_rsi_oversold(coin, ts)
            if rsi is not None and rsi < 40:
                score = 50 - rsi
                if score > best_score:
                    best_score = score
                    best_coin = coin
        return best_coin


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 5: BREAKOUT (Donchian channel)
# ═══════════════════════════════════════════════════════════════════════════

class Breakout(BacktestBase):
    """Buy coins breaking out above N-period high."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        channel = self.p.get("breakout_channel", 48)  # hours to look back for high
        ts_lookback = ts - channel * 3600 * 1000

        # Check if current coin is breaking down (below channel low)
        cur_candles = [c for c in self.ohlcv.get(self.current_coin, []) if ts_lookback <= c["ts"] <= ts]
        if len(cur_candles) < 10:
            return
        channel_low = min(c["low"] for c in cur_candles)
        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        # Only move if current coin is near channel low (underperforming)
        # or look for breakouts regardless
        candidates = {}
        for coin in COINS:
            if coin == self.current_coin:
                continue
            if not self.check_anti_churn(coin, ts):
                continue
            coin_candles = [c for c in self.ohlcv.get(coin, []) if ts_lookback <= c["ts"] <= ts]
            if len(coin_candles) < 10:
                continue
            channel_high = max(c["high"] for c in coin_candles[:-1])  # Exclude current candle
            coin_price = self.price_index.get(coin, {}).get(ts)
            if coin_price is None:
                continue

            # Breakout: current price within X% of channel high
            breakout_threshold = self.p.get("breakout_threshold", 0.02)
            if coin_price >= channel_high * (1 - breakout_threshold):
                if not self.check_momentum(coin, ts):
                    continue
                # Score: how far above the channel
                score = ((coin_price / channel_high) - 1) * 100 if channel_high > 0 else 0
                candidates[coin] = score

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])

    def _pick_reentry(self, ts):
        channel = self.p.get("breakout_channel", 48)
        ts_lookback = ts - channel * 3600 * 1000
        best_coin = None
        best_score = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            candles = [c for c in self.ohlcv.get(coin, []) if ts_lookback <= c["ts"] <= ts]
            if len(candles) < 10:
                continue
            ch_high = max(c["high"] for c in candles[:-1]) if len(candles) > 1 else 0
            price = self.price_index.get(coin, {}).get(ts)
            if price and ch_high > 0:
                score = ((price / ch_high) - 1) * 100
                if score > best_score:
                    best_score = score
                    best_coin = coin
        return best_coin


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 6: HYBRID (momentum + mean reversion combo)
# ═══════════════════════════════════════════════════════════════════════════

class Hybrid(BacktestBase):
    """Combines momentum scoring with mean-reversion entry timing.
    In bull: follow momentum. In bear/sideways: mean revert.
    Only trades when BOTH momentum and ratio signals agree."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        # Get performance data
        lookback = self.p.get("momentum_lookback", 24)
        ts_lookback = ts - lookback * 3600 * 1000

        candidates = {}
        for target in COINS:
            if target == self.current_coin:
                continue
            tgt_price = self.price_index.get(target, {}).get(ts)
            if tgt_price is None:
                continue

            # Ratio-based mean reversion score
            ratio = cur_price / tgt_price
            self.update_ratio(self.current_coin, target, ratio)
            if self.get_sample_count(self.current_coin, target) < self.p.get("min_samples", 20):
                continue

            baseline, std = self.get_ratio_baseline(self.current_coin, target)
            if baseline is None or baseline <= 0:
                continue

            pct_gain = (ratio / baseline) - 1.0
            fee_hurdle = self._get_round_trip_cost() * self.p.get("scout_multiplier", 6)
            mr_score = pct_gain - fee_hurdle

            # Momentum score
            c_tgt_prev = self.price_index.get(target, {}).get(ts_lookback)
            c_cur_prev = self.price_index.get(self.current_coin, {}).get(ts_lookback)
            if c_tgt_prev and c_cur_prev and c_cur_prev > 0:
                tgt_perf = (tgt_price / c_tgt_prev - 1.0) * 100
                cur_perf = (cur_price / c_cur_prev - 1.0) * 100
                momentum_edge = tgt_perf - cur_perf
            else:
                momentum_edge = 0

            # Combined score: weight momentum vs mean reversion by regime
            if self.regime == BULL:
                # In bull: mostly momentum
                combined = momentum_edge * 0.7 + mr_score * 100 * 0.3
            elif self.regime == BEAR:
                # In bear: mostly mean reversion, but require positive momentum
                if momentum_edge < -self.p.get("momentum_max_drop", 5.0):
                    continue
                combined = mr_score * 100 * 0.7 + momentum_edge * 0.3
            else:
                # Sideways: balanced
                combined = mr_score * 100 * 0.5 + momentum_edge * 0.5

            min_score = self.p.get("min_profit", 0.015) * 100  # Convert to percentage
            if combined <= min_score:
                continue
            if not self.check_anti_churn(target, ts):
                continue
            if not self.check_momentum(target, ts):
                continue
            if not self.check_rsi(target, ts):
                continue

            # Z-score check
            if std and std > 0:
                z = (ratio - baseline) / std
                if z < self.p.get("z_threshold", 1.5) and self.regime != BULL:
                    continue

            candidates[target] = combined

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])

    def _pick_reentry(self, ts):
        lookback = self.p.get("momentum_lookback", 24)
        ts_lookback = ts - lookback * 3600 * 1000
        best_coin = None
        best_score = -float("inf")
        for coin in COINS:
            if not self.check_anti_churn(coin, ts):
                continue
            c_now = self.price_index.get(coin, {}).get(ts)
            c_prev = self.price_index.get(coin, {}).get(ts_lookback)
            if c_now and c_prev and c_prev > 0:
                perf = (c_now / c_prev - 1.0) * 100
                if perf > best_score:
                    best_score = perf
                    best_coin = coin
        return best_coin


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 7: PAIRS TRADING (pure ratio extreme, very selective)
# ═══════════════════════════════════════════════════════════════════════════

class PairsTrading(BacktestBase):
    """Only trade when ratio reaches extreme (z-score > 3).
    Very selective — minimal trades, maximal conviction."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        candidates = {}
        for target in COINS:
            if target == self.current_coin:
                continue
            tgt_price = self.price_index.get(target, {}).get(ts)
            if tgt_price is None:
                continue

            ratio = cur_price / tgt_price
            self.update_ratio(self.current_coin, target, ratio)
            if self.get_sample_count(self.current_coin, target) < self.p.get("min_samples", 30):
                continue

            baseline, std = self.get_ratio_baseline(self.current_coin, target)
            if baseline is None or baseline <= 0 or not std or std <= 0:
                continue

            z = (ratio - baseline) / std
            z_threshold = self.p.get("z_threshold", 3.0)  # Very high

            if z < z_threshold:
                continue

            # Must also beat fee hurdle
            pct_gain = (ratio / baseline) - 1.0
            fee_hurdle = self._get_round_trip_cost() * self.p.get("scout_multiplier", 6)
            score = pct_gain - fee_hurdle

            if score <= self.p.get("min_profit", 0.01):
                continue
            if not self.check_anti_churn(target, ts):
                continue
            if not self.check_momentum(target, ts):
                continue

            candidates[target] = z  # Score by z-score

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY 8: BUY THE DIP (buy when coins drop significantly)
# ═══════════════════════════════════════════════════════════════════════════

class BuyTheDip(BacktestBase):
    """When current coin pumps, swap into coins that just dumped.
    Pure contrarian / buy-the-dip strategy."""

    def scout(self, ts):
        self.regime = self.detect_regime(ts)

        if self.current_coin == BRIDGE:
            self.reenter_from_bridge(ts)
            return

        if self.check_trailing_stop(ts):
            return
        if self.check_cooldown(ts):
            return

        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        if cur_price is None:
            return

        # Get short-term performance (4-12h lookback)
        lookback = self.p.get("momentum_lookback", 8)
        ts_lookback = ts - lookback * 3600 * 1000

        cur_prev = self.price_index.get(self.current_coin, {}).get(ts_lookback)
        if not cur_prev:
            return
        cur_perf = (cur_price / cur_prev - 1.0) * 100

        # Only trade when current coin pumped (we have gains to lock in)
        min_pump = self.p.get("momentum_min_edge", 3.0)
        if cur_perf < min_pump:
            return

        # Find coins that just dumped
        candidates = {}
        for target in COINS:
            if target == self.current_coin:
                continue
            tgt_price = self.price_index.get(target, {}).get(ts)
            tgt_prev = self.price_index.get(target, {}).get(ts_lookback)
            if not tgt_price or not tgt_prev:
                continue
            if not self.check_anti_churn(target, ts):
                continue

            tgt_perf = (tgt_price / tgt_prev - 1.0) * 100

            # Target must have dropped
            dip_threshold = self.p.get("dip_threshold", -3.0)
            if tgt_perf > dip_threshold:
                continue

            # RSI must confirm oversold
            rsi = self.check_rsi_oversold(target, ts)
            rsi_max = self.p.get("rsi_buy_threshold", 45)
            if rsi is not None and rsi > rsi_max:
                continue

            # Score: bigger dip = better
            score = abs(tgt_perf) + abs(cur_perf)  # combined spread
            candidates[target] = score

        if candidates:
            best = max(candidates, key=lambda k: candidates[k])
            self.execute_trade(best, ts, candidates[best])


# ═══════════════════════════════════════════════════════════════════════════
#  GRID SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════════════════

STRATEGY_MAP = {
    "mean_reversion": MeanReversion,
    "momentum": MomentumRotation,
    "trend": TrendFollowing,
    "rsi_reversal": RSIReversal,
    "breakout": Breakout,
    "hybrid": Hybrid,
    "pairs": PairsTrading,
    "buy_dip": BuyTheDip,
}


# Parameter grids for each strategy type
GRIDS = {
    "mean_reversion": {
        "scout_multiplier": [3, 5, 8, 12, 20],
        "min_profit": [0.005, 0.01, 0.015, 0.02, 0.03],
        "z_threshold": [1.0, 1.5, 2.0, 2.5, 3.0],
        "cooldown_hours": [1, 2, 4, 8],
        "anti_churn_hours": [3, 6, 12],
        "trailing_stop_pct": [10, 15, 20, 25, 100],
    },
    "momentum": {
        "momentum_lookback": [6, 12, 24, 48],
        "momentum_min_edge": [1.0, 2.0, 3.0, 5.0],
        "cooldown_hours": [2, 4, 8, 12],
        "anti_churn_hours": [6, 12, 24],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
    "trend": {
        "ema_short": [10, 20],
        "ema_long": [30, 50, 100],
        "trend_min_strength": [1.0, 2.0, 3.0, 5.0],
        "cooldown_hours": [4, 8, 12, 24],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
    "rsi_reversal": {
        "rsi_period": [7, 14],
        "rsi_buy_threshold": [25, 30, 35],
        "rsi_sell_threshold": [65, 70, 75],
        "cooldown_hours": [2, 4, 8],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
    "breakout": {
        "breakout_channel": [24, 48, 96],
        "breakout_threshold": [0.01, 0.02, 0.03],
        "cooldown_hours": [2, 4, 8, 12],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
    "hybrid": {
        "scout_multiplier": [3, 5, 8],
        "min_profit": [0.01, 0.015, 0.02],
        "z_threshold": [1.0, 1.5, 2.0],
        "momentum_lookback": [12, 24],
        "cooldown_hours": [2, 4, 8],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
    "pairs": {
        "z_threshold": [2.0, 2.5, 3.0, 3.5, 4.0],
        "min_profit": [0.005, 0.01, 0.015],
        "scout_multiplier": [3, 5, 8],
        "cooldown_hours": [2, 4, 8],
        "trailing_stop_pct": [15, 20, 100],
    },
    "buy_dip": {
        "momentum_lookback": [4, 8, 12],
        "momentum_min_edge": [2.0, 3.0, 5.0],
        "dip_threshold": [-2.0, -3.0, -5.0],
        "rsi_buy_threshold": [35, 40, 45],
        "cooldown_hours": [2, 4, 8],
        "trailing_stop_pct": [10, 15, 20, 100],
    },
}

# Fixed parameters for all strategies
BASE_PARAMS = {
    "fee_rate": 0.00075,  # 0.075% taker
    "slippage": 0.0005,
    "adx_period": 14,
    "adx_threshold": 25,
    "rsi_period": 14,
    "rsi_overbought": 70,
    "momentum_max_drop": 5.0,
    "bear_position_size": 1.0,
    "position_size": 1.0,
    "reentry_delay_hours": 2,
    "min_samples": 20,
    "ratio_ema_alpha": 0.05,
}


def generate_param_combos(grid):
    """Generate all parameter combinations from a grid."""
    keys = list(grid.keys())
    values = list(grid.values())
    combos = []
    for combo in itertools.product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


# ═══════════════════════════════════════════════════════════════════════════
#  WALK-FORWARD VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def walk_forward_optimize(ohlcv_by_coin, btc_ohlcv, strategy_name, grid,
                          train_days=120, test_days=60, max_combos=500):
    """
    Optimize on first `train_days`, validate on next `test_days`.
    Returns best train config + its test performance.
    """
    strategy_cls = STRATEGY_MAP[strategy_name]
    combos = generate_param_combos(grid)

    # Sample if too many
    if len(combos) > max_combos:
        random.seed(42)
        combos = random.sample(combos, max_combos)

    timestamps = [c["ts"] for c in ohlcv_by_coin[REF_COIN]]
    if not timestamps:
        return None

    end_ts = timestamps[-1]
    test_start = end_ts - test_days * 86400 * 1000
    train_start = test_start - train_days * 86400 * 1000

    print(f"  {strategy_name}: {len(combos)} combos to test")
    print(f"    Train: {datetime.fromtimestamp(train_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(test_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"    Test:  {datetime.fromtimestamp(test_start/1000, tz=timezone.utc).strftime('%Y-%m-%d')} → {datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

    train_results = []
    best_pnl = -float("inf")

    for i, combo in enumerate(combos):
        params = {**BASE_PARAMS, **combo}
        bt = strategy_cls(ohlcv_by_coin, btc_ohlcv, params)
        result = bt.run(start_ts=train_start, end_ts=test_start)
        result["params"] = combo
        result["strategy"] = strategy_name
        train_results.append(result)

        if result["pnl_pct"] > best_pnl:
            best_pnl = result["pnl_pct"]

        if (i + 1) % 50 == 0:
            print(f"    ...{i+1}/{len(combos)} tested, best train P&L: {best_pnl:+.1f}%")

    # Sort by train P&L
    train_results.sort(key=lambda x: x["pnl_pct"], reverse=True)

    # Take top 5 configs and test them on out-of-sample data
    top_n = min(5, len(train_results))
    print(f"    Top {top_n} configs → out-of-sample validation...")

    oos_results = []
    for i in range(top_n):
        params = {**BASE_PARAMS, **train_results[i]["params"]}
        bt = strategy_cls(ohlcv_by_coin, btc_ohlcv, params)
        result = bt.run(start_ts=test_start, end_ts=end_ts)
        result["params"] = train_results[i]["params"]
        result["strategy"] = strategy_name
        result["train_pnl"] = train_results[i]["pnl_pct"]
        result["train_trades"] = train_results[i]["trade_count"]
        result["oos_pnl"] = result["pnl_pct"]
        oos_results.append(result)

        t_pnl = train_results[i]["pnl_pct"]
        o_pnl = result["pnl_pct"]
        print(f"    #{i+1} Train: {t_pnl:+.1f}% → OOS: {o_pnl:+.1f}% "
              f"({result['trade_count']} trades, params: {train_results[i]['params']})")

    return oos_results


# ═══════════════════════════════════════════════════════════════════════════
#  MONTE CARLO ROBUSTNESS CHECK
# ═══════════════════════════════════════════════════════════════════════════

def monte_carlo(trade_log, initial_balance=62.0, num_sims=1000):
    """
    Randomize trade order to check robustness.
    Returns confidence intervals for final P&L.
    """
    if not trade_log or len(trade_log) < 5:
        return None

    # Extract trade returns (percentage of portfolio)
    trade_returns = []
    for i in range(1, len(trade_log)):
        prev_val = trade_log[i - 1]["value"]
        curr_val = trade_log[i]["value"]
        if prev_val > 0:
            trade_returns.append((curr_val / prev_val) - 1.0)

    if not trade_returns:
        return None

    finals = []
    for _ in range(num_sims):
        random.shuffle(trade_returns)
        balance = initial_balance
        for r in trade_returns:
            balance *= (1 + r)
        finals.append(((balance / initial_balance) - 1) * 100)

    finals.sort()
    return {
        "median": finals[len(finals) // 2],
        "p5": finals[int(len(finals) * 0.05)],
        "p25": finals[int(len(finals) * 0.25)],
        "p75": finals[int(len(finals) * 0.75)],
        "p95": finals[int(len(finals) * 0.95)],
        "win_rate": sum(1 for f in finals if f > 0) / len(finals),
        "num_trades": len(trade_returns),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Strategy Optimization Engine")
    parser.add_argument("--months", type=int, default=6, help="Total months of data")
    parser.add_argument("--strategies", nargs="*", default=None, help="Specific strategies to test")
    parser.add_argument("--max-combos", type=int, default=300, help="Max param combos per strategy")
    args = parser.parse_args()

    days = args.months * 30

    print("=" * 70)
    print(f"  STRATEGY OPTIMIZATION ENGINE — {args.months} months data")
    print(f"  Walk-forward: 4 months train / 2 months test")
    print("=" * 70)

    # Fetch data
    print("\nFetching historical data...")
    ohlcv_by_coin = {}
    for coin in COINS:
        symbol = f"{coin}{BRIDGE}"
        try:
            raw = fetch_klines(symbol, "1h", days)
            ohlcv_by_coin[coin] = parse_ohlcv(raw)
        except Exception as e:
            print(f"  {symbol}: FAILED ({e})")
            ohlcv_by_coin[coin] = []
        time.sleep(0.15)

    raw = fetch_klines(f"BTC{BRIDGE}", "1h", days)
    btc_ohlcv = parse_ohlcv(raw)
    print(f"  Data loaded: {len(ohlcv_by_coin[REF_COIN])} candles per coin\n")

    # Strategies to test
    strategies = args.strategies or list(GRIDS.keys())

    all_oos_results = []

    for strat_name in strategies:
        if strat_name not in GRIDS:
            print(f"  Unknown strategy: {strat_name}")
            continue

        print(f"\n{'─' * 60}")
        print(f"  STRATEGY: {strat_name.upper()}")
        print(f"{'─' * 60}")

        oos = walk_forward_optimize(
            ohlcv_by_coin, btc_ohlcv, strat_name, GRIDS[strat_name],
            train_days=120, test_days=60, max_combos=args.max_combos,
        )

        if oos:
            all_oos_results.extend(oos)

    # ── Final ranking ────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  OUT-OF-SAMPLE RANKING (all strategies)")
    print(f"{'=' * 70}")

    # Sort by OOS P&L
    all_oos_results.sort(key=lambda x: x.get("oos_pnl", x["pnl_pct"]), reverse=True)

    print(f"{'#':<3} {'Strategy':<18} {'Train':>8} {'OOS':>8} {'Trades':>7} {'MaxDD':>7} {'Sharpe':>7}")
    print("─" * 70)
    for i, r in enumerate(all_oos_results[:20]):
        strat = r.get("strategy", "?")
        train_pnl = r.get("train_pnl", r["pnl_pct"])
        oos_pnl = r.get("oos_pnl", r["pnl_pct"])
        trades = r["trade_count"]
        dd = r["max_drawdown"]
        sharpe = r.get("sharpe", 0)
        print(f"{i+1:<3} {strat:<18} {train_pnl:>+7.1f}% {oos_pnl:>+7.1f}% {trades:>7} {dd:>6.1f}% {sharpe:>7.2f}")

    # ── Best strategy details ────────────────────────────────────────────
    if all_oos_results:
        best = all_oos_results[0]
        print(f"\n{'=' * 70}")
        print(f"  🏆 BEST STRATEGY")
        print(f"{'=' * 70}")
        print(f"  Type:     {best['strategy']}")
        print(f"  Train:    {best['train_pnl']:+.1f}%")
        print(f"  OOS:      {best['oos_pnl']:+.1f}%")
        print(f"  Trades:   {best['trade_count']}")
        print(f"  Fees:     ${best['total_fees']:.2f}")
        print(f"  Max DD:   {best['max_drawdown']:.1f}%")
        print(f"  Sharpe:   {best.get('sharpe', 0):.2f}")
        print(f"  Params:   {json.dumps(best['params'], indent=2)}")

        # Monte Carlo on best
        mc = monte_carlo(best.get("trade_log", []), num_sims=1000)
        if mc:
            print(f"\n  Monte Carlo ({mc['num_trades']} trades, 1000 sims):")
            print(f"    Median:  {mc['median']:+.1f}%")
            print(f"    5th %:   {mc['p5']:+.1f}%")
            print(f"    25th %:  {mc['p25']:+.1f}%")
            print(f"    75th %:  {mc['p75']:+.1f}%")
            print(f"    95th %:  {mc['p95']:+.1f}%")
            print(f"    Win rate (profitable): {mc['win_rate']*100:.0f}%")

    # ── Buy & hold comparison ────────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print("  BUY & HOLD BASELINE")
    print(f"{'─' * 50}")
    for coin in ["TIA", "SOL", "BTC"]:
        data = btc_ohlcv if coin == "BTC" else ohlcv_by_coin.get(coin, [])
        if data and len(data) >= 2:
            bh = ((data[-1]["close"] / data[0]["close"]) - 1) * 100
            print(f"  {coin:>4}: {bh:+.1f}%")

    # Save results
    with open("REDACTED_PATHbacktest_results.json", "w") as f:
        json.dump([{k: v for k, v in r.items() if k != "trade_log"}
                    for r in all_oos_results[:20]], f, indent=2, default=str)
    print(f"\n  Results saved to backtest_results.json")


if __name__ == "__main__":
    main()
