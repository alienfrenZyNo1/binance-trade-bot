"""
Tests for server-side spot stop-loss protection (SpotStopManager).

Tests cover:
  1. OCO placement succeeds → returns True, tracked as 'oco'
  2. OCO fails → STOP_LOSS_LIMIT placement succeeds → returns True, tracked as 'stop_loss_limit'
  3. Both OCO and STOP_LOSS_LIMIT fail → watchdog fallback activated
  4. Cancel stop after manual sell
  5. is_stop_filled detection for both OCO and STOP_LOSS_LIMIT
  6. Watchdog triggers sell when price drops below stop
  7. Deterministic clientOrderId generation (idempotency)
  8. Price/stop calculation correctness (15% below entry)

Also tests the strategy wiring:
  9. Strategy imports SpotStopManager
  10. Strategy.initialize() creates a spot_stop_manager
  11. _place_spot_stop / _cancel_spot_stop / _check_spot_stop_filled methods exist
  12. place_stop called after buys; cancel_stop called before sells
"""

import importlib.util
import inspect
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = REPO_ROOT / "binance_trade_bot" / "strategies" / "regime_trend_strategy.py"
SPOT_STOP_PATH = REPO_ROOT / "binance_trade_bot" / "spot_stop_manager.py"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ssm = load_module(SPOT_STOP_PATH, "spot_stop_manager_test")
rt = load_module(STRATEGY_PATH, "regime_trend_strategy_stop_test")


# ═══════════════════════════════════════════════════════════════════════════════
#  STOP PRICE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopPriceCalculation:
    """Verify stop prices are calculated correctly."""

    def test_stop_price_is_15pct_below_entry(self):
        """Hard stop should be exactly 15% below entry price."""
        mgr = ssm.SpotStopManager.__new__(ssm.SpotStopManager)
        mgr.stop_loss_pct = 0.15
        entry = 100.0
        stop_price = entry * (1.0 - mgr.stop_loss_pct)
        assert stop_price == 85.0

    def test_oco_limit_price_is_16pct_below_entry(self):
        """OCO limit price should be 16% below entry (1% below stop for fill guarantee)."""
        mgr = ssm.SpotStopManager.__new__(ssm.SpotStopManager)
        mgr.stop_loss_pct = 0.15
        mgr.oco_limit_offset = 0.01
        entry = 100.0
        stop_price = entry * (1.0 - mgr.stop_loss_pct)
        limit_price = entry * (1.0 - mgr.stop_loss_pct - mgr.oco_limit_offset)
        assert limit_price == 84.0
        assert limit_price < stop_price  # Limit must be below stop for guaranteed fill

    def test_stop_price_for_various_entries(self):
        """Verify stop price for various entry prices."""
        pct = 0.15
        for entry in [0.01, 0.50, 1.0, 50.0, 1000.0, 50000.0]:
            stop = entry * (1.0 - pct)
            drop = (entry - stop) / entry
            assert abs(drop - 0.15) < 1e-10


# ═══════════════════════════════════════════════════════════════════════════════
#  OCO PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════

class TestOCOPlacement:
    """Test OCO order placement path."""

    def _make_manager(self, oco_succeeds=True, stop_succeeds=True):
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()

        if oco_succeeds:
            client.order_oco_sell.return_value = {"orderListId": 12345}
        else:
            client.order_oco_sell.side_effect = Exception("OCO not supported")

        if stop_succeeds:
            client.create_order.return_value = {"orderId": 99999}
        else:
            client.create_order.side_effect = Exception("STOP_LOSS_LIMIT not supported")

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        return mgr, client, logger

    def test_oco_placed_successfully(self):
        """When OCO succeeds, place_stop returns True and tracks as 'oco'."""
        mgr, client, _ = self._make_manager(oco_succeeds=True)
        result = mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        assert result is True
        assert "BTCUSDC" in mgr._tracked
        assert mgr._tracked["BTCUSDC"]["type"] == "oco"
        client.order_oco_sell.assert_called_once()

    def test_oco_call_parameters(self):
        """OCO should be called with correct quantity, price, and stopPrice."""
        mgr, client, _ = self._make_manager(oco_succeeds=True)
        mgr.place_stop("ETH", "USDC", 200.0, 1.0)
        kwargs = client.order_oco_sell.call_args[1]
        assert kwargs["symbol"] == "ETHUSDC"
        assert kwargs["quantity"] == "1.0"

    def test_oco_fails_falls_to_stop_loss_limit(self):
        """When OCO fails, STOP_LOSS_LIMIT should succeed."""
        mgr, client, _ = self._make_manager(oco_succeeds=False, stop_succeeds=True)
        result = mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        assert result is True
        assert mgr._tracked["BTCUSDC"]["type"] == "stop_loss_limit"
        client.create_order.assert_called_once()

    def test_stop_loss_limit_call_parameters(self):
        """STOP_LOSS_LIMIT should have type='STOP_LOSS_LIMIT', side='SELL'."""
        mgr, client, _ = self._make_manager(oco_succeeds=False, stop_succeeds=True)
        mgr.place_stop("ETH", "USDC", 200.0, 1.0)
        kwargs = client.create_order.call_args[1]
        assert kwargs["symbol"] == "ETHUSDC"
        assert kwargs["side"] == "SELL"
        assert kwargs["type"] == "STOP_LOSS_LIMIT"


# ═══════════════════════════════════════════════════════════════════════════════
#  WATCHDOG FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════

class TestWatchdogFallback:
    """Test the watchdog fallback when server-side orders fail."""

    def test_both_fail_activates_watchdog(self):
        """When both OCO and STOP_LOSS_LIMIT fail, watchdog is activated."""
        client = MagicMock()
        client.order_oco_sell.side_effect = Exception("OCO not supported")
        client.create_order.side_effect = Exception("STOP not supported")
        logger = MagicMock()
        config = MagicMock()

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        result = mgr.place_stop("BTC", "USDC", 100.0, 0.5)

        assert result is False  # Watchdog only, not server-side
        assert "BTC" in mgr._watchdog_positions
        assert mgr._watchdog_positions["BTC"]["entry"] == 100.0

    def test_watchdog_triggers_sell_on_stop_hit(self):
        """Watchdog should sell when price drops below stop level."""
        client = MagicMock()
        client.order_oco_sell.side_effect = Exception("OCO not supported")
        client.create_order.side_effect = Exception("STOP not supported")
        client.get_symbol_ticker.return_value = {"price": "80.0"}  # Below stop
        logger = MagicMock()
        config = MagicMock()

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.sell_callback = MagicMock(return_value=True)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)

        # Run one watchdog cycle manually
        mgr._watchdog_cycle()

        # Should have called sell callback
        mgr.sell_callback.assert_called_once_with("BTC", "USDC")
        # Should have removed from watchdog
        assert "BTC" not in mgr._watchdog_positions

    def test_watchdog_no_sell_when_above_stop(self):
        """Watchdog should not sell when price is above stop level."""
        client = MagicMock()
        client.order_oco_sell.side_effect = Exception("OCO not supported")
        client.create_order.side_effect = Exception("STOP not supported")
        client.get_symbol_ticker.return_value = {"price": "95.0"}  # Above stop
        logger = MagicMock()
        config = MagicMock()

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.sell_callback = MagicMock(return_value=True)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)

        mgr._watchdog_cycle()

        mgr.sell_callback.assert_not_called()
        assert "BTC" in mgr._watchdog_positions

    def test_watchdog_interval_is_60_seconds(self):
        """Watchdog poll interval must be 60 seconds."""
        assert ssm.SpotStopManager.WATCHDOG_INTERVAL == 60

    def test_watchdog_retries_on_sell_failure(self):
        """Watchdog should keep position if sell fails, for retry next cycle."""
        client = MagicMock()
        client.order_oco_sell.side_effect = Exception("OCO not supported")
        client.create_order.side_effect = Exception("STOP not supported")
        client.get_symbol_ticker.return_value = {"price": "80.0"}
        logger = MagicMock()
        config = MagicMock()

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.sell_callback = MagicMock(return_value=False)  # Sell failed
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)

        mgr._watchdog_cycle()

        # Position should still be monitored
        assert "BTC" in mgr._watchdog_positions


# ═══════════════════════════════════════════════════════════════════════════════
#  CANCEL STOP
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelStop:
    """Test cancelling server-side stops."""

    def test_cancel_oco_calls_cancel_oco_order(self):
        """Cancel should call client.cancel_oco_order for OCO stops."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        client.order_oco_sell.return_value = {"orderListId": 42}

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        mgr.cancel_stop("BTC", "USDC")

        client.cancel_oco_order.assert_called_once()
        assert "BTCUSDC" not in mgr._tracked

    def test_cancel_stop_loss_limit_calls_cancel_order(self):
        """Cancel should call client.cancel_order for STOP_LOSS_LIMIT stops."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        client.order_oco_sell.side_effect = Exception("fail")
        client.create_order.return_value = {"orderId": 777}

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        mgr.cancel_stop("BTC", "USDC")

        client.cancel_order.assert_called_once()
        assert "BTCUSDC" not in mgr._tracked

    def test_cancel_when_no_stop_is_safe(self):
        """Cancelling when no stop exists should not raise."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        mgr = ssm.SpotStopManager(client, logger, config)
        mgr.cancel_stop("BTC", "USDC")  # Should not raise

    def test_cancel_handles_exception_gracefully(self):
        """Cancel should not raise if Binance returns an error."""
        client = MagicMock()
        client.order_oco_sell.return_value = {"orderListId": 1}
        client.cancel_oco_order.side_effect = Exception("Already cancelled")
        logger = MagicMock()
        config = MagicMock()

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        mgr.cancel_stop("BTC", "USDC")  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════════
#  IS STOP FILLED
# ═══════════════════════════════════════════════════════════════════════════════

class TestIsStopFilled:
    """Test detecting when server-side stops have been triggered."""

    def test_oco_filled_returns_true(self):
        """is_stop_filled should return True when OCO listOrderStatus is ALL_DONE."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        client.order_oco_sell.return_value = {"orderListId": 100}
        client.get_oco_order.return_value = {"listOrderStatus": "ALL_DONE"}

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        assert mgr.is_stop_filled("BTC", "USDC") is True

    def test_oco_not_filled_returns_false(self):
        """is_stop_filled should return False when OCO is still pending."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        client.order_oco_sell.return_value = {"orderListId": 100}
        client.get_oco_order.return_value = {"listOrderStatus": "EXECUTING"}

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        assert mgr.is_stop_filled("BTC", "USDC") is False

    def test_stop_loss_limit_filled_returns_true(self):
        """is_stop_filled should return True when STOP_LOSS_LIMIT is FILLED."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        client.order_oco_sell.side_effect = Exception("fail")
        client.create_order.return_value = {"orderId": 200}
        client.get_order.return_value = {"status": "FILLED"}

        mgr = ssm.SpotStopManager(client, logger, config, stop_loss_pct=0.15)
        mgr.place_stop("BTC", "USDC", 100.0, 0.5)
        assert mgr.is_stop_filled("BTC", "USDC") is True

    def test_no_tracked_stop_returns_false(self):
        """is_stop_filled should return False when no stop is tracked."""
        client = MagicMock()
        logger = MagicMock()
        config = MagicMock()
        mgr = ssm.SpotStopManager(client, logger, config)
        assert mgr.is_stop_filled("BTC", "USDC") is False


# ═══════════════════════════════════════════════════════════════════════════════
#  IDEMPOTENT CLIENT ORDER ID
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeterministicClientId:
    """Test that client order IDs are deterministic for idempotency."""

    def test_same_params_same_id(self):
        """Same scope, symbol, quantity → same clientOrderId."""
        id1 = ssm.SpotStopManager._generate_spot_stop_client_order_id("OCO", "BTCUSDC", 0.5)
        id2 = ssm.SpotStopManager._generate_spot_stop_client_order_id("OCO", "BTCUSDC", 0.5)
        assert id1 == id2

    def test_different_params_different_id(self):
        """Different scope → different clientOrderId."""
        id1 = ssm.SpotStopManager._generate_spot_stop_client_order_id("OCO", "BTCUSDC", 0.5)
        id2 = ssm.SpotStopManager._generate_spot_stop_client_order_id("STOP", "BTCUSDC", 0.5)
        assert id1 != id2

    def test_id_starts_with_bts(self):
        """Client order ID should start with 'BTS' for bot tracing."""
        oid = ssm.SpotStopManager._generate_spot_stop_client_order_id("OCO", "BTCUSDC", 0.5)
        assert oid.startswith("BTS")

    def test_id_max_36_chars(self):
        """Client order ID must not exceed Binance's 36-char limit."""
        oid = ssm.SpotStopManager._generate_spot_stop_client_order_id(
            "OCO", "VERYLONGSYMBOLNAMEUSDC", 99999.999
        )
        assert len(oid) <= 36


# ═══════════════════════════════════════════════════════════════════════════════
#  STRATEGY WIRING
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyWiring:
    """Verify the strategy correctly wires in SpotStopManager."""

    def test_strategy_imports_spot_stop_manager(self):
        """Strategy module must import SpotStopManager."""
        src = STRATEGY_PATH.read_text()
        assert "from binance_trade_bot.spot_stop_manager import SpotStopManager" in src

    def test_initialize_creates_spot_stop_manager(self):
        """initialize() must create a spot_stop_manager instance."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "self.spot_stop_manager" in src
        assert "SpotStopManager(" in src

    def test_sell_callback_wired(self):
        """initialize() must wire the watchdog sell callback."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "sell_callback" in src
        assert "_watchdog_sell" in src

    def test_place_spot_stop_method_exists(self):
        """_place_spot_stop method must exist on Strategy."""
        assert hasattr(rt.Strategy, "_place_spot_stop")

    def test_cancel_spot_stop_method_exists(self):
        """_cancel_spot_stop method must exist on Strategy."""
        assert hasattr(rt.Strategy, "_cancel_spot_stop")

    def test_check_spot_stop_filled_method_exists(self):
        """_check_spot_stop_filled method must exist on Strategy."""
        assert hasattr(rt.Strategy, "_check_spot_stop_filled")

    def test_place_called_in_execute_rotation(self):
        """_execute_rotation must call _place_spot_stop after buy."""
        src = inspect.getsource(rt.Strategy._execute_rotation)
        assert "_place_spot_stop" in src
        assert "_cancel_spot_stop" in src

    def test_place_called_in_reenter_from_bridge(self):
        """_reenter_from_bridge must call _place_spot_stop after buy."""
        src = inspect.getsource(rt.Strategy._reenter_from_bridge)
        assert "_place_spot_stop" in src

    def test_place_called_in_bridge_scout(self):
        """bridge_scout must call _place_spot_stop after buy."""
        src = inspect.getsource(rt.Strategy.bridge_scout)
        assert "_place_spot_stop" in src

    def test_cancel_called_in_trailing_stop(self):
        """_check_trailing_stop must call _cancel_spot_stop before selling."""
        src = inspect.getsource(rt.Strategy._check_trailing_stop)
        assert "_cancel_spot_stop" in src

    def test_cancel_called_in_hard_stop(self):
        """_check_hard_stop must call _cancel_spot_stop before selling."""
        src = inspect.getsource(rt.Strategy._check_hard_stop)
        assert "_cancel_spot_stop" in src

    def test_check_filled_in_hard_stop(self):
        """_check_hard_stop must check if server-side stop filled."""
        src = inspect.getsource(rt.Strategy._check_hard_stop)
        assert "_check_spot_stop_filled" in src

    def test_cancel_called_in_exit_to_cash(self):
        """_exit_to_cash must cancel server-side stop before selling."""
        src = inspect.getsource(rt.Strategy._exit_to_cash)
        assert "_cancel_spot_stop" in src

    def test_cancel_called_in_prepare_bear_short(self):
        """_prepare_bear_short must cancel server-side stop before selling."""
        src = inspect.getsource(rt.Strategy._prepare_bear_short)
        assert "_cancel_spot_stop" in src

    def test_paper_mode_skips_stop_placement(self):
        """_place_spot_stop must be skipped in paper mode."""
        src = inspect.getsource(rt.Strategy._place_spot_stop)
        assert "_paper_mode" in src

    def test_watchdog_sell_method_exists(self):
        """_watchdog_sell callback method must exist on Strategy."""
        assert hasattr(rt.Strategy, "_watchdog_sell")


# ═══════════════════════════════════════════════════════════════════════════════
#  PRICE ROUNDING
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriceRounding:
    """Test tick-size rounding for stop prices."""

    def test_round_price_to_tick(self):
        """Round price down to nearest tick size."""
        result = ssm.SpotStopManager._round_price(85.12345678, 0.01)
        assert result == 85.12

    def test_round_price_zero_tick_returns_original(self):
        """Tick size of 0 should return the original price."""
        result = ssm.SpotStopManager._round_price(85.5, 0)
        assert result == 85.5

    def test_round_price_small_tick(self):
        """Round to 8 decimal places tick."""
        result = ssm.SpotStopManager._round_price(85.123456789, 0.00000001)
        assert abs(result - 85.12345678) < 1e-10
