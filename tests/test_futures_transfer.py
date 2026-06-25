"""Regression tests for futures wallet transfer edge cases."""

from types import SimpleNamespace

from binance.exceptions import BinanceAPIException

from binance_trade_bot.futures_manager import FuturesManager


class FakeBridge:
    symbol = "USDC"


class FakeLogger:
    def __init__(self):
        self.records = []

    def debug(self, msg, *args, **kwargs):
        self.records.append(("debug", str(msg), kwargs))

    def info(self, msg, *args, **kwargs):
        self.records.append(("info", str(msg), kwargs))

    def warning(self, msg, *args, **kwargs):
        self.records.append(("warning", str(msg), kwargs))

    def error(self, msg, *args, **kwargs):
        self.records.append(("error", str(msg), kwargs))

    def messages(self, level=None):
        return [msg for lvl, msg, _ in self.records if level is None or lvl == level]

    def kwargs_for(self, level):
        return [kwargs for lvl, _msg, kwargs in self.records if lvl == level]


class FakeResponse:
    text = ""
    request = None


def api_error(code, msg):
    return BinanceAPIException(FakeResponse(), 400, f'{{"code": {code}, "msg": "{msg}"}}')


class FakeTransferClient:
    def __init__(self, balances=None, fail_first_with=None, fail_all_with=None):
        self.transfers = []
        self.balance_calls = 0
        self.balances = balances or [
            {"asset": "USDC", "balance": "54.86801431", "maxWithdrawAmount": "54.69950409"}
        ]
        self.fail_first_with = fail_first_with
        self.fail_all_with = fail_all_with

    def futures_account_transfer(self, **kwargs):
        self.transfers.append(kwargs)
        if self.fail_all_with is not None:
            raise self.fail_all_with
        if self.fail_first_with is not None and len(self.transfers) == 1:
            raise self.fail_first_with
        return {"tranId": 123}

    def futures_account_balance(self):
        idx = min(self.balance_calls, len(self.balances) - 1)
        self.balance_calls += 1
        return [self.balances[idx]]


def make_manager(client):
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
    )
    logger = FakeLogger()
    return FuturesManager(client, logger, config), logger


def test_transfer_to_spot_leaves_dust_and_floors_amount_before_transfer():
    client = FakeTransferClient()
    manager, logger = make_manager(client)

    assert manager.transfer_to_spot(54.69950409) is True

    assert client.transfers == [
        {"asset": "USDC", "amount": 54.59, "type": 2}
    ]
    assert any("Transferred 54.59 USDC to spot" in msg for msg in logger.messages("info"))


def test_transfer_to_spot_retries_smaller_amount_after_insufficient_balance():
    client = FakeTransferClient(
        balances=[
            {"asset": "USDC", "balance": "54.86801431", "maxWithdrawAmount": "54.12000000"},
        ],
        fail_first_with=api_error(-5013, "Asset transfer failed: insufficient balance"),
    )
    manager, logger = make_manager(client)

    assert manager.transfer_to_spot(54.69950409) is True

    assert client.transfers == [
        {"asset": "USDC", "amount": 54.59, "type": 2},
        {"asset": "USDC", "amount": 54.02, "type": 2},
    ]
    assert any("Retrying futures→spot transfer" in msg for msg in logger.messages("warning"))
    assert all(kwargs.get("notification") is False for kwargs in logger.kwargs_for("warning"))


def test_transfer_to_spot_insufficient_balance_failure_is_not_notification_spam():
    client = FakeTransferClient(
        fail_all_with=api_error(-5013, "Asset transfer failed: insufficient balance")
    )
    manager, logger = make_manager(client)

    assert manager.transfer_to_spot(54.69950409) is False

    assert len(client.transfers) == 2
    assert any("leaving funds in futures" in msg for msg in logger.messages("warning"))
    assert logger.messages("error") == []
    assert all(kwargs.get("notification") is False for kwargs in logger.kwargs_for("warning"))


def test_transfer_to_spot_skips_tiny_dust_amounts():
    client = FakeTransferClient()
    manager, logger = make_manager(client)

    assert manager.transfer_to_spot(0.09) is False

    assert client.transfers == []
    assert any("below transferable threshold" in msg for msg in logger.messages("debug"))
