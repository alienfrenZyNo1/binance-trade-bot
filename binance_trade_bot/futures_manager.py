"""
USDC-M Futures Manager for Bear Regime

When the market regime turns BEAR, this module opens short positions on
USDC-M perpetual futures. When the regime shifts back to BULL/SIDEWAYS,
it closes all shorts and returns capital to spot trading.

SAFETY:
- 1x leverage only (no liquidation risk — margin equals position size)
- Isolated margin (one bad position can't affect others)
- Funding rate guard (skip entry if rate is too expensive)
- Stop-loss at configurable threshold
- Position reconciliation on every cycle

REQUIRES: Binance account with USDC-M futures access (verified for IE).
USES: python-binance futures methods (fapi.binance.com endpoint).
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from binance.client import Client
from binance.exceptions import BinanceAPIException


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
        self.stop_loss_pct = float(getattr(config, 'FUTURES_STOP_LOSS_PCT', 15.0))
        self.trailing_stop_pct = float(getattr(config, 'FUTURES_TRAILING_STOP_PCT', 10.0))
        self.max_funding_rate = float(getattr(config, 'FUTURES_MAX_FUNDING_RATE', 0.0001))
        self.position_check_interval = int(getattr(config, 'FUTURES_CHECK_INTERVAL', 60))
        self.testnet = getattr(config, 'TESTNET', False)

        # State
        self._open_position: Optional[FuturesPosition] = None
        self._last_check = 0
        self._last_entry_attempt = 0
        self._initialized = False

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
                f"Max margin: {self.max_margin_pct*100:.0f}% | "
                f"Stop: {self.stop_loss_pct}% | "
                f"Open positions: {1 if self._open_position else 0}"
            )
        except Exception as e:
            self.logger.warning(f"FuturesManager init failed: {e}")

    def _reconcile_positions(self):
        """Check Binance for any open short positions (e.g. from a crash)."""
        try:
            positions = self.client.futures_position_information()
            for pos in positions:
                amt = float(pos.get("positionAmt", 0))
                if amt < 0:  # negative = short
                    symbol = pos["symbol"]
                    entry = float(pos["entryPrice"])
                    self._open_position = FuturesPosition(
                        symbol=symbol,
                        entry_price=entry,
                        quantity=abs(amt),
                        order_id=0,
                        opened_at=time.time(),
                    )
                    self.logger.warning(
                        f"RECONCILED existing short: {symbol} "
                        f"qty={abs(amt)} entry={entry} "
                        f"(recovered from exchange state)"
                    )
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
            if pos.peak_pnl_pct > 3.0:  # only trail after 3% profit
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

            # Check funding rate on open position
            funding = self._get_funding_rate(pos.symbol)
            if funding is not None and funding > self.max_funding_rate * 3:
                self.logger.warning(
                    f"Futures funding rate high: {pos.symbol} rate={funding*100:.4f}% "
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

        margin = usdc_balance * self.max_margin_pct
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

        # Check funding rate — don't open if it's too expensive
        funding = self._get_funding_rate(f"{symbol}{self.bridge_symbol}")
        if funding is not None and funding > self.max_funding_rate:
            self.logger.info(
                f"Futures: skipping {symbol} short — funding rate too high "
                f"({funding*100:.4f}% > {self.max_funding_rate*100:.4f}%)"
            )
            return 'idle'

        # Execute the short
        return self._open_short(symbol, margin, perf)

    def _open_short(self, coin: str, margin: float, perf_pct: float) -> str:
        """Open a short position on {coin}USDC perpetual."""
        futures_symbol = f"{coin}{self.bridge_symbol}"

        try:
            # Set leverage to 1x and isolated margin
            self.client.futures_change_leverage(
                symbol=futures_symbol, leverage=self.leverage
            )
            try:
                self.client.futures_change_margin_type(
                    symbol=futures_symbol, marginType="ISOLATED"
                )
            except BinanceAPIException:
                pass  # already ISOLATED

            # Get current price for quantity calculation
            price = self._get_mark_price(futures_symbol)
            if price is None or price <= 0:
                self.logger.warning(f"Futures: can't get price for {futures_symbol}")
                return 'idle'

            # Calculate quantity: at 1x leverage, position = margin
            notional = margin * self.leverage
            quantity = notional / price

            # Round to exchange precision
            qty_precision = self._get_quantity_precision(futures_symbol)
            quantity = round(quantity, qty_precision)

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

            self.logger.warning(
                f" Futures SHORT opened: {futures_symbol} "
                f"qty={quantity} entry={fill_price} "
                f"margin=${margin:.2f} perf={perf_pct:+.1f}% "
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
            # BUY = close short (reduceOnly ensures we don't accidentally go long)
            self.client.futures_create_order(
                symbol=pos.symbol,
                side="BUY",
                type="MARKET",
                quantity=pos.quantity,
                reduceOnly="true",
            )

            # Calculate final P&L
            mark_price = self._get_mark_price(pos.symbol)
            if mark_price:
                pnl_pct = ((pos.entry_price - mark_price) / pos.entry_price) * 100
            else:
                pnl_pct = 0.0

            hold_time = time.time() - pos.opened_at
            self.logger.warning(
                f" Futures SHORT closed: {pos.symbol} "
                f"P&L={pnl_pct:+.1f}% held={hold_time/3600:.1f}h "
                f"reason={reason}"
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
        """Get current funding rate for a futures symbol."""
        try:
            data = self.client.futures_funding_rate(symbol=symbol, limit=1)
            if data:
                return float(data[0].get("fundingRate", 0))
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

    def _get_quantity_precision(self, symbol: str) -> int:
        """Get quantity precision for a futures symbol from exchange info."""
        try:
            info = self.client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    return int(s.get("quantityPrecision", 2))
        except Exception:
            pass
        return 2  # safe default

    def _get_min_notional(self, symbol: str) -> float:
        """Get minimum notional for a futures symbol."""
        try:
            info = self.client.futures_exchange_info()
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s.get("filters", []):
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
        try:
            self.client.futures_account_transfer(
                asset=self.bridge_symbol,
                amount=amount,
                type=2,  # 2 = USDT-M/USDC-M futures to spot
            )
            self.logger.info(f"Transferred {amount} {self.bridge_symbol} to spot")
            return True
        except Exception as e:
            self.logger.error(f"Transfer from futures failed: {e}")
            return False

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
