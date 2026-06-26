"""Regression tests for futures margin mode handling."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from binance.exceptions import BinanceAPIException


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "binance_trade_bot" / "futures_manager.py"


def load_futures_manager_class():
    spec = importlib.util.spec_from_file_location("futures_manager_margin_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FuturesManager


class FakeBridge:
    symbol = "USDC"


class FakeLogger:
    def __init__(self):
        self.records = []

    def debug(self, msg):
        self.records.append(("debug", str(msg)))

    def info(self, msg):
        self.records.append(("info", str(msg)))

    def warning(self, msg):
        self.records.append(("warning", str(msg)))

    def error(self, msg):
        self.records.append(("error", str(msg)))

    def messages(self, level=None):
        if level is None:
            return [msg for _, msg in self.records]
        return [msg for lvl, msg in self.records if lvl == level]


class FakeResponse:
    text = ""
    request = None


def api_error(code, msg):
    return BinanceAPIException(FakeResponse(), 400, f'{{"code": {code}, "msg": "{msg}"}}')


class FakeFuturesClient:
    def __init__(self):
        self.leverage_calls = []
        self.margin_type_calls = []
        self.orders = []
        self.margin_error = None
        self.position_margin_type = "cross"
        self.reported_margin_type = None

    def futures_change_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def futures_change_margin_type(self, symbol, marginType):
        self.margin_type_calls.append((symbol, marginType))
        if self.margin_error is not None:
            raise self.margin_error
        self.position_margin_type = marginType.lower()
        return {"symbol": symbol, "marginType": marginType}

    def futures_create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": 123, "avgPrice": "10"}

    def futures_position_information(self, symbol=None):
        return [
            {
                "symbol": symbol or "TIAUSDC",
                "positionAmt": "-5",
                "entryPrice": "10",
                "marginType": self.reported_margin_type or self.position_margin_type,
            }
        ]


def make_manager(client=None, **config_overrides):
    client = client or FakeFuturesClient()
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
    manager_cls = load_futures_manager_class()
    manager = manager_cls(client, logger, config)
    manager._get_mark_price = lambda symbol: 10.0
    manager._floor_quantity = lambda symbol, quantity: quantity
    manager._get_min_notional = lambda symbol: 5.0
    manager._place_server_stops = lambda symbol, quantity, entry: True
    manager._get_funding_rate = lambda symbol: 0.0
    return manager, client, logger


def test_default_futures_margin_mode_is_cross_not_isolated():
    manager, client, _logger = make_manager()

    result = manager._open_short("TIA", margin=50.0, perf_pct=-6.0)

    assert result == "opened"
    assert client.margin_type_calls == [("TIAUSDC", "CROSS")]
    assert all(call[1] != "ISOLATED" for call in client.margin_type_calls)
    assert client.orders, "short order should still open in configured CROSS mode"


def test_isolated_margin_rejection_aborts_short_instead_of_silently_opening_cross():
    client = FakeFuturesClient()
    client.margin_error = api_error(-4175, "Credit status does not support isolated margin")
    manager, client, logger = make_manager(client, FUTURES_MARGIN_TYPE="ISOLATED")

    result = manager._open_short("TIA", margin=50.0, perf_pct=-6.0)

    assert result == "idle"
    assert client.margin_type_calls == [("TIAUSDC", "ISOLATED")]
    assert client.orders == []
    assert any("margin mode" in msg and "aborted" in msg for msg in logger.messages("error"))


def test_open_short_warns_when_exchange_reports_unexpected_margin_mode():
    client = FakeFuturesClient()
    manager, _client, logger = make_manager(client)
    client.reported_margin_type = "isolated"

    result = manager._open_short("TIA", margin=50.0, perf_pct=-6.0)

    assert result == "opened"
    assert any("margin mode mismatch" in msg for msg in logger.messages("warning"))


def test_futures_entry_blocker_prevents_new_short_entries_only():
    manager, client, logger = make_manager()
    manager._initialized = True
    manager._last_entry_attempt = 0
    manager.position_check_interval = 0
    manager._get_futures_usdc_balance = lambda: 100.0
    manager.new_risk_blocked = lambda: True

    result = manager.manage_bear({"ADA": -8.0}, "bear")

    assert result == "idle"
    assert client.orders == []
    assert any("circuit breaker" in msg.lower() for msg in logger.messages("warning"))


def test_futures_entry_blocker_does_not_prevent_existing_position_management():
    manager, client, logger = make_manager()
    manager._initialized = True
    manager.position_check_interval = 0
    manager.new_risk_blocked = lambda: True
    manager._open_position = SimpleNamespace(
        symbol="TIAUSDC",
        entry_price=10.0,
        quantity=5.0,
        peak_pnl_pct=0.0,
        opened_at=0.0,
    )
    called = []
    manager._manage_open_position = lambda: called.append(True) or "holding"

    result = manager.manage_bear({"TIA": -8.0}, "bear")

    assert result == "holding"
    assert called == [True]
    assert client.orders == []


def test_futures_wallet_balance_helper_uses_raw_balance_not_max_withdraw():
    manager, client, _logger = make_manager()
    client.futures_account_balance = lambda: [
        {"asset": "USDC", "balance": "100.0", "maxWithdrawAmount": "70.0"}
    ]

    assert manager._get_futures_usdc_wallet_balance() == 100.0
    assert manager._get_futures_usdc_balance() == 70.0
