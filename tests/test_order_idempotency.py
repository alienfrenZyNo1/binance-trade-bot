"""Stress tests for order idempotency (issue #99 QA hardening).

Simulates the classic async double-submit failure mode:
    network timeout → client retries → Binance already has the order →
    duplicate rejection → recovery by querying the existing order.

Covers both the spot path (``_buy_alt`` / ``_sell_alt`` in
``binance_api_manager.py``) and the futures path (``_open_short`` in
``futures_manager.py``), plus the pure helper functions
``_generate_client_order_id`` and ``_is_duplicate_order_error``.

These tests never touch the network — every Binance call is mocked.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from binance.exceptions import BinanceAPIException

from binance_trade_bot.binance_api_manager import (
    _generate_client_order_id,
    _is_duplicate_order_error,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Lightweight fakes                                                            #
# --------------------------------------------------------------------------- #
class FakeResponse:
    text = ""
    request = None


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


def api_error(code, status_code=400, msg="boom"):
    return BinanceAPIException(
        FakeResponse(), status_code, f'{{"code": {code}, "msg": "{msg}"}}'
    )


# --------------------------------------------------------------------------- #
# Pure helper: _generate_client_order_id determinism                          #
# --------------------------------------------------------------------------- #
class TestGenerateClientOrderIdDeterminism:
    """The idempotency contract rests on this function being deterministic."""

    def test_same_inputs_produce_same_id(self):
        a = _generate_client_order_id("BUY", "SOL", "100.50", "1.234")
        b = _generate_client_order_id("BUY", "SOL", "100.50", "1.234")
        assert a == b

    def test_different_side_changes_id(self):
        buy = _generate_client_order_id("BUY", "SOL", "100.50", "1.234")
        sell = _generate_client_order_id("SELL", "SOL", "100.50", "1.234")
        assert buy != sell

    @pytest.mark.parametrize(
        "field,new_val",
        [("price", "100.51"), ("qty", "1.235"), ("coin_symbol", "XRP")],
    )
    def test_changing_any_order_param_changes_id(self, field, new_val):
        base = dict(side="BUY", coin_symbol="SOL", price="100.50", qty="1.234")
        changed = dict(base)
        changed[field] = new_val
        assert _generate_client_order_id(**base) != _generate_client_order_id(**changed)

    def test_id_respects_36_char_binance_limit(self):
        # Long coin name + long price/qty must still fit Binance's 36-char cap.
        cid = _generate_client_order_id(
            "BUY", "VERYLONGCOINSYMBOL", "99999999.99999999", "99999999.99999999"
        )
        assert len(cid) <= 36

    def test_id_starts_with_prefix(self):
        cid = _generate_client_order_id("BUY", "SOL", "100", "1")
        assert cid.startswith("BTM")

    def test_contains_no_whitespace_or_special_chars(self):
        cid = _generate_client_order_id("BUY", "SOL", "100.50", "1.234")
        # Must be URL/order-safe: only alphanumeric.
        assert cid.isalnum(), f"client_order_id must be alnum, got {cid!r}"


# --------------------------------------------------------------------------- #
# Pure helper: _is_duplicate_order_error                                      #
# --------------------------------------------------------------------------- #
class TestIsDuplicateOrderError:
    def test_code_minus_2010_is_duplicate(self):
        # Binance reuses -2010 for insufficient balance AND duplicate orderId;
        # the current implementation treats -2010 as duplicate in both cases.
        assert _is_duplicate_order_error(api_error(-2010, msg="duplicateOrder")) is True

    def test_message_duplicate_order_detected(self):
        e = api_error(-9999, msg="Duplicate order sent")
        assert _is_duplicate_order_error(e) is True

    def test_message_duplicate_substring_detected(self):
        e = api_error(-9999, msg="This is a duplicate request")
        assert _is_duplicate_order_error(e) is True

    def test_unrelated_error_is_not_duplicate(self):
        e = api_error(-1121, msg="Invalid symbol")
        assert _is_duplicate_order_error(e) is False

    def test_generic_exception_returns_false(self):
        assert _is_duplicate_order_error(ValueError("nope")) is False


# --------------------------------------------------------------------------- #
# Futures idempotency: _open_short duplicate recovery                         #
# --------------------------------------------------------------------------- #
def load_futures_manager_class():
    """Load futures_manager.py by file path (matches test_futures_margin_mode)."""
    module_path = REPO_ROOT / "binance_trade_bot" / "futures_manager.py"
    spec = importlib.util.spec_from_file_location("futures_manager_idempotency_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FuturesManager


class FakeBridge:
    symbol = "USDC"


class DupFuturesClient:
    """Futures client that simulates timeout-then-duplicate ordering."""

    def __init__(self, fail_first=None, duplicate_code=-2010):
        self.orders = []
        self.calls = 0
        self.fail_first = fail_first or []  # exceptions to raise before succeeding
        self.duplicate_code = duplicate_code

    def futures_change_leverage(self, symbol, leverage):
        return {"symbol": symbol, "leverage": leverage}

    def futures_change_margin_type(self, symbol, marginType):
        return {"symbol": symbol, "marginType": marginType}

    def futures_create_order(self, **kwargs):
        self.calls += 1
        idx = self.calls - 1
        if idx < len(self.fail_first):
            raise self.fail_first[idx]
        self.orders.append(kwargs)
        return {"orderId": 42, "avgPrice": "10.0"}

    def futures_position_information(self, symbol=None):
        return []


def _make_futures_manager(client, **config_overrides):
    FuturesManager = load_futures_manager_class()
    logger = FakeLogger()
    config = SimpleNamespace(
        BRIDGE=FakeBridge(),
        FUTURES_LEVERAGE=1,
        FUTURES_MAX_MARGIN_PCT=0.5,
        FUTURES_STOP_LOSS_PCT=15.0,
        FUTURES_TRAILING_STOP_PCT=10.0,
        FUTURES_TRAILING_ACTIVATION_PCT=3.0,
        FUTURES_SERVER_TRAILING_ENABLED=True,
        FUTURES_SERVER_TRAILING_CALLBACK_RATE=1.0,
        FUTURES_SERVER_TRAILING_MIN_PROFIT_BUFFER_PCT=0.5,
        FUTURES_MAX_FUNDING_RATE=0.0001,
        FUTURES_FUNDING_EXIT_MULTIPLIER=3.0,
        FUTURES_CHECK_INTERVAL=60,
        TESTNET=False,
        **config_overrides,
    )
    mgr = FuturesManager(client, logger, config)
    mgr._get_mark_price = lambda symbol: 10.0
    mgr._floor_quantity = lambda symbol, quantity: quantity
    mgr._get_min_notional = lambda symbol: 5.0
    mgr._place_server_stops = lambda symbol, quantity, entry: True
    mgr._get_funding_rate = lambda symbol: 0.0
    mgr._validate_position_margin_mode = lambda symbol: None
    return mgr, logger


class TestFuturesShortIdempotency:
    def test_duplicate_client_order_id_treated_as_already_opened(self):
        """Futures _open_short: a -2010 duplicate must be recovered, not fatal."""
        client = DupFuturesClient()
        client.fail_first = [api_error(-2010, msg="duplicate order id")]
        mgr, logger = _make_futures_manager(client)

        result = mgr._open_short("SOL", margin=50.0, perf_pct=-6.0)

        # The duplicate rejection is treated as success: order already exists.
        assert result == "opened"
        assert client.calls == 1  # only the rejected call; no infinite loop
        assert any("duplicate" in m.lower() for m in logger.messages("info"))

    def test_timeout_then_duplicate_does_not_create_second_order(self):
        """A first-attempt timeout (non-Binance exception) aborts to 'idle'.

        Unlike the spot path, ``_open_short`` has NO internal retry loop: a
        single ``futures_create_order`` call that dies with a network error is
        caught by the broad ``except Exception`` and returns ``idle`` without
        ever re-submitting — so it cannot double-place. This test pins that
        contract: the caller (``retry()`` / scheduler) owns the retry decision,
        and the deterministic clientOrderId makes any later retry idempotent.
        """
        client = DupFuturesClient()
        client.fail_first = [ConnectionError("read timeout")]
        mgr, logger = _make_futures_manager(client)

        result = mgr._open_short("SOL", margin=50.0, perf_pct=-6.0)

        assert result == "idle"  # network error aborts; no second order placed
        assert client.calls == 1  # exactly one attempt — no internal retry
        assert any("error" in lvl for lvl, _ in logger.records)

    def test_non_duplicate_api_error_aborts_safely(self):
        """A genuinely rejected short (not duplicate) must return idle, not loop."""
        client = DupFuturesClient()
        client.fail_first = [api_error(-1121, msg="Invalid symbol")]  # BAD_SYMBOL
        mgr, logger = _make_futures_manager(client)

        result = mgr._open_short("SOL", margin=50.0, perf_pct=-6.0)
        assert result == "idle"
        assert client.calls == 1

    def test_deterministic_order_id_uses_coin_and_price(self):
        """The futures client_order_id embeds coin + price so retries collide."""
        captured = {}
        orig = DupFuturesClient.futures_create_order

        class CaptureClient(DupFuturesClient):
            def futures_create_order(self, **kwargs):
                captured.update(kwargs)
                raise api_error(-2010, msg="duplicate")

        client = CaptureClient()
        mgr, _ = _make_futures_manager(client)
        mgr._open_short("SOL", margin=50.0, perf_pct=-6.0)

        cid = captured.get("newClientOrderId", "")
        assert cid.startswith("BTMS")
        assert "SOL" in cid


# --------------------------------------------------------------------------- #
# Spot idempotency: _buy_alt / _sell_alt duplicate recovery                    #
# --------------------------------------------------------------------------- #
class DupSpotClient:
    """Mimics python-binance Client enough to drive _buy_alt/_sell_alt.

    Raises a duplicate error on the second submit (simulating that Binance
    already recorded the order from the timed-out first attempt), then serves
    the order via get_order when queried by origClientOrderId.
    """

    def __init__(self):
        self.submit_calls = 0
        self.get_calls = 0
        self.submitted_params = []

    def get_symbol_info(self, symbol):
        return {"quotePrecision": 2, "baseAssetPrecision": 6}

    def get_symbol_ticker(self):
        return [{"symbol": "SOLUSDC", "price": "100.50"}]

    def order_limit_buy(self, **kwargs):
        self.submit_calls += 1
        self.submitted_params.append(kwargs)
        if self.submit_calls == 1:
            raise ConnectionError("read timeout")  # simulate network death
        # second call — Binance says we already sent it
        raise api_error(-2010, msg="duplicate order id")

    def order_limit_sell(self, **kwargs):
        self.submit_calls += 1
        self.submitted_params.append(kwargs)
        if self.submit_calls == 1:
            raise ConnectionError("read timeout")
        raise api_error(-2010, msg="duplicate order id")

    def get_order(self, **kwargs):
        self.get_calls += 1
        return {
            "orderId": 9001,
            "status": "FILLED",
            "cumulativeQuoteQty": "123.45",
            "executedQty": kwargs.get("quantity", "1"),
        }


def _make_spot_manager(client):
    """Build a BinanceAPIManager shell with _buy_alt/_sell_alt dependencies mocked."""
    from binance_trade_bot.binance_api_manager import BinanceAPIManager

    mgr = BinanceAPIManager.__new__(BinanceAPIManager)
    mgr.logger = FakeLogger()
    mgr.binance_client = client
    mgr.config = SimpleNamespace(BRIDGE_SYMBOL="USDC", USE_MAKER_ORDERS=False)

    # Stub the pieces _buy_alt/_sell_alt depend on.
    mgr.cache = SimpleNamespace(open_balances=lambda: _DummyCtxMgr({}))
    mgr._buy_quantity = lambda origin, target, balance, price: 1.0
    mgr._sell_quantity = lambda origin, target, balance=None: 1.0
    mgr.get_ticker_price = lambda symbol: 100.50
    # get_currency_balance must return a DECREASING value when force=True,
    # because _sell_alt polls balance after the sell in a while loop until the
    # balance drops below the pre-sell level. A constant return would hang.
    mgr.get_currency_balance = lambda symbol, force=False: 99.0 if force else 100.0

    # Order guard stub — _buy_alt calls acquire_order_guard().set_order(...)
    order_guard = SimpleNamespace(set_order=lambda *a, **k: None)
    mgr.stream_manager = SimpleNamespace(acquire_order_guard=lambda: order_guard)

    # wait_for_order: return a minimal filled-order object.
    mgr.wait_for_order = lambda *a, **k: SimpleNamespace(
        cumulative_quote_qty="123.45"
    )

    # DB trade log stub.
    mgr.db = SimpleNamespace(
        start_trade_log=lambda *a, **k: SimpleNamespace(
            set_ordered=lambda *a, **k: None,
            set_complete=lambda *a, **k: None,
        )
    )
    return mgr


class _DummyCtxMgr:
    """A context manager mimicking cache.open_balances()."""

    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self._data

    def __exit__(self, *exc):
        return False


class TestSpotBuyIdempotency:
    def test_timeout_then_duplicate_recovers_existing_order(self):
        client = DupSpotClient()
        mgr = _make_spot_manager(client)

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            result = mgr._buy_alt(SimpleNamespace(symbol="SOL"), SimpleNamespace(symbol="USDC"))

        # _buy_alt submits (timeout), retries (duplicate), then queries existing order.
        assert client.submit_calls == 2
        assert client.get_calls == 1  # recovered the existing order
        # Same client_order_id used on both submit attempts (idempotency key).
        cids = [p.get("newClientOrderId") for p in client.submitted_params]
        assert cids[0] == cids[1], "both attempts must reuse the same client_order_id"
        assert result is not None

    def test_duplicate_logs_idempotency_success_message(self):
        client = DupSpotClient()
        mgr = _make_spot_manager(client)
        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            mgr._buy_alt(SimpleNamespace(symbol="SOL"), SimpleNamespace(symbol="USDC"))

        msgs = mgr.logger.messages()
        assert any("duplicate" in m.lower() or "already placed" in m.lower() for m in msgs)


class TestSpotSellIdempotency:
    def test_timeout_then_duplicate_recovers_existing_order(self):
        client = DupSpotClient()
        mgr = _make_spot_manager(client)

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            result = mgr._sell_alt(SimpleNamespace(symbol="SOL"), SimpleNamespace(symbol="USDC"))

        assert client.submit_calls == 2
        assert client.get_calls == 1
        cids = [p.get("newClientOrderId") for p in client.submitted_params]
        assert cids[0] == cids[1]
        assert result is not None
