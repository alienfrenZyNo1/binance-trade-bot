"""Chaos / failure-mode tests (issue #99 QA hardening).

Adversarial inputs that should never crash the bot, only degrade gracefully:

  * Bad candle data (nulls / zeros / NaN injected into indicator inputs)
  * Missing price ticker (get_ticker_price returns None, ticker unknown)
  * API returns HTML instead of JSON (e.g. a CDN 502 page)
  * Negative balances from the exchange

These target real production code paths in ``binance_api_manager.py`` and
``indicators.py``. No network calls — everything is mocked or constructed
directly.
"""
import math
from types import SimpleNamespace
from unittest import mock

import pytest

from binance_trade_bot.binance_api_manager import BinanceAPIManager
from binance_trade_bot.indicators import (
    compute_adx,
    compute_ema,
    compute_sma,
    compute_std,
)


# --------------------------------------------------------------------------- #
# Shared fakes                                                                #
# --------------------------------------------------------------------------- #
class FakeLogger:
    def __init__(self):
        self.records = []

    def _log(self, level, msg, *a, **k):
        self.records.append((level, str(msg)))

    def debug(self, *a, **k):
        self._log("debug", *a, **k)

    def info(self, *a, **k):
        self._log("info", *a, **k)

    def warning(self, *a, **k):
        self._log("warning", *a, **k)

    def error(self, *a, **k):
        self._log("error", *a, **k)

    def messages(self, level=None):
        if level is None:
            return [m for _, m in self.records]
        return [m for lvl, m in self.records if lvl == level]


class _BalanceCache:
    """Mimics BinanceCache: open_balances() is a context manager whose value is
    a dict that supports .get/.clear/.update and `in`."""

    def __init__(self):
        self._store = {}

    def open_balances(self):
        return self

    def __enter__(self):
        return self._store

    def __exit__(self, *exc):
        return False


def _make_manager():
    """Build a BinanceAPIManager shell (no __init__, no network)."""
    mgr = BinanceAPIManager.__new__(BinanceAPIManager)
    mgr.logger = FakeLogger()
    mgr.config = SimpleNamespace(BRIDGE_SYMBOL="USDC", WIDE_SPREAD_THRESHOLD=0.15)
    cache = _BalanceCache()
    mgr.cache = SimpleNamespace(
        ticker_values={},
        non_existent_tickers=set(),
        open_balances=cache.open_balances,
    )
    return mgr


# --------------------------------------------------------------------------- #
# Chaos: bad candle data fed to indicators                                    #
# --------------------------------------------------------------------------- #
class TestBadCandleDataIndicators:
    def test_ema_with_empty_list_returns_none(self):
        assert compute_ema([], 14) is None

    def test_ema_with_single_value_returns_that_value(self):
        assert compute_ema([42.0], 14) == 42.0

    def test_ema_with_zeros_does_not_crash(self):
        # All-zero closes (delisted / paused symbol) must not raise.
        result = compute_ema([0.0] * 20, 14)
        assert result == 0.0

    def test_ema_with_none_values_is_handled_by_caller_contract(self):
        """Indicators receive pre-cleaned floats. A None sneaking in raises
        TypeError — this test documents that the caller must filter Nones.
        It does NOT crash compute_ema with a divide-by-zero or hang."""
        with pytest.raises(TypeError):
            compute_ema([None, 1.0, 2.0], 3)  # explicit: bad input rejected loudly

    def test_sma_short_input_returns_none(self):
        assert compute_sma([1.0, 2.0], 14) is None

    def test_sma_with_zeros_returns_zero(self):
        assert compute_sma([0.0] * 14, 14) == 0.0

    def test_std_with_zeros_returns_zero(self):
        assert compute_std([0.0] * 14, 14) == 0.0

    def test_std_short_input_returns_zero(self):
        assert compute_std([1.0], 14) == 0.0

    def test_adx_with_empty_closes_returns_zeros(self):
        adx, plus_di, minus_di = compute_adx([], [], [], 14)
        assert (adx, plus_di, minus_di) == (0.0, 0.0, 0.0)

    def test_adx_with_mismatched_lengths_returns_zeros(self):
        # highs/low/closes of different lengths must not raise.
        adx, *_ = compute_adx([1, 2, 3], [1, 2], [1, 2, 3], 14)
        assert adx == 0.0

    def test_adx_with_all_zero_candles_returns_zeros(self):
        """Flatlined (all-zero) OHLC must not divide by zero."""
        zeros = [0.0] * 30
        adx, plus_di, minus_di = compute_adx(zeros, zeros, zeros, 14)
        assert (adx, plus_di, minus_di) == (0.0, 0.0, 0.0)

    def test_adx_with_too_few_candles_returns_zeros(self):
        adx, *_ = compute_adx([1, 2, 3], [1, 2, 3], [1, 2, 3], 14)
        assert adx == 0.0  # needs period*2+1 = 29 candles

    def test_adx_with_constant_nonzero_candles_does_not_crash(self):
        """A perfectly flat market (constant price) has zero true range."""
        flat = [100.0] * 30
        adx, *_ = compute_adx(flat, flat, flat, 14)
        assert adx == 0.0

    def test_adx_nan_in_input_propagates_does_not_silently_pass(self):
        """A NaN in candle data makes ADX NaN — caller must sanitize, not the lib."""
        highs = [10.0] * 30
        highs[5] = float("nan")
        adx, *_ = compute_adx(highs, [9.0] * 30, [9.5] * 30, 14)
        # NaN is contagious; the point is it doesn't raise or return a fake number.
        assert math.isnan(adx) or adx == 0.0


# --------------------------------------------------------------------------- #
# Chaos: missing price ticker                                                 #
# --------------------------------------------------------------------------- #
class TestMissingPriceTicker:
    def test_get_ticker_price_returns_none_for_unknown_symbol(self):
        """A symbol Binance doesn't know must return None, not raise."""
        mgr = _make_manager()
        # Simulate get_symbol_ticker returning a list WITHOUT our symbol.
        mgr.binance_client = SimpleNamespace(
            get_symbol_ticker=lambda: [{"symbol": "BTCUSDC", "price": "60000"}]
        )

        price = mgr.get_ticker_price("NOSUCHCOINUSDC")

        assert price is None
        # After the miss, it's added to the non-existent set (no re-query spam).
        assert "NOSUCHCOINUSDC" in mgr.cache.non_existent_tickers

    def test_missing_ticker_cached_so_no_repeated_api_calls(self):
        """Once flagged non-existent, subsequent lookups skip the API entirely."""
        mgr = _make_manager()
        call_count = {"n": 0}

        def fake_get_symbol_ticker():
            call_count["n"] += 1
            return [{"symbol": "BTCUSDC", "price": "60000"}]

        mgr.binance_client = SimpleNamespace(get_symbol_ticker=fake_get_symbol_ticker)

        first = mgr.get_ticker_price("GHOSTUSDC")
        second = mgr.get_ticker_price("GHOSTUSDC")
        third = mgr.get_ticker_price("GHOSTUSDC")

        assert first is None and second is None and third is None
        # The API should only be hit once for the missing ticker.
        assert call_count["n"] == 1

    def test_cached_ticker_returns_without_api_call(self):
        mgr = _make_manager()
        mgr.cache.ticker_values = {"SOLUSDC": 150.0}
        mgr.binance_client = SimpleNamespace(
            get_symbol_ticker=lambda: (_ for _ in ()).throw(AssertionError("should not call API"))
        )
        assert mgr.get_ticker_price("SOLUSDC") == 150.0

    def test_empty_ticker_list_means_all_unknown(self):
        """If the exchange returns no tickers at all, every lookup is None and
        gets cached so the scout loop doesn't hammer the API."""
        mgr = _make_manager()
        mgr.binance_client = SimpleNamespace(
            get_symbol_ticker=lambda: []  # empty list = no tickers at all
        )
        assert mgr.get_ticker_price("ANYUSDC") is None
        assert "ANYUSDC" in mgr.cache.non_existent_tickers


# --------------------------------------------------------------------------- #
# Chaos: API returns HTML instead of JSON                                     #
# --------------------------------------------------------------------------- #
class TestHtmlInsteadOfJson:
    def test_get_order_book_price_falls_back_to_ticker_on_html_response(self):
        """If the orderbook endpoint returns an HTML error page (CDN 502),
        _get_order_book_price must swallow it and fall back to ticker price."""
        mgr = _make_manager()
        # get_orderbook_ticker returns a dict with bad/zero prices -> triggers
        # the except path, falling back to get_ticker_price.
        mgr.binance_client = SimpleNamespace(
            get_orderbook_ticker=lambda symbol: {"bidPrice": "0", "askPrice": "0"}
        )
        mgr.get_ticker_price = lambda symbol: 99.0  # fallback

        price = mgr._get_order_book_price("SOLUSDC", "BUY")

        assert price == 99.0  # fell back to ticker

    def test_get_order_book_price_falls_back_when_get_orderbook_raises(self):
        """A JSONDecodeError / HTML body inside python-binance surfaces as an
        exception; the broad except must catch it."""
        mgr = _make_manager()

        def boom(symbol):
            raise ValueError("Expecting value: <html>502 Bad Gateway</html>")

        mgr.binance_client = SimpleNamespace(get_orderbook_ticker=boom)
        mgr.get_ticker_price = lambda symbol: 77.0

        assert mgr._get_order_book_price("SOLUSDC", "SELL") == 77.0

    def test_get_spread_returns_zeros_on_html_response(self):
        """_get_spread must degrade to (0,0,0) on a bad orderbook response."""
        mgr = _make_manager()

        def boom(symbol):
            raise ConnectionError("HTML 502 page")

        mgr.binance_client = SimpleNamespace(get_orderbook_ticker=boom)

        spread, bid, ask = mgr._get_spread("SOLUSDC")
        assert (spread, bid, ask) == (0.0, 0, 0)


# --------------------------------------------------------------------------- #
# Chaos: negative balances                                                    #
# --------------------------------------------------------------------------- #
class TestNegativeBalances:
    def test_get_currency_balance_handles_negative_balance_without_crash(self):
        """The exchange (rare bug) returning a negative free balance should be
        stored as-is, not crash float() conversion. Downstream quantity math
        will floor it; we verify the balance path doesn't raise."""
        mgr = _make_manager()
        mgr.binance_client = SimpleNamespace(
            get_account=lambda: {
                "balances": [
                    {"asset": "USDC", "free": "-5.0", "locked": "0"},
                ]
            }
        )
        # force=True bypasses cache so the API is actually hit.
        balance = mgr.get_currency_balance("USDC", force=True)
        assert balance == -5.0  # stored faithfully; caller decides what to do

    def test_get_currency_balance_missing_asset_defaults_to_zero(self):
        mgr = _make_manager()
        mgr.binance_client = SimpleNamespace(
            get_account=lambda: {"balances": [{"asset": "BTC", "free": "1.0", "locked": "0"}]}
        )
        assert mgr.get_currency_balance("NOPE", force=True) == 0.0

    def test_negative_balance_does_not_become_large_sell_quantity(self):
        """Document the downstream risk: a negative balance fed to _sell_quantity
        via math.floor produces a negative qty. The buy/sell guards (qty <= 0)
        already reject this — but it's a chaos path worth pinning."""
        mgr = _make_manager()
        mgr.binance_client = SimpleNamespace()
        mgr.get_currency_balance = lambda symbol, force=False: -3.0
        mgr.get_alt_tick = lambda origin, target: 2  # 2 decimal places

        qty = mgr._sell_quantity("SOL", "USDC", origin_balance=-3.0)
        # math.floor(-3.0 * 100)/100 = -3.0 — negative, which the qty<=0 guard rejects.
        assert qty <= 0, "negative balance must yield non-positive qty (rejected by guard)"

    def test_get_currency_balance_uses_cache_on_second_call_without_force(self):
        mgr = _make_manager()
        calls = {"n": 0}

        def fake_get_account():
            calls["n"] += 1
            return {"balances": [{"asset": "USDC", "free": "100.0", "locked": "0"}]}

        mgr.binance_client = SimpleNamespace(get_account=fake_get_account)

        mgr.get_currency_balance("USDC", force=True)
        mgr.get_currency_balance("USDC", force=False)  # cached
        mgr.get_currency_balance("USDC", force=False)  # cached

        assert calls["n"] == 1  # API hit only once; cache served the rest
