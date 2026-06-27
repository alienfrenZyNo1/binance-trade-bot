"""Tests for the kill switch flat-verification (issue #99 QA hardening).

``scripts/telegram_bot.py::_execute_kill`` closes all futures positions and then
re-queries the exchange to verify everything went flat. If any position
remains, it emits a prominent warning. These tests pin that verification path:

  * Positions remain open after close → warning fires, verification fails.
  * All positions close cleanly → success message, verification passes.
  * DB event logging records the verification result correctly.

The module is loaded by file path (matching ``test_telegram_profit.py``) and
all network/DB calls are mocked — no live Binance or Telegram traffic.
"""
import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "telegram_bot.py"


def load_module():
    os.environ.setdefault("TELEGRAM_CHAT_IDS", "0")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
    # Provide dummy API keys so the key-guard early-returns pass.
    os.environ.setdefault("BINANCE_API_KEY", "testkey")
    os.environ.setdefault("BINANCE_API_SECRET", "testsecret")
    spec = importlib.util.spec_from_file_location("telegram_bot_kill_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePostResponse:
    def __init__(self, status_code=200, text='{"orderId":1}'):
        self.status_code = status_code
        self.text = text


def _position(symbol="SOLUSDC", direction="SHORT", qty=5.0, entry=100.0, pnl_usd=-12.0, pnl_pct=-2.4):
    return {
        "symbol": symbol,
        "direction": direction,
        "qty": qty,
        "entry": entry,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
    }


# --------------------------------------------------------------------------- #
# Verification path: positions STILL OPEN after kill                          #
# --------------------------------------------------------------------------- #
class TestKillSwitchVerificationFailsWhenPositionsRemain:
    def test_warning_fires_when_position_still_open(self):
        mod = load_module()

        # get_futures_positions is called twice: once before close (finds 1 pos),
        # once after close for verification (STILL finds 1 pos -> verification fails).
        mod.get_futures_positions = mock.Mock(side_effect=[
            [_position(symbol="SOLUSDC", qty=5.0)],   # before close
            [_position(symbol="SOLUSDC", qty=5.0)],   # verification: still open
        ])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        # Close order POST returns 200 OK (position "closed" but actually not).
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "VERIFICATION FAILED" in result
        assert "positions still open" in result
        assert "Manual intervention required" in result
        assert "Kill switch incomplete" in result
        # The remaining position is surfaced with its symbol and direction.
        assert "SOLUSDC" in result
        assert "SHORT" in result

    def test_kill_returns_incomplete_summary_not_complete(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[
            [_position()],   # before
            [_position()],   # after: still open
        ])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        # Must NOT claim success.
        assert "Kill switch complete" not in result
        assert "confirmed flat" not in result
        assert "Kill switch incomplete" in result

    def test_remaining_position_details_shown_in_warning(self):
        """The warning must list symbol, direction, qty, entry, and unPnL."""
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[
            [_position(symbol="XRPUSDC", direction="SHORT", qty=120.0, entry=0.55, pnl_usd=3.0)],
            [_position(symbol="XRPUSDC", direction="SHORT", qty=120.0, entry=0.55, pnl_usd=3.0)],
        ])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "XRPUSDC" in result
        assert "120" in result  # qty
        assert "0.55" in result  # entry
        assert "3" in result  # unPnL

    def test_db_event_logs_verification_failed(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[[_position()], [_position()]])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            mod._execute_kill()

        mod._log_kill_event.assert_called_once()
        event = mod._log_kill_event.call_args[0][0]
        assert event["verification_passed"] is False
        assert len(event["remaining_positions"]) == 1


# --------------------------------------------------------------------------- #
# Verification path: all positions CLOSE cleanly                              #
# --------------------------------------------------------------------------- #
class TestKillSwitchVerificationPassesWhenFlat:
    def test_success_message_when_all_flat(self):
        mod = load_module()
        # Before: 1 position. After close: 0 positions (flat).
        mod.get_futures_positions = mock.Mock(side_effect=[
            [_position(symbol="SOLUSDC")],   # before close
            [],                               # after close: flat
        ])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "confirmed flat" in result
        assert "Kill switch complete" in result
        assert "VERIFICATION FAILED" not in result

    def test_db_event_logs_verification_passed(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[[_position()], []])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            mod._execute_kill()

        event = mod._log_kill_event.call_args[0][0]
        assert event["verification_passed"] is True
        assert event["remaining_positions"] == []
        assert "SOLUSDC" in event["closed_symbols"]


# --------------------------------------------------------------------------- #
# Edge cases                                                                  #
# --------------------------------------------------------------------------- #
class TestKillSwitchEdgeCases:
    def test_no_positions_to_close_reports_flat(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(return_value=[])  # none, none
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "No open positions to close" in result
        assert "confirmed flat" in result

    def test_multiple_positions_some_remain_shows_all_remaining(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[
            [_position(symbol="SOLUSDC", qty=5), _position(symbol="XRPUSDC", qty=100)],
            [_position(symbol="XRPUSDC", qty=100)],  # only SOL closed
        ])
        mod.get_futures_balance = mock.Mock(return_value=None)
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "VERIFICATION FAILED" in result
        assert "XRPUSDC" in result
        # The remaining list should show exactly the still-open position.
        assert result.count("XRPUSDC") >= 2  # in close attempt + in remaining

    def test_transfer_attempted_when_balance_remains_after_flat(self):
        mod = load_module()
        mod.get_futures_positions = mock.Mock(side_effect=[[_position()], []])
        mod.get_futures_balance = mock.Mock(return_value={
            "balance": 250.0, "available": 250.0, "pnl": 0.0, "non_bridge_assets": []
        })
        mod._sign_request = mock.Mock(return_value={})
        mod._log_kill_event = mock.Mock()
        # Close POST returns 200; transfer POST also 200.
        with mock.patch.object(mod.requests, "post", return_value=FakePostResponse(200)), \
             mock.patch.object(mod.time, "sleep"):
            result = mod._execute_kill()

        assert "Transferred" in result
        assert "250" in result
        assert "confirmed flat" in result
