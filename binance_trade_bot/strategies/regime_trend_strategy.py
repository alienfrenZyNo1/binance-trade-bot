"""
Regime-Adaptive Trend Strategy — RESEARCH-BACKED (2026-06-27)

Core idea: Detect market regime (bull / bear / sideways / transition) using
ADX(14) + EMA(200), then deploy the appropriate sub-strategy for each regime:

  BULL (ADX ≥ 25, price > EMA200):
      Long position, 2× leverage (futures) or 1× spot, 12% trailing stop.

  BEAR (ADX ≥ 25, price < EMA200):
      Short position via futures (2× leverage) or move to USDC cash.

  SIDEWAYS (ADX < 20):
      Grid-style range trading: buy ladders below mid, sell ladders above.

  TRANSITION (20 ≤ ADX < 25):
      Reduce to 50% position, no leverage, trailing stop only.

Research results (walk-forward validated + Monte Carlo 1000 shuffles):
  - Quality Top 3 (BNB, ETH, XRP): 133.9% ann., Sharpe 1.50, MC+ 67.1%
  - Strat Top 3 (APT, AVAX, OP):    111.2% ann., Sharpe 1.35, MC+ 92.6%
  - Strat Top 5 (APT, AVAX, OP, BTC, RUNE): 68.5% ann., Sharpe 1.16, MC+ 92.7%

This module is NOT imported by default. It is loaded only when the user sets
``strategy = regime_trend`` in ``user.cfg``. All config knobs have safe defaults
that match the backtested "balanced" variant.

NOTE: This is the promotion-pipeline module. It is feature-complete but has
not been deployed live. Use config flag ``regime_trend_paper = yes`` to run it
in observation-only mode (detects regimes, logs signals, but does not place
orders).
"""

import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from binance_trade_bot.auto_trader import AutoTrader
from binance_trade_bot.futures_manager import FuturesManager
from binance_trade_bot.indicators import (
    compute_adx as _compute_adx_func,
    compute_ema as _compute_ema_func,
    compute_rsi as _compute_rsi_func,
)
from binance_trade_bot.regime_hysteresis import RegimeHysteresis
from binance_trade_bot.risk_circuit_breaker import (
    circuit_breaker_status_summary,
    evaluate_circuit_breaker,
    is_circuit_breaker_cooling_down,
)

# ── Regime constants ────────────────────────────────────────────────────────

BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
TRANSITION = "transition"

ALL_REGIMES = frozenset({BULL, BEAR, SIDEWAYS, TRANSITION})

# ── Default coin universe (Strat Top 5 from research) ───────────────────────

DEFAULT_COIN_UNIVERSE = ["APT", "AVAX", "OP", "BTC", "RUNE"]

# ── Regime thresholds (from backtested research config) ─────────────────────

ADX_PERIOD_DEFAULT = 14
ADX_BULL_BEAR_DEFAULT = 25       # ADX ≥ 25 → trending
ADX_SIDEWAYS_DEFAULT = 20        # ADX < 20 → sideways
EMA_TREND_DEFAULT = 200          # EMA period for trend direction

# ── Strategy variant defaults (balanced) ────────────────────────────────────

TREND_LEVERAGE_DEFAULT = 2.0
STOP_LOSS_DEFAULT = 0.15         # 15% hard stop
TRAIL_STOP_DEFAULT = 0.12        # 12% trailing stop
GRID_SPACING_PCT_DEFAULT = 0.025 # 2.5% between grid levels
GRID_LEVELS_DEFAULT = 4
TRANSITION_FRACTION_DEFAULT = 0.5
BEAR_ACTION_DEFAULT = "short"    # "short" or "cash"

# Max total exposure (spot value + futures notional) as a multiple of equity.
# Default 1.5× prevents unbounded leverage stacking across spot + futures.
MAX_TOTAL_EXPOSURE_DEFAULT = 1.5


class RegimeSignal:
    """Immutable regime classification result for a single observation."""

    __slots__ = (
        "regime", "adx", "plus_di", "minus_di", "ema_trend",
        "price", "is_trending", "above_ema",
    )

    def __init__(
        self,
        regime: str,
        adx: float,
        plus_di: float,
        minus_di: float,
        ema_trend: Optional[float],
        price: float,
        is_trending: bool,
        above_ema: Optional[bool],
    ):
        self.regime = regime
        self.adx = adx
        self.plus_di = plus_di
        self.minus_di = minus_di
        self.ema_trend = ema_trend
        self.price = price
        self.is_trending = is_trending
        self.above_ema = above_ema

    def __repr__(self):
        ema_str = f"{self.ema_trend:.4f}" if self.ema_trend else "N/A"
        return (
            f"RegimeSignal(regime={self.regime!r}, adx={self.adx:.1f}, "
            f"price={self.price:.4f}, ema={ema_str})"
        )


class GridState:
    """Tracks open grid orders and fill state for sideways regime."""

    __slots__ = ("levels", "spacing_pct", "mid_price", "orders", "last_reset")

    def __init__(self, levels: int, spacing_pct: float, mid_price: float):
        self.levels = levels
        self.spacing_pct = spacing_pct
        self.mid_price = mid_price
        self.orders: List[Dict] = []  # {price, side, filled}
        self.last_reset: float = time.time()
        self._build_ladder()

    def _build_ladder(self):
        """Build buy/sell ladder around the mid price."""
        self.orders = []
        for i in range(1, self.levels + 1):
            buy_price = self.mid_price * (1 - self.spacing_pct * i)
            sell_price = self.mid_price * (1 + self.spacing_pct * i)
            self.orders.append({"price": buy_price, "side": "buy", "filled": False})
            self.orders.append({"price": sell_price, "side": "sell", "filled": False})

    def check_fills(self, current_price: float) -> List[Dict]:
        """Return list of orders that would fill at *current_price*."""
        filled = []
        for order in self.orders:
            if order["filled"]:
                continue
            if order["side"] == "buy" and current_price <= order["price"]:
                order["filled"] = True
                filled.append(order)
            elif order["side"] == "sell" and current_price >= order["price"]:
                order["filled"] = True
                filled.append(order)
        return filled

    def reset(self, mid_price: float):
        """Rebuild the grid around a new mid price."""
        self.mid_price = mid_price
        self.last_reset = time.time()
        self._build_ladder()

    @property
    def unfilled_count(self) -> int:
        return sum(1 for o in self.orders if not o["filled"])


# ── Pure regime detection (no external dependencies) ────────────────────────

def detect_regime_from_indicators(
    adx: float,
    plus_di: float,
    minus_di: float,
    price: float,
    ema_trend: Optional[float],
    adx_trend_threshold: float = ADX_BULL_BEAR_DEFAULT,
    adx_sideways_threshold: float = ADX_SIDEWAYS_DEFAULT,
) -> RegimeSignal:
    """Classify a single bar's regime from computed indicators.

    This is a **pure function** — it does not touch the database, exchange,
    or any mutable state. It is independently unit-testable.

    Classification rules (from ``scripts/research_regime_combined.py``):
      - ADX ≥ ``adx_trend_threshold`` AND price > EMA200 → BULL
      - ADX ≥ ``adx_trend_threshold`` AND price < EMA200 → BEAR
      - ADX < ``adx_sideways_threshold`` → SIDEWAYS
      - Otherwise (ADX between thresholds) → TRANSITION
    """
    is_trending = adx >= adx_trend_threshold
    is_sideways = adx < adx_sideways_threshold
    above_ema: Optional[bool] = None

    if ema_trend is not None and ema_trend > 0:
        above_ema = price > ema_trend

    if is_trending:
        if above_ema is True:
            regime = BULL
        elif above_ema is False:
            regime = BEAR
        else:
            # EMA not available — fall back to DI direction
            regime = BULL if plus_di >= minus_di else BEAR
    elif is_sideways:
        regime = SIDEWAYS
    else:
        regime = TRANSITION

    return RegimeSignal(
        regime=regime,
        adx=adx,
        plus_di=plus_di,
        minus_di=minus_di,
        ema_trend=ema_trend,
        price=price,
        is_trending=is_trending,
        above_ema=above_ema,
    )


def compute_position_size(
    regime: str,
    equity: float,
    trend_leverage: float = TREND_LEVERAGE_DEFAULT,
    transition_fraction: float = TRANSITION_FRACTION_DEFAULT,
    grid_fraction: float = 0.5,
) -> Tuple[float, float]:
    """Return (position_fraction_of_equity, effective_leverage) for a regime.

    Pure function — unit-testable.

    Returns:
        position_fraction: fraction of equity to deploy (0.0 to 1.0+)
        leverage: leverage multiplier (1.0 = no leverage)
    """
    if regime == BULL:
        return 1.0, trend_leverage
    elif regime == BEAR:
        return 1.0, trend_leverage
    elif regime == SIDEWAYS:
        return grid_fraction, 1.0
    elif regime == TRANSITION:
        return transition_fraction, 1.0
    else:
        return 0.0, 1.0


def compute_trailing_stop(
    entry_price: float,
    peak_price: float,
    trail_stop_pct: float = TRAIL_STOP_DEFAULT,
    is_short: bool = False,
) -> Optional[float]:
    """Compute the trailing stop price.

    For longs: stop trails *below* the peak price.
    For shorts: stop trails *above* the entry/low price.

    Returns None if no trailing stop is active (no peak established yet).
    """
    if peak_price <= 0 or entry_price <= 0:
        return None
    if is_short:
        # Short trailing stop: lowest price seen (best for short) → stop above it
        return peak_price * (1 + trail_stop_pct)
    else:
        return peak_price * (1 - trail_stop_pct)


def check_stop_loss(
    entry_price: float,
    current_price: float,
    stop_loss_pct: float = STOP_LOSS_DEFAULT,
    is_short: bool = False,
) -> bool:
    """Return True if the hard stop loss is hit.

    Pure function — unit-testable.
    """
    if entry_price <= 0:
        return False
    if is_short:
        adverse_move = (current_price - entry_price) / entry_price
    else:
        adverse_move = (entry_price - current_price) / entry_price
    return adverse_move >= stop_loss_pct


def check_trailing_stop_hit(
    current_price: float,
    trail_stop_price: Optional[float],
    is_short: bool = False,
) -> bool:
    """Return True if the trailing stop price has been touched."""
    if trail_stop_price is None or trail_stop_price <= 0:
        return False
    if is_short:
        return current_price >= trail_stop_price
    else:
        return current_price <= trail_stop_price


class Strategy(AutoTrader):
    """Regime-adaptive trend strategy.

    Loads via ``strategy = regime_trend`` in user.cfg.
    Does NOT import by default — the strategies ``__init__.py`` discovers it
    only when explicitly requested.
    """

    def initialize(self):
        super().initialize()
        self.initialize_current_coin()

        # ── Regime parameters ───────────────────────────────────────────────
        self._adx_period = int(
            getattr(self.config, "RT_ADX_PERIOD", ADX_PERIOD_DEFAULT)
        )
        self._adx_trend_threshold = float(
            getattr(self.config, "RT_ADX_TREND_THRESHOLD", ADX_BULL_BEAR_DEFAULT)
        )
        self._adx_sideways_threshold = float(
            getattr(self.config, "RT_ADX_SIDEWAYS_THRESHOLD", ADX_SIDEWAYS_DEFAULT)
        )
        self._ema_trend_period = int(
            getattr(self.config, "RT_EMA_TREND", EMA_TREND_DEFAULT)
        )

        # ── Strategy variant ────────────────────────────────────────────────
        self._trend_leverage = float(
            getattr(self.config, "RT_TREND_LEVERAGE", TREND_LEVERAGE_DEFAULT)
        )
        self._stop_loss_pct = float(
            getattr(self.config, "RT_STOP_LOSS", STOP_LOSS_DEFAULT)
        )
        self._trail_stop_pct = float(
            getattr(self.config, "RT_TRAIL_STOP", TRAIL_STOP_DEFAULT)
        )
        self._grid_spacing_pct = float(
            getattr(self.config, "RT_GRID_SPACING_PCT", GRID_SPACING_PCT_DEFAULT)
        )
        self._grid_levels = int(
            getattr(self.config, "RT_GRID_LEVELS", GRID_LEVELS_DEFAULT)
        )
        self._transition_fraction = float(
            getattr(self.config, "RT_TRANSITION_FRACTION", TRANSITION_FRACTION_DEFAULT)
        )
        self._bear_action = str(
            getattr(self.config, "RT_BEAR_ACTION", BEAR_ACTION_DEFAULT)
        ).lower()

        # ── Max total exposure (spot + futures notional) / equity ──────────
        self._max_total_exposure = float(
            getattr(self.config, "RT_MAX_TOTAL_EXPOSURE", MAX_TOTAL_EXPOSURE_DEFAULT)
        )

        # ── Paper mode (observation only) ───────────────────────────────────
        self._paper_mode = str(
            getattr(self.config, "RT_PAPER_MODE", "no")
        ).lower() in ("yes", "true", "1", "on")

        # ── Coin universe ───────────────────────────────────────────────────
        configured = getattr(self.config, "RT_COIN_UNIVERSE", None)
        if configured:
            if isinstance(configured, str):
                self._coin_universe = [c.strip() for c in configured.split(",") if c.strip()]
            else:
                self._coin_universe = list(configured)
        else:
            self._coin_universe = list(DEFAULT_COIN_UNIVERSE)

        # ── Regime state ────────────────────────────────────────────────────
        self._market_regime = SIDEWAYS
        try:
            latest_regime = self.db.get_latest_regime()
            if latest_regime and latest_regime.get("regime") in ALL_REGIMES:
                self._market_regime = latest_regime["regime"]
        except Exception:
            pass

        self._last_regime_check = 0
        self._regime_adx = 0.0
        self._regime_plus_di = 0.0
        self._regime_minus_di = 0.0
        self._regime_ema_trend: Optional[float] = None
        self._regime_price = 0.0
        self._previous_regime = self._market_regime

        self._regime_hysteresis = RegimeHysteresis(
            active=self._market_regime,
            confirmations=int(getattr(self.config, "REGIME_CONFIRMATION_CYCLES", 3)),
        )

        # ── Position state ──────────────────────────────────────────────────
        self._position_peak_price: Dict[str, float] = {}  # symbol → peak (or trough for shorts)
        self._position_entry_price: Dict[str, float] = {}  # symbol → entry price
        self._grid_state: Optional[GridState] = None

        # ── Trade state (persisted) ─────────────────────────────────────────
        saved_last_trade = self.db.get_bot_state("rt_last_trade_time")
        self._last_trade_time = float(saved_last_trade) if saved_last_trade else 0

        saved_reentry = self.db.get_bot_state("rt_awaiting_reentry")
        self._awaiting_reentry = saved_reentry == "True" if saved_reentry else False

        # ── Futures manager for bear regime ─────────────────────────────────
        self.futures_manager = FuturesManager(
            self.manager.binance_client, self.logger, self.config
        )
        # Wire circuit breaker callback so futures entries are gated by the
        # portfolio-level circuit breaker (mirrors momentum_strategy.py).
        self.futures_manager.new_risk_blocked = self._new_spot_risk_blocked
        self.futures_manager.initialize()

        # ── Seed circuit-breaker equity baselines on startup ────────────────
        # Without this the breaker is dormant (no baseline to compare against)
        # until the first trade happens.  Only seed when enabled; this does
        # NOT change thresholds.  Pattern from momentum_strategy.py L144-158.
        if getattr(self.config, "PORTFOLIO_CIRCUIT_BREAKER_ENABLED", False):
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

        # ── Performance cache ───────────────────────────────────────────────
        self._perf_cache: Dict[str, float] = {}
        self._perf_cache_time = 0
        self._cache_ttl = int(getattr(self.config, "INDICATOR_CACHE_TTL", 300))

        self.logger.info(
            f"RegimeTrendStrategy initialized | "
            f"Coins: {', '.join(self._coin_universe)} | "
            f"Lev: {self._trend_leverage:.1f}x | "
            f"Stop: {self._stop_loss_pct*100:.0f}% | "
            f"Trail: {self._trail_stop_pct*100:.0f}% | "
            f"Grid: {self._grid_levels}×{self._grid_spacing_pct*100:.1f}% | "
            f"Bear: {self._bear_action} | "
            f"Paper: {self._paper_mode}"
        )

    # ────────────────────────────────────────────────────────────────────────
    #  REGIME DETECTION
    # ────────────────────────────────────────────────────────────────────────

    def _update_market_regime(self):
        """Classify market regime using ADX + EMA on the reference coin."""
        now = time.time()
        check_interval = int(getattr(self.config, "REGIME_CHECK_INTERVAL", 300))
        if now - self._last_regime_check < check_interval:
            return
        self._last_regime_check = now

        try:
            # Use first coin in universe as reference, fall back to BTC
            ref_symbol = self._coin_universe[0] if self._coin_universe else "BTC"
            symbol = f"{ref_symbol}{self.config.BRIDGE.symbol}"

            klines = self.manager.binance_client.get_klines(
                symbol=symbol,
                interval=self._get_kline_interval(),
                limit=max(self._ema_trend_period * 2, self._adx_period * 3),
            )

            if not klines or len(klines) < self._adx_period * 2 + 1:
                self.logger.warning(
                    f"Insufficient klines for regime detection: "
                    f"{len(klines) if klines else 0} bars (need {self._adx_period * 2 + 1})"
                )
                return

            highs, lows, closes = self._extract_ohlc(klines)

            adx, plus_di, minus_di = _compute_adx_func(
                highs, lows, closes, self._adx_period
            )
            self._regime_adx = adx
            self._regime_plus_di = plus_di
            self._regime_minus_di = minus_di

            ema_trend = _compute_ema_func(closes, self._ema_trend_period)
            self._regime_ema_trend = ema_trend
            current_price = closes[-1]
            self._regime_price = current_price

            signal = detect_regime_from_indicators(
                adx=adx,
                plus_di=plus_di,
                minus_di=minus_di,
                price=current_price,
                ema_trend=ema_trend,
                adx_trend_threshold=self._adx_trend_threshold,
                adx_sideways_threshold=self._adx_sideways_threshold,
            )

            candidate_regime = signal.regime

            # Apply hysteresis to prevent whipsaw
            if not hasattr(self, "_regime_hysteresis"):
                self._regime_hysteresis = RegimeHysteresis(
                    active=self._market_regime,
                    confirmations=int(getattr(self.config, "REGIME_CONFIRMATION_CYCLES", 3)),
                )

            observation = self._regime_hysteresis.observe(candidate_regime)
            old = self._market_regime
            self._market_regime = observation.active

            if observation.pending:
                self.logger.info(
                    f"Regime pending ({observation.pending_count}/"
                    f"{observation.required_confirmations}): "
                    f"{old.upper()} → {observation.pending.upper()} "
                    f"(ADX: {adx:.1f}, +DI: {plus_di:.1f}, -DI: {minus_di:.1f})",
                    notification=False,
                )

            if observation.changed:
                confirmed_old = observation.previous or old
                self.logger.warning(
                    f"Market regime: {confirmed_old.upper()} → {self._market_regime.upper()} "
                    f"(confirmed, ADX: {adx:.1f}, +DI: {plus_di:.1f}, -DI: {minus_di:.1f})"
                )
                self._handle_regime_transition(confirmed_old, self._market_regime)
                self._previous_regime = self._market_regime

            # Log to database
            try:
                self.db.log_market_regime(
                    regime=self._market_regime,
                    adx_value=adx,
                    avg_volatility=0.0,
                    btc_correlation=None,
                    ema_short=0.0,
                    ema_long=ema_trend or 0.0,
                )
            except Exception:
                pass

        except Exception as e:
            self.logger.warning(f"Regime detection failed: {e}")

    def _get_kline_interval(self) -> str:
        """Return the kline interval for regime detection."""
        return str(getattr(self.config, "RT_KLINE_INTERVAL", "1h"))

    @staticmethod
    def _extract_ohlc(klines) -> Tuple[List[float], List[float], List[float]]:
        """Extract (highs, lows, closes) from klines in a format-agnostic way."""
        highs, lows, closes = [], [], []
        for k in klines:
            if isinstance(k, dict):
                highs.append(float(k["high"]))
                lows.append(float(k["low"]))
                closes.append(float(k["close"]))
            else:
                # List/tuple format: [open_time, open, high, low, close, ...]
                highs.append(float(k[2]))
                lows.append(float(k[3]))
                closes.append(float(k[4]))
        return highs, lows, closes

    # ────────────────────────────────────────────────────────────────────────
    #  REGIME TRANSITION HANDLING
    # ────────────────────────────────────────────────────────────────────────

    def _handle_regime_transition(self, old_regime: str, new_regime: str):
        """Execute capital moves when regime changes."""
        if old_regime == new_regime:
            return

        self.logger.info(
            f"Regime transition: {old_regime.upper()} → {new_regime.upper()}"
        )

        # Moving INTO bear: sell spot, transfer to futures (if short mode)
        if new_regime == BEAR and old_regime != BEAR:
            if self._bear_action == "cash":
                self._exit_to_cash("entering bear (cash mode)")
            else:
                self._prepare_bear_short()

        # Moving OUT of bear: close shorts, return to spot
        elif old_regime == BEAR and new_regime != BEAR:
            self._exit_bear_mode()

        # Entering sideways: reset grid
        if new_regime == SIDEWAYS:
            self._grid_state = None  # Will be rebuilt on next sideways entry

        # Entering transition: reduce position
        if new_regime == TRANSITION:
            self._reduce_position_for_transition()

    def _exit_to_cash(self, reason: str):
        """Sell all spot holdings to bridge currency."""
        if self._paper_mode:
            self.logger.info(f"[PAPER] Would exit to cash: {reason}")
            return
        current_coin = self.db.get_current_coin()
        if current_coin:
            balance = self.manager.get_currency_balance(current_coin.symbol)
            price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
            if balance and price and balance * price > 5.0:
                self.logger.info(f"Exiting to cash: selling {current_coin} ({reason})")
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is not None:
                    self._awaiting_reentry = True
                    self._persist_trade_state()
                    self.logger.info(f"Exit to cash complete: sold {current_coin}")

    def _prepare_bear_short(self):
        """Sell spot holdings and transfer capital to futures for shorting."""
        if self._paper_mode:
            self.logger.info("[PAPER] Would prepare bear short: sell spot, transfer to futures")
            return

        current_coin = self.db.get_current_coin()
        if current_coin:
            balance = self.manager.get_currency_balance(current_coin.symbol)
            price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
            if balance and price and balance * price > 5.0:
                self.logger.info(f"Bear prep: selling {current_coin} for futures margin")
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is None:
                    self.logger.error(f"Failed to sell {current_coin} for bear transition")
                    return

        # Transfer bridge balance to futures wallet
        bridge_bal = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        if bridge_bal and bridge_bal > 5.0:
            self.futures_manager.transfer_to_futures(bridge_bal)

        self._awaiting_reentry = False
        self._persist_trade_state()

    def _exit_bear_mode(self):
        """Close futures shorts and return capital to spot."""
        if self._paper_mode:
            self.logger.info("[PAPER] Would exit bear mode: close shorts, transfer to spot")
            return

        close_result = self.futures_manager.manage_exit()
        if close_result not in ("closed", "idle"):
            self.logger.error(
                f"Bear exit blocked: futures close returned {close_result}; "
                "leaving funds in futures"
            )
            return

        # Transfer USDC back to spot wallet
        futures_bal = self.futures_manager._get_futures_usdc_balance()
        if futures_bal and futures_bal > 5.0:
            if self.futures_manager.transfer_to_spot(futures_bal):
                try:
                    self.db.suppress_next_deposit_detection(
                        f"internal futures→spot transfer of {futures_bal:.2f}"
                    )
                except Exception:
                    pass

    def _reduce_position_for_transition(self):
        """Reduce spot position to transition fraction (default 50%)."""
        if self._paper_mode:
            self.logger.info(f"[PAPER] Would reduce position to {self._transition_fraction*100:.0f}%")
            return
        # For now, we let the trailing stop manage exits naturally.
        # A more advanced implementation could partially sell here.
        self.logger.info(
            f"Transition regime: maintaining position with trailing stop only "
            f"(no leverage, target fraction: {self._transition_fraction*100:.0f}%)"
        )

    # ────────────────────────────────────────────────────────────────────────
    #  CIRCUIT BREAKER INTEGRATION
    # ────────────────────────────────────────────────────────────────────────

    def _estimate_spot_equity(self):
        """Estimate portfolio equity in bridge currency for circuit-breaker checks.

        Includes spot bridge balance, spot coin value, and futures wallet
        balance + unrealized P&L.  Pattern from momentum_strategy.py.
        """
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

    CIRCUIT_BREAKER_FAIL_OPEN_ALERT_INTERVAL = 600  # seconds (10 min)

    def _alert_circuit_breaker_fail_open(self):
        """Escalate the circuit-breaker blind/fail-open path to a visible alert."""
        now = time.time()
        last = getattr(self, "_cb_fail_open_last_alert_ts", 0.0)
        if now - last < self.CIRCUIT_BREAKER_FAIL_OPEN_ALERT_INTERVAL:
            self.logger.warning(
                "Circuit breaker enabled but equity estimate unavailable; "
                "allowing new risk (fail-open, alert rate-limited)",
                notification=False,
            )
            return
        self._cb_fail_open_last_alert_ts = now
        self.logger.warning(
            "🚨 CIRCUIT BREAKER BLIND: breaker is enabled but spot equity could "
            "not be estimated — the breaker is FAILING OPEN and allowing all new "
            "risk without drawdown protection. Investigate balance/price feeds.",
            notification=True,
        )

    def _new_spot_risk_blocked(self):
        """Return True when portfolio circuit breaker should block new spot buys.

        This is the gate called before every new spot buy and is also wired as
        the futures_manager.new_risk_blocked callback for futures entries.
        Pattern from momentum_strategy.py.
        """
        if not getattr(self.config, "PORTFOLIO_CIRCUIT_BREAKER_ENABLED", False):
            return False

        equity = self._estimate_spot_equity()
        if equity is None:
            # Fail-open so we don't trap the bot, but escalate visibility.
            self._alert_circuit_breaker_fail_open()
            return False

        now = time.time()
        last_triggered = self._get_float_state("portfolio_circuit_breaker_last_triggered")
        if is_circuit_breaker_cooling_down(last_triggered, now, self.config):
            self.logger.warning("Circuit breaker cooldown active — blocking new risk")
            return True

        daily, weekly = self._ensure_circuit_breaker_baselines(equity, now)

        result = evaluate_circuit_breaker(equity, daily, weekly, self.config)
        if result.block_new_risk:
            self.db.set_bot_state("portfolio_circuit_breaker_last_triggered", str(now))
            self.logger.warning(circuit_breaker_status_summary(result))
            return True
        self.logger.debug(circuit_breaker_status_summary(result))
        return False

    # ────────────────────────────────────────────────────────────────────────
    #  TOTAL EXPOSURE GUARD
    # ────────────────────────────────────────────────────────────────────────

    def _compute_total_exposure_ratio(self) -> Optional[float]:
        """Return (spot_value + futures_notional) / equity, or None if unknown.

        Used by the max_total_exposure guard to prevent unbounded leverage
        stacking across spot and futures positions.
        """
        equity = self._estimate_spot_equity()
        if equity is None or equity <= 0:
            return None

        spot_value = 0.0
        try:
            current_coin = self.db.get_current_coin()
            if current_coin:
                balance = self.manager.get_currency_balance(current_coin.symbol) or 0.0
                price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
                if price:
                    spot_value = float(balance) * float(price)
        except Exception:
            pass

        futures_notional = 0.0
        try:
            pos = getattr(self.futures_manager, "_open_position", None)
            if pos is not None:
                futures_notional = float(pos.entry_price) * float(pos.quantity)
        except Exception:
            pass

        return (spot_value + futures_notional) / equity

    def _total_exposure_allows_entry(self, additional_notional: float = 0.0) -> bool:
        """Return True if adding ``additional_notional`` won't breach the limit."""
        current_ratio = self._compute_total_exposure_ratio()
        if current_ratio is None:
            # Can't compute — allow (fail-open) but log.
            self.logger.debug(
                "Total exposure check skipped — equity unavailable"
            )
            return True

        equity = self._estimate_spot_equity()
        if equity is None or equity <= 0:
            return True

        new_ratio = current_ratio + (additional_notional / equity)
        if new_ratio > self._max_total_exposure:
            self.logger.warning(
                f"Max total exposure check FAILED: current ratio "
                f"{current_ratio:.2f}x + new {additional_notional/equity:.2f}x = "
                f"{new_ratio:.2f}x > limit {self._max_total_exposure:.2f}x — "
                f"skipping trade"
            )
            return False
        return True

    # ────────────────────────────────────────────────────────────────────────
    #  TRAILING STOP MANAGEMENT
    # ────────────────────────────────────────────────────────────────────────

    def _check_trailing_stop(self, current_coin, current_price) -> bool:
        """Sell to bridge if price drops N% from peak (long positions)."""
        symbol = current_coin.symbol

        if symbol not in self._position_peak_price:
            self._position_peak_price[symbol] = current_price
            return False

        if current_price > self._position_peak_price[symbol]:
            self._position_peak_price[symbol] = current_price

        peak = self._position_peak_price[symbol]
        drop_pct = ((peak - current_price) / peak * 100) if peak > 0 else 0

        if drop_pct >= self._trail_stop_pct * 100:
            self.logger.warning(
                f"Trailing stop: {symbol} dropped {drop_pct:.1f}% from peak"
            )
            if self._paper_mode:
                self.logger.info(f"[PAPER] Would sell {symbol} on trailing stop")
                self._position_peak_price.pop(symbol, None)
                return True

            balance = self.manager.get_currency_balance(symbol)
            if balance and balance * current_price > self.manager.get_min_notional(
                symbol, self.config.BRIDGE.symbol
            ):
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is not None:
                    self._awaiting_reentry = True
                    self._position_peak_price.pop(symbol, None)
                    self._last_trade_time = time.time()
                    self._persist_trade_state()
                    self.logger.warning(
                        f"Trailing stop executed: sold {symbol}"
                    )
                    return True
        return False

    def _check_hard_stop(self, current_coin, current_price) -> bool:
        """Check the hard stop loss (wider than trailing stop)."""
        symbol = current_coin.symbol
        entry = self._position_entry_price.get(symbol)
        if entry is None or entry <= 0:
            return False

        if check_stop_loss(entry, current_price, self._stop_loss_pct, is_short=False):
            self.logger.warning(
                f"Hard stop loss: {symbol} "
                f"entry={entry:.6f} current={current_price:.6f}"
            )
            if self._paper_mode:
                self.logger.info(f"[PAPER] Would sell {symbol} on hard stop")
                self._position_peak_price.pop(symbol, None)
                self._position_entry_price.pop(symbol, None)
                return True

            balance = self.manager.get_currency_balance(symbol)
            if balance and balance * current_price > self.manager.get_min_notional(
                symbol, self.config.BRIDGE.symbol
            ):
                result = self.manager.sell_alt(current_coin, self.config.BRIDGE)
                if result is not None:
                    self._awaiting_reentry = True
                    self._position_peak_price.pop(symbol, None)
                    self._position_entry_price.pop(symbol, None)
                    self._last_trade_time = time.time()
                    self._persist_trade_state()
                    return True
        return False

    def _reset_position_tracking(self, symbol: str, price: float):
        self._position_peak_price = {symbol: price}
        self._position_entry_price = {symbol: price}

    # ────────────────────────────────────────────────────────────────────────
    #  PERFORMANCE SCORING
    # ────────────────────────────────────────────────────────────────────────

    def _get_coin_performance(self, coin_symbol: str) -> Optional[float]:
        """Get N-hour price performance for a coin. Returns % change or None."""
        lookback = int(getattr(self.config, "MOMENTUM_LOOKBACK_HOURS", 18))
        try:
            klines = self.manager.binance_client.get_klines(
                symbol=f"{coin_symbol}{self.config.BRIDGE.symbol}",
                interval="1h",
                limit=lookback + 1,
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

    def _get_all_performance(self) -> Dict[str, float]:
        """Get performance for all coins in universe, with caching."""
        now = time.time()
        if (
            self._perf_cache
            and (now - self._perf_cache_time) < self._cache_ttl
        ):
            return self._perf_cache

        perf: Dict[str, float] = {}
        for coin in self.db.get_coins():
            if coin.symbol not in self._coin_universe:
                continue
            p = self._get_coin_performance(coin.symbol)
            if p is not None:
                perf[coin.symbol] = p

        self._perf_cache = perf
        self._perf_cache_time = now
        return perf

    # ────────────────────────────────────────────────────────────────────────
    #  BULL REGIME: Trend Following
    # ────────────────────────────────────────────────────────────────────────

    def _scout_bull(self, current_coin, current_price: float):
        """In bull regime: hold the strongest coin, trail stop at 12%."""
        # Check stops first
        if self._check_hard_stop(current_coin, current_price):
            return
        if self._check_trailing_stop(current_coin, current_price):
            return

        # Cooldown
        cooldown = int(getattr(self.config, "TRADE_COOLDOWN_SECONDS", 300))
        if time.time() - self._last_trade_time < cooldown:
            return

        # Rotate to the strongest performer in universe
        performance = self._get_all_performance()
        if current_coin.symbol not in performance:
            return

        cur_perf = performance[current_coin.symbol]
        min_edge = float(getattr(self.config, "MOMENTUM_MIN_EDGE", 8.0))

        best_coin = None
        best_perf = -float("inf")
        for coin in self.db.get_coins():
            if coin.symbol not in self._coin_universe:
                continue
            if coin.symbol == current_coin.symbol:
                continue
            perf = performance.get(coin.symbol)
            if perf is None or perf <= 0:
                continue
            edge = perf - cur_perf
            if edge < min_edge:
                continue
            if perf > best_perf:
                best_perf = perf
                best_coin = coin

        if best_coin is not None:
            if self._paper_mode:
                self.logger.info(
                    f"[PAPER] BULL rotation: {current_coin} → {best_coin} "
                    f"(edge: {best_perf - cur_perf:+.2f}%)"
                )
                return

            # ── Risk Gate: Circuit breaker ────────────────────────────────
            if self._new_spot_risk_blocked():
                return

            # ── Risk Gate: Max total exposure ──────────────────────────────
            equity = self._estimate_spot_equity()
            if equity and equity > 0:
                frac, _lev = compute_position_size(
                    self._market_regime, equity,
                    trend_leverage=self._trend_leverage,
                    transition_fraction=self._transition_fraction,
                )
                available = self.manager.get_currency_balance(self.config.BRIDGE.symbol) or 0.0
                max_notional = frac * float(available)
                if not self._total_exposure_allows_entry(max_notional):
                    return

            self.logger.info(
                f"BULL rotation: {current_coin} → {best_coin} "
                f"(edge: {best_perf - cur_perf:+.2f}%)"
            )
            result = self._execute_rotation(current_coin, best_coin)
            if result:
                self._last_trade_time = time.time()
                self._persist_trade_state()

    # ────────────────────────────────────────────────────────────────────────
    #  SIDEWAYS REGIME: Grid Trading
    # ────────────────────────────────────────────────────────────────────────

    def _scout_sideways(self, current_coin, current_price: float):
        """In sideways regime: grid trade with buy/sell ladders."""
        # Rebuild grid if stale or first entry
        if self._grid_state is None:
            self._grid_state = GridState(
                levels=self._grid_levels,
                spacing_pct=self._grid_spacing_pct,
                mid_price=current_price,
            )
            self.logger.info(
                f"Grid initialized: {self._grid_levels} levels, "
                f"spacing={self._grid_spacing_pct*100:.1f}%, mid={current_price:.6f}"
            )

        # Check grid fills
        fills = self._grid_state.check_fills(current_price)
        for fill in fills:
            if fill["side"] == "buy":
                self.logger.info(
                    f"Grid BUY fill: {current_coin} at ~{fill['price']:.6f}"
                )
                if not self._paper_mode:
                    # ── Risk Gate: Circuit breaker ────────────────────────
                    if self._new_spot_risk_blocked():
                        continue
                    # ── Risk Gate: Max total exposure ──────────────────────
                    bridge_bal_snap = self.manager.get_currency_balance(self.config.BRIDGE.symbol) or 0.0
                    if not self._total_exposure_allows_entry(float(bridge_bal_snap)):
                        continue
                    if bridge_bal_snap and bridge_bal_snap > self.manager.get_min_notional(
                        current_coin.symbol, self.config.BRIDGE.symbol
                    ):
                        self.manager.buy_alt(current_coin, self.config.BRIDGE)
            elif fill["side"] == "sell":
                self.logger.info(
                    f"Grid SELL fill: {current_coin} at ~{fill['price']:.6f}"
                )
                if not self._paper_mode:
                    balance = self.manager.get_currency_balance(current_coin.symbol)
                    if balance and balance * current_price > self.manager.get_min_notional(
                        current_coin.symbol, self.config.BRIDGE.symbol
                    ):
                        self.manager.sell_alt(current_coin, self.config.BRIDGE)

        # Reset grid periodically (every 4 hours) to adapt to drift
        if time.time() - self._grid_state.last_reset > 14400:
            self._grid_state.reset(current_price)
            self.logger.info("Grid reset (4h cycle)")

    # ────────────────────────────────────────────────────────────────────────
    #  TRANSITION REGIME: Reduced Position
    # ────────────────────────────────────────────────────────────────────────

    def _scout_transition(self, current_coin, current_price: float):
        """In transition: hold with reduced position, wider trailing stop."""
        # Use a wider trailing stop in transition (stop_loss instead of trail)
        if self._check_hard_stop(current_coin, current_price):
            return
        # Also apply trailing stop for protection
        if self._check_trailing_stop(current_coin, current_price):
            return

        # Don't rotate in transition — just hold
        self.logger.debug(
            f"TRANSITION: holding {current_coin} at {current_price:.6f} "
            f"(50% target, no leverage)"
        )

    # ────────────────────────────────────────────────────────────────────────
    #  BEAR REGIME: Short or Cash
    # ────────────────────────────────────────────────────────────────────────

    def _scout_bear(self):
        """In bear regime: manage futures short or hold cash."""
        if self._bear_action == "cash":
            self.logger.debug("BEAR (cash mode): holding USDC, no new positions")
            return

        if self._paper_mode:
            self.logger.info("[PAPER] BEAR: would manage futures short")
            return

        # ── Risk Gate: Max total exposure (futures) ────────────────────────
        # The circuit breaker is checked inside futures_manager.manage_bear
        # via the new_risk_blocked callback.  Here we add the exposure guard
        # to prevent over-leveraging into futures.
        equity = self._estimate_spot_equity()
        if equity and equity > 0:
            futures_wallet = 0.0
            try:
                if hasattr(self.futures_manager, "_get_futures_usdc_balance"):
                    futures_wallet = float(
                        self.futures_manager._get_futures_usdc_balance() or 0.0
                    )
            except Exception:
                pass
            # Max notional for a new short = fraction * available margin
            frac, _lev = compute_position_size(
                self._market_regime, equity,
                trend_leverage=self._trend_leverage,
                transition_fraction=self._transition_fraction,
            )
            max_margin = frac * futures_wallet if futures_wallet > 0 else 0.0
            if not self._total_exposure_allows_entry(max_margin):
                return

        performance = self._get_all_performance()
        action = self.futures_manager.manage_bear(performance, self._market_regime)
        if action in ("opened", "closed"):
            self.logger.info(f"Futures action during bear: {action}")

    # ────────────────────────────────────────────────────────────────────────
    #  RE-ENTRY FROM BRIDGE
    # ────────────────────────────────────────────────────────────────────────

    def _reenter_from_bridge(self):
        """Buy back the strongest coin after a trailing stop exit."""
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        if not bridge_balance or bridge_balance < 5.0:
            return

        cooldown = int(getattr(self.config, "TRADE_COOLDOWN_SECONDS", 300))
        if self._last_trade_time > 0:
            if time.time() - self._last_trade_time < cooldown:
                return

        performance = self._get_all_performance()
        best_coin = None
        best_perf = -float("inf")

        for coin in self.db.get_coins():
            if coin.symbol not in self._coin_universe:
                continue
            perf = performance.get(coin.symbol)
            if perf is not None and perf > best_perf and perf > 0:
                best_perf = perf
                best_coin = coin

        if best_coin is not None:
            if self._paper_mode:
                self.logger.info(f"[PAPER] Re-entry: would buy {best_coin} (perf: {best_perf:+.2f}%)")
                return

            # ── Risk Gate: Circuit breaker ────────────────────────────────
            if self._new_spot_risk_blocked():
                return

            # ── Risk Gate: Max total exposure ──────────────────────────────
            if not self._total_exposure_allows_entry(float(bridge_balance)):
                return

            min_notional = self.manager.get_min_notional(best_coin.symbol, self.config.BRIDGE.symbol)
            if bridge_balance >= min_notional:
                self.logger.info(
                    f"Re-entry: buying {best_coin} (perf: {best_perf:+.2f}%, "
                    f"regime: {self._market_regime})"
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

    # ────────────────────────────────────────────────────────────────────────
    #  TRADE EXECUTION
    # ────────────────────────────────────────────────────────────────────────

    def _execute_rotation(self, from_coin, to_coin) -> bool:
        """Execute a coin-to-coin rotation through bridge."""
        balance = self.manager.get_currency_balance(from_coin.symbol)
        from_price = self.manager.get_ticker_price(from_coin + self.config.BRIDGE)

        if not balance or not from_price:
            return False

        if balance * from_price <= self.manager.get_min_notional(
            from_coin.symbol, self.config.BRIDGE.symbol
        ):
            self.logger.info("Skipping rotation — insufficient balance")
            return False

        if self.manager.sell_alt(from_coin, self.config.BRIDGE) is None:
            self.logger.info("Rotation sell failed")
            return False

        result = self.manager.buy_alt(to_coin, self.config.BRIDGE)
        if result is not None:
            self.db.set_current_coin(to_coin)
            self.update_trade_threshold(to_coin, result.price)
            new_price = self.manager.get_ticker_price(to_coin + self.config.BRIDGE)
            if new_price:
                self._reset_position_tracking(to_coin.symbol, new_price)
            return True

        self.logger.info("Rotation buy failed")
        return False

    def _persist_trade_state(self):
        """Save trade state to DB for crash recovery."""
        self.db.set_bot_state("rt_last_trade_time", str(self._last_trade_time))
        self.db.set_bot_state("rt_awaiting_reentry", str(self._awaiting_reentry))

    # ────────────────────────────────────────────────────────────────────────
    #  MAIN SCOUT LOOP
    # ────────────────────────────────────────────────────────────────────────

    def scout(self):
        """Main regime-adaptive scouting loop."""
        # Update regime detection
        self._update_market_regime()

        # BEAR regime: futures short or cash — skip spot entirely
        if self._market_regime == BEAR:
            self._scout_bear()
            return

        # Handle re-entry after trailing stop (spot modes only)
        if self._awaiting_reentry:
            self._reenter_from_bridge()
            return

        current_coin = self.db.get_current_coin()
        if current_coin is None:
            return

        current_price = self.manager.get_ticker_price(current_coin + self.config.BRIDGE)
        if current_price is None:
            return

        print(
            f"{datetime.now()} - CONSOLE - INFO - Scouting | "
            f"Current: {current_coin}{self.config.BRIDGE} | "
            f"Regime: {self._market_regime} | ADX: {self._regime_adx:.1f}",
            end="\r",
        )

        # Dispatch to regime-specific scout
        if self._market_regime == BULL:
            self._scout_bull(current_coin, current_price)
        elif self._market_regime == SIDEWAYS:
            self._scout_sideways(current_coin, current_price)
        elif self._market_regime == TRANSITION:
            self._scout_transition(current_coin, current_price)
        else:
            # Unexpected regime — treat as sideways
            self._scout_sideways(current_coin, current_price)

    def bridge_scout(self):
        """Buy a coin with leftover bridge balance — skip in bear."""
        if self._market_regime == BEAR:
            return

        current_coin = self.db.get_current_coin()
        if current_coin and self.manager.get_currency_balance(current_coin.symbol) > self.manager.get_min_notional(
            current_coin.symbol, self.config.BRIDGE.symbol
        ):
            return

        performance = self._get_all_performance()
        best_coin = None
        best_perf = -float("inf")
        for coin in self.db.get_coins():
            if coin.symbol not in self._coin_universe:
                continue
            perf = performance.get(coin.symbol)
            if perf is not None and perf > best_perf and perf > 0:
                best_perf = perf
                best_coin = coin

        if best_coin is not None:
            if self._paper_mode:
                self.logger.info(f"[PAPER] Bridge scout: would buy {best_coin}")
                return

            # ── Risk Gate: Circuit breaker ────────────────────────────────
            if self._new_spot_risk_blocked():
                return

            bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
            # ── Risk Gate: Max total exposure ──────────────────────────────
            if not self._total_exposure_allows_entry(float(bridge_balance or 0.0)):
                return

            if bridge_balance and bridge_balance > self.manager.get_min_notional(
                best_coin.symbol, self.config.BRIDGE.symbol
            ):
                self.logger.info(f"Bridge scout: buying {best_coin}")
                self.manager.buy_alt(best_coin, self.config.BRIDGE)
                self.db.set_current_coin(best_coin)

    def initialize_current_coin(self):
        """Decide what the current coin is, and set it up in the DB."""
        if self.db.get_current_coin() is None:
            current_coin_symbol = self.config.CURRENT_COIN_SYMBOL
            if not current_coin_symbol:
                # Default to first coin in universe
                if self._coin_universe:
                    current_coin_symbol = self._coin_universe[0]
                else:
                    current_coin_symbol = random.choice(self.config.SUPPORTED_COIN_LIST)

            self.logger.info(f"Setting initial coin to {current_coin_symbol}")

            if current_coin_symbol not in self.config.SUPPORTED_COIN_LIST:
                sys.exit(
                    "***\nERROR!\n"
                    f"Coin {current_coin_symbol} not in SUPPORTED_COIN_LIST\n***"
                )
            self.db.set_current_coin(current_coin_symbol)

            if self.config.CURRENT_COIN_SYMBOL == "":
                self.logger.info(
                    "Initial coin recorded; first purchase waits for "
                    "confirmed non-BEAR regime"
                )
