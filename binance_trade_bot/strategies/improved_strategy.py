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
from binance_trade_bot.indicators import compute_ema as _compute_ema_func, compute_adx as _compute_adx_func


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
        self._trades_since_profit_take = self._count_completed_trades()
        self._awaiting_reentry = False

        # Trailing stop-loss state
        self._position_entry_price = {}  # coin_symbol -> entry price
        self._position_peak_price = {}   # coin_symbol -> peak price since entry

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
                self.logger.warning(
                    f"Market regime: {old_regime.upper()} → {new_regime.upper()} "
                    f"(ADX: {adx:.1f}, Vol: {avg_volatility:.1f}%, "
                    f"EMA20: {ema_short:.4f}, EMA50: {ema_long:.4f}, "
                    f"Price: {current_price:.4f}, +DI: {plus_di:.1f}, -DI: {minus_di:.1f})"
                )

            # Log to DB
            self.db.log_market_regime(
                regime=new_regime,
                adx_value=adx,
                avg_volatility=avg_volatility,
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
                self._awaiting_reentry = True
                self._trades_since_profit_take = 0
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

        if bridge_balance <= 0:
            self._awaiting_reentry = False
            return

        self.logger.info(
            f"Re-entry scouting with {bridge_balance} {self.config.BRIDGE.symbol} "
            f"(regime: {self._market_regime})"
        )

        best_coin = None
        best_score = -float("inf")

        for coin in self.db.get_coins():
            coin_price = self.manager.get_ticker_price(coin + self.config.BRIDGE)
            if coin_price is None:
                continue

            min_notional = self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol)
            if bridge_balance < min_notional:
                continue

            # Momentum filter
            if not self._check_momentum(coin.symbol):
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

    def _jump_to_best_coin(self, coin, coin_price):
        """Find the best coin to jump to, applying regime-specific logic."""
        # Get scores based on regime
        if self._market_regime == BULL:
            ratio_dict = self._get_momentum_scores(coin, coin_price)
        else:
            ratio_dict = self._get_ratios(coin, coin_price)

        # Phase 1: keep only positive scores
        candidates = {k: v for k, v in ratio_dict.items() if v > 0}
        if not candidates:
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

        # Phase 4: Momentum filter
        momentum_filtered = {}
        for pair, score in candidates.items():
            if self._check_momentum(pair.to_coin_id):
                momentum_filtered[pair] = score

        if not momentum_filtered:
            self.logger.info("All candidates filtered out by momentum filter")
            return

        # Jump to best remaining candidate
        best_pair = max(momentum_filtered, key=momentum_filtered.get)
        self.logger.info(
            f"Jumping from {coin} to {best_pair.to_coin_id} "
            f"(score: {momentum_filtered[best_pair]:.6f}, "
            f"regime: {self._market_regime}, "
            f"ADX: {self._regime_adx:.1f})"
        )

        result = self.transaction_through_bridge(best_pair)
        if result is not None:
            self._last_trade_time = time.time()
            self._trades_since_profit_take = self._count_completed_trades()
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
