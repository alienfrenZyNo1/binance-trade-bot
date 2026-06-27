"""Edge-case tests for retry() error classification (issue #99 QA hardening).

Companion to ``test_retry_backoff.py`` (22 tests). This file adds adversarial /
edge cases the original suite does not cover:

  * Concurrent / repeated HTTP 429 rate-limit storms
  * Error code -2010 (insufficient balance / NEW_ORDER_REJECTED)
  * Connection reset arriving mid-order
  * Mixed transient-then-terminal failure sequences
  * Backoff schedule integrity under mixed classifications
  * Error objects missing expected attributes (no code / no status_code)

These exercise ``_classify_retry_error`` and ``BinanceAPIManager.retry()``
without a live Binance connection. ``time.sleep`` is patched so the tests run
instantly while still verifying the backoff schedule.
"""
from unittest import mock

import pytest
from binance.exceptions import BinanceAPIException

from binance_trade_bot.binance_api_manager import (
    BinanceAPIManager,
    _classify_retry_error,
)


# --------------------------------------------------------------------------- #
# Shared fakes (mirror test_retry_backoff.py for consistency)                 #
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
    mgr = BinanceAPIManager.__new__(BinanceAPIManager)
    mgr.logger = FakeLogger()
    return mgr


def api_error(code, status_code=400, msg="boom"):
    return BinanceAPIException(
        FakeResponse(), status_code, f'{{"code": {code}, "msg": "{msg}"}}'
    )


def func(name="do_thing"):
    def _f(*a, **k):
        return "ok"

    _f.__name__ = name
    return _f


# --------------------------------------------------------------------------- #
# Concurrent / sustained 429 rate-limit storms                                #
# --------------------------------------------------------------------------- #
class TestConcurrentRateLimitStorm:
    """Simulate many concurrent callers all hitting 429 at once."""

    def test_sustained_429_still_retries_with_multiplier(self):
        """A long 429 storm must keep retrying with the rate-limit backoff."""
        mgr = _make_manager()

        def always_429(*a, **k):
            raise api_error(-1003, status_code=429, msg="rate limited")

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep, mock.patch(
            "binance_trade_bot.binance_api_manager.traceback.format_exc",
            return_value="tb",
        ):
            result = mgr.retry(always_429)

        assert result is None  # never succeeds
        assert fake_sleep.call_count == BinanceAPIManager.MAX_RETRY_ATTEMPTS
        backoffs = [c.args[0] for c in fake_sleep.call_args_list]
        # Every backoff must carry the rate-limit multiplier (>=3) until the cap.
        assert all(b >= 3 for b in backoffs)
        assert max(backoffs) == BinanceAPIManager.MAX_BACKOFF_SECONDS

    def test_repeated_429_does_not_short_circuit_as_non_retryable(self):
        """429 must never be misclassified as non_retryable (which would abort)."""
        for _ in range(100):
            assert _classify_retry_error(api_error(-1003, status_code=429)) == "rate_limited"

    def test_429_recovery_after_storm_succeeds(self):
        """After several 429s, a successful call returns the value (no abort)."""
        mgr = _make_manager()
        attempts = {"n": 0}

        def storm_then_ok(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 5:
                raise api_error(-1003, status_code=429, msg="rate limited")
            return "storm-cleared"

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            result = mgr.retry(storm_then_ok)

        assert result == "storm-cleared"
        assert attempts["n"] == 5

    def test_multiple_simulated_concurrent_callers_recover_independently(self):
        """Simulate N 'concurrent' retry() calls — each must recover on its own."""
        results = []
        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            for i in range(8):
                mgr = _make_manager()
                n = {"i": 0}

                def flaky(*a, **k):
                    n["i"] += 1
                    if n["i"] < 3:
                        raise api_error(-1003, status_code=429)
                    return f"caller-{i}-ok"

                results.append(mgr.retry(flaky))

        assert all(r.endswith("-ok") for r in results)
        assert len(set(results)) == 8  # each caller got its own result


# --------------------------------------------------------------------------- #
# Error code -2010 (insufficient balance / NEW_ORDER_REJECTED)                #
# --------------------------------------------------------------------------- #
class TestInsufficientBalanceCode2010:
    """-2010 is terminal (non-retryable): retrying cannot create balance."""

    def test_classify_minus_2010_is_non_retryable(self):
        assert _classify_retry_error(api_error(-2010, msg="insufficient balance")) == "non_retryable"

    def test_classify_minus_2010_with_duplicate_message_still_non_retryable_in_retry(self):
        """``retry()`` treats -2010 as terminal even if the message says 'duplicate'.

        The duplicate-order *recovery* (query existing order) is handled inside
        ``_buy_alt``/``_sell_alt``/``_open_short``, NOT in ``retry()``. From the
        retry() perspective, -2010 is always non_retryable. This pins that
        boundary so the two layers don't get conflated.
        """
        mgr = _make_manager()
        calls = {"n": 0}

        def fail(*a, **k):
            calls["n"] += 1
            raise api_error(-2010, msg="duplicate order id")

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep") as fake_sleep:
            assert mgr.retry(fail) is None

        assert calls["n"] == 1  # no retry loop
        assert fake_sleep.call_count == 0

    def test_minus_2010_among_other_codes_aborts_immediately(self):
        """-2010 appearing after transient errors must still abort instantly."""
        mgr = _make_manager()
        seq = iter([
            ConnectionError("transient"),       # attempt 1: retryable
            api_error(-1001, status_code=500),  # attempt 2: retryable
            api_error(-2010, msg="insufficient"),  # attempt 3: terminal
        ])

        def fail(*a, **k):
            raise next(seq)

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep") as fake_sleep:
            assert mgr.retry(fail) is None

        # Only 2 backoff sleeps (between attempts 1->2 and 2->3); attempt 3 aborts.
        assert fake_sleep.call_count == 2

    def test_minus_2010_logs_non_retryable_error(self):
        mgr = _make_manager()

        def fail(*a, **k):
            raise api_error(-2010, msg="insufficient balance")

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            mgr.retry(fail)

        assert any("Non-retryable" in m for m in mgr.logger.errors())


# --------------------------------------------------------------------------- #
# Connection reset mid-order                                                   #
# --------------------------------------------------------------------------- #
class TestConnectionResetMidOrder:
    """A TCP reset that arrives during an order submit must be retryable."""

    def test_connection_reset_error_is_retryable(self):
        # ConnectionResetError is a subclass of ConnectionError.
        assert _classify_retry_error(ConnectionResetError("Connection reset by peer")) == "retryable"

    def test_plain_connection_error_is_retryable(self):
        assert _classify_retry_error(ConnectionError("[Errno 104] reset")) == "retryable"

    def test_broken_pipe_is_retryable(self):
        # BrokenPipeError is also a ConnectionError subclass.
        assert _classify_retry_error(BrokenPipeError("[Errno 32] Broken pipe")) == "retryable"

    def test_retry_recovers_after_connection_resets(self):
        mgr = _make_manager()
        attempts = {"n": 0}

        def reset_then_ok(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 4:
                raise ConnectionResetError("peer reset the connection")
            return "order-placed"

        with mock.patch("binance_trade_bot.binance_api_manager.time.sleep"):
            result = mgr.retry(reset_then_ok)

        assert result == "order-placed"
        assert attempts["n"] == 4

    def test_connection_reset_exhausts_max_attempts_without_success(self):
        mgr = _make_manager()

        def always_reset(*a, **k):
            raise ConnectionResetError("persistent reset")

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep, mock.patch(
            "binance_trade_bot.binance_api_manager.traceback.format_exc",
            return_value="tb",
        ):
            assert mgr.retry(always_reset) is None

        assert fake_sleep.call_count == BinanceAPIManager.MAX_RETRY_ATTEMPTS

    def test_reset_then_429_then_reset_uses_correct_backoff_each_time(self):
        """Mixed reset/rate-limit sequence: each backoff matches its classification."""
        mgr = _make_manager()
        seq = iter([
            ConnectionResetError("reset"),                    # retryable
            api_error(-1003, status_code=429),                # rate_limited
            ConnectionResetError("reset"),                    # retryable
            "ok",
        ])

        def fail(*a, **k):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep:
            result = mgr.retry(fail)

        assert result == "ok"
        backoffs = [c.args[0] for c in fake_sleep.call_args_list]
        assert len(backoffs) == 3
        # Attempt 0 (reset): normal backoff = 2**0 = 1
        assert backoffs[0] == 1
        # Attempt 1 (429): rate-limited backoff = min(2**1, 60) * 3 = 6
        assert backoffs[1] == min(2 ** 1, BinanceAPIManager.MAX_BACKOFF_SECONDS) * \
            BinanceAPIManager.RATE_LIMIT_BACKOFF_MULTIPLIER
        # Attempt 2 (reset): normal backoff = 2**2 = 4
        assert backoffs[2] == 4


# --------------------------------------------------------------------------- #
# Malformed / attribute-less error objects                                    #
# --------------------------------------------------------------------------- #
class TestMalformedErrorObjects:
    """Errors missing ``code`` / ``status_code`` must not crash classification."""

    def test_error_without_code_or_status_is_retryable(self):
        # A BinanceAPIException with unrecognised shape falls through to retryable.
        e = api_error(-9999, status_code=500, msg="weird")
        assert _classify_retry_error(e) == "retryable"

    def test_generic_exception_is_retryable(self):
        assert _classify_retry_error(RuntimeError("unexpected")) == "retryable"

    def test_key_error_does_not_crash_classification(self):
        # KeyError or ValueError must be classified, not crash _classify_retry_error.
        assert _classify_retry_error(KeyError("missing")) == "retryable"
        assert _classify_retry_error(ValueError("bad value")) == "retryable"


# --------------------------------------------------------------------------- #
# Backoff schedule integrity under mixed classifications                      #
# --------------------------------------------------------------------------- #
class TestBackoffScheduleIntegrity:
    def test_monotonic_non_decreasing_normal_backoffs(self):
        """Normal retryable backoffs must never decrease across attempts."""
        mgr = _make_manager()
        attempt = {"n": 0}

        def fail(*a, **k):
            attempt["n"] += 1
            raise ConnectionError("down")

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep:
            mgr.retry(fail)

        backoffs = [c.args[0] for c in fake_sleep.call_args_list]
        for prev, cur in zip(backoffs, backoffs[1:]):
            assert cur >= prev, f"backoff decreased: {prev} -> {cur}"

    def test_backoff_never_exceeds_max_cap(self):
        mgr = _make_manager()

        def always_rate_limited(*a, **k):
            raise api_error(-1003, status_code=429)

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep:
            mgr.retry(always_rate_limited)

        backoffs = [c.args[0] for c in fake_sleep.call_args_list]
        assert all(b <= BinanceAPIManager.MAX_BACKOFF_SECONDS for b in backoffs)

    def test_first_backoff_on_normal_error_is_one_second(self):
        mgr = _make_manager()

        def fail(*a, **k):
            raise ConnectionError("first hit")

        with mock.patch(
            "binance_trade_bot.binance_api_manager.time.sleep"
        ) as fake_sleep:
            mgr.retry(fail)

        first = fake_sleep.call_args_list[0].args[0]
        assert first == 1  # 2**0
