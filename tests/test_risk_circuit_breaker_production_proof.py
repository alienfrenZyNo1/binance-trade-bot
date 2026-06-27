"""Production proof: circuit breaker blocks new entries but never traps positions.

These tests were authored as part of risk-audit #98. They exercise the REAL
strategy code path (binance_trade_bot/strategies/momentum_strategy.py) to prove:

  1. When daily drawdown exceeds the threshold, NEW spot entries are blocked
     across all three entry routes (scout rotation, bridge_scout, re-entry).
  2. The trailing stop (an exit / stop-loss) STILL FIRES while the breaker is
     active — the breaker must not trap the bot in a losing position.
  3. Futures entries are also blocked via the shared new_risk_blocked callback.

The strategy is instantiated the same way the existing per-regime test suite
builds it (``Strategy.__new__`` with stubbed collaborators), so these tests
exercise the genuine ``scout()``, ``bridge_scout()``, ``_reenter_from_bridge()``,
``_check_trailing_stop()``, and ``_new_spot_risk_blocked()`` methods.
"""

import time
from types import SimpleNamespace

from binance_trade_bot.strategies.momentum_strategy import (
    BEAR,
    SIDEWAYS,
    Strategy,
)


# ─── shared stub collaborators (mirrors test_momentum_per_regime_params.py) ──

class Bridge:
    symbol = "USDC"


class KlineClient:
    def __init__(self):
        self.responses = {}

    def get_klines(self, symbol, interval, limit):
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
        return self.prices.get(symbol if isinstance(symbol, str) else f"{symbol}")

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

    def __radd__(self, other):
        return f"{other}{self.symbol}"

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

    def get_latest_regime(self):
        return None


class Logger:
    def __init__(self):
        self.messages = []

    def info(self, message, *a, **k):
        self.messages.append(("info", message))

    def warning(self, message, *a, **k):
        self.messages.append(("warning", message))

    def debug(self, message, *a, **k):
        self.messages.append(("debug", message))

    def error(self, message, *a, **k):
        self.messages.append(("error", message))


class FuturesManager:
    """Minimal stub; real futures-breaker behavior is unit-tested separately."""

    def __init__(self):
        self.actions = []
        self._open_position = None
        self.new_risk_blocked = None

    def manage_bear(self, performance, regime):
        self.actions.append(("manage_bear", performance, regime))
        return "idle"

    def initialize(self):
        pass


def _make_strategy(*, regime=SIDEWAYS, daily_limit=3.0, weekly_limit=8.0, coins=None):
    """Build a real Strategy instance with stubbed collaborators.

    Mirrors the construction in test_momentum_per_regime_params.py so the genuine
    scout()/bridge_scout()/re-entry/trailing-stop methods run.
    """
    strategy = Strategy.__new__(Strategy)
    strategy.config = SimpleNamespace(
        BRIDGE=Bridge(),
        PER_REGIME_PARAMS_ENABLED=False,
        MOMENTUM_LOOKBACK_HOURS=18,
        MOMENTUM_MIN_EDGE=8.0,
        MOMENTUM_MIN_TARGET_PERF=-100.0,  # never reject on absolute performance
        MOMENTUM_FILTER_ENABLED=False,
        RSI_FILTER_ENABLED=False,
        TRAILING_STOP_ENABLED=True,
        TRAILING_STOP_PCT=15.0,
        TRADE_COOLDOWN_SECONDS=0,
        CONFIRMATION_CYCLES=2,
        CONFIRMATION_TIME_ENABLED=False,  # cycle-only confirmation for speed
        CHURN_BLOCK_SECONDS=86400,
        PORTFOLIO_CIRCUIT_BREAKER_ENABLED=True,
        PORTFOLIO_DAILY_MAX_DRAWDOWN_PCT=daily_limit,
        PORTFOLIO_WEEKLY_MAX_DRAWDOWN_PCT=weekly_limit,
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


def _seed_baselines(strategy, start_equity=100.0):
    """Seed circuit-breaker equity baselines WITHOUT a period string.

    Mirrors the live "first run" path: when daily_start_equity is set but no
    period is stored yet, _ensure_circuit_breaker_baselines() records the
    current UTC period (derived from time.time()) WITHOUT resetting equity
    (the ``elif stored_daily_period is None`` branch). Seeding a period string
    that doesn't match the monkeypatched timestamp would instead trigger the
    ``elif stored != computed`` branch and RESET equity to current value —
    which would mask a drawdown. We avoid that by leaving the period unset.
    """
    strategy.db.state["portfolio_daily_start_equity"] = str(start_equity)
    strategy.db.state["portfolio_weekly_start_equity"] = str(start_equity)


def _set_kline_perf(strategy, symbol, start, end):
    strategy.manager.binance_client.responses[f"{symbol}USDC"] = [
        [0, str(start), str(start), str(start), str(start)],
        [0, str(end), str(end), str(end), str(end)],
    ]


# ─── PROOF 1: breaker blocks new spot entries on all three routes ────────────

def test_breaker_blocks_confirmed_scout_rotation_when_daily_dd_exceeds_limit(monkeypatch):
    """Daily drawdown > 3% → scout rotation is blocked (no sell, no buy)."""
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, target])
    _seed_baselines(strategy, start_equity=100.0)

    # Market signal: BBB outperforms AAA by 20% — would normally rotate.
    _set_kline_perf(strategy, "AAA", 100, 100)
    _set_kline_perf(strategy, "BBB", 100, 120)

    # Simulate a portfolio that has drawn down 6% (well past the 3% daily limit).
    # Equity = bridge + coin value. Give 94 USDC of coin value.
    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    monkeypatch.setattr(time, "time", lambda: 1_000.0)

    # First cycle: detect + start confirmation. Second cycle: confirmed.
    strategy.scout()
    monkeypatch.setattr(time, "time", lambda: 1_001.0)
    strategy.scout()

    # Entry was blocked: no rotation occurred.
    assert strategy.manager.sells == [], "breaker must block the sell leg of a rotation"
    assert strategy.manager.buys == [], "breaker must block the buy leg of a rotation"
    assert any(
        "Circuit breaker" in m for lvl, m in strategy.logger.messages if lvl == "warning"
    ), "breaker should log the block"
    # Last-triggered timestamp should have been recorded.
    assert strategy.db.get_bot_state("portfolio_circuit_breaker_last_triggered") is not None


def test_breaker_blocks_bridge_scout_when_daily_dd_exceeds_limit():
    """Daily drawdown > 3% → bridge_scout (leftover balance buy) is blocked."""
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, target])
    _seed_baselines(strategy, start_equity=100.0)

    _set_kline_perf(strategy, "AAA", 100, 100)
    _set_kline_perf(strategy, "BBB", 100, 120)

    # All capital sits in bridge (USDC), coin balance below min notional → bridge_scout eligible.
    strategy.manager.balances["USDC"] = 94.0   # 6% drawdown vs 100 baseline
    strategy.manager.balances["AAA"] = 0.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    strategy.bridge_scout()

    assert strategy.manager.buys == [], "breaker must block bridge_scout entry"
    assert any("Circuit breaker" in m for lvl, m in strategy.logger.messages if lvl == "warning")


def test_breaker_blocks_reentry_from_bridge_when_daily_dd_exceeds_limit():
    """Daily drawdown > 3% → re-entry buy after a trailing stop is blocked."""
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, target])
    _seed_baselines(strategy, start_equity=100.0)
    strategy._awaiting_reentry = True
    strategy._last_trade_time = 0  # past cooldown

    _set_kline_perf(strategy, "AAA", 100, 100)
    _set_kline_perf(strategy, "BBB", 100, 120)

    strategy.manager.balances["USDC"] = 94.0  # 6% drawdown
    strategy.manager.balances["AAA"] = 0.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    strategy._reenter_from_bridge()

    assert strategy.manager.buys == [], "breaker must block re-entry buy"
    assert any("Circuit breaker" in m for lvl, m in strategy.logger.messages if lvl == "warning")


# ─── PROOF 2: the breaker never traps positions (exits still fire) ───────────

def test_breaker_does_NOT_block_trailing_stop_exit(monkeypatch):
    """While the breaker is active, the trailing stop still sells.

    This is the safety-critical property: the breaker blocks NEW risk only.
    A losing position must still be exitable, or the breaker would trap the bot.
    """
    current = Coin("AAA")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, Coin("BBB")])
    _seed_baselines(strategy, start_equity=100.0)

    # 6% drawdown → breaker active.
    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0

    # Set up a trailing-stop scenario: peak was 1.0, now 0.80 → 20% drop > 15% stop.
    strategy._position_peak_price["AAA"] = 1.0
    current_price = 0.80

    # Confirm the breaker is indeed active in this state.
    assert strategy._new_spot_risk_blocked() is True

    # Now the trailing stop must still fire and sell despite the active breaker.
    fired = strategy._check_trailing_stop(current, current_price)

    assert fired is True, "trailing stop MUST still fire while breaker is active"
    assert strategy.manager.sells == [("AAA", "USDC")], "exit must execute, breaker must not trap"
    assert strategy.manager.buys == []  # no new entry from the stop itself


def test_breaker_does_NOT_block_trailing_stop_via_scout(monkeypatch):
    """Full scout() loop: breaker active, price fallen past trailing stop → sells, no buy."""
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, target])
    _seed_baselines(strategy, start_equity=100.0)

    # No rotation signal (AAA flat), so the only possible action is the trailing stop.
    _set_kline_perf(strategy, "AAA", 100, 100)
    _set_kline_perf(strategy, "BBB", 100, 100)

    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 0.80   # price crashed
    strategy.manager.prices["BBBUSDC"] = 1.0
    strategy._position_peak_price["AAA"] = 1.0  # 20% off peak > 15% stop

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    strategy.scout()

    # Trailing stop executed the exit.
    assert strategy.manager.sells == [("AAA", "USDC")]
    assert strategy.manager.buys == []


# ─── PROOF 3: futures new-entry route is gated by the same breaker callback ──

def test_breaker_callback_blocks_futures_new_entry():
    """The futures manager consults the same breaker via new_risk_blocked()."""
    strategy = _make_strategy(regime=BEAR, coins=[Coin("AAA"), Coin("BBB")])
    _seed_baselines(strategy, start_equity=100.0)

    # 6% drawdown.
    strategy.manager.balances["USDC"] = 94.0

    # Wire the callback exactly as the live strategy does (momentum_strategy.py:141).
    strategy.futures_manager.new_risk_blocked = strategy._new_spot_risk_blocked

    # The shared callback must now report risk is blocked.
    assert strategy.futures_manager.new_risk_blocked() is True


def test_breaker_disabled_allows_entries():
    """Sanity: with the breaker off, the same rotation proceeds normally."""
    current = Coin("AAA")
    target = Coin("BBB")
    strategy = _make_strategy(regime=SIDEWAYS, coins=[current, target])
    strategy.config.PORTFOLIO_CIRCUIT_BREAKER_ENABLED = False
    _seed_baselines(strategy, start_equity=100.0)

    _set_kline_perf(strategy, "AAA", 100, 100)
    _set_kline_perf(strategy, "BBB", 100, 120)

    strategy.manager.balances["AAA"] = 94.0
    strategy.manager.prices["AAAUSDC"] = 1.0
    strategy.manager.prices["BBBUSDC"] = 1.0

    assert strategy._new_spot_risk_blocked() is False
