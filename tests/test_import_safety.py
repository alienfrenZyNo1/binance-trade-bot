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


def test_database_send_update_default_does_not_import_socketio_or_eventlet():
    code = """
import sys
from types import SimpleNamespace
from binance_trade_bot.database import Database

class Logger:
    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass

class Model:
    __tablename__ = 'dummy'
    def info(self):
        return {'ok': True}

config = SimpleNamespace()
db = Database(Logger(), config, uri='sqlite:///:memory:')
db.send_update(Model())
assert 'socketio' not in sys.modules
assert 'eventlet' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-W", "default", "-c", code],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
