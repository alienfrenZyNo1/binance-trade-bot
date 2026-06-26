"""Tests for BinanceAPIManager.retry() exponential backoff + error classification.

These exercise the retry() logic without a live Binance connection. ``time.sleep``
is patched so the tests run instantly while still verifying the *backoff schedule*
that would have been used.
"""
import types
from unittest import mock

import pytest
from binance.exceptions import BinanceAPIException

from binance_trade_bot.binance_api_manager import (
    BinanceAPIManager,
    _classify_retry_error,
)


# --------------------------------------------------------------------------- #
# Lightweight fakes (no Binance/DB connection required)                       #
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

    def warnings(self):
        return [m for lvl, m in self.records if lvl == "warning"]

    def errors(self):
        return [m for lvl, m in self.records if lvl == "error"]


def _make_manager():
    """Build a BinanceAPIManager without running __init__ (no network/DB)."""
    mgr = BinanceAPIManager.__new__(BinanceAPIManager)
    mgr.logger = FakeLogger()
    return mgr


def api_error(code, status_code=400, msg="boom"):
    return BinanceAPIException(
        FakeResponse(), status_code, f'{{"code": {code}, "msg": "{msg}"}}'
    )


def func(name="do_thing"):
    """Return a callable whose __name__ we can reference for logging."""
    def _f(*a, **k):
        return "ok"

    _f.__name__ = name
    return _f


# --------------------------------------------------------------------------- #
# _classify_retry_error unit tests                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [-1013, -1121, -2010, -2013, -2015])
def test_classify_non_retryable_api_codes(code):
    assert _classify_retry_error(api_error(code)) == "non_retryable"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_classify_non_retryable_http_status(status):
    assert _classify_retry_error(api_error(-9999, status_code=status)) == "non_retryable"


def test_classify_rate_limited():
    # HTTP 429 must be classified as rate-limited (retryable but longer backoff).
    assert _classify_retry_error(api_error(-1003, status_code=429)) == "rate_limited"


def test_classify_transient_binance_error_is_retryable():
    # A 5xx with an unclassified code is transient.
    assert _classify_retry_error(api_error(-1001, status_code=500)) == "retryable"


def test_classify_network_errors_are_retryable():
    assert _classify_retry_error(TimeoutError("read timeout")) == "retryable"
    assert _classify_retry_error(ConnectionError("reset")) == "retryable"


# --------------------------------------------------------------------------- #
# retry() behaviour                                                           #
# --------------------------------------------------------------------------- #
def test_retry_returns_value_on_success():
    mgr = _make_manager()
    f = func()
    with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
        assert mgr.retry(f) == "ok"
    assert mgr.logger.records == []  # no warnings when it just works


def test_retry_uses_exponential_backoff_schedule():
    mgr = _make_manager()
    f = func()

    # Always fails -> exhausts all 20 attempts.
    def always_fail(*a, **k):
        raise ConnectionError("network down")

    f.__call__ = always_fail  # type: ignore[attr-defined]

    with mock.patch(
        "binance_trade_bot.binance_api_manager.time.sleep"
    ) as fake_sleep, mock.patch(
        "binance_trade_bot.binance_api_manager.traceback.format_exc",
        return_value="tb",
    ):
        result = mgr.retry(always_fail)

    assert result is None
    # Exactly one sleep per failed attempt (all 20).
    assert fake_sleep.call_count == BinanceAPIManager.MAX_RETRY_ATTEMPTS
    # Verify the backoff schedule: min(2**attempt, 60).
    expected = [min(2 ** a, BinanceAPIManager.MAX_BACKOFF_SECONDS) for a in range(20)]
    actual = [c.args[0] for c in fake_sleep.call_args_list]
    assert actual == expected
    # The cap is respected.
    assert max(actual) == BinanceAPIManager.MAX_BACKOFF_SECONDS
    assert min(actual) == 1  # 2**0


def test_retry_does_not_sleep_flat_one():
    """Regression guard: old behaviour was a flat time.sleep(1) every time."""
    mgr = _make_manager()

    def always_fail(*a, **k):
        raise ConnectionError("down")

    with mock.patch(
        "binance_trade_bot.binance_api_manager.time.sleep"
    ) as fake_sleep, mock.patch(
        "binance_trade_bot.binance_api_manager.traceback.format_exc",
        return_value="tb",
    ):
        mgr.retry(always_fail)

    actual = [c.args[0] for c in fake_sleep.call_args_list]
    assert actual.count(1) == 1  # only the very first attempt is 1s


def test_retry_non_retryable_error_returns_immediately_without_retries():
    mgr = _make_manager()
    calls = {"n": 0}

    def bad_symbol(*a, **k):
        calls["n"] += 1
        raise api_error(-1121, status_code=400, msg="Invalid symbol")  # BAD_SYMBOL

    with mock.patch(
        "binance_trade_bot.binance_api_manager.time.sleep"
    ) as fake_sleep:
        result = mgr.retry(bad_symbol)

    assert result is None
    assert calls["n"] == 1  # exactly one attempt, no retry loop
    assert fake_sleep.call_count == 0  # no backoff slept
    assert any("Non-retryable" in m for m in mgr.logger.errors())


@pytest.mark.parametrize("code", [-1013, -2010])
def test_retry_non_retryable_codes_skip_loop(code):
    mgr = _make_manager()

    def fail(*a, **k):
        raise api_error(code)

    with mock.patch("binance_trade_bot.binance_api_manager.time.sleep") as fake_sleep:
        assert mgr.retry(fail) is None
    assert fake_sleep.call_count == 0


def test_retry_rate_limited_uses_longer_backoff_than_normal():
    mgr = _make_manager()

    def rate_limited(*a, **k):
        raise api_error(-1003, status_code=429, msg="rate limited")

    with mock.patch(
        "binance_trade_bot.binance_api_manager.time.sleep"
    ) as fake_sleep, mock.patch(
        "binance_trade_bot.binance_api_manager.traceback.format_exc",
        return_value="tb",
    ):
        mgr.retry(rate_limited)

    backoffs = [c.args[0] for c in fake_sleep.call_args_list]
    assert backoffs  # we did sleep
    # First backoff must be > 1s (normal would be 1s; 429 multiplies by 3).
    assert backoffs[0] >= 3
    # Still capped at MAX_BACKOFF_SECONDS.
    assert max(backoffs) <= BinanceAPIManager.MAX_BACKOFF_SECONDS
    # Each backoff should be the normal schedule * multiplier (capped).
    expected = [
        min(min(2 ** a, BinanceAPIManager.MAX_BACKOFF_SECONDS)
            * BinanceAPIManager.RATE_LIMIT_BACKOFF_MULTIPLIER,
            BinanceAPIManager.MAX_BACKOFF_SECONDS)
        for a in range(20)
    ]
    assert backoffs == expected


def test_retry_succeeds_after_transient_failures():
    mgr = _make_manager()
    attempts = {"n": 0}

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return "recovered"

    with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
        result = mgr.retry(flaky)

    assert result == "recovered"
    assert attempts["n"] == 3


def test_retry_max_attempts_is_twenty():
    assert BinanceAPIManager.MAX_RETRY_ATTEMPTS == 20
