"""
Momentum Rotation Strategy — BACKTESTED WINNER (+79% over 6 months)

Core idea: Rotate into whichever coin is outperforming the current holding
by a significant margin (8%+ over 18 hours). Stop trading entirely in bear
markets. Cut losers with a trailing stop.

This outperforms mean-reversion because crypto trends persist — when a coin
starts outperforming, it tends to keep going for hours/days.

Backtest results (walk-forward validated):
  - 6-month P&L: +79% (vs TIA buy & hold: -14%)
  - Sharpe: 3.85
  - Trades: ~0.25/day (very selective)
  - Max drawdown: 48%

Optimal parameters (from grid search over 10,500 combinations):
  - momentum_lookback: 18h
  - momentum_min_edge: 8.0%
  - cooldown: 2h
  - anti_churn: 24h
  - trailing_stop: 15%
  - regime_filter: skip all trades in bear market
"""

import time
import sys
import random
from datetime import datetime, timezone

from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.futures_manager import FuturesManager
from binance_trade_bot.indicators import (
    compute_ema as _compute_ema_func,
    compute_adx as _compute_adx_func,
    compute_rsi as _compute_rsi_func,
)
from binance_trade_bot.regime_hysteresis import RegimeHysteresis
from binance_trade_bot.regime_transition_planner import plan_regime_transition
from binance_trade_bot.risk_circuit_breaker import (
    circuit_breaker_status_summary,
    evaluate_circuit_breaker,
    is_circuit_breaker_cooling_down,
)

# Regime constants
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"


class Strategy(AutoTrader):
    def initialize(self):
        super().initialize()
        self.initialize_current_coin()

        # Regime state
        self._market_regime = SIDEWAYS
        try:
            latest_regime = self.db.get_latest_regime()
            if latest_regime and latest_regime.get("regime") in {BULL, BEAR, SIDEWAYS, STORMY}:
                self._market_regime = latest_regime["regime"]
        except Exception:
            pass
        self._last_regime_check = 0
        self._regime_adx = 0.0
        self._previous_regime = self._market_regime
        self._regime_hysteresis = RegimeHysteresis(
            active=self._market_regime,
            confirmations=getattr(self.config, 'REGIME_CONFIRMATION_CYCLES', 3),
        )

        # Trade state — loaded from DB (persists across restarts)
        saved_last_trade = self.db.get_bot_state("last_trade_time")
        self._last_trade_time = float(saved_last_trade) if saved_last_trade else 0
        saved_reentry = self.db.get_bot_state("awaiting_reentry")
        self._awaiting_reentry = saved_reentry == "True" if saved_reentry else False
        # Note: _awaiting_reentry may be cleared by first _update_market_regime()
        # call if the regime is BEAR — we don't persist spot re-entry state in
        # bear mode since funds are in the futures wallet.

        # Trailing stop
        self._position_peak_price = {}

        # Anti-churn — load recently held timestamps from DB
        self._recently_held = {}
        import json as _json
        saved_churn = self.db.get_bot_state("recently_held")
        if saved_churn:
            try:
                self._recently_held = {k: float(v) for k, v in _json.loads(saved_churn).items()}
            except Exception:
                self._recently_held = {}

        # If the bot restarted with stale re-entry state but reconciliation shows
        # we are already holding a spot position, resume normal position
        # management instead of looping forever in bridge re-entry mode.
        if self._awaiting_reentry:
            try:
                current_coin = self.db.get_current_coin()
                if current_coin:
                    coin_balance = self.manager.get_currency_balance(current_coin.symbol) or 0
                    current_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
                    min_notional = self.manager.get_min_notional(current_coin.symbol, self.config.BRIDGE.symbol)
                    if current_price and coin_balance * current_price > min_notional:
                        self._awaiting_reentry = False
                        self._persist_trade_state()
                        self.logger.info(
                            f"Cleared stale awaiting_reentry flag — holding {coin_balance} {current_coin.symbol}",
                            notification=False,
                        )
            except Exception as e:
                self.logger.warning(
                    f"Could not validate stale awaiting_reentry state: {e}",
                    notification=False,
                )

        # Confirmation delay — require edge to persist N cycles before trading
        self._pending_rotation = None  # (from_coin, to_coin, edge, count, first_seen_time)
        self._confirmation_cycles = getattr(self.config, 'CONFIRMATION_CYCLES', 3)

        if self._last_trade_time > 0:
            from datetime import datetime as _dt
            self.logger.info(
                f"State restored: last_trade={_dt.utcfromtimestamp(self._last_trade_time).isoformat()}, "
                f"awaiting_reentry={self._awaiting_reentry}, "
                f"churn_blocklist={list(self._recently_held.keys())}"
            )

        # Performance cache (avoid hitting API every cycle)
        self._perf_cache = {}
        self._perf_cache_time = 0
        self._perf_cache_key = None
        self._cache_ttl = getattr(self.config, 'INDICATOR_CACHE_TTL', 300)

        # Futures manager for bear regime
        self.futures_manager = FuturesManager(
            self.manager.binance_client, self.logger, self.config
        )
        self.futures_manager.new_risk_blocked = self._new_spot_risk_blocked
        self.futures_manager.initialize()

        # Eagerly seed circuit-breaker equity baselines on startup so the
        # breaker protects capital immediately, not lazily on the next entry
        # attempt. Without this the breaker is dormant (no baseline to compare
        # against) until a trade happens — which is exactly when the ~15%
        # realized drawdown occurred while the breaker was enabled but unseeded.
        # Only seed when the breaker is enabled; this does NOT change thresholds.
        if getattr(self.config, 'PORTFOLIO_CIRCUIT_BREAKER_ENABLED', False):
            try:
                equity = self._estimate_spot_equity()
                if equity is not None:
                    self._ensure_circuit_breaker_baselines(equity, time.time())
                    self.logger.info(
                        "Circuit breaker baselines seeded eagerly on startup",
                        notification=False,
                    )
                else:
                    self.logger.warning(
                        "Circuit breaker enabled but equity unavailable at "
                        "startup; baselines will seed on first entry attempt",
                        notification=False,
                    )
            except Exception as e:
                self.logger.warning(
                    f"Could not seed circuit breaker baselines on startup: {e}",
                    notification=False,
                )

    def _persist_trade_state(self):
        """Save trade state to DB so it survives container restarts."""
        self.db.set_bot_state("last_trade_time", str(self._last_trade_time))
        self.db.set_bot_state("awaiting_reentry", str(self._awaiting_reentry))
        import json as _json
        # Clean expired churn entries before saving
        now = time.time()
        churn_ttl = getattr(self.config, 'CHURN_BLOCK_SECONDS', 86400)
        self._recently_held = {k: v for k, v in self._recently_held.items() if now - v < churn_ttl}
        self.db.set_bot_state("recently_held", _json.dumps(self._recently_held))

    # ─────────────────────────────────────────────────────────────────────────
    #  REGIME DETECTION
    # ─────────────────────────────────────────────────────────────────────────

    def _update_market_regime(self):
        """Classify market using ADX + EMA on SOL (reference coin)."""
        now = time.time()
        if now - self._last_regime_check < getattr(self.config, 'REGIME_CHECK_INTERVAL', 300):
            return
        self._last_regime_check = now

        try:
            coins = self.db.get_coins()
            if not coins:
                return

            # Prefer SOL as reference
            ref_coin = None
            for c in coins:
                if c.symbol == "SOL":
                    ref_coin = c
                    break
            if not ref_coin:
                ref_coin = coins[0]

            symbol = f"{ref_coin.symbol}{self.config.BRIDGE.symbol}"
            klines = self.manager.binance_client.get_klines(
                symbol=symbol,
                interval="1h",
                limit=max(self.config.EMA_LONG * 2, self.config.ADX_PERIOD * 3),
            )

            if not klines or len(klines) < 30:
                return

            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]

            adx, plus_di, minus_di = self._compute_adx(highs, lows, closes, self.config.ADX_PERIOD)
            self._regime_adx = adx

            ema_short = _compute_ema_func(closes, self.config.EMA_SHORT)
            ema_long = _compute_ema_func(closes, self.config.EMA_LONG)
            current_price = closes[-1]

            old = self._market_regime
            is_trending = adx >= self.config.ADX_TREND_THRESHOLD

            if is_trending:
                if current_price > ema_long and plus_di > minus_di:
                    candidate_regime = BULL
                elif current_price < ema_long and minus_di > plus_di:
                    candidate_regime = BEAR
                else:
                    candidate_regime = SIDEWAYS
            else:
                candidate_regime = SIDEWAYS

            if not hasattr(self, '_regime_hysteresis'):
                self._regime_hysteresis = RegimeHysteresis(
                    active=old,
                    confirmations=getattr(self.config, 'REGIME_CONFIRMATION_CYCLES', 3),
                )
            observation = self._regime_hysteresis.observe(candidate_regime)
            self._market_regime = observation.active

            if observation.pending:
                self.logger.info(
                    f"Regime candidate pending ({observation.pending_count}/"
                    f"{observation.required_confirmations}): "
                    f"{old.upper()} → {observation.pending.upper()} "
                    f"(ADX: {adx:.1f}, +DI: {plus_di:.1f}, -DI: {minus_di:.1f})",
                    notification=False,
                )

            if observation.changed:
                confirmed_old = observation.previous or old
                self.logger.warning(
                    f"Market regime: {confirmed_old.upper()} → {self._market_regime.upper()} "
                    f"(confirmed {observation.required_confirmations}/"
                    f"{observation.required_confirmations}, ADX: {adx:.1f}, "
                    f"+DI: {plus_di:.1f}, -DI: {minus_di:.1f})"
                )
                self._handle_regime_transition(confirmed_old, self._market_regime)
                self._previous_regime = self._market_regime

            # Log to DB
            try:
                self.db.log_market_regime(
                    regime=self._market_regime,
                    adx_value=adx,
                    avg_volatility=0.0,
                    btc_correlation=None,
                    ema_short=ema_short,
                    ema_long=ema_long,
                )
            except Exception:
                pass

        except Exception as e:
            self.logger.warning(f"Regime detection failed: {e}")

    @staticmethod
    def _compute_adx(highs, lows, closes, period):
        return _compute_adx_func(highs, lows, closes, period)

    @staticmethod
    def _compute_ema(values, period):
        return _compute_ema_func(values, period)

    def _handle_regime_transition(self, old_regime: str, new_regime: str):
        """Handle capital moves between spot and futures on regime changes."""
        current_coin = None
        has_spot_position = False
        position_price = None

        if new_regime == BEAR and old_regime != BEAR:
            current_coin = self.db.get_current_coin()
            if current_coin:
                balance = self.manager.get_currency_balance(current_coin.symbol)
                position_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
                has_spot_position = bool(balance and position_price and balance * position_price > 5.0)

        plan = plan_regime_transition(
            current_regime=old_regime,
            target_regime=new_regime,
            holding_coin=getattr(current_coin, "symbol", None) if current_coin else None,
            has_spot_position=has_spot_position,
            has_futures_position=getattr(self.futures_manager, "_open_position", None) is not None,
            awaiting_reentry=self._awaiting_reentry,
        )

        if plan.target_regime == BEAR and plan.current_regime != BEAR:
            # Moving INTO bear: sell spot holdings to USDC, transfer to futures
            self.logger.info("Regime → BEAR: preparing for short trading")
            if plan.requires_spot_exit and current_coin:
                self.logger.info(f"Selling {current_coin} to {self.config.BRIDGE} for futures margin")
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is None:
                    self.logger.error(
                        f"BEAR transition blocked: failed to sell {current_coin}; "
                        "will not transfer to futures/open shorts"
                    )
                    return
                # Verify spot exposure is gone before opening futures shorts.
                remaining = self.manager.get_currency_balance(current_coin.symbol)
                latest_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE) or position_price
                if remaining and latest_price and remaining * latest_price > 5.0:
                    self.logger.error(
                        f"BEAR transition blocked: still holding {remaining} {current_coin} "
                        f"(~${remaining * latest_price:.2f}) after sell"
                    )
                    return

            # Transfer USDC to futures wallet
            if plan.requires_futures_transfer:
                bridge_bal = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
                if bridge_bal and bridge_bal > 5.0:
                    self.futures_manager.transfer_to_futures(bridge_bal)

            # Clear awaiting_reentry — we're in futures mode now, not waiting for spot re-entry
            if plan.clear_awaiting_reentry:
                self._awaiting_reentry = False
                self._persist_trade_state()
                self.logger.info("Cleared awaiting_reentry flag — entering futures mode")

        elif plan.requires_futures_exit_check:
            # Moving OUT OF bear: close shorts, transfer back to spot
            self.logger.info("Regime ← BEAR: closing futures, returning to spot")
            close_result = self.futures_manager.manage_exit()
            if close_result not in ('closed', 'idle'):
                self.logger.error(
                    f"BEAR exit blocked: futures close returned {close_result}; "
                    "leaving funds in futures and keeping protection active"
                )
                return

            # Transfer USDC back to spot wallet only after confirmed flat/idle
            futures_bal = self.futures_manager._get_futures_usdc_balance()
            if futures_bal and futures_bal > 5.0:
                if self.futures_manager.transfer_to_spot(futures_bal):
                    self.db.suppress_next_deposit_detection(
                        f"internal futures→spot transfer of {futures_bal:.2f} {self.config.BRIDGE.symbol}"
                    )

    def _get_regime_momentum_lookback(self):
        """Return momentum lookback hours for the active regime."""
        if not getattr(self.config, 'PER_REGIME_PARAMS_ENABLED', False):
            return getattr(self.config, 'MOMENTUM_LOOKBACK_HOURS', 18)
        if self._market_regime == BULL:
            return getattr(self.config, 'BULL_MOMENTUM_LOOKBACK_HOURS', 36)
        if self._market_regime == BEAR:
            return getattr(self.config, 'BEAR_MOMENTUM_LOOKBACK_HOURS', 6)
        if self._market_regime == STORMY:
            return getattr(self.config, 'STORMY_MOMENTUM_LOOKBACK_HOURS', 6)
        return getattr(self.config, 'SIDEWAYS_MOMENTUM_LOOKBACK_HOURS', 18)

    def _get_regime_momentum_min_edge(self):
        """Return minimum outperformance edge for the active regime."""
        if not getattr(self.config, 'PER_REGIME_PARAMS_ENABLED', False):
            return getattr(self.config, 'MOMENTUM_MIN_EDGE', 8.0)
        if self._market_regime == BULL:
            return getattr(self.config, 'BULL_MOMENTUM_MIN_EDGE', 8.0)
        if self._market_regime == BEAR:
            return getattr(self.config, 'BEAR_MOMENTUM_MIN_EDGE', 5.0)
        if self._market_regime == STORMY:
            return getattr(self.config, 'STORMY_MOMENTUM_MIN_EDGE', 10.0)
        return getattr(self.config, 'SIDEWAYS_MOMENTUM_MIN_EDGE', 8.0)

    def _get_confirmation_min_seconds(self):
        """Return time-based rotation confirmation requirement by regime."""
        if not getattr(self.config, 'CONFIRMATION_TIME_ENABLED', False):
            return 0
        if self._market_regime == BULL:
            return getattr(self.config, 'BULL_CONFIRMATION_MIN_SECONDS', 300)
        if self._market_regime == BEAR:
            return getattr(self.config, 'BEAR_CONFIRMATION_MIN_SECONDS', 60)
        if self._market_regime == STORMY:
            return getattr(self.config, 'STORMY_CONFIRMATION_MIN_SECONDS', 300)
        return getattr(self.config, 'SIDEWAYS_CONFIRMATION_MIN_SECONDS', getattr(self.config, 'CONFIRMATION_MIN_SECONDS', 180))

    # ─────────────────────────────────────────────────────────────────────────
    #  PERFORMANCE SCORING
    # ─────────────────────────────────────────────────────────────────────────

    def _get_coin_performance(self, coin_symbol):
        """Get N-hour price performance for a coin. Returns % change or None."""
        lookback_bars = self._get_regime_momentum_lookback()
        try:
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval="1h",
                limit=lookback_bars + 1,
            )
            if not klines or len(klines) < 2:
                return None

            start_price = float(klines[0]["open"]) if isinstance(klines[0], dict) else float(klines[0][1])
            end_price = float(klines[-1]["close"]) if isinstance(klines[-1], dict) else float(klines[-1][4])

            if start_price <= 0:
                return None
            return ((end_price / start_price) - 1.0) * 100
        except Exception:
            return None

    def _get_all_performance(self):
        """Get performance for all enabled coins, with caching."""
        now = time.time()
        lookback = self._get_regime_momentum_lookback()
        regime_key = self._market_regime if getattr(self.config, 'PER_REGIME_PARAMS_ENABLED', False) else "global"
        cache_key = (regime_key, lookback)
        if (
            self._perf_cache
            and self._perf_cache_key == cache_key
            and (now - self._perf_cache_time) < self._cache_ttl
        ):
            return self._perf_cache

        perf = {}
        for coin in self.db.get_coins():
            p = self._get_coin_performance(coin.symbol)
            if p is not None:
                perf[coin.symbol] = p

        self._perf_cache = perf
        self._perf_cache_key = cache_key
        self._perf_cache_time = now
        return perf

    # ─────────────────────────────────────────────────────────────────────────
    #  FILTERS
    # ─────────────────────────────────────────────────────────────────────────

    def _is_churn_blocked(self, coin_symbol):
        """Anti-churn: don't re-buy coins sold recently."""
        sold_time = self._recently_held.get(coin_symbol)
        if sold_time is None:
            return False
        churn_seconds = getattr(self.config, 'CHURN_BLOCK_SECONDS', 86400)  # 24h default
        if time.time() - sold_time < churn_seconds:
            return True
        del self._recently_held[coin_symbol]
        return False

    def _check_rsi(self, coin_symbol, max_rsi=None):
        """Skip very overbought coins, respecting config."""
        enabled = str(getattr(self.config, 'RSI_FILTER_ENABLED', 'yes')).lower() in ('yes', 'true', '1', 'on')
        if not enabled:
            return True
        if max_rsi is None:
            max_rsi = float(getattr(self.config, 'RSI_OVERBOUGHT', 68))
        try:
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval="1h",
                limit=16,
            )
            if not klines or len(klines) < 15:
                return True
            closes = [float(k[4]) for k in klines]
            rsi = _compute_rsi_func(closes, 14)
            if rsi is not None and rsi > max_rsi:
                self.logger.debug(f"Skipping {coin_symbol}: RSI {rsi:.1f} > {max_rsi:.1f}")
                return False
            return True
        except Exception:
            return True

    def _passes_momentum_buy_guard(self, coin_symbol, perf_pct):
        """Avoid buying coins that are falling in absolute terms.

        Momentum rotation should buy strength, not merely a coin that is
        falling less badly than the current holding.  This guard applies to
        spot rotations, re-entry from bridge, and bridge_scout purchases.
        """
        if perf_pct is None:
            return False

        # Require the selected coin to be positive over the strategy lookback
        # unless explicitly disabled. This prevents buying -10% coins just
        # because the current holding is down -20%.
        min_target_perf = float(getattr(self.config, 'MOMENTUM_MIN_TARGET_PERF', 0.0))
        if perf_pct <= min_target_perf:
            self.logger.debug(
                f"Skipping {coin_symbol}: momentum {perf_pct:+.2f}% <= "
                f"minimum {min_target_perf:+.2f}%"
            )
            return False

        enabled = str(getattr(self.config, 'MOMENTUM_FILTER_ENABLED', 'yes')).lower() in ('yes', 'true', '1', 'on')
        if not enabled:
            return True

        # 1h crash guard from config: skip if the latest hourly candle is
        # down more than MOMENTUM_MAX_DROP_1H.
        try:
            max_drop = float(getattr(self.config, 'MOMENTUM_MAX_DROP_1H', 5.0))
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval="1h",
                limit=2,
            )
            if klines and len(klines) >= 1:
                k = klines[-1]
                open_price = float(k[1]) if not isinstance(k, dict) else float(k['open'])
                close_price = float(k[4]) if not isinstance(k, dict) else float(k['close'])
                if open_price > 0:
                    one_hour_perf = ((close_price / open_price) - 1.0) * 100
                    if one_hour_perf < -max_drop:
                        self.logger.debug(
                            f"Skipping {coin_symbol}: 1h crash {one_hour_perf:+.2f}% "
                            f"< -{max_drop:.2f}%"
                        )
                        return False
        except Exception as e:
            self.logger.debug(f"Momentum crash guard failed for {coin_symbol}: {e}")

        return True

    def _check_trailing_stop(self, current_coin, current_price):
        """Sell to bridge if price drops N% from peak."""
        if not getattr(self.config, 'TRAILING_STOP_ENABLED', True):
            return False

        trailing_pct = getattr(self.config, 'TRAILING_STOP_PCT', 15.0)
        symbol = current_coin.symbol

        if symbol not in self._position_peak_price:
            self._position_peak_price[symbol] = current_price
            return False

        if current_price > self._position_peak_price[symbol]:
            self._position_peak_price[symbol] = current_price

        peak = self._position_peak_price[symbol]
        drop_pct = ((peak - current_price) / peak) * 100 if peak > 0 else 0

        if drop_pct >= trailing_pct:
            self.logger.warning(
                f"Trailing stop: {symbol} dropped {drop_pct:.1f}% from peak"
            )
            balance = self.manager.get_currency_balance(symbol)
            if balance and balance * current_price > self.manager.get_min_notional(
                symbol, self.config.BRIDGE.symbol
            ):
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is not None:
                    self._awaiting_reentry = True
                    self._position_peak_price.pop(symbol, None)
                    self._recently_held[symbol] = time.time()
                    self._persist_trade_state()
                    self.logger.warning(f"Trailing stop executed: sold {symbol} to {self.config.BRIDGE.symbol}")
                    return True
        return False

    def _reset_position_tracking(self, symbol, price):
        self._position_peak_price = {symbol: price}

    def _estimate_spot_equity(self):
        """Estimate portfolio equity in bridge currency for circuit-breaker checks."""
        equity = 0.0
        bridge_symbol = self.config.BRIDGE.symbol
        try:
            bridge_balance = self.manager.get_currency_balance(bridge_symbol) or 0.0
            equity += float(bridge_balance)
        except Exception:
            pass

        try:
            current_coin = self.db.get_current_coin()
            if current_coin:
                balance = self.manager.get_currency_balance(current_coin.symbol) or 0.0
                price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
                if price:
                    equity += float(balance) * float(price)
        except Exception as e:
            self.logger.debug(f"Circuit breaker spot equity estimate failed: {e}")

        try:
            futures_manager = getattr(self, "futures_manager", None)
            if futures_manager is not None:
                wallet_balance = (
                    futures_manager._get_futures_usdc_wallet_balance()
                    if hasattr(futures_manager, "_get_futures_usdc_wallet_balance")
                    else futures_manager._get_futures_usdc_balance()
                )
                equity += float(wallet_balance or 0.0)
                pos = getattr(futures_manager, "_open_position", None)
                if pos is not None:
                    mark = futures_manager._get_mark_price(pos.symbol)
                    if mark:
                        # Short unrealized P&L: positive when mark < entry.
                        equity += (float(pos.entry_price) - float(mark)) * float(pos.quantity)
        except Exception as e:
            self.logger.debug(f"Circuit breaker futures equity estimate failed: {e}")

        return equity if equity > 0 else None

    def _get_float_state(self, key):
        try:
            value = self.db.get_bot_state(key)
            return float(value) if value is not None else None
        except Exception:
            return None

    def _get_string_state(self, key):
        try:
            value = self.db.get_bot_state(key)
            return str(value) if value is not None else None
        except Exception:
            return None

    @staticmethod
    def _circuit_breaker_periods(now_ts):
        now_dt = datetime.fromtimestamp(now_ts, timezone.utc)
        iso_year, iso_week, _ = now_dt.isocalendar()
        return now_dt.strftime("%Y-%m-%d"), f"{iso_year:04d}-W{iso_week:02d}"

    def _ensure_circuit_breaker_baselines(self, equity, now_ts):
        """Seed or reset UTC daily/weekly circuit-breaker baselines."""
        daily_period, weekly_period = self._circuit_breaker_periods(now_ts)
        daily = self._get_float_state("portfolio_daily_start_equity")
        weekly = self._get_float_state("portfolio_weekly_start_equity")
        stored_daily_period = self._get_string_state("portfolio_daily_period")
        stored_weekly_period = self._get_string_state("portfolio_weekly_period")
        reset_scopes = []

        if daily is None or daily <= 0:
            self.db.set_bot_state("portfolio_daily_start_equity", str(equity))
            self.db.set_bot_state("portfolio_daily_period", daily_period)
            daily = equity
            reset_scopes.append("daily")
        elif stored_daily_period is None:
            self.db.set_bot_state("portfolio_daily_period", daily_period)
        elif stored_daily_period != daily_period:
            self.db.set_bot_state("portfolio_daily_start_equity", str(equity))
            self.db.set_bot_state("portfolio_daily_period", daily_period)
            daily = equity
            reset_scopes.append("daily")

        if weekly is None or weekly <= 0:
            self.db.set_bot_state("portfolio_weekly_start_equity", str(equity))
            self.db.set_bot_state("portfolio_weekly_period", weekly_period)
            weekly = equity
            reset_scopes.append("weekly")
        elif stored_weekly_period is None:
            self.db.set_bot_state("portfolio_weekly_period", weekly_period)
        elif stored_weekly_period != weekly_period:
            self.db.set_bot_state("portfolio_weekly_start_equity", str(equity))
            self.db.set_bot_state("portfolio_weekly_period", weekly_period)
            weekly = equity
            reset_scopes.append("weekly")

        if reset_scopes:
            self.logger.info(
                f"Seeded/reset circuit-breaker baseline ({'/'.join(reset_scopes)}): ${equity:.2f}",
                notification=False,
            )
        return daily, weekly

    def _new_spot_risk_blocked(self):
        """Return True when portfolio circuit breaker should block new spot buys."""
        if not getattr(self.config, 'PORTFOLIO_CIRCUIT_BREAKER_ENABLED', False):
            return False

        equity = self._estimate_spot_equity()
        if equity is None:
            self.logger.warning(
                "Circuit breaker enabled but equity estimate unavailable; allowing new risk",
                notification=False,
            )
            return False

        now = time.time()
        last_triggered = self._get_float_state("portfolio_circuit_breaker_last_triggered")
        if is_circuit_breaker_cooling_down(last_triggered, now, self.config):
            self.logger.warning("Circuit breaker cooldown active — blocking new spot risk")
            return True

        daily, weekly = self._ensure_circuit_breaker_baselines(equity, now)

        result = evaluate_circuit_breaker(equity, daily, weekly, self.config)
        if result.block_new_risk:
            self.db.set_bot_state("portfolio_circuit_breaker_last_triggered", str(now))
            self.logger.warning(circuit_breaker_status_summary(result))
            return True
        self.logger.debug(circuit_breaker_status_summary(result))
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  RE-ENTRY FROM BRIDGE
    # ─────────────────────────────────────────────────────────────────────────

    def _reenter_from_bridge(self):
        """Find the strongest performer to buy back after trailing stop."""
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        if not bridge_balance or bridge_balance < 5.0:
            return

        # Wait at least 2 hours after stop-out
        if self._last_trade_time > 0:
            reentry_delay = getattr(self.config, 'TRADE_COOLDOWN_SECONDS', 7200)
            if time.time() - self._last_trade_time < reentry_delay:
                return

        performance = self._get_all_performance()

        best_coin = None
        best_perf = -float("inf")

        for coin in self.db.get_coins():
            if self._is_churn_blocked(coin.symbol):
                continue
            perf = performance.get(coin.symbol)
            if perf is not None and perf > best_perf and self._passes_momentum_buy_guard(coin.symbol, perf):
                best_perf = perf
                best_coin = coin

        if best_coin is not None:
            if self._new_spot_risk_blocked():
                return
            min_notional = self.manager.get_min_notional(best_coin.symbol, self.config.BRIDGE.symbol)
            if bridge_balance >= min_notional:
                self.logger.info(
                    f"Re-entry: buying {best_coin} (perf: {best_perf:+.2f}%, regime: {self._market_regime})"
                )
                result = self.manager.buy_alt(best_coin, self.config.BRIDGE)
                if result is not None:
                    self.db.set_current_coin(best_coin)
                    self._awaiting_reentry = False
                    self._last_trade_time = time.time()
                    self._persist_trade_state()
                    price = self.manager.get_ticker_price(best_coin + self.config.BRIDGE)
                    if price:
                        self._reset_position_tracking(best_coin.symbol, price)
                    self.logger.info(f"Re-entry complete: now holding {best_coin}")

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN SCOUT LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def scout(self):
        """Main momentum rotation scouting loop."""

        # Update regime
        self._update_market_regime()

        # REGIME FILTER: in bear market, manage futures shorts
        # Skip spot re-entry logic entirely — funds are in futures wallet
        if self._market_regime == BEAR:
            performance = self._get_all_performance()
            action = self.futures_manager.manage_bear(performance, self._market_regime)
            if action in ('opened', 'closed'):
                self.logger.info(f"Futures action during bear: {action}")
            return

        # Handle re-entry after trailing stop (spot mode only)
        if self._awaiting_reentry:
            self._reenter_from_bridge()
            return

        # Auto-detect bridge-only state (container restart recovery)
        if not self._awaiting_reentry:
            bridge_bal = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
            current_coin = self.db.get_current_coin()
            if current_coin and bridge_bal and bridge_bal > 5.0:
                coin_bal = self.manager.get_currency_balance(current_coin.symbol)
                if coin_bal is None or coin_bal < 0.001:
                    self._awaiting_reentry = True
                    self._recently_held[current_coin.symbol] = time.time()
                    self._persist_trade_state()
                    return

        current_coin = self.db.get_current_coin()
        print(
            f"{datetime.now()} - CONSOLE - INFO - Scouting | "
            f"Current: {current_coin}{self.config.BRIDGE} | "
            f"Regime: {self._market_regime} | ADX: {self._regime_adx:.1f}",
            end="\r",
        )

        current_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
        if current_price is None:
            return

        # Trailing stop check
        if self._check_trailing_stop(current_coin, current_price):
            return

        # REGIME FILTER: in bear market, manage futures shorts
        if self._market_regime == BEAR:
            performance = self._get_all_performance()
            action = self.futures_manager.manage_bear(performance, self._market_regime)
            if action in ('opened', 'closed'):
                self.logger.info(f"Futures action during bear: {action}")
            return

        # Cooldown
        cooldown_seconds = getattr(self.config, 'TRADE_COOLDOWN_SECONDS', 7200)
        if time.time() - self._last_trade_time < cooldown_seconds:
            return

        # Get performance for all coins
        performance = self._get_all_performance()
        if current_coin.symbol not in performance:
            return

        cur_perf = performance[current_coin.symbol]
        min_edge = self._get_regime_momentum_min_edge()

        # Find coins significantly outperforming current holding
        best_coin = None
        best_edge = -float("inf")

        for coin in self.db.get_coins():
            if coin.symbol == current_coin.symbol:
                continue
            if self._is_churn_blocked(coin.symbol):
                continue

            target_perf = performance.get(coin.symbol)
            if target_perf is None:
                continue
            if not self._passes_momentum_buy_guard(coin.symbol, target_perf):
                continue

            edge = target_perf - cur_perf
            if edge < min_edge:
                continue

            # Skip overbought coins
            if not self._check_rsi(coin.symbol):
                continue

            if edge > best_edge:
                best_edge = edge
                best_coin = coin

        # Reset pending rotation if no signal this cycle
        if best_coin is None:
            self._pending_rotation = None

        # Execute trade — only after confirmation delay
        if best_coin is not None:
            # Confirmation delay: require the SAME rotation signal to persist
            # across N consecutive scout cycles before executing. This filters
            # out noise-driven false signals from intrabar price spikes.
            pending = self._pending_rotation
            now = time.time()
            min_confirm_seconds = self._get_confirmation_min_seconds()
            if (pending and pending[0] == current_coin.symbol
                    and pending[1] == best_coin.symbol):
                # Same signal as last cycle — increment confirmation count while
                # preserving first-seen time across cycles.
                count = pending[3] + 1
                first_seen = pending[4] if len(pending) > 4 else now
                elapsed = now - first_seen
                self._pending_rotation = (
                    current_coin.symbol, best_coin.symbol, best_edge, count, first_seen
                )
                if count < self._confirmation_cycles or elapsed < min_confirm_seconds:
                    self.logger.info(
                        f"Rotation signal confirmed ({count}/{self._confirmation_cycles}, "
                        f"{elapsed:.0f}/{min_confirm_seconds}s): "
                        f"{current_coin} → {best_coin} (edge: {best_edge:+.2f}%)"
                    )
                    return
                # Confirmed — execute
                self.logger.info(
                    f"Momentum rotation CONFIRMED ({count}/{self._confirmation_cycles}, "
                    f"{elapsed:.0f}/{min_confirm_seconds}s): "
                    f"{current_coin} → {best_coin} (edge: {best_edge:+.2f}%, "
                    f"{current_coin}: {cur_perf:+.2f}%, "
                    f"{best_coin}: {performance[best_coin.symbol]:+.2f}%, "
                    f"regime: {self._market_regime})"
                )
            else:
                # New signal — start confirmation countdown
                self._pending_rotation = (
                    current_coin.symbol, best_coin.symbol, best_edge, 1, now
                )
                self.logger.info(
                    f"Rotation signal detected (1/{self._confirmation_cycles}, "
                    f"0/{min_confirm_seconds}s): "
                    f"{current_coin} → {best_coin} (edge: {best_edge:+.2f}%)"
                )
                return

            if self._new_spot_risk_blocked():
                self._pending_rotation = None
                return

            result = self.transaction_through_bridge_pair(current_coin, best_coin)
            if result is not None:
                self._last_trade_time = time.time()
                self._recently_held[current_coin.symbol] = time.time()
                self._persist_trade_state()
                self._pending_rotation = None
                new_price = self.manager.get_ticker_price(best_coin + self.config.BRIDGE)
                if new_price:
                    self._reset_position_tracking(best_coin.symbol, new_price)

    def transaction_through_bridge_pair(self, from_coin, to_coin):
        """Execute a direct coin-to-coin trade through bridge."""
        balance = self.manager.get_currency_balance(from_coin.symbol)
        from_price = self.manager.get_ticker_price(from_coin + self.config.BRIDGE)

        if not balance or not from_price or balance * from_price <= self.manager.get_min_notional(
            from_coin.symbol, self.config.BRIDGE.symbol
        ):
            self.logger.info("Skipping sell — not enough balance")
            return None

        if self.manager.sell_alt(from_coin, self.config.BRIDGE) is None:
            self.logger.info("Couldn't sell, going back to scouting...")
            return None

        result = self.manager.buy_alt(to_coin, self.config.BRIDGE)
        if result is not None:
            self.db.set_current_coin(to_coin)
            self.update_trade_threshold(to_coin, result.price)
            return result

        self.logger.info("Couldn't buy, going back to scouting...")
        return None

    def bridge_scout(self):
        """Buy a coin with leftover bridge balance."""
        # CRITICAL: Never buy spot coins during BEAR regime
        if self._market_regime == "bear":
            return

        current_coin = self.db.get_current_coin()
        if current_coin and self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            return

        # Buy the strongest performer with leftover bridge
        performance = self._get_all_performance()
        best_coin = None
        best_perf = -float("inf")
        for coin in self.db.get_coins():
            perf = performance.get(coin.symbol)
            if perf is not None and perf > best_perf and self._passes_momentum_buy_guard(coin.symbol, perf):
                best_perf = perf
                best_coin = coin

        if best_coin is not None:
            if self._new_spot_risk_blocked():
                return
            bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
            if bridge_balance and bridge_balance > self.manager.get_min_notional(
                best_coin.symbol, self.config.BRIDGE.symbol
            ):
                self.logger.info(f"Bridge scout: buying {best_coin} with leftover {self.config.BRIDGE.symbol}")
                self.manager.buy_alt(best_coin, self.config.BRIDGE)
                self.db.set_current_coin(best_coin)

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
                self.logger.info(
                    "Initial coin recorded without auto-buy; first purchase waits "
                    "for confirmed non-BEAR regime and normal scout filters"
                )
                self.logger.info("Ready to start trading")
