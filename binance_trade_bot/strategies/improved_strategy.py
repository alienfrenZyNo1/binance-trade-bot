"""
Improved trading strategy implementing 6 enhancement phases:

Phase 1: Percentage-based scoring (eliminates ratio magnitude bias)
Phase 2: Rolling EMA baseline (replaces single-point snapshot)
Phase 3: Z-score volatility-scaled threshold
Phase 4: Momentum filter (skip falling knives)
Phase 5: Market regime detection (pause in high volatility)
Phase 6: Trade cooldown + periodic USDC profit-taking
"""

import time
import sys
import random
from datetime import datetime

from binance_trade_bot.auto_trader import AutoTrader


class Strategy(AutoTrader):
    def initialize(self):
        super().initialize()  # initialize_trade_thresholds
        self.initialize_current_coin()

        # Phase 5: cached market regime
        self._market_regime = "normal"
        self._last_regime_check = 0

        # Phase 6: trade cooldown + profit-taking state
        self._last_trade_time = 0
        self._trades_since_profit_take = self._count_completed_trades()
        self._awaiting_reentry = False

    # ── Phase 1 + 2: Improved ratio computation ───────────────────────────

    def _get_ratios(self, coin, coin_price):
        """
        Compute percentage-based scores for every enabled pair.

        Returns dict: Pair -> float (percentage score, >0 = favorable).
        Uses rolling EMA baseline from pair_stats when available,
        falls back to pair.ratio (single-point snapshot).
        """
        ratio_dict = {}

        for pair in self.db.get_pairs_from(coin):
            optional_coin_price = self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)

            if optional_coin_price is None:
                self.logger.info(f"Skipping scouting... optional coin {pair.to_coin + self.config.BRIDGE} not found")
                continue

            # Log scout data (Phase 2 data collection via scout_history)
            self.db.log_scout(pair, pair.ratio, coin_price, optional_coin_price)

            current_ratio = coin_price / optional_coin_price

            # Fees
            from_fee = self.manager.get_fee(pair.from_coin, self.config.BRIDGE, True)
            to_fee = self.manager.get_fee(pair.to_coin, self.config.BRIDGE, False)
            transaction_fee = from_fee + to_fee - from_fee * to_fee

            # Phase 2: Use rolling EMA as baseline when available
            ema_ratio, _std = self.db.get_pair_stat(pair.id)
            baseline = ema_ratio if ema_ratio is not None else pair.ratio
            if baseline is None or baseline <= 0:
                continue

            # Phase 1: Percentage-based score (net of fees)
            # This eliminates the ratio-magnitude bias of the original formula
            pct_gain = (current_ratio / baseline) - 1.0
            fee_hurdle = transaction_fee * self.config.SCOUT_MULTIPLIER
            score = pct_gain - fee_hurdle

            ratio_dict[pair] = score

        return ratio_dict

    # ── Phase 3: Z-score lookup ───────────────────────────────────────────

    def _get_z_score(self, pair, current_ratio):
        """Get z-score for a pair (std devs from EMA). Returns 0 if no data."""
        ema, std = self.db.get_pair_stat(pair.id)
        if ema is not None and std and std > 0:
            return (current_ratio - ema) / std
        return 0.0

    # ── Phase 4: Momentum filter ──────────────────────────────────────────

    def _check_momentum(self, coin_symbol):
        """
        Check if a coin is crashing. Returns True if safe to buy, False if falling knife.
        """
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

            # Use the most recent candle
            candle = klines[-1]
            open_price = float(candle[1])
            close_price = float(candle[4])
            change_pct = ((close_price - open_price) / open_price) * 100

            if change_pct < -self.config.MOMENTUM_MAX_DROP_1H:
                self.logger.info(
                    f"Momentum filter: skipping {coin_symbol} "
                    f"(1h change: {change_pct:+.2f}%, threshold: -{self.config.MOMENTUM_MAX_DROP_1H}%)"
                )
                return False
            return True
        except Exception as e:
            self.logger.warning(f"Momentum check failed for {coin_symbol}: {e}")
            return True  # Fail open

    # ── Phase 5: Market regime detection ──────────────────────────────────

    def _update_market_regime(self):
        """Check overall market volatility and set regime."""
        if not self.config.REGIME_CHECK_ENABLED:
            return

        now = time.time()
        if now - self._last_regime_check < 300:  # Check every 5 minutes max
            return
        self._last_regime_check = now

        try:
            coins = self.db.get_coins()
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

            if count == 0:
                return

            avg_volatility = total_abs_change / count

            if avg_volatility > self.config.REGIME_HIGH_VOL_THRESHOLD:
                if self._market_regime != "volatile":
                    self.logger.warning(
                        f"Market regime: VOLATILE (avg 24h change: {avg_volatility:.1f}%) — raising thresholds"
                    )
                self._market_regime = "volatile"
            else:
                if self._market_regime != "normal":
                    self.logger.info(
                        f"Market regime: NORMAL (avg 24h change: {avg_volatility:.1f}%)"
                    )
                self._market_regime = "normal"
        except Exception as e:
            self.logger.warning(f"Regime check failed: {e}")

    def _get_z_score_threshold(self):
        """Return z-score threshold, adjusted for market regime."""
        base = self.config.Z_SCORE_THRESHOLD
        if self._market_regime == "volatile":
            return base * self.config.REGIME_Z_SCORE_MULTIPLIER
        return base

    # ── Phase 6: Profit-taking + cooldown ─────────────────────────────────

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

        if self._trades_since_profit_take < self.config.PROFIT_TAKING_INTERVAL:
            return False

        self.logger.info(
            f"Profit-taking triggered after {self._trades_since_profit_take} trades"
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
                self.logger.info("Profit-taking complete, awaiting re-entry signal")
                return True
            else:
                self.logger.warning("Profit-taking sell failed, continuing normal operation")

        return False

    def _reenter_from_usdc(self):
        """Find the best coin to buy back after profit-taking."""
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)

        if bridge_balance <= 0:
            self._awaiting_reentry = False
            return

        self.logger.info(f"Re-entry scouting with {bridge_balance} {self.config.BRIDGE.symbol}")

        best_coin = None
        best_score = -float("inf")

        for coin in self.db.get_coins():
            coin_price = self.manager.get_ticker_price(coin + self.config.BRIDGE)
            if coin_price is None:
                continue

            min_notional = self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol)
            if bridge_balance < min_notional:
                continue

            # Check momentum — don't buy a falling knife
            if not self._check_momentum(coin.symbol):
                continue

            # Get ratios FROM this coin
            ratio_dict = self._get_ratios(coin, coin_price)

            # Buy the most undervalued coin (all ratios negative = nobody wants to jump from it)
            if ratio_dict and all(v < 0 for v in ratio_dict.values()):
                avg_score = sum(ratio_dict.values()) / len(ratio_dict)
                if avg_score > best_score:
                    best_score = avg_score
                    best_coin = coin

        if best_coin is not None:
            self.logger.info(f"Re-entry: buying {best_coin}")
            result = self.manager.buy_alt(best_coin, self.config.BRIDGE)
            if result is not None:
                self.db.set_current_coin(best_coin)
                self._awaiting_reentry = False
                self._last_trade_time = time.time()
                self.logger.info(f"Re-entry complete: now holding {best_coin}")
        else:
            self.logger.info("No clear re-entry opportunity yet, waiting...")

    # ── Override _jump_to_best_coin with all filters ──────────────────────

    def _jump_to_best_coin(self, coin, coin_price):
        """
        Find the best coin to jump to, applying all filter phases.
        """
        ratio_dict = self._get_ratios(coin, coin_price)

        # Phase 1: keep only positive scores
        candidates = {k: v for k, v in ratio_dict.items() if v > 0}
        if not candidates:
            return

        # Phase 3: Z-score filter (regime-aware)
        z_threshold = self._get_z_score_threshold()
        z_filtered = {}
        for pair, score in candidates.items():
            current_ratio = coin_price / self.manager.get_ticker_price(pair.to_coin + self.config.BRIDGE)
            z = self._get_z_score(pair, current_ratio)

            # Skip z-score filter if we don't have enough data yet
            ema, std = self.db.get_pair_stat(pair.id)
            if std is not None and std > 0 and z < z_threshold:
                self.logger.debug(
                    f"Z-score filter: {pair.to_coin_id} blocked "
                    f"(z={z:.2f}, need {z_threshold:.2f})"
                )
                continue
            z_filtered[pair] = score

        if not z_filtered:
            return

        # Phase 4: Momentum filter — skip falling knives
        momentum_filtered = {}
        for pair, score in z_filtered.items():
            if self._check_momentum(pair.to_coin_id):
                momentum_filtered[pair] = score

        if not momentum_filtered:
            self.logger.info("All candidates filtered out by momentum filter")
            return

        # Jump to best remaining candidate
        best_pair = max(momentum_filtered, key=momentum_filtered.get)
        self.logger.info(
            f"Jumping from {coin} to {best_pair.to_coin_id} "
            f"(score: {momentum_filtered[best_pair]:.4f}, "
            f"regime: {self._market_regime})"
        )

        result = self.transaction_through_bridge(best_pair)
        if result is not None:
            self._last_trade_time = time.time()
            self._trades_since_profit_take = self._count_completed_trades()

    # ── Main scout loop ───────────────────────────────────────────────────

    def scout(self):
        """Main scouting loop with all 6 phases applied."""

        # Phase 5: Update market regime (cached, checks every 5 min)
        self._update_market_regime()

        # Phase 6: Handle re-entry after profit-taking
        if self._awaiting_reentry:
            self._reenter_from_usdc()
            return

        current_coin = self.db.get_current_coin()
        print(
            f"{datetime.now()} - CONSOLE - INFO - Scouting | "
            f"Current: {current_coin}{self.config.BRIDGE} | "
            f"Regime: {self._market_regime}",
            end="\r",
        )

        current_coin_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
        if current_coin_price is None:
            self.logger.info(f"Skipping scouting... {current_coin + self.config.BRIDGE} not found")
            return

        # Phase 6: Profit-taking check
        if self._check_profit_taking(current_coin, current_coin_price):
            return

        # Phase 6: Trade cooldown
        if self.config.TRADE_COOLDOWN_SECONDS > 0:
            elapsed = time.time() - self._last_trade_time
            if elapsed < self.config.TRADE_COOLDOWN_SECONDS:
                return

        # Normal scouting with improved _jump_to_best_coin
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
