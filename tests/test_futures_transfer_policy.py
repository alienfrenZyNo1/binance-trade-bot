"""Pure transfer-policy tests for futures wallet transfers."""

from binance.exceptions import BinanceAPIException

from binance_trade_bot.futures_transfer_policy import (
    choose_retry_transfer_amount,
    is_insufficient_balance_error,
    safe_transfer_amount,
)


class FakeResponse:
    text = ""
    request = None


def api_error(code, msg):
    return BinanceAPIException(FakeResponse(), 400, f'{{"code": {code}, "msg": "{msg}"}}')


def test_safe_transfer_amount_leaves_dust_and_floors_to_cents():
    assert safe_transfer_amount(54.69950409) == 54.59


def test_safe_transfer_amount_rejects_dust_only_amounts():
    assert safe_transfer_amount(0.99) == 0.0
    assert safe_transfer_amount(1.09) == 0.0
    assert safe_transfer_amount(1.10) == 1.0


def test_choose_retry_transfer_amount_uses_lower_refreshed_or_previous_safe_amount():
    assert choose_retry_transfer_amount(previous_attempt=54.59, refreshed_withdrawable=54.12) == 54.02
    assert choose_retry_transfer_amount(previous_attempt=54.59, refreshed_withdrawable=100.0) == 54.49


def test_insufficient_balance_error_detection_is_specific_to_binance_code():
    assert is_insufficient_balance_error(api_error(-5013, "insufficient balance")) is True
    assert is_insufficient_balance_error(api_error(-4175, "credit status")) is False
    assert is_insufficient_balance_error(RuntimeError("insufficient balance")) is False
    assert is_insufficient_balance_error(None) is False
