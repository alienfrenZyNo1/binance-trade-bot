"""Import-safety tests for package-level side effects."""

import subprocess
import sys


def test_indicator_submodule_import_does_not_import_socketio_or_eventlet():
    code = """
import sys
from binance_trade_bot.indicators import compute_adx
assert callable(compute_adx)
assert 'socketio' not in sys.modules
assert 'eventlet' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
