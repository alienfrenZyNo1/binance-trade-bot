"""
USDC-M Futures Manager for Bear Regime

When the market regime turns BEAR, this module opens short positions on
USDC-M perpetual futures. When the regime shifts back to BULL/SIDEWAYS,
it closes all shorts and returns capital to spot trading.

SAFETY:
- 1x leverage only (low liquidation risk at current sizing)
- Cross margin currently required (Binance rejects ISOLATED on this account)
- Funding rate guard (skip/exit when short funding is adverse)
- Server-side STOP_MARKET orders (instant execution, survives crashes)
- Client-side profit trailing + funding rate monitoring
- Position reconciliation on every cycle

REQUIRES: Binance account with USDC-M futures access (verified for IE).
USES: python-binance futures methods (fapi.binance.com endpoint).
"""

import time
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from binance.client import Client
from binance.exceptions import BinanceAPIException

try:
    from .futures_transfer_policy import (
        TransferAttemptResult,
        TransferStatus,
        binance_error_code,
        choose_retry_transfer_amount,
        is_insufficient_balance_error,
        safe_transfer_amount,
    )
    from .canary_capital_guard import cap_futures_margin, canary_status_summary
except ImportError:  # pragma: no cover - supports direct spec loading in legacy tests
    from binance_trade_bot.futures_transfer_policy import (
        TransferAttemptResult,
        TransferStatus,
        binance_error_code,
        choose_retry_transfer_amount,
        is_insufficient_balance_error,
        safe_transfer_amount,
    )
    from binance_trade_bot.canary_capital_guard import cap_futures_margin, canary_status_summary


# Coins with USDC-M perpetual futures on Binance (verified via API)
# Updated: all have $5 min notional, deep order books
FUTURES_ELIGIBLE_COINS = {
    "SOL", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
    "SUI", "TIA", "ENA",
    # Also available but not in bot's coin list:
    # "BTC", "ETH", "BNB"
}


class FuturesPosition:
    """Tracks an open short position in memory."""
    def __init__(self, symbol: str, entry_price: float, quantity: float,
                 order_id: int, opened_at: float):
        self.symbol = symbol              # e.g. "SOLUSDC"
        self.entry_price = entry_price    # fill price at entry
        self.quantity = quantity           # number of contracts
        self.order_id = order_id           # opening order ID
        self.opened_at = opened_at         # unix timestamp
        self.peak_pnl_pct = 0.0           # best P&L % reached (for trailing)
        self.funding_paid = 0.0           # cumulative funding paid


class FuturesManager:
    """Manages USDC-M short positions during bear regime."""

    def __init__(self, client: Client, logger, config):
        self.client = client
        self.logger = logger
        self.config = config

        # Config
        self.bridge_symbol = config.BRIDGE.symbol  # "USDC"
        self.leverage = int(getattr(config, 'FUTURES_LEVERAGE', 1))
        self.max_margin_pct = float(getattr(config, 'FUTURES_MAX_MARGIN_PCT', 0.5))
        self.margin_type = self._normalize_margin_type(
            getattr(config, 'FUTURES_MARGIN_TYPE', 'CROSS')
        )
        self.stop_loss_pct = float(getattr(config, 'FUTURES_STOP_LOSS_PCT', 15.0))
        self.trailing_stop_pct = float(getattr(config, 'FUTURES_TRAILING_STOP_PCT', 10.0))
        self.trailing_activation_pct = float(getattr(config, 'FUTURES_TRAILING_ACTIVATION_PCT', 3.0))
        self.server_trailing_enabled = bool(getattr(config, 'FUTURES_SERVER_TRAILING_ENABLED', True))
        self.server_trailing_callback_rate = float(getattr(config, 'FUTURES_SERVER_TRAILING_CALLBACK_RATE', 1.0))
        self.server_trailing_min_profit_buffer_pct = float(
            getattr(config, 'FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT', 0.5)
        )
        self.max_funding_rate = float(getattr(config, 'FUTURES_MAX_FUNDING_RATE', 0.0001))
        self.funding_exit_multiplier = float(getattr(config, 'FUTURES_FUNDING_EXIT_MULTIPLIER', 3.0))
        self.position_check_interval = int(getattr(config, 'FUTURES_CHECK_INTERVAL', 60))
        self.testnet = getattr(config, 'TESTNET', False)

        # State
        self._open_position: Optional[FuturesPosition] = None
        self._stop_order_id: Optional[int] = None     # algo order ID for hard stop
        self._trailing_order_id: Optional[int] = None  # algo order ID for trailing stop
        self._last_check = 0
        self._last_entry_attempt = 0
        self._initialized = False
        self._exchange_info_cache = None

    def _normalize_margin_type(self, value) -> str:
        """Normalize configured futures margin type.

        Binance rejected ISOLATED for this account with credit-status errors, so
        CROSS is the production default. ISOLATED remains configurable for a
        future account state where Binance allows it, but failure to set it must
        abort the short instead of silently opening CROSS exposure.
        """
        margin_type = str(value or "CROSS").upper()
        if margin_type not in {"CROSS", "ISOLATED"}:
            self.logger.warning(
                f"Unknown FUTURES_MARGIN_TYPE={margin_type!r}; defaulting to CROSS"
            )
            return "CROSS"
        return margin_type

    @staticmethod
    def _is_margin_mode_noop_error(error: BinanceAPIException) -> bool:
        """Return True when Binance says the requested margin mode is already set."""
        message = str(getattr(error, "message", "") or error).lower()
        return getattr(error, "code", None) == -4046 or "no need to change margin type" in message

    def _ensure_margin_mode(self, futures_symbol: str) -> bool:
        """Set leverage and the configured margin mode before opening a short."""
        self.client.futures_change_leverage(
            symbol=futures_symbol, leverage=self.leverage
        )
        try:
            self.client.futures_change_margin_type(
                symbol=futures_symbol, marginType=self.margin_type
            )
        except BinanceAPIException as e:
            if self._is_margin_mode_noop_error(e):
                self.logger.debug(
                    f"Futures margin mode already {self.margin_type} for {futures_symbol}"
                )
                return True
            self.logger.error(
                f"Futures margin mode {self.margin_type} setup failed for {futures_symbol}; "
                f"short aborted: {e}"
            )
            return False
        return True

    def _validate_position_margin_mode(self, futures_symbol: str):
        """Warn if Binance reports a different margin mode than configured."""
        try:
            positions = self.client.futures_position_information(symbol=futures_symbol)
        except Exception as e:
            self.logger.debug(f"Could not verify margin mode for {futures_symbol}: {e}")
            return
        for pos in positions:
            if pos.get("symbol") != futures_symbol:
                continue
            try:
                if abs(float(pos.get("positionAmt", 0))) == 0:
                    continue
            except Exception:
                continue
            actual = str(pos.get("marginType") or "?").upper()
            if actual != self.margin_type:
                self.logger.warning(
                    f"Futures margin mode mismatch for {futures_symbol}: "
                    f"expected {self.margin_type}, exchange reports {actual}. "
                    "Risk controls use conservative sizing and server-side stops."
                )
            return

    # ─────────────────────────────────────────────────────────────────────────
    #  INITIALIZATION
    # ─────────────────────────────────────────────────────────────────────────

    def initialize(self):
        """Set up leverage and margin type for all eligible coins.
        Reconcile any existing positions from a previous session."""
        if self._initialized:
            return

        try:
            # Reconcile existing positions
            self._reconcile_positions()
            self._initialized = True
            self.logger.info(
                f"FuturesManager initialized | "
                f"Leverage: {self.leverage}x | "
                f"Margin mode: {self.margin_type} | "
                f"Max margin: {self.max_margin_pct*100:.0f}% | "
                f"Stop: {self.stop_loss_pct}% | "
                f"Open positions: {1 if self._open_position else 0} | "
                f"{canary_status_summary(self.config)}"
            )
        except Exception as e:
            self.logger.warning(f"FuturesManager init failed: {e}")

    def _reconcile_positions(self):
        """Check Binance for any open short positions (e.g. from a crash).
        Also cleans up orphaned algo orders from previous positions."""
        # First: check for any stale algo orders across all symbols
        self._cleanup_orphaned_algo_orders()

        try:
            positions = self.client.futures_position_information()
            shorts = []
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt < 0:  # negative = short
                    symbol = pos["symbol"]
                    entry = float(pos["entryPrice"])
                    qty = abs(amt)
                    shorts.append((symbol, qty, entry))

            if not shorts:
                return

            # The strategy is intentionally single-position.  If Binance ever
            # contains multiple shorts (manual trade, crash edge case), keep the
            # largest notional as the managed position and immediately flatten
            # the rest reduce-only.  If an orphan close fails, place a hard stop
            # on it so it is not left naked.
            shorts.sort(key=lambda x: x[1] * x[2], reverse=True)
            symbol, qty, entry = shorts[0]
            self._open_position = FuturesPosition(
                symbol=symbol,
                entry_price=entry,
                quantity=qty,
                order_id=0,
                opened_at=time.time(),
            )
            self.logger.warning(
                f"RECONCILED existing short: {symbol} "
                f"qty={qty} entry={entry} "
                f"(recovered from exchange state)"
            )
            self._cancel_server_stops(symbol)
            if not self._place_server_stops(symbol, qty, entry):
                self.logger.error(
                    f"Recovered {symbol} short is unprotected — closing immediately"
                )
                self._close_position("unprotected after reconciliation")

            for extra_symbol, extra_qty, extra_entry in shorts[1:]:
                self.logger.error(
                    f"Multiple futures shorts detected; closing unmanaged orphan "
                    f"{extra_symbol} qty={extra_qty} entry={extra_entry}"
                )
                try:
                    self.client.futures_create_order(
                        symbol=extra_symbol,
                        side="BUY",
                        type="MARKET",
                        quantity=extra_qty,
                        reduceOnly="true",
                    )
                    self._cancel_server_stops(extra_symbol)
                except Exception as e:
                    self.logger.error(
                        f"Failed to close orphan short {extra_symbol}: {e}; placing hard stop"
                    )
                    self._cancel_server_stops(extra_symbol)
                    self._place_server_stops(extra_symbol, extra_qty, extra_entry)
            return
        except BinanceAPIException as e:
            self.logger.warning(f"Position reconciliation failed: {e}")
        except Exception as e:
            self.logger.warning(f"Position reconciliation error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN MANAGEMENT LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def manage_bear(self, performance: Dict[str, float], regime: str) -> str:
        """Called every scout cycle during BEAR regime.
        
        Args:
            performance: {symbol: pct_change} for all coins
            regime: current market regime string
            
        Returns:
            Action taken: 'opened', 'closed', 'holding', 'managed', 'idle'
        """
        if not self._initialized:
            self.initialize()

        now = time.time()
        if now - self._last_check < self.position_check_interval:
            return 'idle'
        self._last_check = now

        # If we have an open position, manage it
        if self._open_position is not None:
            return self._manage_open_position()

        # No open position — look for entry opportunity
        # Don't open more than one position every 5 minutes
        if now - self._last_entry_attempt < 300:
            return 'idle'

        return self._attempt_entry(performance)

    def manage_exit(self) -> str:
        """Called when regime shifts away from BEAR.
        Closes all short positions."""
        if self._open_position is None:
            # Double-check exchange for any orphans
            self._reconcile_positions()
            if self._open_position is None:
                return 'idle'

        return self._close_position("regime change")

    # ─────────────────────────────────────────────────────────────────────────
    #  POSITION MANAGEMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _manage_open_position(self) -> str:
        """Check stop-loss, trailing stop, and funding on the open position."""
        pos = self._open_position
        try:
            # Check if server-side stop already closed the position externally
            if self._check_server_stopped():
                self.logger.warning(
                    f" Futures SHORT closed externally (server-side stop): {pos.symbol} "
                    f"entry={pos.entry_price}"
                )
                # Clean up the OTHER algo order still live on Binance
                self._cancel_server_stops(pos.symbol)
                self._open_position = None
                return 'closed'

            # Get current mark price
            mark_price = self._get_mark_price(pos.symbol)
            if mark_price is None:
                return 'holding'

            # Calculate P&L percentage
            # Short profit = price went DOWN
            pnl_pct = ((pos.entry_price - mark_price) / pos.entry_price) * 100

            # Update trailing peak
            if pnl_pct > pos.peak_pnl_pct:
                pos.peak_pnl_pct = pnl_pct

            # Trailing stop: if we were up X% but gave back Y%, close
            if pos.peak_pnl_pct > self.trailing_activation_pct:
                giveback = pos.peak_pnl_pct - pnl_pct
                if giveback >= self.trailing_stop_pct:
                    self.logger.info(
                        f"Futures trailing stop: {pos.symbol} "
                        f"peak={pos.peak_pnl_pct:.1f}% current={pnl_pct:.1f}% "
                        f"giveback={giveback:.1f}%"
                    )
                    return self._close_position("trailing stop")

            # Hard stop loss
            if pnl_pct <= -self.stop_loss_pct:
                self.logger.warning(
                    f"Futures STOP LOSS: {pos.symbol} P&L={pnl_pct:.1f}%"
                )
                return self._close_position("stop loss")

            # Check funding rate on open position.
            # For shorts, NEGATIVE funding is adverse (shorts pay longs).
            funding = self._get_funding_rate(pos.symbol)
            if funding is not None and funding < -(self.max_funding_rate * self.funding_exit_multiplier):
                self.logger.warning(
                    f"Futures adverse funding rate: {pos.symbol} rate={funding*100:.4f}% "
                    f"— closing position to avoid bleed"
                )
                return self._close_position("funding rate")

            self.logger.debug(
                f"Futures holding: {pos.symbol} P&L={pnl_pct:+.1f}% "
                f"peak={pos.peak_pnl_pct:.1f}% funding={funding}"
            )
            return 'holding'

        except Exception as e:
            self.logger.warning(f"Position management error: {e}")
            return 'holding'

    def _attempt_entry(self, performance: Dict[str, float]) -> str:
        """Find the worst-performing eligible coin and short it."""
        self._last_entry_attempt = time.time()

        # Get USDC balance available for margin
        usdc_balance = self._get_futures_usdc_balance()
        if usdc_balance < 5.0:
            self.logger.debug(f"Futures: insufficient USDC for margin ({usdc_balance})")
            return 'idle'

        cap = cap_futures_margin(usdc_balance, self.max_margin_pct, self.config)
        margin = cap.allowed_margin
        if cap.capped:
            self.logger.warning(
                f"{cap.reason}: limiting futures margin from ${cap.original_margin:.2f} "
                f"to ${cap.allowed_margin:.2f}"
            )
        if margin < 5.0:
            return 'idle'

        # Find worst performer among futures-eligible coins
        candidates = []
        for symbol, perf in performance.items():
            if symbol not in FUTURES_ELIGIBLE_COINS:
                continue
            if perf is None:
                continue
            # Only short coins that are actually falling
            if perf < 0:
                candidates.append((symbol, perf))

        if not candidates:
            self.logger.debug("Futures: no coins with negative performance to short")
            return 'idle'

        # Sort by worst performance (most negative)
        candidates.sort(key=lambda x: x[1])
        best_short = candidates[0]
        symbol = best_short[0]
        perf = best_short[1]

        # Check funding rate — don't open if short funding is adverse.
        # For shorts, NEGATIVE funding means shorts pay longs.
        funding = self._get_funding_rate(f"{symbol}{self.bridge_symbol}")
        if funding is not None and funding < -self.max_funding_rate:
            self.logger.info(
                f"Futures: skipping {symbol} short — adverse funding rate "
                f"({funding*100:.4f}% < -{self.max_funding_rate*100:.4f}%)"
            )
            return 'idle'

        # Execute the short
        return self._open_short(symbol, margin, perf)

    def _open_short(self, coin: str, margin: float, perf_pct: float) -> str:
        """Open a short position on {coin}USDC perpetual."""
        futures_symbol = f"{coin}{self.bridge_symbol}"

        try:
            # Set leverage and configured margin mode before sizing/opening.
            # Default is CROSS because Binance currently rejects ISOLATED on
            # this account; if ISOLATED is explicitly configured and cannot be
            # set, abort instead of silently opening an unexpected cross short.
            if not self._ensure_margin_mode(futures_symbol):
                return 'idle'

            # Get current price for quantity calculation
            price = self._get_mark_price(futures_symbol)
            if price is None or price <= 0:
                self.logger.warning(f"Futures: can't get price for {futures_symbol}")
                return 'idle'

            # Calculate quantity: at 1x leverage, position = margin
            notional = margin * self.leverage
            quantity = notional / price

            # Floor to exchange step size. Never round up: rounding can create
            # a quantity Binance rejects or a notional slightly above intended.
            quantity = self._floor_quantity(futures_symbol, quantity)

            if quantity <= 0:
                self.logger.warning(f"Futures: quantity rounds to 0 for {futures_symbol}")
                return 'idle'

            # Check minimum notional
            min_notional = self._get_min_notional(futures_symbol)
            if quantity * price < min_notional:
                self.logger.warning(
                    f"Futures: order too small for {futures_symbol} "
                    f"(${quantity*price:.2f} < ${min_notional})"
                )
                return 'idle'

            # Place MARKET short order (SELL = open short on futures)
            order = self.client.futures_create_order(
                symbol=futures_symbol,
                side="SELL",
                type="MARKET",
                quantity=quantity,
            )

            order_id = order.get("orderId", 0)

            # Get actual fill price
            fill_price = float(order.get("avgPrice", 0))
            if fill_price == 0:
                fill_price = price  # fallback to mark price

            self._open_position = FuturesPosition(
                symbol=futures_symbol,
                entry_price=fill_price,
                quantity=quantity,
                order_id=order_id,
                opened_at=time.time(),
            )

            # Place server-side hard stop for instant crash/VPS protection.
            # If protection cannot be confirmed, immediately flatten the short.
            if not self._place_server_stops(futures_symbol, quantity, fill_price):
                self.logger.error(
                    f"Protective stop failed for {futures_symbol}; closing new short immediately"
                )
                try:
                    self.client.futures_create_order(
                        symbol=futures_symbol,
                        side="BUY",
                        type="MARKET",
                        quantity=quantity,
                        reduceOnly="true",
                    )
                finally:
                    self._open_position = None
                return 'idle'

            self._validate_position_margin_mode(futures_symbol)

            self.logger.warning(
                f" Futures SHORT opened: {futures_symbol} "
                f"qty={quantity} entry={fill_price} "
                f"margin=${margin:.2f} mode={self.margin_type} perf={perf_pct:+.1f}% "
                f"funding={self._get_funding_rate(futures_symbol)}"
            )
            return 'opened'

        except BinanceAPIException as e:
            self.logger.error(f"Futures short failed: {e}")
            return 'idle'
        except Exception as e:
            self.logger.error(f"Futures short error: {e}")
            return 'idle'

    def _close_position(self, reason: str) -> str:
        """Close the open short position with a market buy."""
        pos = self._open_position
        if pos is None:
            return 'idle'

        try:
            # BUY = close short (reduceOnly ensures we don't accidentally go long).
            # Keep the server stop live until after the market close succeeds;
            # if close fails, protection remains intact.
            close_order = self.client.futures_create_order(
                symbol=pos.symbol,
                side="BUY",
                type="MARKET",
                quantity=pos.quantity,
                reduceOnly="true",
                newOrderRespType="RESULT",
            )

            # Confirm the position is actually flat before canceling protection.
            time.sleep(1)
            still_open = False
            for p in self.client.futures_position_information(symbol=pos.symbol):
                if p.get("symbol") == pos.symbol and abs(float(p.get("positionAmt", 0))) > 0:
                    still_open = True
                    break
            if still_open:
                self.logger.error(
                    f"Futures close for {pos.symbol} did not flatten position; "
                    "keeping server stop active"
                )
                return 'holding'

            # Now that we are flat, cancel any leftover server-side stop orders.
            self._cancel_server_stops(pos.symbol)

            # Calculate final P&L from the actual close fill price, not the
            # post-close mark price (which can move after execution).
            close_price = float(close_order.get("avgPrice") or 0)
            close_order_id = close_order.get("orderId")
            if close_price <= 0 and close_order_id:
                try:
                    fetched = self.client.futures_get_order(
                        symbol=pos.symbol,
                        orderId=close_order_id,
                    )
                    close_price = float(fetched.get("avgPrice") or 0)
                except Exception as e:
                    self.logger.debug(f"Could not fetch close avgPrice for {pos.symbol}: {e}")
            if close_price <= 0:
                close_price = self._get_mark_price(pos.symbol) or pos.entry_price

            pnl_pct = ((pos.entry_price - close_price) / pos.entry_price) * 100
            pnl_usd = (pos.entry_price - close_price) * pos.quantity

            hold_time = time.time() - pos.opened_at
            self.logger.warning(
                f" Futures SHORT closed: {pos.symbol} "
                f"entry={pos.entry_price} close={close_price} "
                f"P&L={pnl_pct:+.1f}% (${pnl_usd:+.2f}) "
                f"held={hold_time/3600:.1f}h reason={reason}"
            )

            self._open_position = None
            return 'closed'

        except BinanceAPIException as e:
            self.logger.error(f"Futures close failed: {e}")
            return 'holding'
        except Exception as e:
            self.logger.error(f"Futures close error: {e}")
            return 'holding'

    # ─────────────────────────────────────────────────────────────────────────
    #  SERVER-SIDE STOP ORDERS (algo orders on Binance)
    # ─────────────────────────────────────────────────────────────────────────

    def _place_server_stops(self, symbol: str, quantity: float, entry_price: float) -> bool:
        """Place server-side hard STOP_MARKET protection.

        Returns True only if the hard stop was accepted and an order/algo ID
        was recorded. Server-side trailing is disabled; client-side trailing
        handles profit exits.
        """
        try:
            # Hard stop-loss: fires when price goes UP by stop_loss_pct (short loses)
            stop_price = self._round_price_to_tick(
                symbol,
                entry_price * (1 + self.stop_loss_pct / 100),
                rounding=ROUND_CEILING,
            )
            stop = self.client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="STOP_MARKET",
                quantity=quantity,
                stopPrice=str(stop_price),
                workingType="MARK_PRICE",
                reduceOnly="true",
            )
            self._stop_order_id = stop.get("algoId", stop.get("orderId", 0))
            if not self._stop_order_id:
                self.logger.error(f"Server stop placement returned no order/algo ID for {symbol}")
                return False
            self.logger.info(
                f"Server stop placed: {symbol} trigger={stop_price} "
                f"(+{self.stop_loss_pct}%) algoId={self._stop_order_id}"
            )
        except Exception as e:
            self.logger.warning(f"Failed to place server stop-loss: {e}")
            return False

        if not self._place_server_trailing_stop(symbol, quantity, entry_price):
            self.logger.info(
                f"Server trailing stop not active for {symbol}; hard STOP_MARKET remains live"
            )
        return True

    def _place_server_trailing_stop(self, symbol: str, quantity: float, entry_price: float) -> bool:
        """Place a verified server-side trailing stop for a short.

        This is intentionally separate from the hard stop: if the trailing stop
        is rejected or fails safety verification, the already-placed hard
        STOP_MARKET remains live.

        For a SHORT close, the trailing order is a BUY. It should activate only
        after price falls below entry (short in profit). Binance callbackRate is
        a percent of PRICE, not P&L, so callback must be smaller than the
        activation profit or the worst-case trigger can be above entry.
        """
        self._trailing_order_id = None
        if not self.server_trailing_enabled:
            self.logger.info(f"Server trailing disabled by config for {symbol}")
            return False

        activation_pct = max(0.1, self.trailing_activation_pct)
        buffer_pct = max(0.0, self.server_trailing_min_profit_buffer_pct)
        activation_price = entry_price * (1 - activation_pct / 100)

        # Require worst-case trigger at activation to still close in profit.
        # For BUY trailing on a short: worst_trigger = activation * (1 + callback/100)
        max_safe_callback = ((1 - buffer_pct / 100) / (1 - activation_pct / 100) - 1) * 100
        callback_rate = max(0.1, min(self.server_trailing_callback_rate, 5.0, max_safe_callback))
        if callback_rate < 0.1 or max_safe_callback < 0.1:
            self.logger.warning(
                f"Server trailing skipped for {symbol}: activation={activation_pct}% "
                f"buffer={buffer_pct}% leaves no safe Binance callbackRate"
            )
            return False
        if callback_rate != self.server_trailing_callback_rate:
            self.logger.warning(
                f"Server trailing callback clamped for {symbol}: requested "
                f"{self.server_trailing_callback_rate}% -> safe {callback_rate:.2f}%"
            )

        activation_price = self._round_price_to_tick(symbol, activation_price, rounding=ROUND_DOWN)
        worst_trigger = activation_price * (1 + callback_rate / 100)
        if worst_trigger >= entry_price * (1 - buffer_pct / 100):
            self.logger.warning(
                f"Server trailing skipped for {symbol}: worst trigger {worst_trigger:.8f} "
                f"would not preserve {buffer_pct}% profit buffer vs entry {entry_price}"
            )
            return False

        try:
            trail = self.client.futures_create_algo_order(
                algoType="CONDITIONAL",
                symbol=symbol,
                side="BUY",
                type="TRAILING_STOP_MARKET",
                quantity=quantity,
                activatePrice=str(activation_price),
                callbackRate=str(round(callback_rate, 2)),
                workingType="MARK_PRICE",
                reduceOnly="true",
            )
            algo_id = trail.get("algoId", trail.get("orderId", 0))
            if not algo_id:
                self.logger.warning(f"Server trailing placement returned no algo ID for {symbol}")
                return False
            self._trailing_order_id = algo_id
        except Exception as e:
            self.logger.warning(f"Failed to place server trailing stop for {symbol}: {e}")
            return False

        if not self._verify_server_trailing_stop(symbol, entry_price, activation_price, callback_rate):
            self.logger.warning(f"Server trailing verification failed for {symbol}; cancelling trailing only")
            self._cancel_algo_order(symbol, self._trailing_order_id, "trailing stop")
            self._trailing_order_id = None
            return False

        self.logger.info(
            f"Server trailing stop placed: {symbol} activate={activation_price} "
            f"callback={callback_rate:.2f}% algoId={self._trailing_order_id}"
        )
        return True

    def _verify_server_trailing_stop(
        self,
        symbol: str,
        entry_price: float,
        expected_activation: float,
        callback_rate: float,
    ) -> bool:
        """Verify Binance stored a safe trailing algo order for a short."""
        try:
            orders = self.client.futures_get_open_algo_orders(symbol=symbol)
            trailing = None
            for order in orders:
                if int(order.get("algoId", order.get("orderId", 0)) or 0) == int(self._trailing_order_id or 0):
                    trailing = order
                    break
            if not trailing:
                self.logger.warning(f"Could not find trailing algo order for {symbol} after placement")
                return False

            activate = float(
                trailing.get("activatePrice")
                or trailing.get("activationPrice")
                or expected_activation
            )
            # Binance reports a pre-activation triggerPrice based on current
            # mark price for NEW trailing algos. That value can be above entry
            # while the order is still inactive; it is not executable until
            # activatePrice is reached. Safety is therefore verified from
            # activatePrice + callbackRate math below, not this display field.
            trigger_raw = trailing.get("triggerPrice")
            trigger = float(trigger_raw) if trigger_raw not in (None, "", 0, "0") else 0.0

            if activate >= entry_price:
                self.logger.warning(
                    f"Unsafe trailing activatePrice for {symbol}: {activate} >= entry {entry_price}"
                )
                return False

            expected_worst_trigger = activate * (1 + callback_rate / 100)
            max_allowed_trigger = entry_price * (1 - self.server_trailing_min_profit_buffer_pct / 100)
            if expected_worst_trigger >= max_allowed_trigger:
                self.logger.warning(
                    f"Unsafe trailing worst trigger for {symbol}: {expected_worst_trigger:.8f} "
                    f">= allowed {max_allowed_trigger:.8f}"
                )
                return False

            if trigger and trigger >= max_allowed_trigger:
                self.logger.info(
                    f"Server trailing pre-activation trigger display for {symbol}: "
                    f"{trigger} (ignored until activatePrice {activate} is reached)"
                )

            return True
        except Exception as e:
            self.logger.warning(f"Trailing stop verification error for {symbol}: {e}")
            return False

    def _cancel_algo_order(self, symbol: str, algo_id, label: str):
        """Cancel one algo order by algoId without removing the hard stop."""
        if not algo_id:
            return
        try:
            self.client.futures_cancel_algo_order(symbol=symbol, algoId=int(algo_id))
            self.logger.info(f"Cancelled {label} algo order on {symbol}: {algo_id}")
        except Exception as e:
            self.logger.warning(f"Failed to cancel {label} algo order {algo_id} on {symbol}: {e}")

    def _cancel_server_stops(self, symbol: str):
        """Cancel server-side stop orders when we close manually."""
        try:
            result = self.client.futures_cancel_all_algo_open_orders(symbol=symbol)
            self.logger.info(f"Cancelled server-side stop orders on {symbol}")
        except Exception as e:
            self.logger.warning(f"Failed to cancel server stops: {e}")
        finally:
            self._stop_order_id = None
            self._trailing_order_id = None

    def _cleanup_orphaned_algo_orders(self):
        """Find and cancel algo orders that have no matching open position.
        
        This handles:
        - Server stop fired → the OTHER stop is still live
        - Bot crashed/restarted after position closed → stale orders remain
        - Multiple restarts → duplicate orders accumulated
        """
        try:
            open_algo = self.client.futures_get_open_algo_orders()
            if not open_algo:
                return

            # Get all symbols with open positions
            open_symbols = set()
            for pos in self.client.futures_position_information():
                if float(pos.get("positionAmt", 0)) != 0:
                    open_symbols.add(pos["symbol"])

            # Cancel any algo order whose symbol has no open position
            cancelled = 0
            for order in open_algo:
                sym = order.get("symbol", "")
                if sym not in open_symbols:
                    try:
                        self.client.futures_cancel_all_algo_open_orders(symbol=sym)
                        cancelled += 1
                        self.logger.info(
                            f"Cleaned up orphaned {order.get('orderType', '?')} "
                            f"on {sym} (no open position)"
                        )
                    except Exception:
                        pass

            if cancelled:
                self.logger.info(
                    f"Orphan cleanup: removed {cancelled} stale algo order(s) "
                    f"across {cancelled} symbol(s)"
                )
        except Exception as e:
            self.logger.warning(f"Orphan algo cleanup failed: {e}")

    def _check_server_stopped(self) -> bool:
        """Check if server-side stops already closed our position (e.g. bot was down)."""
        if self._open_position is None:
            return False
        try:
            pos = self.client.futures_position_information(symbol=self._open_position.symbol)
            amt = float(pos[0].get("positionAmt", 0))
            if amt == 0:
                return True  # position was closed externally (by server stop)
        except Exception as e:
            self.logger.warning(f"Server stop status check failed for {self._open_position.symbol}: {e}")
        return False

    # ─────────────────────────────────────────────────────────────────────────
    #  API HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_mark_price(self, symbol: str) -> Optional[float]:
        """Get current mark price for a futures symbol."""
        try:
            data = self.client.futures_mark_price(symbol=symbol)
            return float(data.get("markPrice", 0))
        except Exception:
            return None

    def _get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get current/predicted funding rate for a futures symbol.

        Uses premiumIndex.lastFundingRate rather than funding history. For
        shorts: positive = shorts get paid, negative = shorts pay.
        """
        try:
            data = self.client.futures_mark_price(symbol=symbol)
            rate = data.get("lastFundingRate")
            if rate is not None:
                return float(rate)
        except Exception:
            pass
        return None

    def _get_futures_usdc_balance(self) -> float:
        """Get available USDC balance in futures wallet.

        NOTE: Binance API 'availableBalance' field can return 0.0 even when
        funds are present (known quirk with no open positions). We use 'balance'
        as the primary field and 'maxWithdrawAmount' as a cross-check.
        """
        try:
            balances = self.client.futures_account_balance()
            for bal in balances:
                if bal.get("asset") == self.bridge_symbol:
                    # Use 'balance' — 'availableBalance' is unreliable
                    balance = float(bal.get("balance", 0))
                    max_wd = float(bal.get("maxWithdrawAmount", 0))
                    # Return the more conservative of the two
                    return min(balance, max_wd) if max_wd > 0 else balance
        except Exception as e:
            self.logger.warning(f"Futures balance check failed: {e}")
        return 0.0

    def _get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return cached futures exchangeInfo entry for a symbol."""
        try:
            if self._exchange_info_cache is None:
                self._exchange_info_cache = self.client.futures_exchange_info()
            for s in self._exchange_info_cache.get("symbols", []):
                if s.get("symbol") == symbol:
                    return s
        except Exception as e:
            self.logger.debug(f"Exchange info lookup failed for {symbol}: {e}")
        return None

    def _get_symbol_filter(self, symbol: str, filter_type: str) -> Optional[dict]:
        info = self._get_symbol_info(symbol)
        if not info:
            return None
        for f in info.get("filters", []):
            if f.get("filterType") == filter_type:
                return f
        return None

    @staticmethod
    def _round_to_step(value: float, step: float, rounding=ROUND_DOWN) -> float:
        """Round a value to a Binance step/tick using Decimal arithmetic."""
        if step <= 0:
            return value
        d_value = Decimal(str(value))
        d_step = Decimal(str(step))
        units = (d_value / d_step).to_integral_value(rounding=rounding)
        return float(units * d_step)

    def _floor_quantity(self, symbol: str, quantity: float) -> float:
        """Floor market quantity to Binance MARKET_LOT_SIZE/LOT_SIZE stepSize."""
        f = self._get_symbol_filter(symbol, "MARKET_LOT_SIZE") or self._get_symbol_filter(symbol, "LOT_SIZE")
        step = float(f.get("stepSize", 0)) if f else 0
        if step <= 0:
            # Fallback to old quantityPrecision when no stepSize is provided.
            precision = self._get_quantity_precision(symbol)
            return round(quantity, precision)
        return self._round_to_step(quantity, step, ROUND_DOWN)

    def _round_price_to_tick(self, symbol: str, price: float, rounding=ROUND_DOWN) -> float:
        """Round a trigger/limit price to Binance PRICE_FILTER tickSize."""
        f = self._get_symbol_filter(symbol, "PRICE_FILTER")
        tick = float(f.get("tickSize", 0)) if f else 0
        if tick <= 0:
            return round(price, 6)
        return self._round_to_step(price, tick, rounding)

    def _get_quantity_precision(self, symbol: str) -> int:
        """Get quantity precision for a futures symbol from exchange info."""
        try:
            info = self._get_symbol_info(symbol)
            if info:
                return int(info.get("quantityPrecision", 2))
        except Exception:
            pass
        return 2  # safe default

    def _get_min_notional(self, symbol: str) -> float:
        """Get minimum notional for a futures symbol."""
        try:
            info = self._get_symbol_info(symbol)
            if info:
                for f in info.get("filters", []):
                    if f["filterType"] == "MIN_NOTIONAL":
                        return float(f.get("notional", 5))
        except Exception:
            pass
        return 5.0

    # ─────────────────────────────────────────────────────────────────────────
    #  TRANSFER (spot ↔ futures)
    # ─────────────────────────────────────────────────────────────────────────

    def transfer_to_futures(self, amount: float) -> bool:
        """Transfer USDC from spot wallet to futures wallet."""
        try:
            self.client.futures_account_transfer(
                asset=self.bridge_symbol,
                amount=amount,
                type=1,  # 1 = spot to USDT-M/USDC-M futures
            )
            self.logger.info(f"Transferred {amount} {self.bridge_symbol} to futures")
            return True
        except Exception as e:
            self.logger.error(f"Transfer to futures failed: {e}")
            return False

    def transfer_to_spot(self, amount: float) -> bool:
        """Transfer USDC from futures wallet to spot wallet."""
        return bool(self.transfer_to_spot_result(amount))

    def transfer_to_spot_result(self, amount: float) -> TransferAttemptResult:
        """Transfer USDC from futures wallet to spot wallet and return metadata.

        Binance can reject an exact max-withdrawable amount with -5013 even
        when account fields show funds present. Transfer conservatively: leave
        small dust, floor to cents, and retry once with a freshly-read lower
        withdrawable balance. Insufficient-balance failures are logged without
        Telegram notification spam; funds remain safely in futures for the next
        cycle/manual inspection.
        """
        first_amount = safe_transfer_amount(amount)
        if first_amount <= 0:
            self.logger.debug(
                f"Futures→spot transfer skipped: {amount:.8f} {self.bridge_symbol} "
                "is below transferable threshold after dust buffer"
            )
            return TransferAttemptResult(
                status=TransferStatus.SKIPPED,
                requested_amount=amount,
                retryable=False,
            )

        attempts = [first_amount]
        completed_attempts = []
        last_error = None
        for idx, transfer_amount in enumerate(attempts):
            completed_attempts.append(transfer_amount)
            try:
                self.client.futures_account_transfer(
                    asset=self.bridge_symbol,
                    amount=transfer_amount,
                    type=2,  # 2 = USDT-M/USDC-M futures to spot
                )
                self.logger.info(f"Transferred {transfer_amount:.2f} {self.bridge_symbol} to spot")
                return TransferAttemptResult(
                    status=TransferStatus.SUCCESS,
                    requested_amount=amount,
                    attempted_amounts=tuple(completed_attempts),
                    transferred_amount=transfer_amount,
                )
            except Exception as e:
                last_error = e
                if idx == 0 and is_insufficient_balance_error(e):
                    refreshed_amount = choose_retry_transfer_amount(
                        previous_attempt=transfer_amount,
                        refreshed_withdrawable=self._get_futures_usdc_balance(),
                    )
                    if refreshed_amount > 0 and refreshed_amount < transfer_amount:
                        self.logger.warning(
                            f"Retrying futures→spot transfer after insufficient balance: "
                            f"{transfer_amount:.2f} → {refreshed_amount:.2f} {self.bridge_symbol}",
                            notification=False,
                        )
                        attempts.append(refreshed_amount)
                        continue
                break

        if is_insufficient_balance_error(last_error):
            self.logger.warning(
                f"Futures→spot transfer unavailable after conservative retry; "
                f"leaving funds in futures ({last_error})",
                notification=False,
            )
            return TransferAttemptResult(
                status=TransferStatus.RETRYABLE_FAILURE,
                requested_amount=amount,
                attempted_amounts=tuple(completed_attempts),
                error_code=binance_error_code(last_error),
                retryable=True,
                error_message=str(last_error),
            )

        self.logger.error(f"Transfer from futures failed: {last_error}")
        return TransferAttemptResult(
            status=TransferStatus.FAILED,
            requested_amount=amount,
            attempted_amounts=tuple(completed_attempts),
            error_code=binance_error_code(last_error),
            retryable=False,
            error_message=str(last_error),
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  STATUS
    # ─────────────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get current futures status for Telegram/API."""
        if self._open_position:
            mark = self._get_mark_price(self._open_position.symbol)
            if mark:
                pnl_pct = ((self._open_position.entry_price - mark) / self._open_position.entry_price) * 100
            else:
                pnl_pct = 0
            return {
                "active": True,
                "symbol": self._open_position.symbol,
                "entry": self._open_position.entry_price,
                "mark": mark,
                "pnl_pct": pnl_pct,
                "peak_pnl": self._open_position.peak_pnl_pct,
                "quantity": self._open_position.quantity,
                "hold_hours": (time.time() - self._open_position.opened_at) / 3600,
            }
        return {"active": False}
