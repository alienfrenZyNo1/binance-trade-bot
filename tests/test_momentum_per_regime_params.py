"""Tests for live momentum strategy per-regime parameters."""

import time
from types import SimpleNamespace

from binance_trade_bot.strategies.momentum_strategy import (
    BEAR,
    BULL,
    SIDEWAYS,
    STORMY,
    Strategy,
)


class Bridge:
    symbol = "USDC"


class KlineClient:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def get_klines(self, symbol, interval, limit):
        self.calls.append({"symbol": symbol, "interval": interval, "limit": limit})
        return self.responses.get(symbol) or [
            [0, "10", "10", "10", "10"],
            [0, "12", "12", "12", "12"],
        ]


class Manager:
    def __init__(self):
        self.binance_client = KlineClient()
        self.prices = {}
        self.balances = {}
        self.min_notional = 5.0
        self.buys = []
        self.sells = []

    def get_ticker_price(self, symbol):
        return self.prices.get(symbol, 1.0)

    def get_currency_balance(self, symbol):
        return self.balances.get(symbol, 0.0)

    def get_min_notional(self, coin, bridge):
        return self.min_notional

    def buy_alt(self, coin, bridge):
        self.buys.append((coin.symbol, bridge.symbol))
        return SimpleNamespace(price=self.prices.get(f"{coin.symbol}{bridge.symbol}", 1.0))

    def sell_alt(self, coin, bridge):
        self.sells.append((coin.symbol, bridge.symbol))
        return SimpleNamespace(price=self.prices.get(f"{coin.symbol}{bridge.symbol}", 1.0))


class Coin:
    def __init__(self, symbol):
        self.symbol = symbol

    def __add__(self, other):
        return f"{self.symbol}{other.symbol}"

    def __str__(self):
        return self.symbol


class DB:
    def __init__(self, coins=None, current=None):
        self.coins = coins or []
        self.current = current or (self.coins[0] if self.coins else None)
        self.state = {}

    def get_coins(self):
        return self.coins

    def get_current_coin(self):
        return self.current

    def set_current_coin(self, coin):
        self.current = coin

    def get_bot_state(self, key, default=None):
        return self.state.get(key, default)

    def set_bot_state(self, key, value):
        self.state[key] = value

class Logger:

    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(("info", message))

    def warning(self, message, *args, **kwargs):
        self.messages.append(("warning", message))

    def debug(self, message, *args, **kwargs):
        self.messages.append(("debug", message))

    def error(self, message, *args, **kwargs):
        self.messages.append(("error", message))


class FuturesManager:
    def __init__(self):
        self.actions = []

    def manage_bear(self, performance, regime):
        self.actions.append((performance, regime))
        return "idle"


def make_strategy(*, regime=SIDEWAYS, per_regime=True, coins=None):
    strategy = Strategy.__new__(Strategy)
    strategy.config = SimpleNamespace(
        BRIDGE=Bridge(),
        PER_REGIME_PARAMS_ENABLED=per_regime,
        MOMENTUM_LOOKBACK_HOURS=18,
        MOMENTUM_MIN_EDGE=8.0,
        BULL_MOMENTUM_LOOKBACK_HOURS=36,
        BULL_MOMENTUM_MIN_EDGE=8.0,
        SIDEWAYS_MOMENTUM_LOOKBACK_HOURS=18,
        SIDEWAYS_MOMENTUM_MIN_EDGE=8.0,
        BEAR_MOMENTUM_LOOKBACK_HOURS=6,
        BEAR_MOMENTUM_MIN_EDGE=5.0,
        STORMY_MOMENTUM_LOOKBACK_HOURS=6,
        STORMY_MOMENTUM_MIN_EDGE=10.0,
        MOMENTUM_MIN_TARGET_PERF=-100.0,
        MOMENTUM_FILTER_ENABLED=False,
        RSI_FILTER_ENABLED=False,
        TRAILING_STOP_ENABLED=False,
        TRADE_COOLDOWN_SECONDS=0,
        CONFIRMATION_CYCLES=2,
        CONFIRMATION_TIME_ENABLED=True,
        CONFIRMATION_MIN_SECONDS=180,
        BULL_CONFIRMATION_MIN_SECONDS=300,
        SIDEWAYS_CONFIRMATION_MIN_SECONDS=180,
        BEAR_CONFIRMATION_MIN_SECONDS=60,
        STORMY_CONFIRMATION_MIN_SECONDS=300,
        PORTFOLIO_CIRCUIT_BREAKER_ENABLED=False,
        PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT=5.0,
        PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT=12.0,
        PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS=24.0,
    )
    strategy._market_regime = regime
    strategy._perf_cache = {}
    strategy._perf_cache_time = 0
    strategy._perf_cache_key = None
    strategy._cache_ttl = 300
    strategy._last_trade_time = 0
    strategy._regime_adx = 0.0
    strategy._position_peak_price = {}
    strategy._awaiting_reentry = False
    strategy._recently_held = {}
    strategy._pending_rotation = None
    strategy._confirmation_cycles = 2
    strategy.manager = Manager()
    strategy.logger = Logger()
    coins = coins or [Coin("AAA"), Coin("BBB")]
    strategy.db = DB(coins=coins, current=coins[0])
    strategy.futures_manager = FuturesManager()
    strategy._update_market_regime = lambda: None
    strategy._persist_trade_state = lambda: None
    strategy.update_trade_threshold = lambda coin, price: None
    return strategy


def set_kline_perf(strategy, symbol, start, end):
    strategy.manager.binance_client.responses[f"{symbol}USDC"] = [
        [0, str(start), str(start), str(start), str(start)],
        [0, str(end), str(end), str(end), str(end)],
    ]


def test_regime_specific_lookback_and_min_edge_getters():
    strategy = make_strategy(regime=BULL)
    assert strategy._get_regime_momentum_lookback() == 36
    assert strategy._get_regime_momentum_min_edge() == 8.0

    strategy._market_regime = BEAR
    assert strategy._get_regime_momentum_lookback() == 6
    assert strategy._get_regime_momentum_min_edge() == 5.0

    strategy._market_regime = SIDEWAYS
    assert strategy._get_regime_momentum_lookback() == 18
    assert strategy._get_regime_momentum_min_edge() == 8.0

    strategy._market_regime = STORMY
    assert strategy._get_regime_momentum_lookback() == 6
    assert strategy._get_regime_momentum_min_edge() == 10.0


def test_disabled_per_regime_params_fall_back_to_global_values():
    strategy = make_strategy(regime=BULL, per_regime=False)
    assert strategy._get_regime_momentum_lookback() == 18
    assert strategy._get_regime_momentum_min_edge() == 8.0


def test_coin_performance_uses_active_regime_lookback():
    strategy = make_strategy(regime=BULL)
    strategy._get_coin_performance("AAA")
    assert strategy.manager.binance_client.calls[-1]["limit"] == 37

    strategy._market_regime = BEAR
    strategy._get_coin_performance("AAA")
    assert strategy.manager.binance_client.calls[-1]["limit"] == 7


def test_performance_cache_is_separated_by_regime_and_lookback():
    strategy = make_strategy(regime=BULL)
    strategy._get_all_performance()
    strategy._market_regime = BEAR
    strategy._get_all_performance()

    limits = [call["limit"] for call in strategy.manager.binance_client.calls if call["symbol"] == "AAAUSDC"]
    assert limits == [37, 7]


def test_scout_uses_regime_specific_min_edge_for_rotation():
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = make_strategy(regime=STORMY, coins=[current, target])
    set_kline_perf(strategy, "AAA", 100, 100)  # 0%
    set_kline_perf(strategy, "BBB", 100, 109)  # +9%, above BULL edge but below STORMY edge

    strategy.scout()
    assert strategy._pending_rotation is None

    strategy._market_regime = BULL
    strategy._perf_cache = {}
    strategy._perf_cache_key = None
    strategy.scout()
    assert strategy._pending_rotation[:2] == ("AAA", "BBB")


def test_rotation_confirmation_requires_minimum_elapsed_time(monkeypatch):
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = make_strategy(regime=SIDEWAYS, coins=[current, target])
    set_kline_perf(strategy, "AAA", 100, 100)
    set_kline_perf(strategy, "BBB", 100, 120)

    strategy.manager.balances["AAA"] = 10.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    now = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    strategy.scout()
    assert strategy.manager.buys == []

    now[0] += 10
    strategy.scout()
    assert strategy.manager.buys == []

    now[0] += 181
    strategy.scout()
    assert strategy.manager.buys == [("BBB", "USDC")]


def test_confirmation_time_gate_disabled_preserves_cycle_only_behavior(monkeypatch):
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = make_strategy(regime=SIDEWAYS, coins=[current, target])
    strategy.config.CONFIRMATION_TIME_ENABLED = False
    set_kline_perf(strategy, "AAA", 100, 100)
    set_kline_perf(strategy, "BBB", 100, 120)
    strategy.manager.balances["AAA"] = 10.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    now = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    strategy.scout()
    now[0] += 1
    strategy.scout()

    assert strategy.manager.buys == [("BBB", "USDC")]


def test_confirmation_min_seconds_getter_is_zero_when_disabled():
    strategy = make_strategy(regime=BULL)
    strategy.config.CONFIRMATION_TIME_ENABLED = False
    assert strategy._get_confirmation_min_seconds() == 0


def test_confirmation_min_seconds_getter_uses_regime_values_when_enabled():
    strategy = make_strategy(regime=BULL)
    assert strategy._get_confirmation_min_seconds() == 300
    strategy._market_regime = BEAR
    assert strategy._get_confirmation_min_seconds() == 60
    strategy._market_regime = STORMY
    assert strategy._get_confirmation_min_seconds() == 300
    strategy._market_regime = SIDEWAYS
    assert strategy._get_confirmation_min_seconds() == 180


def test_portfolio_circuit_breaker_blocks_confirmed_spot_rotation(monkeypatch):
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = make_strategy(regime=SIDEWAYS, coins=[current, target])
    strategy.config.CONFIRMATION_TIME_ENABLED = False
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    set_kline_perf(strategy, "AAA", 100, 100)
    set_kline_perf(strategy, "BBB", 100, 120)
    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"

    now = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])

    strategy.scout()
    now[0] += 1
    strategy.scout()

    assert strategy.manager.sells == []
    assert strategy.manager.buys == []
    assert any("Circuit breaker" in message for level, message in strategy.logger.messages if level == "warning")


def test_portfolio_circuit_breaker_cooldown_blocks_even_after_recovery(monkeypatch):
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = make_strategy(regime=SIDEWAYS, coins=[current, target])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_COOLDOWN_HOURS = 24
    strategy.db.state["portfolio_circuit_breaker_last_triggered"] = str(1_000.0)
    strategy.manager.balances["AAA"] = 100.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"

    monkeypatch.setattr(time, "time", lambda: 1_000.0 + 2 * 3600)

    assert strategy._new_spot_risk_blocked() is True
    assert any("cooldown" in message.lower() for level, message in strategy.logger.messages if level == "warning")


def test_portfolio_circuit_breaker_resets_daily_baseline_on_utc_day_change(monkeypatch):
    strategy = make_strategy(regime=SIDEWAYS, coins=[Coin("AAA"), Coin("BBB")])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "100.0"
    strategy.db.state["portfolio_daily_period"] = "2026-06-25"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_period"] = "2026-W26"

    # 2026-06-26T00:05:00Z: daily baseline should reset to 94 and not block.
    monkeypatch.setattr(time, "time", lambda: 1782432300.0)

    assert strategy._new_spot_risk_blocked() is False
    assert strategy.db.state["portfolio_daily_start_equity"] == "94.0"
    assert strategy.db.state["portfolio_daily_period"] == "2026-06-26"


def test_portfolio_circuit_breaker_resets_weekly_baseline_on_utc_week_change(monkeypatch):
    strategy = make_strategy(regime=SIDEWAYS, coins=[Coin("AAA"), Coin("BBB")])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    strategy.manager.balances["AAA"] = 90.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "90.0"
    strategy.db.state["portfolio_daily_period"] = "2026-06-29"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_period"] = "2026-W26"

    # 2026-06-29T00:05:00Z is ISO week 27: weekly baseline should reset to 90.
    monkeypatch.setattr(time, "time", lambda: 1782691500.0)

    assert strategy._new_spot_risk_blocked() is False
    assert strategy.db.state["portfolio_weekly_start_equity"] == "90.0"
    assert strategy.db.state["portfolio_weekly_period"] == "2026-W27"


def test_portfolio_circuit_breaker_legacy_baseline_gets_period_without_reset(monkeypatch):
    strategy = make_strategy(regime=SIDEWAYS, coins=[Coin("AAA"), Coin("BBB")])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"

    monkeypatch.setattr(time, "time", lambda: 1782432300.0)

    assert strategy._new_spot_risk_blocked() is True
    assert strategy.db.state["portfolio_daily_start_equity"] == "100.0"
    assert strategy.db.state["portfolio_weekly_start_equity"] == "100.0"
    assert strategy.db.state["portfolio_daily_period"] == "2026-06-26"
    assert strategy.db.state["portfolio_weekly_period"] == "2026-W26"


def test_portfolio_circuit_breaker_monday_resets_daily_and_weekly(monkeypatch):
    strategy = make_strategy(regime=SIDEWAYS, coins=[Coin("AAA"), Coin("BBB")])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = True
    strategy.manager.balances["AAA"] = 90.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.db.state["portfolio_daily_start_equity"] = "100.0"
    strategy.db.state["portfolio_daily_period"] = "2026-06-28"
    strategy.db.state["portfolio_weekly_start_equity"] = "100.0"
    strategy.db.state["portfolio_weekly_period"] = "2026-W26"

    monkeypatch.setattr(time, "time", lambda: 1782691500.0)

    assert strategy._new_spot_risk_blocked() is False
    assert strategy.db.state["portfolio_daily_start_equity"] == "90.0"
    assert strategy.db.state["portfolio_daily_period"] == "2026-06-29"
    assert strategy.db.state["portfolio_weekly_start_equity"] == "90.0"
    assert strategy.db.state["portfolio_weekly_period"] == "2026-W27"
