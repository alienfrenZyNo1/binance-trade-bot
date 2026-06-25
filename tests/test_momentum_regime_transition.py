"""Regression tests for MomentumStrategy regime-transition side effects."""

from types import SimpleNamespace

from binance_trade_bot.strategies.momentum_strategy import Strategy


class Asset:
    def __init__(self, symbol):
        self.symbol = symbol

    def __add__(self, other):
        return f"{self.symbol}{other.symbol}"

    def __str__(self):
        return self.symbol


class Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(("info", message))

    def warning(self, message, *args, **kwargs):
        self.messages.append(("warning", message))

    def error(self, message, *args, **kwargs):
        self.messages.append(("error", message))


class DB:
    def __init__(self, current_coin=None):
        self.current_coin = current_coin
        self.suppressed = []
        self.state = {}

    def get_current_coin(self):
        return self.current_coin

    def suppress_next_deposit_detection(self, reason):
        self.suppressed.append(reason)

    def set_bot_state(self, key, value):
        self.state[key] = value


class Manager:
    def __init__(self, balances=None, prices=None, sell_result=True):
        self.balances = balances or {}
        self.prices = prices or {}
        self.sell_result = sell_result
        self.sell_calls = []

    def get_currency_balance(self, symbol):
        return self.balances.get(symbol)

    def get_ticker_price(self, symbol):
        return self.prices.get(symbol)

    def sell_alt(self, coin, bridge):
        self.sell_calls.append((coin.symbol, bridge.symbol))
        return self.sell_result


class FuturesManager:
    def __init__(self, open_position=None, futures_balance=0.0, close_result="idle"):
        self._open_position = open_position
        self.futures_balance = futures_balance
        self.close_result = close_result
        self.transfer_to_futures_calls = []
        self.transfer_to_spot_calls = []
        self.manage_exit_calls = 0

    def transfer_to_futures(self, amount):
        self.transfer_to_futures_calls.append(amount)
        return True

    def manage_exit(self):
        self.manage_exit_calls += 1
        return self.close_result

    def _get_futures_usdc_balance(self):
        return self.futures_balance

    def transfer_to_spot(self, amount):
        self.transfer_to_spot_calls.append(amount)
        return True


def make_strategy(*, db, manager, futures_manager, awaiting_reentry=False):
    strategy = Strategy.__new__(Strategy)
    strategy.db = db
    strategy.manager = manager
    strategy.futures_manager = futures_manager
    strategy.config = SimpleNamespace(BRIDGE=Asset("USDC"))
    strategy.logger = Logger()
    strategy._awaiting_reentry = awaiting_reentry
    strategy._last_trade_time = 0
    strategy._recently_held = {}
    return strategy


def test_bear_entry_sell_failure_blocks_futures_transfer():
    coin = Asset("JUP")
    strategy = make_strategy(
        db=DB(current_coin=coin),
        manager=Manager(
            balances={"JUP": 10.0, "USDC": 50.0},
            prices={"JUPUSDC": 2.0},
            sell_result=None,
        ),
        futures_manager=FuturesManager(),
    )

    strategy._handle_regime_transition("sideways", "bear")

    assert strategy.manager.sell_calls == [("JUP", "USDC")]
    assert strategy.futures_manager.transfer_to_futures_calls == []
    assert any("failed to sell JUP" in message for level, message in strategy.logger.messages if level == "error")


def test_bear_exit_checks_exchange_even_without_local_open_position():
    strategy = make_strategy(
        db=DB(current_coin=Asset("JUP")),
        manager=Manager(),
        futures_manager=FuturesManager(open_position=None, futures_balance=12.34, close_result="idle"),
    )

    strategy._handle_regime_transition("bear", "sideways")

    assert strategy.futures_manager.manage_exit_calls == 1
    assert strategy.futures_manager.transfer_to_spot_calls == [12.34]
    assert strategy.db.suppressed == ["internal futures→spot transfer of 12.34 USDC"]


def test_bear_entry_clears_awaiting_reentry_after_transfer_path():
    strategy = make_strategy(
        db=DB(current_coin=None),
        manager=Manager(balances={"USDC": 20.0}),
        futures_manager=FuturesManager(),
        awaiting_reentry=True,
    )

    strategy._handle_regime_transition("sideways", "bear")

    assert strategy.futures_manager.transfer_to_futures_calls == [20.0]
    assert strategy._awaiting_reentry is False
    assert strategy.db.state["awaiting_reentry"] == "False"
