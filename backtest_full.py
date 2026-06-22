#!/usr/bin/env python3
"""
FULL backtest of the improved strategy — ALL filters included.

Simulates the complete decision pipeline:
  1. Market regime detection (ADX, EMA, volatility, BTC correlation)
  2. Regime-aware parameter switching
  3. Phase 1+2: Percentage scoring with EMA baseline
  4. Phase 3: Z-score filter (regime-scaled threshold)
  5. Phase 4: Momentum filter (1h candle drop guard)
  6. RSI filter (skip overbought coins)
  7. Correlation penalty (reduce score for highly correlated coins)
  8. BB squeeze bonus (boost score for impending breakouts)
  9. Anti-churn block (6h cooldown on recently held coins)
  10. Min profit threshold (1.5%)
  11. Dynamic position sizing (70% in bear, 90% sideways, 100% bull)
  12. Trailing stop-loss (auto-sell at -15% from peak)
  13. Trade cooldown (regime-aware)
  14. Realistic fees (0.075% taker per side) + slippage (0.05%)

Usage: python backtest_full.py [--months 6] [--interval 1h] [--verbose]
"""

import math
import time
import argparse
import requests
from datetime import datetime, timezone
from collections import defaultdict

import importlib.util

# Load indicators.py directly (bypass package __init__ which pulls in socketio etc.)
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

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION (mirrors user.cfg / config.py defaults)
# ═══════════════════════════════════════════════════════════════════════════

COINS = ["SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
         "APT", "INJ", "TIA", "ENA", "PEPE", "JUP"]
BRIDGE = "USDC"
REF_COIN = "SOL"  # reference coin for regime detection

# Fees
TAKER_FEE = 0.00075   # 0.075% per side (BNB discount)
SLIPPAGE = 0.0005     # 0.05% per side (USDC pair spread simulation)

# Strategy parameters
SCOUT_MULTIPLIER = 6
Z_SCORE_THRESHOLD = 1.5
MIN_PROFIT_THRESHOLD = 0.015
MOMENTUM_MAX_DROP_1H = 5.0
BEAR_MOMENTUM_MAX_DROP = 3.0
RSI_OVERBOUGHT = 68
RSI_PERIOD = 14
CORRELATION_THRESHOLD = 0.85
BB_PERIOD = 20
BB_SQUEEZE_LOOKBACK = 50

# Regime detection
ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25
EMA_SHORT = 20
EMA_LONG = 50
REGIME_HIGH_VOL_THRESHOLD = 5.0
REGIME_CHECK_INTERVAL_HOURS = 1  # Re-check regime every hour (vs 300s live)

# Regime multipliers
BEAR_ZSCORE_MULT = 1.2
BULL_ZSCORE_MULT = 0.8
REGIME_ZSCORE_MULT = 1.5

# Cooldowns (in seconds, but we work in hours for backtest)
BEAR_COOLDOWN_HRS = 2.0
SIDEWAYS_COOLDOWN_HRS = 0.5
BULL_COOLDOWN_HRS = 0.25

# Position sizing
BEAR_POSITION_SIZE = 0.7
SIDEWAYS_POSITION_SIZE = 0.9

# Trailing stop
TRAILING_STOP_ENABLED = True
TRAILING_STOP_PCT = 15.0

# Anti-churn
ANTI_CHURN_HOURS = 6

# Profit-taking (disabled in live config)
PROFIT_TAKING_ENABLED = False

# Ratio EMA tracking
RATIO_EMA_ALPHA = 0.05  # Slow EMA for ratio baselines (~20-sample smoothing)
RATIO_MIN_SAMPLES = 20   # Need at least 20 samples before generating signals

# Regime constants
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"

# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════

BINANCE_API = "https://api.binance.com/api/v3"


def fetch_klines(symbol, interval="1h", days=180):
    """Fetch historical OHLCV klines from Binance public API."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_data = []
    cur_start = start_ms

    while cur_start < end_ms:
        url = f"{BINANCE_API}/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur_start,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
        if not data:
            break
        all_data.extend(data)
        cur_start = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.15)  # Rate limit

    return all_data


def parse_ohlcv(raw_klines):
    """Convert raw Binance klines to structured arrays."""
    result = []
    for k in raw_klines:
        result.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════

class FullBacktest:
    def __init__(self, ohlcv_by_coin, btc_ohlcv, initial_balance=62.0,
                 starting_coin="TIA", verbose=False):
        self.ohlcv = ohlcv_by_coin  # {coin: [{ts, open, high, low, close, volume}]}
        self.btc_ohlcv = btc_ohlcv
        self.initial_balance = initial_balance
        self.verbose = verbose

        # Build aligned timestamp index from reference coin
        self.timestamps = [c["ts"] for c in self.ohlcv[REF_COIN]]

        # Index price data by timestamp for fast lookup
        self.price_index = {}  # {coin: {ts: close_price}}
        self.ohlcv_index = {}  # {coin: {ts: {ohlcv}}}
        for coin, candles in self.ohlcv.items():
            self.price_index[coin] = {c["ts"]: c["close"] for c in candles}
            self.ohlcv_index[coin] = {c["ts"]: c for c in candles}

        self.btc_price_index = {c["ts"]: c["close"] for c in self.btc_ohlcv}
        self.btc_ohlcv_idx = {c["ts"]: c for c in self.btc_ohlcv}

        # State
        self.balance = initial_balance / self._get_starting_price(starting_coin)
        self.bridge_reserve = 0.0  # Leftover USDC from position sizing
        self.current_coin = starting_coin
        self.last_trade_ts = None
        self.trade_count = 0
        self.fee_total = 0.0
        self.slippage_total = 0.0
        self.peak_value = initial_balance
        self.max_drawdown = 0.0

        # Position tracking (trailing stop)
        self.entry_price = None
        self.peak_price = None

        # Anti-churn
        self.recently_held = {}  # coin -> ts when sold

        # Ratio tracking (simulates DB pair_stat EMA)
        self.ratio_ema = {}     # (from, to) -> current EMA
        self.ratio_std = {}     # (from, to) -> current std
        self.ratio_samples = defaultdict(list)  # (from, to) -> [ratio, ...]
        self.ratio_ema_value = {}  # (from, to) -> running EMA value

        # Regime
        self.regime = SIDEWAYS
        self.regime_history = []
        self.regime_adx = 0.0
        self.regime_volatility = 0.0
        self.regime_btc_corr = 0.0

        # Filter statistics
        self.filter_stats = defaultdict(int)

        # Trade log
        self.trade_log = []

    # ── Helpers ──────────────────────────────────────────────────────────

    def _get_starting_price(self, coin):
        """Get first available price for starting coin."""
        candles = self.ohlcv.get(coin, [])
        if candles:
            return candles[0]["close"]
        return 1.0

    # ── Portfolio value ──────────────────────────────────────────────────

    def portfolio_value(self, ts):
        if self.current_coin == BRIDGE:
            return self.balance + self.bridge_reserve
        price = self.price_index.get(self.current_coin, {}).get(ts)
        if price is None:
            return self.bridge_reserve
        return self.balance * price + self.bridge_reserve

    # ── Regime detection ─────────────────────────────────────────────────

    def update_regime(self, ts):
        """Classify market using ADX + EMA + volatility + BTC correlation."""
        ref_candles = []
        for i, c in enumerate(self.ohlcv.get(REF_COIN, [])):
            if c["ts"] <= ts:
                ref_candles.append(c)
            else:
                break

        if len(ref_candles) < max(EMA_LONG * 2, ADX_PERIOD * 3, 30):
            return

        # Get candles up to this point
        lookback = min(len(ref_candles), max(EMA_LONG * 2, ADX_PERIOD * 3, 60))
        recent = ref_candles[-lookback:]

        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]
        closes = [c["close"] for c in recent]

        adx, plus_di, minus_di = _adx(highs, lows, closes, ADX_PERIOD)
        self.regime_adx = adx

        ema_short = _ema(closes, EMA_SHORT)
        ema_long = _ema(closes, EMA_LONG)
        current_price = closes[-1]

        # Average volatility across all coins
        total_abs_change = 0.0
        count = 0
        for coin in COINS:
            coin_candles = self.ohlcv.get(coin, [])
            # Find candles up to this ts
            recent_coin = [c for c in coin_candles if c["ts"] <= ts]
            if len(recent_coin) >= 2:
                pct = abs(((recent_coin[-1]["close"] - recent_coin[-2]["close"])
                          / recent_coin[-2]["close"]) * 100)
                total_abs_change += pct
                count += 1
        avg_volatility = total_abs_change / count if count > 0 else 0.0
        self.regime_volatility = avg_volatility

        # BTC correlation
        btc_corr = None
        btc_recent = [c for c in self.btc_ohlcv if c["ts"] <= ts]
        if len(btc_recent) >= 20 and len(recent) >= 20:
            btc_closes = [c["close"] for c in btc_recent[-min(len(btc_recent), 50):]]
            ref_closes = closes[-len(btc_closes):]
            ref_ret = _returns(ref_closes)
            btc_ret = _returns(btc_closes)
            btc_corr = _corr(ref_ret, btc_ret) if len(ref_ret) >= 5 else None
        self.regime_btc_corr = btc_corr if btc_corr is not None else 0.0

        # Classify
        is_trending = adx >= ADX_TREND_THRESHOLD
        is_stormy = avg_volatility > REGIME_HIGH_VOL_THRESHOLD

        if is_stormy and not is_trending:
            new_regime = STORMY
        elif is_trending:
            if ema_short and ema_long and current_price > ema_long:
                if plus_di > minus_di:
                    new_regime = BULL
                else:
                    new_regime = SIDEWAYS
            elif ema_short and ema_long and current_price < ema_long:
                if minus_di > plus_di:
                    new_regime = BEAR
                else:
                    new_regime = SIDEWAYS
            else:
                new_regime = SIDEWAYS
        else:
            new_regime = SIDEWAYS

        if is_stormy and new_regime == SIDEWAYS:
            new_regime = STORMY

        old = self.regime
        self.regime = new_regime

        if new_regime != old:
            corr_str = f", BTC corr: {btc_corr:.2f}" if btc_corr is not None else ""
            if self.verbose:
                print(f"  [{datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}] "
                      f"Regime: {old.upper()} → {new_regime.upper()} "
                      f"(ADX: {adx:.1f}, Vol: {avg_volatility:.1f}%, "
                      f"+DI: {plus_di:.1f}, -DI: {minus_di:.1f}{corr_str})")

        self.regime_history.append((ts, new_regime, adx, avg_volatility))

    # ── Regime-aware parameters ──────────────────────────────────────────

    def get_z_threshold(self):
        base = Z_SCORE_THRESHOLD
        if self.regime == BULL:
            return base * BULL_ZSCORE_MULT
        elif self.regime == BEAR:
            return base * BEAR_ZSCORE_MULT
        elif self.regime == STORMY:
            return base * REGIME_ZSCORE_MULT
        return base

    def get_cooldown_hours(self):
        if self.regime == BULL:
            return BULL_COOLDOWN_HRS
        elif self.regime == BEAR:
            return BEAR_COOLDOWN_HRS
        return SIDEWAYS_COOLDOWN_HRS

    def get_momentum_threshold(self):
        if self.regime == BEAR:
            return BEAR_MOMENTUM_MAX_DROP
        return MOMENTUM_MAX_DROP_1H

    def get_position_size(self):
        if self.regime == BEAR:
            return BEAR_POSITION_SIZE
        elif self.regime == SIDEWAYS:
            return SIDEWAYS_POSITION_SIZE
        return 1.0

    # ── Ratio tracking (simulates DB pair_stat) ──────────────────────────

    def update_ratio(self, from_coin, to_coin, ratio, ts):
        key = (from_coin, to_coin)
        self.ratio_samples[key].append(ratio)

        samples = self.ratio_samples[key]
        if len(samples) == 1:
            self.ratio_ema_value[key] = ratio
        else:
            self.ratio_ema_value[key] = (
                RATIO_EMA_ALPHA * ratio + (1 - RATIO_EMA_ALPHA) * self.ratio_ema_value[key]
            )

        # Rolling std over last 50 samples
        window = samples[-50:]
        if len(window) >= 2:
            mean = sum(window) / len(window)
            variance = sum((x - mean) ** 2 for x in window) / len(window)
            self.ratio_std[key] = math.sqrt(variance)
        else:
            self.ratio_std[key] = 0.0

    def get_ratio_baseline(self, from_coin, to_coin):
        key = (from_coin, to_coin)
        ema = self.ratio_ema_value.get(key)
        std = self.ratio_std.get(key, 0.0)
        return ema, std

    def get_sample_count(self, from_coin, to_coin):
        return len(self.ratio_samples.get((from_coin, to_coin), []))

    # ── Filters ──────────────────────────────────────────────────────────

    def check_momentum(self, coin, ts):
        """Phase 4: Skip coins that dropped too much in the last hour."""
        ts_hours_back = ts - 3600 * 1000
        candle_now = self.ohlcv_index.get(coin, {}).get(ts)
        candle_prev = self.ohlcv_index.get(coin, {}).get(ts_hours_back)

        if not candle_now or not candle_prev:
            return True

        change_pct = ((candle_now["close"] - candle_prev["close"])
                      / candle_prev["close"]) * 100
        threshold = self.get_momentum_threshold()

        if change_pct < -threshold:
            self.filter_stats["momentum_block"] += 1
            return False
        return True

    def check_rsi(self, coin, ts):
        """RSI filter: skip overbought coins."""
        candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
        if len(candles) < RSI_PERIOD + 2:
            return True

        closes = [c["close"] for c in candles[-(RSI_PERIOD + 2):]]
        rsi = _rsi(closes, RSI_PERIOD)
        if rsi is None:
            return True

        if rsi > RSI_OVERBOUGHT:
            self.filter_stats["rsi_block"] += 1
            return False
        return True

    def check_anti_churn(self, coin, ts):
        """Anti-churn: don't re-buy coins sold recently."""
        sold_ts = self.recently_held.get(coin)
        if sold_ts is None:
            return True
        elapsed_hours = (ts - sold_ts) / (3600 * 1000)
        if elapsed_hours < ANTI_CHURN_HOURS:
            self.filter_stats["anti_churn_block"] += 1
            return False
        # Expired — clean up
        del self.recently_held[coin]
        return True

    def get_correlation_penalty(self, current_coin, target_coin, ts):
        """Reduce score for highly correlated coins."""
        cur_candles = [c["close"] for c in self.ohlcv.get(current_coin, [])
                       if c["ts"] <= ts][-50:]
        tgt_candles = [c["close"] for c in self.ohlcv.get(target_coin, [])
                       if c["ts"] <= ts][-50:]

        min_len = min(len(cur_candles), len(tgt_candles))
        if min_len < 10:
            return 1.0

        cur_ret = _returns(cur_candles[-min_len:])
        tgt_ret = _returns(tgt_candles[-min_len:])
        corr = _corr(cur_ret, tgt_ret)

        if abs(corr) > CORRELATION_THRESHOLD:
            excess = (abs(corr) - CORRELATION_THRESHOLD) / (1.0 - CORRELATION_THRESHOLD)
            penalty = max(0.2, 1.0 - excess * 0.5)
            return penalty
        return 1.0

    def get_bb_squeeze_bonus(self, coin, ts):
        """Boost score for coins in Bollinger squeeze."""
        candles = [c["close"] for c in self.ohlcv.get(coin, [])
                   if c["ts"] <= ts]
        if len(candles) < BB_PERIOD + BB_SQUEEZE_LOOKBACK:
            return 1.0

        is_squeeze, bandwidth, percentile = _bb_squeeze(
            candles, period=BB_PERIOD, squeeze_lookback=BB_SQUEEZE_LOOKBACK
        )
        if is_squeeze:
            bonus = 1.0 + (1.0 - percentile / 20.0) * 0.3
            return bonus
        return 1.0

    def get_z_score(self, from_coin, to_coin, current_ratio):
        """Phase 3: Z-score for pair."""
        ema, std = self.get_ratio_baseline(from_coin, to_coin)
        if ema is not None and std and std > 0:
            return (current_ratio - ema) / std
        return 0.0

    # ── Trailing stop ────────────────────────────────────────────────────

    def check_trailing_stop(self, ts):
        """Phase D: Auto-sell if price drops N% from peak."""
        if not TRAILING_STOP_ENABLED:
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

        drop_pct = ((self.peak_price - price) / self.peak_price) * 100
        if drop_pct >= TRAILING_STOP_PCT:
            if self.verbose:
                print(f"  📉 Trailing stop: {self.current_coin} dropped "
                      f"{drop_pct:.1f}% from peak ${self.peak_price:.4f}")
            self._execute_sell_to_bridge(ts, reason="trailing_stop")
            return True
        return False

    # ── Trade execution ──────────────────────────────────────────────────

    def _execute_trade(self, target_coin, ts, score, regime):
        """Execute a trade: sell current coin → buy target coin."""
        cur_price = self.price_index.get(self.current_coin, {}).get(ts)
        tgt_price = self.price_index.get(target_coin, {}).get(ts)

        if cur_price is None or tgt_price is None:
            return

        # Total portfolio value (coin + any existing bridge reserve)
        coin_value = self.balance * cur_price
        total_value = coin_value + self.bridge_reserve

        # Sell current coin → bridge (with fees + slippage)
        sell_cost = coin_value * (TAKER_FEE + SLIPPAGE)
        bridge_from_sell = coin_value - sell_cost

        # Total bridge available = what we just got + existing reserve
        total_bridge = bridge_from_sell + self.bridge_reserve

        # Dynamic position sizing
        position_pct = self.get_position_size()
        max_deploy = total_bridge * position_pct
        new_reserve = total_bridge - max_deploy

        # Buy target coin
        buy_cost = max_deploy * (TAKER_FEE + SLIPPAGE)
        investable = max_deploy - buy_cost

        # Convert to target coin units
        new_balance = investable / tgt_price

        total_fees = sell_cost + buy_cost
        self.fee_total += total_fees

        # Anti-churn: record the coin we're leaving
        self.recently_held[self.current_coin] = ts

        trade_entry = {
            "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "from": self.current_coin,
            "to": target_coin,
            "value": total_value,
            "fee": total_fees,
            "score": score,
            "regime": regime,
            "adx": self.regime_adx,
            "reserve": new_reserve,
        }
        self.trade_log.append(trade_entry)

        self.current_coin = target_coin
        self.balance = new_balance
        self.bridge_reserve = new_reserve
        self.last_trade_ts = ts
        self.trade_count += 1

        # Reset position tracking
        self.entry_price = tgt_price
        self.peak_price = tgt_price

        if self.verbose:
            print(f"  🔄 Trade #{self.trade_count}: {trade_entry['from']} → {target_coin} "
                  f"(${total_value:.2f}, fee: ${total_fees:.3f}, reserve: ${new_reserve:.2f}, "
                  f"score: {score:.6f}, regime: {regime})")

    def _execute_sell_to_bridge(self, ts, reason=""):
        """Sell current coin to bridge (for trailing stop / profit-taking)."""
        price = self.price_index.get(self.current_coin, {}).get(ts)
        if price is None:
            return

        coin_value = self.balance * price
        sell_cost = coin_value * (TAKER_FEE + SLIPPAGE)
        self.fee_total += sell_cost

        # Merge into bridge reserve
        self.bridge_reserve += coin_value - sell_cost
        self.recently_held[self.current_coin] = ts

        self.trade_log.append({
            "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "from": self.current_coin,
            "to": BRIDGE,
            "value": coin_value,
            "fee": sell_cost,
            "score": 0,
            "regime": self.regime,
            "reason": reason,
        })

        self.current_coin = BRIDGE
        self.balance = self.bridge_reserve  # balance is now bridge
        self.bridge_reserve = 0.0
        self.trade_count += 1

    # ── Main scouting logic ──────────────────────────────────────────────

    def scout(self, ts):
        """Main scouting loop — replicate the live strategy's decision pipeline."""

        # Update regime
        self.update_regime(ts)

        # If holding bridge (from trailing stop), try to re-enter
        if self.current_coin == BRIDGE:
            self._reenter(ts)
            return

        # Trailing stop check
        if self.check_trailing_stop(ts):
            return

        # Profit-taking (disabled)
        # if self._check_profit_taking(ts): return

        # Cooldown
        cooldown_hrs = self.get_cooldown_hours()
        if self.last_trade_ts:
            elapsed_hrs = (ts - self.last_trade_ts) / (3600 * 1000)
            if elapsed_hrs < cooldown_hrs:
                self.filter_stats["cooldown_block"] += 1
                return

        current_price = self.price_index.get(self.current_coin, {}).get(ts)
        if current_price is None:
            return

        # Score all candidates
        candidates = {}
        for target in COINS:
            if target == self.current_coin:
                continue

            target_price = self.price_index.get(target, {}).get(ts)
            if target_price is None:
                continue

            ratio = current_price / target_price
            self.update_ratio(self.current_coin, target, ratio, ts)

            # Need enough ratio history
            n_samples = self.get_sample_count(self.current_coin, target)
            if n_samples < RATIO_MIN_SAMPLES:
                self.filter_stats["insufficient_data"] += 1
                continue

            # Phase 1+2: Score
            baseline, _ = self.get_ratio_baseline(self.current_coin, target)
            if baseline is None or baseline <= 0:
                continue

            pct_gain = (ratio / baseline) - 1.0
            fee_hurdle = (TAKER_FEE * 2) * SCOUT_MULTIPLIER
            score = pct_gain - fee_hurdle

            # Min profit threshold
            if score <= MIN_PROFIT_THRESHOLD:
                self.filter_stats["score_too_low"] += 1
                continue

            candidates[target] = score

        if not candidates:
            return

        # Anti-churn filter
        candidates = {
            t: s for t, s in candidates.items()
            if self.check_anti_churn(t, ts)
        }
        if not candidates:
            return

        # Z-score filter (regime-aware)
        z_threshold = self.get_z_threshold()
        z_filtered = {}
        for target, score in candidates.items():
            target_price = self.price_index.get(target, {}).get(ts)
            current_price = self.price_index.get(self.current_coin, {}).get(ts)
            if target_price is None:
                continue
            current_ratio = current_price / target_price
            z = self.get_z_score(self.current_coin, target, current_ratio)
            _, std = self.get_ratio_baseline(self.current_coin, target)
            if std and std > 0 and z < z_threshold:
                self.filter_stats["zscore_block"] += 1
                continue
            z_filtered[target] = score
        candidates = z_filtered

        if not candidates:
            return

        # Momentum + RSI filter
        filtered = {}
        for target, score in candidates.items():
            if not self.check_momentum(target, ts):
                continue
            if not self.check_rsi(target, ts):
                continue
            filtered[target] = score
        candidates = filtered

        if not candidates:
            return

        # Correlation penalty + BB squeeze bonus
        adjusted = {}
        for target, score in candidates.items():
            corr_penalty = self.get_correlation_penalty(self.current_coin, target, ts)
            bb_bonus = self.get_bb_squeeze_bonus(target, ts)
            adjusted[target] = score * corr_penalty * bb_bonus

        if not adjusted:
            return

        # Jump to best
        best_target = max(adjusted, key=lambda k: adjusted[k])
        best_score = adjusted[best_target]
        self._execute_trade(best_target, ts, best_score, self.regime)

    def _reenter(self, ts):
        """Re-enter from bridge after trailing stop."""
        # Cooldown
        if self.last_trade_ts:
            elapsed_hrs = (ts - self.last_trade_ts) / (3600 * 1000)
            if elapsed_hrs < 2.0:  # 2h re-entry delay
                return

        # Total bridge available
        total_bridge = self.balance + self.bridge_reserve

        if total_bridge < 5.0:
            return

        # Find best coin to buy
        best_coin = None
        best_score = -float("inf")

        for coin in COINS:
            if self.check_anti_churn(coin, ts):
                candles = [c for c in self.ohlcv.get(coin, []) if c["ts"] <= ts]
                if len(candles) >= 2:
                    perf = ((candles[-1]["close"] - candles[-2]["close"])
                            / candles[-2]["close"]) * 100
                    if perf > best_score:
                        best_score = perf
                        best_coin = coin

        if best_coin:
            price = self.price_index.get(best_coin, {}).get(ts)
            if price:
                buy_cost = total_bridge * (TAKER_FEE + SLIPPAGE)
                investable = total_bridge - buy_cost
                self.fee_total += buy_cost

                self.balance = investable / price
                self.current_coin = best_coin
                self.bridge_reserve = 0.0
                self.last_trade_ts = ts
                self.trade_count += 1
                self.entry_price = price
                self.peak_price = price

                if self.verbose:
                    print(f"  🔄 Re-entry: buying {best_coin} (${investable:.2f})")

    # ── Run ──────────────────────────────────────────────────────────────

    def run(self):
        """Run the full backtest."""
        regime_check_counter = 0

        for ts in self.timestamps:
            # Update peak value
            val = self.portfolio_value(ts)
            self.peak_value = max(self.peak_value, val)

            # Track max drawdown
            if val < self.peak_value:
                dd = ((self.peak_value - val) / self.peak_value) * 100
                self.max_drawdown = max(self.max_drawdown, dd)

            self.scout(ts)

        return self.results()

    def results(self):
        """Compile final results."""
        last_ts = self.timestamps[-1] if self.timestamps else 0

        if self.current_coin == BRIDGE:
            final_value = self.balance + self.bridge_reserve
        else:
            final_price = self.price_index.get(self.current_coin, {}).get(last_ts, 0)
            final_value = self.balance * final_price + self.bridge_reserve

        pnl_pct = ((final_value / self.initial_balance) - 1) * 100

        # Regime distribution
        regime_dist = defaultdict(int)
        for _, regime, _, _ in self.regime_history:
            regime_dist[regime] += 1

        return {
            "initial_balance": self.initial_balance,
            "final_value": final_value,
            "pnl_pct": pnl_pct,
            "trade_count": self.trade_count,
            "total_fees": self.fee_total,
            "fee_pct": (self.fee_total / self.initial_balance) * 100,
            "max_value": self.peak_value,
            "max_drawdown_pct": self.max_drawdown,
            "regime_distribution": dict(regime_dist),
            "filter_stats": dict(self.filter_stats),
            "trade_log": self.trade_log,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  MULTI-CONFIG RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run_config(ohlcv_by_coin, btc_ohlcv, months, config_name, params, verbose=False):
    """Run backtest with a specific parameter set."""
    # Apply parameter overrides
    global SCOUT_MULTIPLIER, MIN_PROFIT_THRESHOLD, Z_SCORE_THRESHOLD
    global BEAR_COOLDOWN_HRS, SIDEWAYS_COOLDOWN_HRS, ANTI_CHURN_HOURS
    global TRAILING_STOP_ENABLED, TRAILING_STOP_PCT, BEAR_POSITION_SIZE

    SCOUT_MULTIPLIER = params.get("scout_multiplier", 6)
    MIN_PROFIT_THRESHOLD = params.get("min_profit", 0.015)
    Z_SCORE_THRESHOLD = params.get("z_threshold", 1.5)
    BEAR_COOLDOWN_HRS = params.get("bear_cooldown", 2.0)
    SIDEWAYS_COOLDOWN_HRS = params.get("sideways_cooldown", 0.5)
    ANTI_CHURN_HOURS = params.get("anti_churn_hours", 6)
    TRAILING_STOP_ENABLED = params.get("trailing_stop", True)
    TRAILING_STOP_PCT = params.get("trailing_stop_pct", 15.0)
    BEAR_POSITION_SIZE = params.get("bear_position_size", 0.7)

    bt = FullBacktest(
        ohlcv_by_coin, btc_ohlcv,
        initial_balance=62.0,
        starting_coin="TIA",
        verbose=verbose,
    )
    return bt.run()


def main():
    parser = argparse.ArgumentParser(description="Full strategy backtest")
    parser.add_argument("--months", type=int, default=6, help="Months of history")
    parser.add_argument("--interval", default="1h", help="Candle interval")
    parser.add_argument("--verbose", action="store_true", help="Print trades/regime changes")
    parser.add_argument("--config", default="all", help="Config preset to run")
    args = parser.parse_args()

    days = args.months * 30

    # ── Fetch data ───────────────────────────────────────────────────────
    print(f"{'='*60}")
    print(f"  FULL STRATEGY BACKTEST — {args.months} months, {args.interval}")
    print(f"{'='*60}\n")

    print("Fetching historical data...")
    ohlcv_by_coin = {}

    for coin in COINS:
        symbol = f"{coin}{BRIDGE}"
        print(f"  {symbol}...", end=" ", flush=True)
        try:
            raw = fetch_klines(symbol, args.interval, days)
            ohlcv_by_coin[coin] = parse_ohlcv(raw)
            print(f"{len(ohlcv_by_coin[coin])} candles")
        except Exception as e:
            print(f"FAILED: {e}")
            ohlcv_by_coin[coin] = []
        time.sleep(0.2)

    # BTC for correlation
    print(f"  BTC{BRIDGE}...", end=" ", flush=True)
    try:
        raw = fetch_klines(f"BTC{BRIDGE}", args.interval, days)
        btc_ohlcv = parse_ohlcv(raw)
        print(f"{len(btc_ohlcv)} candles")
    except Exception as e:
        print(f"FAILED: {e}")
        btc_ohlcv = []

    print()

    # ── Config presets ───────────────────────────────────────────────────
    configs = [
        {
            "name": "Current (Live)",
            "params": {
                "scout_multiplier": 6, "min_profit": 0.015, "z_threshold": 1.5,
                "bear_cooldown": 2.0, "sideways_cooldown": 0.5,
                "anti_churn_hours": 6, "trailing_stop": True, "trailing_stop_pct": 15.0,
                "bear_position_size": 0.7,
            },
        },
        {
            "name": "Aggressive",
            "params": {
                "scout_multiplier": 3, "min_profit": 0.005, "z_threshold": 1.0,
                "bear_cooldown": 0.5, "sideways_cooldown": 0.25,
                "anti_churn_hours": 2, "trailing_stop": False, "bear_position_size": 1.0,
            },
        },
        {
            "name": "Ultra-Safe",
            "params": {
                "scout_multiplier": 10, "min_profit": 0.025, "z_threshold": 2.0,
                "bear_cooldown": 4.0, "sideways_cooldown": 2.0,
                "anti_churn_hours": 12, "trailing_stop": True, "trailing_stop_pct": 10.0,
                "bear_position_size": 0.5,
            },
        },
        {
            "name": "No Filters (baseline)",
            "params": {
                "scout_multiplier": 3, "min_profit": 0.0, "z_threshold": 0.0,
                "bear_cooldown": 0.0, "sideways_cooldown": 0.0,
                "anti_churn_hours": 0, "trailing_stop": False, "bear_position_size": 1.0,
            },
        },
    ]

    if args.config != "all":
        configs = [c for c in configs if c["name"].lower().startswith(args.config.lower())]
        if not configs:
            configs = [c for c in configs if args.config.lower() in c["name"].lower()]

    # ── Run all configs ──────────────────────────────────────────────────
    results_list = []
    for cfg in configs:
        print(f"\n{'─'*50}")
        print(f"  Config: {cfg['name']}")
        print(f"{'─'*50}")

        result = run_config(
            ohlcv_by_coin, btc_ohlcv, args.months,
            cfg["name"], cfg["params"],
            verbose=args.verbose,
        )
        result["config_name"] = cfg["name"]
        results_list.append(result)

        pnl = result["pnl_pct"]
        trades = result["trade_count"]
        fees = result["fee_pct"]
        dd = result["max_drawdown_pct"]

        print(f"  P&L:        {pnl:+.2f}% (${result['final_value']:.2f} from ${result['initial_balance']:.2f})")
        print(f"  Trades:     {trades} ({trades / (args.months * 30):.1f}/day avg)")
        print(f"  Fees:       ${result['total_fees']:.2f} ({fees:.1f}% of initial)")
        if trades > 0:
            print(f"  Avg/trade:  {pnl / trades:+.3f}%")
        print(f"  Max DD:     {dd:.1f}%")

        regime_dist = result.get("regime_distribution", {})
        if regime_dist:
            total_r = sum(regime_dist.values())
            regime_str = " | ".join(
                f"{k}: {v/total_r*100:.0f}%" for k, v in
                sorted(regime_dist.items(), key=lambda x: -x[1])
            )
            print(f"  Regimes:    {regime_str}")

        fstats = result.get("filter_stats", {})
        if fstats and sum(fstats.values()) > 0:
            total_blocks = sum(fstats.values())
            top_filters = sorted(fstats.items(), key=lambda x: -x[1])[:5]
            filter_str = " | ".join(f"{k}: {v}" for k, v in top_filters)
            print(f"  Blocks:     {filter_str}")

    # ── Summary table ────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  SUMMARY — {args.months} months")
    print(f"{'='*70}")
    print(f"{'Config':<22} {'P&L':>8} {'Trades':>8} {'Fees%':>7} {'MaxDD':>7} {'$/Trade':>8}")
    print("─" * 70)
    for r in results_list:
        pnl = r["pnl_pct"]
        trades = r["trade_count"]
        fees = r["fee_pct"]
        dd = r["max_drawdown_pct"]
        per_trade = pnl / trades if trades > 0 else 0
        print(f"{r['config_name']:<22} {pnl:>+7.1f}% {trades:>8} {fees:>6.1f}% {dd:>6.1f}% {per_trade:>+7.2f}%")

    # ── Buy & hold comparison ────────────────────────────────────────────
    print(f"\n{'─'*50}")
    print("  BUY & HOLD COMPARISON")
    print(f"{'─'*50}")
    for coin in ["TIA", "SOL", "BTC"]:
        if coin == "BTC":
            data = btc_ohlcv
        else:
            data = ohlcv_by_coin.get(coin, [])
        if data and len(data) >= 2:
            bh_pnl = ((data[-1]["close"] / data[0]["close"]) - 1) * 100
            print(f"  {coin:>4} buy & hold: {bh_pnl:+.1f}%")

    # ── Trade log for current config ─────────────────────────────────────
    if args.verbose and results_list:
        live_result = next((r for r in results_list if "Current" in r["config_name"]), results_list[0])
        log = live_result.get("trade_log", [])
        if log:
            print(f"\n{'─'*50}")
            print(f"  TRADE LOG ({live_result['config_name']}) — first 20 + last 10")
            print(f"{'─'*50}")
            for t in log[:20]:
                reason = f" [{t.get('reason', '')}]" if t.get("reason") else ""
                print(f"  {t['time']} {t['from']:>5} → {t['to']:<5} "
                      f"${t['value']:.2f} fee:${t['fee']:.3f} "
                      f"score:{t['score']:.6f} {t['regime']}{reason}")
            if len(log) > 30:
                print(f"  ... ({len(log) - 30} more trades)")
            for t in log[-10:]:
                reason = f" [{t.get('reason', '')}]" if t.get("reason") else ""
                print(f"  {t['time']} {t['from']:>5} → {t['to']:<5} "
                      f"${t['value']:.2f} fee:${t['fee']:.3f} "
                      f"score:{t['score']:.6f} {t['regime']}{reason}")

    print(f"\n{'='*70}")
    print("  Backtest complete.")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
