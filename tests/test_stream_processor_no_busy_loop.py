"""Regression test for the CPU busy-loop bug (#103).

`BinanceStreamManager._stream_processor()` previously only slept when
`pop_stream_data_from_stream_buffer()` returned ``False``. In practice that
method returns ``None`` when the buffer is empty, so the idle ``time.sleep``
was never reached and the worker thread spun at ~100% CPU for the bot's
entire uptime.

This test exercises the real `_stream_processor` body with a stubbed
BinanceWebSocketApiManager and asserts that:

  1. An empty buffer (``None``) results in ``time.sleep`` being called
     (the bug fix). Before the fix this test fails: no sleep occurs and the
     loop would spin forever.
  2. Real stream data is still drained/processed (no cadence change).
"""

import time
from unittest import mock

import pytest

from binance_trade_bot.binance_stream_manager import (
    BinanceCache,
    BinanceStreamManager,
)


class _StubBinanceWSManager:
    """Minimal stand-in for unicorn_binance_websocket_api.BinanceWebSocketApiManager.

    Drives exactly the behaviour `_stream_processor` depends on so we can run
    the loop in-process without a live websocket connection.
    """

    def __init__(self):
        # Pre-loaded sequence of return values for the stream buffers. The
        # processor pops one signal + one data value per iteration.
        self.signal_seq = []
        self.data_seq = []
        self.signal_calls = 0
        self.data_calls = 0

    def is_manager_stopping(self):
        return False

    def pop_stream_signal_from_stream_signal_buffer(self):
        idx = self.signal_calls
        self.signal_calls += 1
        if idx < len(self.signal_seq):
            return self.signal_seq[idx]
        # No more scripted signals: tell the loop to stop after the data runs
        # out as well.
        return False

    def pop_stream_data_from_stream_buffer(self):
        idx = self.data_calls
        self.data_calls += 1
        if idx < len(self.data_seq):
            return self.data_seq[idx]
        # After the scripted data is consumed, request a graceful stop so the
        # loop terminates the test deterministically.
        self._request_stop()
        return None

    def get_stream_info(self, stream_id):  # pragma: no cover - unused path
        return {"markets": []}

    def _request_stop(self):
        self.is_manager_stopping = lambda: True


def _make_manager():
    """Build a BinanceStreamManager WITHOUT triggering its real __init__ (which
    opens websocket streams). We only need `_stream_processor` to be runnable.
    """
    mgr = BinanceStreamManager.__new__(BinanceStreamManager)
    mgr.cache = BinanceCache()
    mgr.logger = mock.Mock()
    mgr.bw_api_manager = _StubBinanceWSManager()
    return mgr


def _patch_sleep(mgr):
    """Replace time.sleep seen by the manager's module with a recorder and a
    hard stop after the first sleep, so the loop can't spin even if the gate
    regresses. Returns the list of recorded sleep durations.
    """
    recorded = []

    def fake_sleep(secs):
        recorded.append(secs)
        # Once we've slept once (the fix path) stop the manager so the loop
        # exits the next iteration.
        mgr.bw_api_manager._request_stop()

    return fake_sleep, recorded


def test_empty_buffer_none_triggers_sleep(monkeypatch):
    """Issue #103: an empty buffer returns None and MUST reach time.sleep."""
    mgr = _make_manager()
    # Two idle iterations: both buffers empty (None data, False signal).
    mgr.bw_api_manager.data_seq = [None, None]
    mgr.bw_api_manager.signal_seq = [False, False]

    fake_sleep, recorded = _patch_sleep(mgr)
    import binance_trade_bot.binance_stream_manager as bsm_mod

    monkeypatch.setattr(bsm_mod.time, "sleep", fake_sleep)

    mgr._stream_processor()

    assert len(recorded) >= 1, (
        "time.sleep was never reached for an empty (None) buffer — CPU "
        "busy-loop regression (#103)"
    )
    assert all(d > 0 for d in recorded), "all sleep durations must be positive"


def test_stream_data_still_processed_when_present(monkeypatch):
    """Guard: real stream messages must still be drained (no cadence change)."""
    mgr = _make_manager()
    processed = []

    # First iteration: a real mini-ticker payload; second: empty (None).
    mgr.bw_api_manager.data_seq = [
        {
            "event_type": "24hrMiniTicker",
            "data": [{"symbol": "BTCUSDT", "close_price": "100"}],
        },
        None,
    ]
    mgr.bw_api_manager.signal_seq = [False, False]

    monkeypatch.setattr(
        BinanceStreamManager, "_process_stream_data",
        lambda self, data: processed.append(data),
    )

    import binance_trade_bot.binance_stream_manager as bsm_mod

    # Short-circuit sleep so the loop exits promptly, but still record it.
    def fake_sleep(secs):
        mgr.bw_api_manager._request_stop()

    monkeypatch.setattr(bsm_mod.time, "sleep", fake_sleep)

    mgr._stream_processor()

    assert len(processed) == 1, "real stream data must be drained exactly once"
    assert processed[0]["event_type"] == "24hrMiniTicker"
