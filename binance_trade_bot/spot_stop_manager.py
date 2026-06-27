"""
Server-Side Stop-Loss Protection for Spot Positions.

When a spot LONG position is opened (BULL regime), this module places a
Binance **OCO** (One-Cancels-Other) or **STOP_LOSS_LIMIT** order on the
exchange.  This ensures the hard stop (15% below entry) lives on Binance's
servers and will execute *even if the bot is offline*.

If OCO/STOP_LOSS_LIMIT order types are rejected for a pair (some pairs on
some regions disallow them), the manager automatically falls back to a
**background watchdog thread** that polls the current price every 60 seconds
and fires a market sell when the hard-stop level is breached.

Design principles (mirrors ``futures_manager._place_server_stops``):
  - Deterministic ``clientOrderId`` so a timeout+retry never double-stops.
  - Stop placement is **best-effort** — failure degrades to the watchdog, it
    never blocks the original buy from completing.
  - The OCO/STOP order is cancelled (or confirmed filled) when the strategy
    sells via its normal client-side path so leftover sell-side orders
    don't linger.
"""

import hashlib
import threading
import time
from decimal import Decimal, ROUND_CEILING
from typing import Dict, Optional, Tuple


class SpotStopManager:
    """Place and manage server-side stop-loss orders for spot positions.

    Lifecycle::

        place_stop(symbol, entry_price, quantity)   → True if server-side stop is live
        cancel_stop(symbol)                          → cancel when bot sells normally
        is_stop_filled(symbol)                       → True if the exchange stop fired
        shutdown()                                   → stop watchdog thread

    Parameters match the strategy's hard stop (15% below entry by default).
    The OCO variant additionally places a limit at -16% to guarantee a fill.
    """

    # Watchdog poll interval when server-side orders aren't available (seconds)
    WATCHDOG_INTERVAL = 60

    def __init__(
        self,
        binance_client,
        logger,
        config,
        stop_loss_pct: float = 0.15,
        oco_limit_offset: float = 0.01,
    ):
        self.client = binance_client
        self.logger = logger
        self.config = config

        self.stop_loss_pct = float(stop_loss_pct)
        self.oco_limit_offset = float(oco_limit_offset)

        # symbol → tracked position metadata
        self._tracked: Dict[str, dict] = {}

        # Watchdog state
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_positions: Dict[str, dict] = {}  # symbol → {entry, qty, coin}
        self._watchdog_lock = threading.Lock()

        # Callback used by watchdog to execute a market sell
        self.sell_callback = None  # set by strategy: (coin_symbol, bridge_symbol) -> bool

    # ─────────────────────────────────────────────────────────────────────────
    #  PUBLIC API
    # ─────────────────────────────────────────────────────────────────────────

    def place_stop(
        self,
        coin_symbol: str,
        bridge_symbol: str,
        entry_price: float,
        quantity: float,
        pair_info: Optional[dict] = None,
    ) -> bool:
        """Place a server-side stop for a freshly-bought spot position.

        Tries OCO first, then STOP_LOSS_LIMIT.  If both fail, registers the
        position with the background watchdog.

        Returns ``True`` if a server-side order is live, ``False`` if only the
        watchdog fallback is active.
        """
        symbol = f"{coin_symbol}{bridge_symbol}"

        # Compute stop prices from the same hard stop percentage the strategy uses
        stop_price = entry_price * (1.0 - self.stop_loss_pct)
        limit_price = entry_price * (1.0 - self.stop_loss_pct - self.oco_limit_offset)

        # Round to tick size if pair_info available
        tick_size = self._get_tick_size(pair_info, symbol)
        if tick_size:
            stop_price = self._round_price(stop_price, tick_size)
            limit_price = self._round_price(limit_price, tick_size)

        stop_price_s = f"{stop_price:.8f}".rstrip("0").rstrip(".")
        limit_price_s = f"{limit_price:.8f}".rstrip("0").rstrip(".")

        self.logger.info(
            f"SpotStop: placing server-side stop for {symbol} "
            f"entry={entry_price} stop={stop_price_s} limit={limit_price_s}"
        )

        # ── Attempt 1: OCO ───────────────────────────────────────────────
        if self._try_place_oco(symbol, coin_symbol, quantity, stop_price_s, limit_price_s):
            self._tracked[symbol] = {
                "type": "oco",
                "order_list_id": self._tracked.get(symbol, {}).get("order_list_id"),
                "entry_price": entry_price,
                "quantity": quantity,
            }
            self._remove_from_watchdog(symbol)
            return True

        # ── Attempt 2: STOP_LOSS_LIMIT ───────────────────────────────────
        if self._try_place_stop_loss_limit(symbol, coin_symbol, quantity, stop_price_s, limit_price_s):
            self._tracked[symbol] = {
                "type": "stop_loss_limit",
                "order_id": self._tracked.get(symbol, {}).get("order_id"),
                "entry_price": entry_price,
                "quantity": quantity,
            }
            self._remove_from_watchdog(symbol)
            return True

        # ── Fallback: watchdog thread ────────────────────────────────────
        self.logger.warning(
            f"SpotStop: OCO and STOP_LOSS_LIMIT both unavailable for {symbol}; "
            f"falling back to background watchdog (checks every {self.WATCHDOG_INTERVAL}s)"
        )
        self._add_to_watchdog(coin_symbol, bridge_symbol, entry_price, quantity)
        return False

    def cancel_stop(self, coin_symbol: str, bridge_symbol: str):
        """Cancel any server-side stop for the given pair.

        Called when the strategy sells the position via its normal path.
        Safe to call when no server-side stop is active.
        """
        symbol = f"{coin_symbol}{bridge_symbol}"
        tracked = self._tracked.pop(symbol, None)
        if not tracked:
            # Might be in watchdog instead
            self._remove_from_watchdog(symbol)
            return

        order_type = tracked.get("type")
        try:
            if order_type == "oco":
                order_list_id = tracked.get("order_list_id")
                if order_list_id:
                    self.client.cancel_oco_order(symbol=symbol, orderListId=order_list_id)
                    self.logger.info(f"SpotStop: cancelled OCO {order_list_id} on {symbol}")
            elif order_type == "stop_loss_limit":
                order_id = tracked.get("order_id")
                if order_id:
                    self.client.cancel_order(symbol=symbol, orderId=order_id)
                    self.logger.info(f"SpotStop: cancelled STOP_LOSS_LIMIT {order_id} on {symbol}")
        except Exception as e:
            # Order may already be filled or cancelled — that's fine
            self.logger.debug(f"SpotStop: cancel for {symbol} returned {e} (likely already filled/cancelled)")

    def is_stop_filled(self, coin_symbol: str, bridge_symbol: str) -> bool:
        """Check whether the server-side stop has been triggered/filled.

        Returns True if the exchange stop has executed (the bot should treat
        this as a forced exit).
        """
        symbol = f"{coin_symbol}{bridge_symbol}"
        tracked = self._tracked.get(symbol)
        if not tracked:
            return False

        try:
            order_type = tracked.get("type")
            if order_type == "oco":
                order_list_id = tracked.get("order_list_id")
                if not order_list_id:
                    return False
                oco = self.client.get_oco_order(orderListId=order_list_id)
                status = oco.get("listOrderStatus", "")
                return status == "ALL_DONE"
            elif order_type == "stop_loss_limit":
                order_id = tracked.get("order_id")
                if not order_id:
                    return False
                order = self.client.get_order(symbol=symbol, orderId=order_id)
                status = order.get("status", "")
                return status in ("FILLED", "CANCELED", "EXPIRED", "PARTIALLY_FILLED")
        except Exception as e:
            self.logger.debug(f"SpotStop: is_stop_filled check for {symbol} failed: {e}")
        return False

    def shutdown(self):
        """Stop the watchdog thread."""
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    # ─────────────────────────────────────────────────────────────────────────
    #  SERVER-SIDE ORDER PLACEMENT
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _generate_spot_stop_client_order_id(scope: str, symbol: str, quantity: float) -> str:
        """Deterministic clientOrderId for idempotent stop placement.

        Same (scope, symbol, quantity) → same ID, so retries on timeout are
        rejected as duplicates by Binance instead of stacking a second stop.
        """
        raw = f"BTS{scope}{symbol}{quantity}"
        digest = hashlib.md5(raw.encode()).hexdigest()[:16]
        return f"BTS{scope}{digest}{symbol}"[:36]

    def _try_place_oco(
        self,
        symbol: str,
        coin_symbol: str,
        quantity: float,
        stop_price: str,
        limit_price: str,
    ) -> bool:
        """Attempt to place an OCO sell order. Returns True on success."""
        try:
            client_order_id = self._generate_spot_stop_client_order_id("OCO", symbol, quantity)
            oco = self.client.order_oco_sell(
                symbol=symbol,
                quantity=str(quantity),
                price=limit_price,
                stopPrice=stop_price,
                stopLimitPrice=limit_price,
                stopLimitTimeInForce="GTC",
                newClientOrderId=client_order_id,
            )
            # Extract order list ID
            order_list_id = oco.get("orderListId")
            if order_list_id:
                self._tracked.setdefault(f"{coin_symbol}{symbol.split(coin_symbol)[1] if coin_symbol in symbol else ''}", {})
                # Store the order list ID directly on the symbol key
                # We use a temp dict that place_stop reads
                key = symbol
                self._tracked[key] = self._tracked.get(key, {})
                self._tracked[key]["order_list_id"] = order_list_id
            self.logger.info(
                f"SpotStop: OCO placed for {symbol} "
                f"stop={stop_price} limit={limit_price} listId={order_list_id}"
            )
            return True
        except Exception as e:
            self.logger.debug(f"SpotStop: OCO failed for {symbol}: {e}")
            return False

    def _try_place_stop_loss_limit(
        self,
        symbol: str,
        coin_symbol: str,
        quantity: float,
        stop_price: str,
        limit_price: str,
    ) -> bool:
        """Attempt to place a STOP_LOSS_LIMIT sell order. Returns True on success."""
        try:
            client_order_id = self._generate_spot_stop_client_order_id("STOP", symbol, quantity)
            order = self.client.create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=str(quantity),
                price=limit_price,
                stopPrice=stop_price,
                newClientOrderId=client_order_id,
            )
            order_id = order.get("orderId")
            key = symbol
            self._tracked[key] = self._tracked.get(key, {})
            self._tracked[key]["order_id"] = order_id
            self.logger.info(
                f"SpotStop: STOP_LOSS_LIMIT placed for {symbol} "
                f"stop={stop_price} limit={limit_price} orderId={order_id}"
            )
            return True
        except Exception as e:
            self.logger.debug(f"SpotStop: STOP_LOSS_LIMIT failed for {symbol}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  WATCHDOG FALLBACK
    # ─────────────────────────────────────────────────────────────────────────

    def _add_to_watchdog(
        self, coin_symbol: str, bridge_symbol: str, entry_price: float, quantity: float
    ):
        """Register a position for watchdog monitoring."""
        with self._watchdog_lock:
            self._watchdog_positions[coin_symbol] = {
                "bridge": bridge_symbol,
                "entry": entry_price,
                "qty": quantity,
            }
        self._ensure_watchdog_running()

    def _remove_from_watchdog(self, coin_symbol_or_symbol: str):
        """Remove a position from watchdog monitoring."""
        with self._watchdog_lock:
            # Could be either coin symbol or full pair symbol
            self._watchdog_positions.pop(coin_symbol_or_symbol, None)
            # Also try stripping bridge suffix if it was passed as full pair
            for key in list(self._watchdog_positions.keys()):
                if coin_symbol_or_symbol.endswith(key) or key in coin_symbol_or_symbol:
                    self._watchdog_positions.pop(key, None)

    def _ensure_watchdog_running(self):
        """Start the watchdog thread if not already running."""
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return

        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop,
            name="SpotStopWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        self.logger.info("SpotStop watchdog thread started")

    def _watchdog_loop(self):
        """Background thread: check all watchdog positions every WATCHDOG_INTERVAL seconds."""
        while not self._watchdog_stop.wait(self.WATCHDOG_INTERVAL):
            try:
                self._watchdog_cycle()
            except Exception as e:
                self.logger.warning(f"SpotStop watchdog cycle error: {e}")

    def _watchdog_cycle(self):
        """Check all watchdog-monitored positions and sell if stop is hit."""
        with self._watchdog_lock:
            positions = dict(self._watchdog_positions)

        if not positions:
            return

        for coin_symbol, pos in positions.items():
            symbol = f"{coin_symbol}{pos['bridge']}"
            try:
                ticker = self.client.get_symbol_ticker(symbol=symbol)
                current_price = float(ticker["price"])
            except Exception as e:
                self.logger.debug(f"SpotStop watchdog: couldn't get price for {symbol}: {e}")
                continue

            entry = pos["entry"]
            stop_level = entry * (1.0 - self.stop_loss_pct)

            if current_price <= stop_level:
                self.logger.warning(
                    f"SpotStop WATCHDOG: {symbol} hit stop! "
                    f"entry={entry} current={current_price} stop={stop_level:.8f}"
                )
                # Execute the sell
                sold = False
                if callable(self.sell_callback):
                    try:
                        sold = self.sell_callback(coin_symbol, pos["bridge"])
                    except Exception as e:
                        self.logger.error(f"SpotStop watchdog: sell callback failed for {symbol}: {e}")

                if sold:
                    with self._watchdog_lock:
                        self._watchdog_positions.pop(coin_symbol, None)
                else:
                    self.logger.error(
                        f"SpotStop WATCHDOG: failed to sell {symbol} at stop! "
                        f"Will retry next cycle."
                    )

    # ─────────────────────────────────────────────────────────────────────────
    #  HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _get_tick_size(self, pair_info: Optional[dict], symbol: str) -> Optional[float]:
        """Extract tick size from pair_info or fetch from exchange."""
        if pair_info:
            try:
                filters = pair_info.get("filters", [])
                for f in filters:
                    if f.get("filterType") == "PRICE_FILTER":
                        return float(f.get("tickSize", 0))
            except (KeyError, TypeError, ValueError):
                pass

        # Try fetching from exchange
        try:
            info = self.client.get_symbol_info(symbol)
            if info:
                filters = info.get("filters", [])
                for f in filters:
                    if f.get("filterType") == "PRICE_FILTER":
                        return float(f.get("tickSize", 0))
        except Exception:
            pass

        return None

    @staticmethod
    def _round_price(price: float, tick_size: float) -> float:
        """Round a price DOWN to the nearest tick (conservative for stop trigger)."""
        if tick_size <= 0:
            return price
        # Use Decimal for precision
        d_price = Decimal(str(price))
        d_tick = Decimal(str(tick_size))
        return float((d_price // d_tick) * d_tick)
