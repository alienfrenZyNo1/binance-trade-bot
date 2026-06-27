"""
Tests for spot position reconciliation in the Regime-Adaptive Trend Strategy.

Verifies that _reconcile_spot_positions():
  1. Queries the actual Binance account balances
  2. Classifies holdings as expected (in coin universe) or unexpected (orphan)
  3. Seeds trailing-stop tracking for expected holdings
  4. Logs a WARNING and sells unexpected holdings (or skips in paper mode)
  5. Handles API errors gracefully
  6. Is called during initialize()

Uses the same file-load pattern as test_regime_trend_risk_fixes.py so tests
run without DB/exchange/network dependencies.
"""

import importlib.util
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = REPO_ROOT / "binance_trade_bot" / "strategies" / "regime_trend_strategy.py"


def load_strategy_module():
    """Load the strategy module from file (avoids importing AutoTrader chain)."""
    spec = importlib.util.spec_from_file_location(
        "regime_trend_strategy_spot_recon", STRATEGY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load_strategy_module()


# ═══════════════════════════════════════════════════════════════════════════════
#  STRUCTURAL TESTS — method exists and is called from initialize()
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileStructure:
    """Verify the reconciliation method exists and is wired into initialize()."""

    def test_method_exists(self):
        """_reconcile_spot_positions must exist on the Strategy class."""
        assert hasattr(rt.Strategy, "_reconcile_spot_positions"), (
            "_reconcile_spot_positions method must exist on Strategy"
        )
        assert callable(getattr(rt.Strategy, "_reconcile_spot_positions"))

    def test_called_in_initialize(self):
        """initialize() must call _reconcile_spot_positions()."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "_reconcile_spot_positions()" in src, (
            "initialize() must call _reconcile_spot_positions()"
        )

    def test_reconcile_does_not_modify_strategy_parameters(self):
        """The reconciliation method must not change strategy logic parameters."""
        src = inspect.getsource(rt.Strategy._reconcile_spot_positions)
        # Must NOT reassign any strategy parameters
        forbidden = [
            "self._stop_loss_pct =",
            "self._trail_stop_pct =",
            "self._trend_leverage =",
            "self._bear_action =",
            "self._grid_spacing_pct =",
            "self._grid_levels =",
            "self._coin_universe =",
            "self._max_total_exposure =",
            "self._adx_period =",
            "self._ema_trend_period =",
        ]
        for pattern in forbidden:
            assert pattern not in src, (
                f"Reconciliation must not modify strategy parameter: {pattern}"
            )

    def test_reconcile_queries_account(self):
        """The method must call get_account() to fetch real balances."""
        src = inspect.getsource(rt.Strategy._reconcile_spot_positions)
        assert "get_account" in src, (
            "Must query account balances via get_account()"
        )

    def test_reconcile_logs_summary(self):
        """The method must log a summary of findings."""
        src = inspect.getsource(rt.Strategy._reconcile_spot_positions)
        assert "summary" in src.lower() or "Spot reconcile" in src, (
            "Must log a reconciliation summary"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  FUNCTIONAL TESTS — using mocked manager
# ═══════════════════════════════════════════════════════════════════════════════

def _build_mock_strategy(
    coin_universe=("BNB", "ETH", "XRP"),
    bridge_symbol="USDC",
    paper_mode=False,
    balances=None,
    prices=None,
):
    """Build a minimal mock strategy instance for reconciliation tests."""
    strategy = MagicMock()
    strategy._coin_universe = list(coin_universe)
    strategy.config = SimpleNamespace(
        BRIDGE=SimpleNamespace(symbol=bridge_symbol)
    )
    strategy._paper_mode = paper_mode
    strategy._position_peak_price = {}
    strategy._position_entry_price = {}
    strategy.logger = MagicMock()
    strategy.manager = MagicMock()

    # Account balances
    if balances is None:
        balances = []
    strategy.manager.get_account.return_value = {"balances": balances}

    # Prices: prices is {ticker_symbol: price}
    if prices is None:
        prices = {}
    strategy.manager.get_ticker_price.side_effect = lambda t: prices.get(t)

    return strategy


class TestExpectedHoldings:
    """Verify expected holdings (in coin universe) are handled correctly."""

    def test_expected_holding_seeds_trailing_stop(self):
        """An asset in the coin universe should seed trailing-stop tracking."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[
                {"asset": "ETH", "free": "0.5"},
                {"asset": "USDC", "free": "1000"},
            ],
            prices={"ETHUSDC": 3000.0},
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        assert "ETH" in strategy._position_peak_price
        assert strategy._position_peak_price["ETH"] == 3000.0
        assert "ETH" in strategy._position_entry_price
        assert strategy._position_entry_price["ETH"] == 3000.0

    def test_expected_holding_logs_info(self):
        """An expected holding should be logged at INFO level."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "XRP", "free": "100"}],
            prices={"XRPUSDC": 0.50},
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        info_calls = [c for c in strategy.logger.info.call_args_list]
        assert len(info_calls) > 0
        # At least one info call mentions "XRP"
        found = any("XRP" in str(c) for c in info_calls)
        assert found, "Expected holding should be logged in INFO"

    def test_multiple_expected_holdings(self):
        """Multiple expected holdings should all seed tracking."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[
                {"asset": "ETH", "free": "0.5"},
                {"asset": "XRP", "free": "100"},
            ],
            prices={"ETHUSDC": 3000.0, "XRPUSDC": 0.50},
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        assert "ETH" in strategy._position_peak_price
        assert "XRP" in strategy._position_peak_price


class TestUnexpectedHoldings:
    """Verify unexpected holdings (not in coin universe) are handled correctly."""

    def test_unexpected_holding_logs_warning(self):
        """An unexpected holding should log a WARNING."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[
                {"asset": "DOGE", "free": "500"},
            ],
            prices={"DOGEUSDC": 0.10},
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        warning_calls = [c for c in strategy.logger.warning.call_args_list]
        found = any("UNEXPECTED" in str(c) or "unexpected" in str(c).lower()
                     for c in warning_calls)
        assert found, "Unexpected holding should trigger a WARNING log"

    def test_unexpected_holding_sold_in_live_mode(self):
        """An unexpected holding should be sold via sell_alt in live mode."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "DOGE", "free": "500"}],
            prices={"DOGEUSDC": 0.10},
            paper_mode=False,
        )
        strategy.manager.sell_alt.return_value = {"orderId": 12345}

        with patch("binance_trade_bot.models.Coin") as MockCoin:
            MockCoin.return_value = SimpleNamespace(symbol="DOGE")
            rt.Strategy._reconcile_spot_positions(strategy)

        strategy.manager.sell_alt.assert_called_once()

    def test_unexpected_holding_not_sold_in_paper_mode(self):
        """In paper mode, unexpected holdings should NOT be sold."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "DOGE", "free": "500"}],
            prices={"DOGEUSDC": 0.10},
            paper_mode=True,
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        strategy.manager.sell_alt.assert_not_called()
        # Should log paper message
        info_calls = [str(c) for c in strategy.logger.info.call_args_list]
        assert any("[PAPER]" in c for c in info_calls), (
            "Paper mode should log [PAPER] message instead of selling"
        )

    def test_sell_failure_logs_error_for_manual_review(self):
        """If sell_alt returns None (failure), should log an ERROR."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "DOGE", "free": "500"}],
            prices={"DOGEUSDC": 0.10},
            paper_mode=False,
        )
        strategy.manager.sell_alt.return_value = None  # sell failed

        with patch("binance_trade_bot.models.Coin") as MockCoin:
            MockCoin.return_value = SimpleNamespace(symbol="DOGE")
            rt.Strategy._reconcile_spot_positions(strategy)

        error_calls = [str(c) for c in strategy.logger.error.call_args_list]
        assert any("manual review" in c.lower() for c in error_calls), (
            "Sell failure should flag for manual review"
        )

    def test_sell_exception_logs_error(self):
        """If sell_alt raises an exception, should log an ERROR."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "DOGE", "free": "500"}],
            prices={"DOGEUSDC": 0.10},
            paper_mode=False,
        )
        strategy.manager.sell_alt.side_effect = Exception("API error")

        with patch("binance_trade_bot.models.Coin"):
            rt.Strategy._reconcile_spot_positions(strategy)

        error_calls = [str(c) for c in strategy.logger.error.call_args_list]
        assert any("manual review" in c.lower() for c in error_calls)


class TestEdgeCases:
    """Edge cases for reconciliation."""

    def test_dust_balance_ignored(self):
        """Balances below 0.001 should be ignored."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[
                {"asset": "ETH", "free": "0.0005"},  # dust
            ],
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        assert "ETH" not in strategy._position_peak_price
        assert "ETH" not in strategy._position_entry_price

    def test_bridge_and_bnb_excluded(self):
        """Bridge asset and BNB should always be excluded."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            bridge_symbol="USDC",
            balances=[
                {"asset": "USDC", "free": "10000"},
                {"asset": "BNB", "free": "5.0"},
                {"asset": "ETH", "free": "1.0"},
            ],
            prices={"ETHUSDC": 3000.0},
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        # Only ETH should be tracked, not USDC or BNB
        assert "ETH" in strategy._position_peak_price
        assert "USDC" not in strategy._position_peak_price
        assert "BNB" not in strategy._position_peak_price

    def test_empty_balances(self):
        """No balances → clean state, no errors."""
        strategy = _build_mock_strategy(
            balances=[],
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        # Should log a "clean state" message
        info_calls = [str(c) for c in strategy.logger.info.call_args_list]
        assert any("clean state" in c.lower() for c in info_calls), (
            "Empty balances should log a clean state message"
        )

    def test_account_api_error_handled_gracefully(self):
        """If get_account() fails, reconciliation should not crash."""
        strategy = _build_mock_strategy()
        strategy.manager.get_account.side_effect = Exception("API down")

        # Should not raise
        rt.Strategy._reconcile_spot_positions(strategy)

        warning_calls = [str(c) for c in strategy.logger.warning.call_args_list]
        assert any("could not fetch account" in c.lower() or "skipped" in c.lower()
                     for c in warning_calls), (
            "API error should be logged as a warning, not crash"
        )

    def test_price_unavailable_does_not_crash(self):
        """If get_ticker_price fails, the holding should still be processed."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[{"asset": "ETH", "free": "1.0"}],
            prices={},  # no prices available
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        # ETH should still be tracked (with price 0)
        assert "ETH" in strategy._position_peak_price

    def test_summary_logged(self):
        """A summary with expected/unexpected counts should be logged."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            balances=[
                {"asset": "ETH", "free": "1.0"},
                {"asset": "DOGE", "free": "100"},
            ],
            prices={"ETHUSDC": 3000.0, "DOGEUSDC": 0.10},
            paper_mode=True,  # don't actually sell
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        info_calls = [str(c) for c in strategy.logger.info.call_args_list]
        assert any("summary" in c.lower() for c in info_calls), (
            "Reconciliation summary should be logged"
        )


class TestMixedScenario:
    """Test a realistic mixed scenario with both expected and unexpected."""

    def test_mixed_holdings_correctly_classified(self):
        """A mix of expected, unexpected, bridge, BNB, and dust."""
        strategy = _build_mock_strategy(
            coin_universe=["BNB", "ETH", "XRP"],
            bridge_symbol="USDC",
            balances=[
                {"asset": "ETH", "free": "2.0"},     # expected
                {"asset": "XRP", "free": "500"},      # expected
                {"asset": "ADA", "free": "1000"},     # unexpected (orphan)
                {"asset": "USDC", "free": "5000"},    # bridge, skip
                {"asset": "BNB", "free": "3.0"},      # BNB, skip
                {"asset": "SOL", "free": "0.0001"},   # dust, skip
            ],
            prices={
                "ETHUSDC": 3000.0,
                "XRPUSDC": 0.50,
                "ADAUSDC": 0.45,
            },
            paper_mode=True,  # don't sell
        )

        rt.Strategy._reconcile_spot_positions(strategy)

        # Expected holdings should be tracked
        assert "ETH" in strategy._position_peak_price
        assert "XRP" in strategy._position_peak_price

        # Unexpected holding should NOT seed tracking
        assert "ADA" not in strategy._position_peak_price

        # Bridge, BNB, dust should not appear
        assert "USDC" not in strategy._position_peak_price
        assert "BNB" not in strategy._position_peak_price
        assert "SOL" not in strategy._position_peak_price

        # Should have logged a WARNING for ADA
        warning_calls = [str(c) for c in strategy.logger.warning.call_args_list]
        assert any("ADA" in c for c in warning_calls)

        # Summary should show 2 expected, 1 unexpected
        info_calls = [str(c) for c in strategy.logger.info.call_args_list]
        summary_call = [c for c in info_calls if "summary" in c.lower()]
        assert len(summary_call) > 0
        assert "2" in summary_call[0]  # 2 expected
        assert "1" in summary_call[0]  # 1 unexpected
