"""
Adaptive Multi-Regime Trading Strategy

Detects market conditions and switches tactics accordingly:
  - SIDEWAYS: Mean reversion (buy dips, sell rips)
  - BULL:     Momentum following (buy strength, hold longer)
  - BEAR:     Capital preservation (defensive, bank gains fast)
  - STORMY:   Conservative (high volatility, extreme thresholds only)

Also includes:
  - Phase 1: Percentage-based scoring
  - Phase 2: Rolling EMA baseline
  - Phase 3: Z-score volatility-scaled threshold
  - Phase 4: Momentum filter (skip falling knives)
  - Phase 5: Market regime detection (4-way classification)
  - Phase 6: Trade cooldown + periodic USDC profit-taking
  - Phase D: Trailing stop-loss (auto-sell at -N% from peak)
"""

import time
import sys
import math
import random
from datetime import datetime

from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.indicators import (
    compute_ema as _compute_ema_func,
    compute_adx as _compute_adx_func,
    compute_rsi as _compute_rsi_func,
    compute_bollinger_bands as _compute_bb_func,
    detect_bollinger_squeeze as _detect_bb_squeeze,
    compute_correlation as _compute_corr_func,
    compute_returns as _compute_returns_func,
    compute_correlation_matrix as _compute_corr_matrix,
)


# ── Regime constants ─────────────────────────────────────────────────────────
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"

REGIME_EMOJI = {
    BULL: "🟢",
    BEAR: "🔴",
    SIDEWAYS: "🟡",
    STORMY: "🟠",
}

REGIME_DESC = {
    BULL: "Momentum mode — buying strength, holding longer",
    BEAR: "Defense mode — preserving capital, banking gains fast",
    SIDEWAYS: "Mean reversion — buying dips, selling rips",
    STORMY: "Conservative — extreme thresholds only",
}


class Strategy(AutoTrader):
    def initialize(self):
        super().initialize()  # initialize_trade_thresholds
        self.initialize_current_coin()

        # Regime state
        self._market_regime = SIDEWAYS
        self._last_regime_check = 0
        self._regime_adx = 0.0
        self._regime_volatility = 0.0
        self._regime_btc_corr = 0.0

        # Trade cooldown + profit-taking state
        self._last_trade_time = 0
        self._trades_since_profit_take = 0  # Start at 0, increment per trade
        self._awaiting_reentry = False

        # Trailing stop-loss state
        self._position_entry_price = {}  # coin_symbol -> entry price
        self._position_peak_price = {}   # coin_symbol -> peak price since entry
        self._last_sold_coin = None      # Track what we just sold (avoid immediate buyback)
        self._profit_take_time = 0       # When profit-taking happened (for re-entry delay)

        # Anti-churn: track recently held coins with timestamps
        # Prevents the bot from rapidly cycling TIA→ENA→TIA→ENA
        self._recently_held = {}  # coin_symbol -> timestamp when sold
        self._churn_block_seconds = getattr(self.config, 'CHURN_BLOCK_SECONDS', 14400)  # 4 hours

        # Indicator cache: avoid recomputing RSI/correlation/BB every scout cycle (1s)
        # TTL is configurable, default 5 minutes
        self._indicator_cache = {}  # key -> (value, timestamp)
        self._cache_ttl = getattr(self.config, 'INDICATOR_CACHE_TTL', 300)

    # ─────────────────────────────────────────────────────────────────────────
    #  TECHNICAL INDICATORS (delegated to indicators.py)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_ema(values, period):
        return _compute_ema_func(values, period)

    @staticmethod
    def _compute_adx(highs, lows, closes, period=14):
        return _compute_adx_func(highs, lows, closes, period)

    # ─────────────────────────────────────────────────────────────────────────
    #  INDICATOR CACHE HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _cache_get(self, key):
        """Get cached value if fresh, else return None."""
        entry = self._indicator_cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.time() - ts > self._cache_ttl:
            del self._indicator_cache[key]
            return None
        return value

    def _cache_set(self, key, value):
        """Store value in cache with current timestamp."""
        self._indicator_cache[key] = (value, time.time())

    def _cached_klines(self, coin_symbol, interval="1h", limit=50):
        """Fetch klines with caching to avoid API spam."""
        key = f"klines:{coin_symbol}:{interval}:{limit}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval=interval, limit=limit,
            )
            self._cache_set(key, klines)
            return klines
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    #  PHASE A: MARKET REGIME DETECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _update_market_regime(self):
        """Classify the market into bull, bear, sideways, or stormy."""
        now = time.time()
        if now - self._last_regime_check < self.config.REGIME_CHECK_INTERVAL:
            return
        self._last_regime_check = now

        try:
            # Use a representative coin for ADX/EMA calculation
            # Prefer SOL (high liquidity), fallback to first enabled coin
            coins = self.db.get_coins()
            if not coins:
                return

            ref_coin = None
            for c in coins:
                if c.symbol == "SOL":
                    ref_coin = c
                    break
            if not ref_coin:
                ref_coin = coins[0]

            symbol = f"{ref_coin.symbol}{self.config.BRIDGE.symbol}"

            # Fetch klines (1h candles, enough for ADX + EMA)
            klines = self.manager.binance_client.get_klines(
                symbol=symbol,
                interval="1h",
                limit=max(self.config.EMA_LONG * 2, self.config.ADX_PERIOD * 3),
            )

            if not klines or len(klines) < 30:
                self.logger.warning(f"Not enough klines for regime detection ({len(klines) if klines else 0})")
                return

            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]

            # ADX
            adx, plus_di, minus_di = self._compute_adx(highs, lows, closes, self.config.ADX_PERIOD)
            self._regime_adx = adx

            # EMA
            ema_short = self._compute_ema(closes, self.config.EMA_SHORT)
            ema_long = self._compute_ema(closes, self.config.EMA_LONG)
            current_price = closes[-1]

            # Average volatility across all coins
            avg_volatility = self._compute_avg_volatility(coins)
            self._regime_volatility = avg_volatility

            # BTC correlation: how correlated is this coin with BTC?
            # High correlation + bearish BTC = more defensive
            btc_corr = None
            if getattr(self.config, 'BTC_CORRELATION_ENABLED', False):
                try:
                    btc_klines = self.manager.binance_client.get_klines(
                        symbol=f"BTC{self.config.BRIDGE.symbol}",
                        interval="1h",
                        limit=min(len(closes), 50),
                    )
                    if btc_klines and len(btc_klines) >= 10:
                        btc_closes = [float(k[4]) for k in btc_klines]
                        ref_returns = _compute_returns_func(closes[-len(btc_closes):])
                        btc_returns = _compute_returns_func(btc_closes)
                        btc_corr = _compute_corr_func(ref_returns, btc_returns)
                        self._regime_btc_corr = btc_corr
                except Exception:
                    pass

            # Classify
            old_regime = self._market_regime
            is_trending = adx >= self.config.ADX_TREND_THRESHOLD
            is_stormy = avg_volatility > self.config.REGIME_HIGH_VOL_THRESHOLD

            if is_stormy and not is_trending:
                new_regime = STORMY
            elif is_trending:
                # Determine bull vs bear
                if ema_short and ema_long and current_price > ema_long:
                    if plus_di > minus_di:
                        new_regime = BULL
                    else:
                        new_regime = SIDEWAYS  # ADX says trend but -DI dominant = choppy
                elif ema_short and ema_long and current_price < ema_long:
                    if minus_di > plus_di:
                        new_regime = BEAR
                    else:
                        new_regime = SIDEWAYS
                else:
                    new_regime = SIDEWAYS
            else:
                new_regime = SIDEWAYS

            # Override: stormy takes priority over sideways
            if is_stormy and new_regime == SIDEWAYS:
                new_regime = STORMY

            self._market_regime = new_regime

            if new_regime != old_regime:
                corr_str = f", BTC corr: {btc_corr:.2f}" if btc_corr is not None else ""
                self.logger.warning(
                    f"Market regime: {old_regime.upper()} → {new_regime.upper()} "
                    f"(ADX: {adx:.1f}, Vol: {avg_volatility:.1f}%, "
                    f"EMA20: {ema_short:.4f}, EMA50: {ema_long:.4f}, "
                    f"Price: {current_price:.4f}, +DI: {plus_di:.1f}, -DI: {minus_di:.1f}{corr_str})"
                )

            # Log to DB
            self.db.log_market_regime(
                regime=new_regime,
                adx_value=adx,
                avg_volatility=avg_volatility,
                btc_correlation=btc_corr,
                ema_short=ema_short,
                ema_long=ema_long,
            )

        except Exception as e:
            self.logger.warning(f"Regime detection failed: {e}")

    def _compute_avg_volatility(self, coins):
        """Compute average absolute 24h % change across all enabled coins."""
        try:
            total_abs_change = 0.0
            count = 0
            for coin in coins:
                try:
                    ticker = self.manager.binance_client.get_ticker(
                        symbol=f"{coin.symbol}{self.config.BRIDGE.symbol}"
                    )
                    if ticker and "priceChangePercent" in ticker:
                        total_abs_change += abs(float(ticker["priceChangePercent"]))
                        count += 1
                except Exception:
                    continue
            return total_abs_change / count if count > 0 else 0.0
        except Exception:
            return 0.0

    # ─────────────────────────────────────────────────────────────────────────
    #  REGIME-AWARE PARAMETERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_z_score_threshold(self):
        """Return z-score threshold adjusted for current regime."""
        base = self.config.Z_SCORE_THRESHOLD
        if self._market_regime == BULL:
            return base * self.config.BULL_ZSCORE_MULT  # looser — ride trends
        elif self._market_regime == BEAR:
            return base * self.config.BEAR_ZSCORE_MULT  # tighter — be selective
        elif self._market_regime == STORMY:
            return base * self.config.REGIME_Z_SCORE_MULTIPLIER  # very tight
        return base  # SIDEWAYS

    def _get_cooldown_seconds(self):
        """Return cooldown based on regime."""
        if self._market_regime == BULL:
            return self.config.BULL_COOLDOWN
        elif self._market_regime == BEAR:
            return self.config.BEAR_COOLDOWN
        return self.config.TRADE_COOLDOWN_SECONDS

    def _get_profit_take_interval(self):
        """Return profit-taking interval based on regime."""
        if self._market_regime == BULL:
            return self.config.BULL_PROFIT_TAKE_INTERVAL
        elif self._market_regime == BEAR:
            return self.config.BEAR_PROFIT_TAKE_INTERVAL
        return self.config.PROFIT_TAKING_INTERVAL

    def _get_momentum_threshold(self):
        """Return momentum crash threshold based on regime."""
        if self._market_regime == BEAR:
            return self.config.BEAR_MOMENTUM_MAX_DROP  # stricter in bear
        return self.config.MOMENTUM_MAX_DROP_1H

    # ─────────────────────────────────────────────────────────────────────────
    #  SCORING (Phase 1 + 2)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_ratios(self, coin, coin_price):
        """
        Compute percentage-based scores for every enabled pair.
        In BULL mode, scoring is inverted to favor momentum (relative strength).
        """
        ratio_dict = {}

        for pair in self.db.get_pairs_from(coin):
            optional_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)

            if optional_coin_price is None:
                self.logger.info(f"Skipping scouting... optional coin {pair.to_coin + self.config.BRIDGE} not found")
                continue

            self.db.log_scout(pair, pair.ratio or 0.0, coin_price, optional_coin_price)

            current_ratio = coin_price / optional_coin_price

            from_fee = self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True)
            to_fee = self.manager.get_fee(pair.to_coin, self.config.BRIDGE, False)
            transaction_fee = from_fee + to_fee - from_fee * to_fee

            # Phase 2: Use rolling EMA as baseline
            ema_ratio, _std = self.db.get_pair_stat(pair.id)
            baseline = ema_ratio if ema_ratio is not None else pair.ratio
            if baseline is None or baseline <= 0:
                continue

            # Phase 1: Percentage-based score
            pct_gain = (current_ratio / baseline) - 1.0
            fee_hurdle = transaction_fee * self.config.SCOUT_MULTIPLIER
            score = pct_gain - fee_hurdle

            ratio_dict[pair] = score

        return ratio_dict

    def _get_momentum_scores(self, coin, coin_price):
        """
        BULL MODE: Score coins by relative strength (momentum).
        Instead of mean reversion, buy coins that are outperforming.
        Returns dict: Pair -> float (higher = stronger momentum).
        """
        ratio_dict = {}
        coins = self.db.get_coins()

        # Get 24h performance for all coins
        performance = {}
        for c in coins:
            try:
                ticker = self.manager.binance_client.get_ticker(
                    symbol=f"{c.symbol}{self.config.BRIDGE.symbol}"
                )
                if ticker and "priceChangePercent" in ticker:
                    performance[c.symbol] = float(ticker["priceChangePercent"])
            except Exception:
                continue

        current_perf = performance.get(coin.symbol, 0.0)

        for pair in self.db.get_pairs_from(coin):
            optional_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)
            if optional_coin_price is None:
                continue

            self.db.log_scout(pair, pair.ratio, coin_price, optional_coin_price)

            # Relative strength: how much is target outperforming current coin?
            target_perf = performance.get(pair.to_coin_id, 0.0)
            relative_strength = target_perf - current_perf

            # Fees
            from_fee = self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True)
            to_fee = self.manager.get_fee(pair.to_coin, self.config.BRIDGE, False)
            transaction_fee = from_fee + to_fee - from_fee * to_fee

            # Score = relative strength minus fee hurdle
            # Convert % to decimal
            score = (relative_strength / 100.0) - (transaction_fee * self.config.SCOUT_MULTIPLIER)

            ratio_dict[pair] = score

        return ratio_dict

    # ─────────────────────────────────────────────────────────────────────────
    #  PHASE 3: Z-SCORE
    # ─────────────────────────────────────────────────────────────────────────

    def _get_z_score(self, pair, current_ratio):
        """Get z-score for a pair (std devs from EMA). Returns 0 if no data."""
        ema, std = self.db.get_pair_stat(pair.id)
        if ema is not None and std and std > 0:
            return (current_ratio - ema) / std
        return 0.0

    # ─────────────────────────────────────────────────────────────────────────
    #  PHASE 4: MOMENTUM FILTER
    # ─────────────────────────────────────────────────────────────────────────

    def _check_momentum(self, coin_symbol):
        """Check if a coin is crashing. Returns True if safe to buy."""
        if not self.config.MOMENTUM_FILTER_ENABLED:
            return True

        try:
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval="1h",
                limit=2,
            )
            if not klines or len(klines) < 1:
                return True

            candle = klines[-1]
            open_price = float(candle[1])
            close_price = float(candle[4])
            change_pct = ((close_price - open_price) / open_price) * 100

            threshold = self._get_momentum_threshold()

            if change_pct < -threshold:
                self.logger.info(
                    f"Momentum filter: skipping {coin_symbol} "
                    f"(1h change: {change_pct:+.2f}%, threshold: -{threshold}%)"
                )
                return False
            return True
        except Exception as e:
            self.logger.warning(f"Momentum check failed for {coin_symbol}: {e}")
            return True  # Fail open

    def _get_rsi(self, coin_symbol, period=14):
        """Calculate RSI for a coin with caching. Returns 0-100, or None."""
        cache_key = f"rsi:{coin_symbol}:{period}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            klines = self._cached_klines(coin_symbol, "1h", period + 2)
            if not klines or len(klines) < period + 1:
                return None
            closes = [float(k[4]) for k in klines]
            rsi = _compute_rsi_func(closes, period)
            if rsi is not None:
                self._cache_set(cache_key, rsi)
            return rsi
        except Exception:
            return None

    def _check_rsi_filter(self, coin_symbol):
        """RSI filter: only buy when RSI indicates the coin is oversold or fair-valued.
        Returns True if OK to buy, False if coin is overbought (avoid buying at the top)."""
        if not getattr(self.config, 'RSI_FILTER_ENABLED', False):
            return True

        rsi = self._get_rsi(coin_symbol)
        if rsi is None:
            return True  # Not enough data, allow

        overbought = getattr(self.config, 'RSI_OVERBOUGHT', 70)
        if rsi > overbought:
            self.logger.info(
                f"RSI filter: skipping {coin_symbol} "
                f"(RSI: {rsi:.1f}, overbought threshold: {overbought})"
            )
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    #  FEATURE 4: CORRELATION-BASED COIN SELECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _get_correlation_penalty(self, current_coin_symbol, target_coin_symbol):
        """Compute correlation between current and target coin with caching.
        Returns penalty multiplier (0-1) to reduce score for highly correlated pairs."""
        if not getattr(self.config, 'CORRELATION_FILTER_ENABLED', False):
            return 1.0

        # Check cache
        pair_key = f"corr:{current_coin_symbol}:{target_coin_symbol}"
        cached = self._cache_get(pair_key)
        if cached is not None:
            return cached

        try:
            current_klines = self._cached_klines(current_coin_symbol, "1h", 50)
            target_klines = self._cached_klines(target_coin_symbol, "1h", 50)
            if not current_klines or not target_klines:
                return 1.0

            current_closes = [float(k[4]) for k in current_klines]
            target_closes = [float(k[4]) for k in target_klines]

            min_len = min(len(current_closes), len(target_closes))
            current_returns = _compute_returns_func(current_closes[-min_len:])
            target_returns = _compute_returns_func(target_closes[-min_len:])

            corr = _compute_corr_func(current_returns, target_returns)
            threshold = getattr(self.config, 'CORRELATION_THRESHOLD', 0.85)

            if abs(corr) > threshold:
                # Penalty: reduce score proportionally to how far above threshold
                excess = (abs(corr) - threshold) / (1.0 - threshold)
                penalty = max(0.2, 1.0 - excess * 0.5)
                self.logger.debug(
                    f"Correlation penalty for {current_coin_symbol}→{target_coin_symbol}: "
                    f"corr={corr:.2f}, penalty={penalty:.2f}"
                )
                self._cache_set(pair_key, penalty)
                return penalty
            self._cache_set(pair_key, 1.0)
            return 1.0
        except Exception:
            return 1.0

    # ─────────────────────────────────────────────────────────────────────────
    #  FEATURE 5: BOLLINGER BAND SQUEEZE DETECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _check_bb_squeeze_bonus(self, coin_symbol):
        """Detect Bollinger Band squeeze with caching.
        Returns a multiplier (1.0 = no bonus, up to ~1.3 for strong squeeze)."""
        if not getattr(self.config, 'BB_SQUEEZE_ENABLED', False):
            return 1.0

        cache_key = f"bb:{coin_symbol}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            period = getattr(self.config, 'BB_PERIOD', 20)
            lookback = getattr(self.config, 'BB_SQUEEZE_LOOKBACK', 50)
            klines = self._cached_klines(coin_symbol, "1h", period + lookback)
            if not klines or len(klines) < period + 10:
                return 1.0

            closes = [float(k[4]) for k in klines]
            is_squeeze, bandwidth, percentile = _detect_bb_squeeze(
                closes, period=period, squeeze_lookback=lookback
            )

            if is_squeeze:
                bonus = 1.0 + (1.0 - percentile / 20.0) * 0.3
                self.logger.info(
                    f"BB squeeze detected for {coin_symbol} "
                    f"(bandwidth: {bandwidth:.4f}, percentile: {percentile:.0f}%, "
                    f"bonus: {bonus:.2f}x)"
                )
                self._cache_set(cache_key, bonus)
                return bonus
            self._cache_set(cache_key, 1.0)
            return 1.0
        except Exception:
            return 1.0

    # ─────────────────────────────────────────────────────────────────────────
    #  PHASE D: TRAILING STOP-LOSS
    # ─────────────────────────────────────────────────────────────────────────

    def _check_trailing_stop(self, current_coin, current_coin_price):
        """Check if current position should be stopped out."""
        if not self.config.TRAILING_STOP_ENABLED:
            return False

        symbol = current_coin.symbol

        # Track entry price on first sighting
        if symbol not in self._position_entry_price:
            self._position_entry_price[symbol] = current_coin_price
            self._position_peak_price[symbol] = current_coin_price
            return False

        # Update peak
        if current_coin_price > self._position_peak_price.get(symbol, 0):
            self._position_peak_price[symbol] = current_coin_price

        peak = self._position_peak_price[symbol]
        drop_pct = ((peak - current_coin_price) / peak) * 100 if peak > 0 else 0

        if drop_pct >= self.config.TRAILING_STOP_PCT:
            self.logger.warning(
                f"Trailing stop triggered for {symbol}: "
                f"dropped {drop_pct:.1f}% from peak ${peak:.6f}"
            )
            # Sell to USDC
            balance = self.manager.get_currency_balance(symbol)
            if balance and balance * current_coin_price > self.manager.get_min_notional(
                symbol, self.config.BRIDGE.symbol
            ):
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is not None:
                    self._awaiting_reentry = True
                    self._trades_since_profit_take = 0
                    # Clear tracking
                    self._position_entry_price.pop(symbol, None)
                    self._position_peak_price.pop(symbol, None)
                    self.logger.warning(f"Trailing stop executed: sold {symbol} to {self.config.BRIDGE.symbol}")
                    return True

        return False

    def _reset_position_tracking(self, new_coin_symbol, entry_price):
        """Reset tracking when we buy a new coin."""
        self._position_entry_price = {new_coin_symbol: entry_price}
        self._position_peak_price = {new_coin_symbol: entry_price}

    # ─────────────────────────────────────────────────────────────────────────
    #  PHASE 6: PROFIT-TAKING
    # ─────────────────────────────────────────────────────────────────────────

    def _count_completed_trades(self):
        """Count total completed trades in history."""
        try:
            from binance_trade_bot.models import Trade, TradeState
            with self.db.db_session() as session:
                return session.query(Trade).filter(Trade.state == TradeState.COMPLETE).count()
        except Exception:
            return 0

    def _check_profit_taking(self, current_coin, current_coin_price):
        """Every N trades, sell to USDC and set re-entry flag."""
        if not self.config.PROFIT_TAKING_ENABLED:
            return False

        interval = self._get_profit_take_interval()
        if self._trades_since_profit_take < interval:
            return False

        self.logger.info(
            f"Profit-taking triggered after {self._trades_since_profit_take} trades "
            f"(regime: {self._market_regime}, interval: {interval})"
        )

        balance = self.manager.get_currency_balance(current_coin.symbol)
        if balance and balance * current_coin_price > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            self.logger.info(f"Profit-taking: selling {current_coin} to {self.config.BRIDGE.symbol}")
            result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
            if result is not None:
                # Verify the sell actually executed — check remaining balance
                time.sleep(1)  # Brief pause for order to process
                remaining = self.manager.get_currency_balance(current_coin.symbol)
                if remaining and remaining * current_coin_price > self.manager.get_min_notional(
                    current_coin.symbol, self.config.BRIDGE.symbol
                ):
                    self.logger.warning(
                        f"Profit-taking: sell order placed but {remaining} {current_coin.symbol} "
                        f"still held — order may not have filled. Aborting re-entry."
                    )
                    return False
                self._awaiting_reentry = True
                self._trades_since_profit_take = 0
                self._last_sold_coin = current_coin.symbol
                self._profit_take_time = time.time()
                self._position_entry_price.pop(current_coin.symbol, None)
                self._position_peak_price.pop(current_coin.symbol, None)
                self.logger.info("Profit-taking complete, awaiting re-entry signal")
                return True
            else:
                self.logger.warning("Profit-taking sell failed, continuing normal operation")

        return False

    def _reenter_from_usdc(self):
        """Find the best coin to buy back after profit-taking or stop-loss."""
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

        # Use min_notional threshold, not just > 0
        min_threshold = 5.0  # $5 conservative default
        if bridge_balance < min_threshold:
            if not hasattr(self, '_reentry_wait_logged') or not self._reentry_wait_logged:
                self.logger.info(
                    f"Re-entry: bridge balance {bridge_balance} {self.config.BRIDGE.symbol} "
                    f"below ${min_threshold} threshold, waiting..."
                )
                self._reentry_wait_logged = True
            return
        self._reentry_wait_logged = False

        # Don't re-enter immediately — wait at least 2 minutes after profit-taking
        # to avoid buying back the same coin at the same price (paying fees for nothing)
        if self._profit_take_time > 0:
            elapsed = time.time() - self._profit_take_time
            if elapsed < 120:
                return  # Silently wait

        self.logger.info(
            f"Re-entry scouting with {bridge_balance} {self.config.BRIDGE.symbol} "
            f"(regime: {self._market_regime})"
        )

        best_coin = None
        best_score = -float("inf")

        for coin in self.db.get_coins():
            # Skip the coin we just sold — don't buy it back immediately
            if self._last_sold_coin and coin.symbol == self._last_sold_coin:
                continue

            # Anti-churn: don't re-buy recently held coins
            if self._is_churn_blocked(coin.symbol):
                continue

            coin_price = self.manager.get_ticker_price(coin + self.config.BRIDGE)
            if coin_price is None:
                continue

            min_notional = self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol)
            if bridge_balance < min_notional:
                continue

            # Momentum filter + RSI filter
            if not self._check_momentum(coin.symbol):
                continue
            if not self._check_rsi_filter(coin.symbol):
                continue

            if self._market_regime == BULL:
                # In bull mode: buy the strongest performer
                try:
                    ticker = self.manager.binance_client.get_ticker(
                        symbol=f"{coin.symbol}{self.config.BRIDGE.symbol}"
                    )
                    perf = float(ticker.get("priceChangePercent", 0)) if ticker else 0.0
                    if perf > best_score:
                        best_score = perf
                        best_coin = coin
                except Exception:
                    continue
            elif self._market_regime == BEAR:
                # In bear mode: buy the least bad (lowest negative performance)
                try:
                    ticker = self.manager.binance_client.get_ticker(
                        symbol=f"{coin.symbol}{self.config.BRIDGE.symbol}"
                    )
                    perf = float(ticker.get("priceChangePercent", 0)) if ticker else 0.0
                    # Require z-score signal (don't buy on random noise)
                    if perf > best_score:
                        best_score = perf
                        best_coin = coin
                except Exception:
                    continue
            else:
                # Sideways/stormy: buy most undervalued (original logic)
                ratio_dict = self._get_ratios(coin, coin_price)
                if ratio_dict and all(v < 0 for v in ratio_dict.values()):
                    avg_score = sum(ratio_dict.values()) / len(ratio_dict)
                    if avg_score > best_score:
                        best_score = avg_score
                        best_coin = coin

        if best_coin is not None:
            self.logger.info(f"Re-entry: buying {best_coin} (regime: {self._market_regime})")
            result = self.manager.buy_alt(best_coin, self.config.BRIDGE)
            if result is not None:
                self.db.set_current_coin(best_coin)
                self._awaiting_reentry = False
                self._last_trade_time = time.time()
                entry_price = self.manager.get_ticker_price(best_coin + self.config.BRIDGE)
                if entry_price:
                    self._reset_position_tracking(best_coin.symbol, entry_price)
                self.logger.info(f"Re-entry complete: now holding {best_coin}")
        else:
            self.logger.info("No clear re-entry opportunity yet, waiting...")

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN JUMP LOGIC (regime-aware)
    # ─────────────────────────────────────────────────────────────────────────

    def _is_churn_blocked(self, coin_symbol):
        """Check if a coin was recently held and shouldn't be re-bought yet."""
        sold_time = self._recently_held.get(coin_symbol)
        if sold_time is None:
            return False
        elapsed = time.time() - sold_time
        if elapsed < self._churn_block_seconds:
            return True
        # Expired — clean up
        del self._recently_held[coin_symbol]
        return False

    def transaction_through_bridge(self, pair):
        """Override with dynamic position sizing support.
        
        In bear/sideways mode, only deploy a fraction of bridge balance,
        keeping the rest as dry powder for buying dips.
        """
        # Standard sell logic
        balance = self.manager.get_currency_balance(pair.from_coin.symbol)
        from_coin_price = self.manager.get_ticker_price(pair.from_coin + self.config.BRIDGE)

        if not balance or not from_coin_price or balance * from_coin_price <= self.manager.get_min_notional(
            pair.from_coin.symbol, self.config.BRIDGE.symbol
        ):
            self.logger.info("Skipping sell - not enough balance")
            return None

        if self.manager.sell_alt(pair.from_coin, self.config.BRIDGE) is None:
            self.logger.info("Couldn't sell, going back to scouting mode...")
            return None

        # Feature 3: Dynamic position sizing
        max_balance = None
        if getattr(self.config, 'DYNAMIC_POSITION_ENABLED', False):
            if self._market_regime == BEAR:
                position_pct = getattr(self.config, 'BEAR_POSITION_SIZE', 0.7)
            elif self._market_regime == SIDEWAYS:
                position_pct = getattr(self.config, 'SIDEWAYS_POSITION_SIZE', 0.9)
            else:
                position_pct = 1.0  # Full position in bull mode

            if position_pct < 1.0:
                bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
                max_balance = bridge_balance * position_pct
                reserve = bridge_balance - max_balance
                if reserve >= 5.0:  # Only keep reserve if it's above min notional
                    self.logger.info(
                        f"Dynamic position sizing: deploying {position_pct*100:.0f}% "
                        f"(${max_balance:.2f}), keeping ${reserve:.2f} as dry powder "
                        f"(regime: {self._market_regime})"
                    )
                else:
                    max_balance = None  # Reserve too small, go all in

        result = self.manager.buy_alt(pair.to_coin, self.config.BRIDGE, max_target_balance=max_balance)
        if result is not None:
            self.db.set_current_coin(pair.to_coin)
            self.update_trade_threshold(pair.to_coin, result.price)
            return result

        self.logger.info("Couldn't buy, going back to scouting mode...")
        return None

    def _jump_to_best_coin(self, coin, coin_price):
        """Find the best coin to jump to, applying regime-specific logic."""
        # Get scores based on regime
        if self._market_regime == BULL:
            ratio_dict = self._get_momentum_scores(coin, coin_price)
        else:
            ratio_dict = self._get_ratios(coin, coin_price)

        # Phase 1: keep only positive scores ABOVE minimum profit threshold
        min_profit = getattr(self.config, 'MIN_PROFIT_THRESHOLD', 0.01)
        candidates = {k: v for k, v in ratio_dict.items() if v > min_profit}
        if not candidates:
            return

        # Anti-churn: filter out coins held recently
        candidates = {
            k: v for k, v in candidates.items()
            if not self._is_churn_blocked(k.to_coin_id)
        }
        if not candidates:
            self.logger.debug(
                f"All candidates blocked by anti-churn rule "
                f"(recently_held: {list(self._recently_held.keys())})"
            )
            return

        # Phase 3: Z-score filter (regime-aware)
        # Skip z-score in bull mode (momentum doesn't use mean-reversion baselines)
        if self._market_regime != BULL:
            z_threshold = self._get_z_score_threshold()
            z_filtered = {}
            for pair, score in candidates.items():
                other_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)
                if other_price is None or other_price == 0:
                    continue
                current_ratio = coin_price / other_price
                z = self._get_z_score(pair, current_ratio)
                ema, std = self.db.get_pair_stat(pair.id)
                if std is not None and std > 0 and z < z_threshold:
                    self.logger.debug(
                        f"Z-score filter: {pair.to_coin_id} blocked "
                        f"(z={z:.2f}, need {z_threshold:.2f})"
                    )
                    continue
                z_filtered[pair] = score
            candidates = z_filtered

        if not candidates:
            return

        # Phase 4: Momentum filter + RSI filter
        momentum_filtered = {}
        for pair, score in candidates.items():
            if not self._check_momentum(pair.to_coin_id):
                continue
            if not self._check_rsi_filter(pair.to_coin_id):
                continue
            momentum_filtered[pair] = score

        if not momentum_filtered:
            self.logger.info("All candidates filtered out by momentum/RSI filter")
            return

        # Feature 4: Apply correlation penalty (reduce score for highly correlated coins)
        # Feature 5: Apply BB squeeze bonus (boost score for coins about to break out)
        adjusted = {}
        for pair, score in momentum_filtered.items():
            corr_penalty = self._get_correlation_penalty(coin.symbol, pair.to_coin_id)
            bb_bonus = self._check_bb_squeeze_bonus(pair.to_coin_id)
            adjusted_score = score * corr_penalty * bb_bonus
            adjusted[pair] = adjusted_score

        if not adjusted:
            return

        # Jump to best remaining candidate
        best_pair = max(adjusted, key=adjusted.get)
        self.logger.info(
            f"Jumping from {coin} to {best_pair.to_coin_id} "
            f"(score: {momentum_filtered[best_pair]:.6f}, "
            f"regime: {self._market_regime}, "
            f"ADX: {self._regime_adx:.1f})"
        )

        result = self.transaction_through_bridge(best_pair)
        if result is not None:
            self._last_trade_time = time.time()
            self._trades_since_profit_take += 1
            # Anti-churn: record the coin we're leaving
            self._recently_held[coin.symbol] = time.time()
            # Track new position for trailing stop
            new_price = self.manager.get_ticker_price(best_pair.to_coin + self.config.BRIDGE)
            if new_price:
                self._reset_position_tracking(best_pair.to_coin_id, new_price)

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN SCOUT LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def scout(self):
        """Main scouting loop with adaptive regime logic."""

        # Phase A: Update market regime (cached)
        self._update_market_regime()

        # Auto-detect: if we're holding mostly USDC, we're in re-entry mode
        # (handles container restarts where in-memory state is lost)
        if not self._awaiting_reentry:
            bridge_bal = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
            current_coin = self.db.get_current_coin()
            if current_coin and bridge_bal > 5.0:
                coin_bal = self.manager.get_currency_balance(current_coin.symbol)
                if coin_bal is None or coin_bal < 0.001:
                    self.logger.info(
                        f"Detected USDC balance ({bridge_bal}) with no {current_coin.symbol} — "
                        f"entering re-entry mode"
                    )
                    self._awaiting_reentry = True
                    self._last_sold_coin = current_coin.symbol

        # Phase 6: Handle re-entry after profit-taking or stop-loss
        if self._awaiting_reentry:
            self._reenter_from_usdc()
            return

        current_coin = self.db.get_current_coin()
        print(
            f"{datetime.now()} - CONSOLE - INFO - Scouting | "
            f"Current: {current_coin}{self.config.BRIDGE} | "
            f"Regime: {self._market_regime} | ADX: {self._regime_adx:.1f}",
            end="\r",
        )

        current_coin_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
        if current_coin_price is None:
            self.logger.info(f"Skipping scouting... {current_coin + self.config.BRIDGE} not found")
            return

        # Phase D: Trailing stop-loss check
        if self._check_trailing_stop(current_coin, current_coin_price):
            return

        # Phase 6: Profit-taking check
        if self._check_profit_taking(current_coin, current_coin_price):
            return

        # Phase 6: Trade cooldown (regime-aware)
        cooldown = self._get_cooldown_seconds()
        if cooldown > 0:
            elapsed = time.time() - self._last_trade_time
            if elapsed < cooldown:
                return

        # Normal scouting
        self._jump_to_best_coin(current_coin, current_coin_price)

    def bridge_scout(self):
        """Buy a coin with leftover bridge balance."""
        current_coin = self.db.get_current_coin()
        if self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            return
        new_coin = super().bridge_scout()
        if new_coin is not None:
            self.db.set_current_coin(new_coin)

    def initialize_current_coin(self):
        """Decide what is the current coin, and set it up in the DB."""
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit("***\nERROR!\nSince there is no backup file, a proper coin name must be provided at init\n***")
            self.db.set_current_coin(current_coin_symbol)

            if self.config.CURRENT_COIN_SYMBOL == "":
                current_coin = self.db.get_current_coin()
                self.logger.info(f"Purchasing {current_coin} to begin trading")
                self.manager.buy_alt(current_coin, self.config.BRIDGE)
                self.logger.info("Ready to start trading")
