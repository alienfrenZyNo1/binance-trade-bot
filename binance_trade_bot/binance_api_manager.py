import math
import time
import traceback
from typing import Dict, Optional

from binance.client import Client
from binance.exceptions import BinanceAPIException
from cachetools import TTLCache, cached

from .binance_stream_manager import BinanceCache, BinanceOrder, BinanceStreamManager, OrderGuard
from .canary_capital_guard import cap_spot_trade_balance
from .config import Config
from .database import Database
from .logger import Logger
from .models import Coin


class BinanceAPIManager:
    def __init__(self, config: Config, db: Database, logger: Logger, testnet = False):
        # initializing the client class calls `ping` API endpoint, verifying the connection
        self.binance_client = Client(
            config.BINANCE_API_KEY,
            config.BINANCE_API_SECRET_KEY,
            tld=config.BINANCE_TLD,
            testnet=testnet,
        )
        self.db = db
        self.logger = logger
        self.config = config
        self.testnet = testnet

        self.cache = BinanceCache()
        self.stream_manager: Optional[BinanceStreamManager] = None
        self.setup_websockets()

    def setup_websockets(self):
        self.stream_manager = BinanceStreamManager(
            self.cache,
            self.config,
            self.binance_client,
            self.logger,
        )

    @cached(cache=TTLCache(maxsize=1, ttl=43200))
    def get_trade_fees(self) -> Dict[str, float]:
        if not self.testnet:
            return {ticker["symbol"]: float(ticker["takerCommission"]) for ticker in self.binance_client.get_trade_fee()}


        ## testnet does not provide trade fee API, emulating it
        exchange_info = self.binance_client.get_exchange_info()
        symbols = exchange_info["symbols"]
        return {
            symbol["symbol"]: 0.001
            for symbol in symbols
        }


    @cached(cache=TTLCache(maxsize=1, ttl=60))
    def get_using_bnb_for_fees(self):
        return self.binance_client.get_bnb_burn_spot_margin()["spotBNBBurn"]

    def get_fee(self, origin_coin: Coin, target_coin: Coin, selling: bool):
        base_fee = self.get_trade_fees()[origin_coin + target_coin]
        if not self.testnet:
            if not self.get_using_bnb_for_fees():
                return base_fee

        # The discount is only applied if we have enough BNB to cover the fee
        amount_trading = (
            self._sell_quantity(origin_coin.symbol, target_coin.symbol)
            if selling
            else self._buy_quantity(origin_coin.symbol, target_coin.symbol)
        )

        fee_amount = amount_trading * base_fee * 0.75
        if origin_coin.symbol == "BNB":
            fee_amount_bnb = fee_amount
        else:
            origin_price = self.get_ticker_price(origin_coin + Coin("BNB"))
            if origin_price is None:
                return base_fee
            fee_amount_bnb = fee_amount * origin_price

        bnb_balance = self.get_currency_balance("BNB")

        if bnb_balance >= fee_amount_bnb:
            return base_fee * 0.75
        return base_fee

    def get_account(self):
        """
        Get account information
        """
        return self.binance_client.get_account()

    def get_ticker_price(self, ticker_symbol: str):
        """
        Get ticker price of a specific coin
        """
        price = self.cache.ticker_values.get(ticker_symbol, None)
        if price is None and ticker_symbol not in self.cache.non_existent_tickers:
            self.cache.ticker_values = {
                ticker["symbol"]: float(ticker["price"]) for ticker in self.binance_client.get_symbol_ticker()
            }
            self.logger.debug(f"Fetched all ticker prices: {self.cache.ticker_values}")
            price = self.cache.ticker_values.get(ticker_symbol, None)
            if price is None:
                self.logger.info(f"Ticker does not exist: {ticker_symbol} - will not be fetched from now on")
                self.cache.non_existent_tickers.add(ticker_symbol)

        return price

    def get_currency_balance(self, currency_symbol: str, force=False) -> float:
        """
        Get balance of a specific coin
        """
        with self.cache.open_balances() as cache_balances:
            balance = cache_balances.get(currency_symbol, None)
            if force or balance is None:
                cache_balances.clear()
                cache_balances.update(
                    {
                        currency_balance["asset"]: float(currency_balance["free"])
                        for currency_balance in self.binance_client.get_account()["balances"]
                    }
                )
                self.logger.debug(f"Fetched all balances: {cache_balances}")
                if currency_symbol not in cache_balances:
                    cache_balances[currency_symbol] = 0.0
                    return 0.0
                return cache_balances.get(currency_symbol, 0.0)

            return balance

    def retry(self, func, *args, **kwargs):
        for attempt in range(20):
            try:
                return func(*args, **kwargs)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(
                    f"Failed to Buy/Sell. Trying Again (attempt {attempt + 1}/20): {e}"
                )
                if attempt == 0:
                    self.logger.warning(traceback.format_exc())
                time.sleep(1)
        self.logger.error(f"Retry exhausted after 20 attempts for {func.__name__}")
        return None

    def get_symbol_filter(self, origin_symbol: str, target_symbol: str, filter_type: str):
        return next(
            _filter
            for _filter in self.binance_client.get_symbol_info(origin_symbol + target_symbol)["filters"]
            if _filter["filterType"] == filter_type
        )

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_alt_tick(self, origin_symbol: str, target_symbol: str):
        step_size = self.get_symbol_filter(origin_symbol, target_symbol, "LOT_SIZE")["stepSize"]
        if step_size.find("1") == 0:
            return 1 - step_size.find(".")
        return step_size.find("1") - 1

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_min_notional(self, origin_symbol: str, target_symbol: str):
        return float(self.get_symbol_filter(origin_symbol, target_symbol, "NOTIONAL")["minNotional"])

    def _get_order_book_price(self, symbol: str, side: str):
        """Get the best maker price from the order book.
        
        For BUY: returns best bid (passive buy = maker fill)
        For SELL: returns best ask (passive sell = maker fill)
        
        If the USDC spread is very wide (> threshold), uses midpoint pricing
        to fill faster while still getting partial maker benefits.
        Falls back to ticker price if order book unavailable.
        """
        try:
            depth = self.binance_client.get_orderbook_ticker(symbol=symbol)
            bid = float(depth.get("bidPrice", 0))
            ask = float(depth.get("askPrice", 0))

            if bid > 0 and ask > 0:
                spread_pct = ((ask - bid) / bid) * 100
                spread_threshold = float(getattr(self.config, 'WIDE_SPREAD_THRESHOLD', 0.15))

                if spread_pct > spread_threshold * 2:
                    # Very wide spread — use midpoint for faster fill
                    # (still better than taker, but more aggressive than best bid/ask)
                    mid = (bid + ask) / 2
                    self.logger.info(
                        f"Wide spread on {symbol}: {spread_pct:.3f}% — using midpoint {mid}"
                    )
                    return mid

                if side == "BUY":
                    return bid
                else:
                    return ask
        except Exception:
            pass
        return self.get_ticker_price(symbol)

    def _get_spread(self, symbol: str):
        """Get the bid-ask spread for a symbol. Returns (spread_pct, bid, ask).
        spread_pct = (ask - bid) / bid * 100"""
        try:
            depth = self.binance_client.get_orderbook_ticker(symbol=symbol)
            bid = float(depth.get("bidPrice", 0))
            ask = float(depth.get("askPrice", 0))
            if bid > 0 and ask > 0:
                return ((ask - bid) / bid) * 100, bid, ask
        except Exception:
            pass
        return 0.0, 0, 0

    def _get_tick_size(self, symbol: str):
        """Get the price tick size for a symbol."""
        try:
            price_filter = self.get_symbol_filter(
                symbol.replace(self.config.BRIDGE_SYMBOL, ""),
                self.config.BRIDGE_SYMBOL,
                "PRICE_FILTER"
            )
            return float(price_filter.get("tickSize", 0.0001))
        except Exception:
            return 0.0001

    def _check_order_filled(self, order_id, symbol):
        """Quick check if an order has been filled. Returns True/False."""
        try:
            order = self.binance_client.get_order(symbol=symbol, orderId=order_id)
            return order.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
        except Exception:
            return True  # Assume filled if we can't check (avoid stuck orders)

    def _should_reprice_order(self, order_status):
        """Check if a maker order should be repriced to taker (aggressive).
        Returns True if the order has been NEW for longer than MAKER_REPRICE_TIMEOUT."""
        reprice_timeout = float(getattr(self.config, 'MAKER_REPRICE_TIMEOUT', 5))
        if reprice_timeout <= 0:
            return False
        if order_status is None or order_status.status != "NEW":
            return False
        minutes = (time.time() - order_status.time / 1000) / 60
        return minutes > reprice_timeout

    def _wait_for_order(
        self, order_id, origin_symbol: str, target_symbol: str,
        reprice_callback=None
    ) -> Optional[BinanceOrder]:  # pylint: disable=unsubscriptable-object
        order_repriced = False

        while True:
            order_status: BinanceOrder = self.cache.orders.get(order_id, None)
            if order_status is not None:
                break
            self.logger.debug(f"Waiting for order {order_id} to be created")
            time.sleep(1)

        self.logger.debug(f"Order created: {order_status}")

        while order_status.status != "FILLED":
            try:
                order_status = self.cache.orders.get(order_id, None)

                self.logger.debug(f"Waiting for order {order_id} to be filled")

                # Maker reprice: if maker order hasn't filled, reprice to taker
                if not order_repriced and reprice_callback and self._should_reprice_order(order_status):
                    self.logger.info(
                        f"Maker order {order_id} not filled in "
                        f"{getattr(self.config, 'MAKER_REPRICE_TIMEOUT', 5)} min — repricing to taker"
                    )
                    # Cancel the maker order
                    try:
                        self.binance_client.cancel_order(
                            symbol=origin_symbol + target_symbol, orderId=order_id
                        )
                    except Exception:
                        pass

                    # Place new aggressive order via callback
                    new_order = reprice_callback()
                    if new_order is None:
                        self.logger.warning("Reprice failed — returning to scouting")
                        return None

                    order_repriced = True
                    new_order_id = new_order["orderId"]
                    self.logger.info(f"Repriced order placed: {new_order_id} (taker)")

                    # Poll REST API for the repriced order (websocket may not track new ID)
                    from .binance_stream_manager import BinanceOrder as _BO
                    symbol_str = origin_symbol + target_symbol
                    while True:
                        try:
                            order_info = self.binance_client.get_order(symbol=symbol_str, orderId=new_order_id)
                            status = order_info.get("status", "")
                            if status == "FILLED":
                                self.logger.info(f"Repriced order filled: {new_order_id}")
                                report = {
                                    "symbol": order_info["symbol"],
                                    "side": order_info["side"],
                                    "order_type": order_info.get("type", "LIMIT"),
                                    "order_id": order_info["orderId"],
                                    "cumulative_quote_asset_transacted_quantity": order_info.get("cummulativeQuoteQty", 0),
                                    "current_order_status": order_info["status"],
                                    "order_price": order_info.get("price", 0),
                                    "transaction_time": order_info.get("time", int(time.time() * 1000)),
                                }
                                return _BO(report)
                            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                                self.logger.warning(f"Repriced order {status}: {new_order_id}")
                                return None
                            time.sleep(1)
                        except Exception as e:
                            self.logger.debug(f"Polling repriced order: {e}")
                            time.sleep(2)

                if self._should_cancel_order(order_status):
                    cancel_order = None
                    while cancel_order is None:
                        cancel_order = self.binance_client.cancel_order(
                            symbol=origin_symbol + target_symbol, orderId=order_id
                        )
                    self.logger.info("Order timeout, canceled...")

                    # sell partially
                    if order_status.status == "PARTIALLY_FILLED" and order_status.side == "BUY":
                        self.logger.info("Sell partially filled amount")

                        order_quantity = self._sell_quantity(origin_symbol, target_symbol)
                        partially_order = None
                        while partially_order is None:
                            partially_order = self.binance_client.order_market_sell(
                                symbol=origin_symbol + target_symbol,
                                quantity=order_quantity,
                            )

                    self.logger.info("Going back to scouting mode...")
                    return None

                if order_status.status == "CANCELED":
                    self.logger.info("Order is canceled, going back to scouting mode...")
                    return None

                time.sleep(1)
            except BinanceAPIException as e:
                self.logger.info(e)
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.info(f"Unexpected Error: {e}")
                time.sleep(1)

        self.logger.debug(f"Order filled: {order_status}")
        return order_status

    def wait_for_order(
        self, order_id, origin_symbol: str, target_symbol: str, order_guard: OrderGuard,
        reprice_callback=None
    ) -> Optional[BinanceOrder]:  # pylint: disable=unsubscriptable-object
        with order_guard:
            return self._wait_for_order(order_id, origin_symbol, target_symbol, reprice_callback)

    def _should_cancel_order(self, order_status):
        minutes = (time.time() - order_status.time / 1000) / 60
        timeout = 0

        if order_status.side == "SELL":
            timeout = float(self.config.SELL_TIMEOUT)
        else:
            timeout = float(self.config.BUY_TIMEOUT)

        if timeout and minutes > timeout and order_status.status == "NEW":
            return True

        if timeout and minutes > timeout and order_status.status == "PARTIALLY_FILLED":
            if order_status.side == "SELL":
                return True

            if order_status.side == "BUY":
                current_price = self.get_ticker_price(order_status.symbol)
                if float(current_price) * (1 - 0.001) > float(order_status.price):
                    return True

        return False

    def buy_alt(self, origin_coin: Coin, target_coin: Coin, max_target_balance: Optional[float] = None) -> BinanceOrder:
        return self.retry(self._buy_alt, origin_coin, target_coin, max_target_balance)

    def _buy_quantity(
        self,
        origin_symbol: str,
        target_symbol: str,
        target_balance: float = None,
        from_coin_price: float = None,
    ):
        target_balance = target_balance or self.get_currency_balance(target_symbol)
        from_coin_price = from_coin_price or self.get_ticker_price(origin_symbol + target_symbol)

        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(target_balance * 10**origin_tick / from_coin_price) / float(10**origin_tick)

    def _buy_alt(self, origin_coin: Coin, target_coin: Coin, max_target_balance: Optional[float] = None):  # pylint: disable=too-many-locals
        """
        Buy altcoin
        """
        trade_log = self.db.start_trade_log(origin_coin, target_coin, False)
        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        with self.cache.open_balances() as balances:
            balances.clear()

        origin_balance = self.get_currency_balance(origin_symbol)
        target_balance = self.get_currency_balance(target_symbol)
        
        # Dynamic position sizing: cap the amount deployed
        if max_target_balance is not None:
            target_balance = min(target_balance, max_target_balance)
        canary_cap = cap_spot_trade_balance(target_balance, self.config)
        if canary_cap.capped:
            self.logger.warning(
                f"{canary_cap.reason}: limiting spot buy from ${canary_cap.original_balance:.2f} "
                f"to ${canary_cap.allowed_balance:.2f}"
            )
        target_balance = canary_cap.allowed_balance
        pair_info = self.binance_client.get_symbol_info(origin_symbol + target_symbol)
        
        # Maker order support: use best bid for passive fill (0.025% fee vs 0.075%)
        use_maker = getattr(self.config, 'USE_MAKER_ORDERS', False)
        if use_maker:
            from_coin_price = self._get_order_book_price(origin_symbol + target_symbol, "BUY")
            self.logger.info(f"Using MAKER price for buy: {from_coin_price}")
        else:
            from_coin_price = self.get_ticker_price(origin_symbol + target_symbol)
        from_coin_price_s = "{:0.0{}f}".format(from_coin_price, pair_info["quotePrecision"])

        order_quantity = self._buy_quantity(origin_symbol, target_symbol, target_balance, from_coin_price)
        order_quantity_s = "{:0.0{}f}".format(order_quantity, pair_info["baseAssetPrecision"])

        self.logger.info(f"BUY QTY {order_quantity}")

        if order_quantity <= 0:
            self.logger.error(
                f"Buy quantity is 0 for {origin_symbol}/{target_symbol} "
                f"(balance: {target_balance}, price: {from_coin_price}). "
                f"Not enough funds to trade."
            )
            return None

        # Try to buy until successful
        order = None
        order_guard = self.stream_manager.acquire_order_guard()
        max_attempts = 5
        attempts = 0
        while order is None:
            attempts += 1
            try:
                order = self.binance_client.order_limit_buy(
                    symbol=origin_symbol + target_symbol,
                    quantity=order_quantity_s,
                    price=from_coin_price_s,
                )
                self.logger.info(order)
            except BinanceAPIException as e:
                self.logger.info(e)
                if attempts >= max_attempts:
                    self.logger.error(
                        f"Buy failed after {max_attempts} attempts. Giving up to avoid API spam."
                    )
                    return None
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(f"Unexpected Error: {e}")
                if attempts >= max_attempts:
                    return None

        trade_log.set_ordered(origin_balance, target_balance, order_quantity)

        # Maker reprice callback: if maker order doesn't fill, reprice to taker
        reprice_fn = None
        if use_maker:
            _self = self
            _sym = origin_symbol + target_symbol
            _qty = order_quantity_s
            _precision = pair_info["quotePrecision"]
            def _reprice_buy():
                taker_price = _self.get_ticker_price(_sym)
                taker_price_s = "{:0.0{}f}".format(taker_price, _precision)
                _self.logger.info(f"Repricing BUY to taker price: {taker_price}")
                return _self.binance_client.order_limit_buy(symbol=_sym, quantity=_qty, price=taker_price_s)
            reprice_fn = _reprice_buy

        order_guard.set_order(origin_symbol, target_symbol, int(order["orderId"]))
        order = self.wait_for_order(order["orderId"], origin_symbol, target_symbol, order_guard, reprice_fn)

        if order is None:
            return None

        self.logger.info(f"Bought {origin_symbol}")

        trade_log.set_complete(order.cumulative_quote_qty)

        return order

    def sell_alt(self, origin_coin: Coin, target_coin: Coin) -> BinanceOrder:
        return self.retry(self._sell_alt, origin_coin, target_coin)

    def _sell_quantity(self, origin_symbol: str, target_symbol: str, origin_balance: float = None):
        origin_balance = origin_balance or self.get_currency_balance(origin_symbol)

        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(origin_balance * 10**origin_tick) / float(10**origin_tick)

    def _sell_alt(self, origin_coin: Coin, target_coin: Coin):  # pylint: disable=too-many-locals
        """
        Sell altcoin
        """
        trade_log = self.db.start_trade_log(origin_coin, target_coin, True)
        origin_symbol = origin_coin.symbol
        target_symbol = target_coin.symbol

        with self.cache.open_balances() as balances:
            balances.clear()

        origin_balance = self.get_currency_balance(origin_symbol)
        target_balance = self.get_currency_balance(target_symbol)

        pair_info = self.binance_client.get_symbol_info(origin_symbol + target_symbol)
        
        # Maker order support: use best ask for passive fill (0.025% fee vs 0.075%)
        use_maker = getattr(self.config, 'USE_MAKER_ORDERS', False)
        if use_maker:
            from_coin_price = self._get_order_book_price(origin_symbol + target_symbol, "SELL")
            self.logger.info(f"Using MAKER price for sell: {from_coin_price}")
        else:
            from_coin_price = self.get_ticker_price(origin_symbol + target_symbol)
        from_coin_price_s = "{:0.0{}f}".format(from_coin_price, pair_info["quotePrecision"])

        order_quantity = self._sell_quantity(origin_symbol, target_symbol, origin_balance)
        order_quantity_s = "{:0.0{}f}".format(order_quantity, pair_info["baseAssetPrecision"])
        self.logger.info(f"Selling {order_quantity} of {origin_symbol}")

        if order_quantity <= 0:
            self.logger.error(
                f"Sell quantity is 0 for {origin_symbol}/{target_symbol} "
                f"(balance: {origin_balance}). Not enough to sell."
            )
            return None

        self.logger.info(f"Balance is {origin_balance}")
        order = None
        order_guard = self.stream_manager.acquire_order_guard()
        max_attempts = 5
        attempts = 0
        while order is None:
            attempts += 1
            try:
                # Should sell at calculated price to avoid lost coin
                order = self.binance_client.order_limit_sell(
                    symbol=origin_symbol + target_symbol,
                    quantity=(order_quantity_s),
                    price=from_coin_price_s,
                )
            except BinanceAPIException as e:
                self.logger.info(e)
                if attempts >= max_attempts:
                    self.logger.error(
                        f"Sell failed after {max_attempts} attempts. Giving up to avoid API spam."
                    )
                    return None
                time.sleep(1)
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(f"Unexpected Error: {e}")
                if attempts >= max_attempts:
                    return None

        self.logger.info("order")
        self.logger.info(order)

        trade_log.set_ordered(origin_balance, target_balance, order_quantity)

        # Maker reprice callback: if maker order doesn't fill, reprice to taker
        reprice_fn = None
        if use_maker:
            _self = self
            _sym = origin_symbol + target_symbol
            _qty = order_quantity_s
            _precision = pair_info["quotePrecision"]
            def _reprice_sell():
                taker_price = _self.get_ticker_price(_sym)
                taker_price_s = "{:0.0{}f}".format(taker_price, _precision)
                _self.logger.info(f"Repricing SELL to taker price: {taker_price}")
                return _self.binance_client.order_limit_sell(symbol=_sym, quantity=_qty, price=taker_price_s)
            reprice_fn = _reprice_sell

        order_guard.set_order(origin_symbol, target_symbol, int(order["orderId"]))
        order = self.wait_for_order(order["orderId"], origin_symbol, target_symbol, order_guard, reprice_fn)

        if order is None:
            return None

        new_balance = self.get_currency_balance(origin_symbol)
        while new_balance >= origin_balance:
            new_balance = self.get_currency_balance(origin_symbol, True)

        self.logger.info(f"Sold {origin_symbol}")

        trade_log.set_complete(order.cumulative_quote_qty)

        return order
