"""
QA gap-filler tests for the Regime-Adaptive Trend Strategy.

Written by QUINN (QA Agent) during formal QA review.

These tests cover gaps identified during the review:
  1. RegimeSignal.__repr__ with ema_trend=None (BUG FOUND)
  2. _total_exposure_allows_entry exact boundary
  3. _compute_total_exposure_ratio with zero/None equity
  4. _handle_regime_transition noop when old == new
  5. GridState edge case: levels=0
  6. RegimeHysteresis pending state interaction
  7. Circuit breaker fail-open alert rate limiting
  8. _circuit_breaker_periods UTC computation correctness
  9. compute_position_size with negative equity
  10. _extract_ohlc with mixed input formats
"""

import importlib.util
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = REPO_ROOT / "binance_trade_bot" / "strategies" / "regime_trend_strategy.py"


def load_strategy_module():
    """Load the strategy module from file."""
    spec = importlib.util.spec_from_file_location("regime_trend_strategy_qa", STRATEGY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load_strategy_module()


# ═══════════════════════════════════════════════════════════════════════════════
#  BUG FIX: RegimeSignal.__repr__ CRASH WITH ema_trend=None
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeSignalReprBug:
    """RegimeSignal.__repr__ crashes when ema_trend is None.

    The f-string ``f"{self.ema_trend:.4f if self.ema_trend else 'N/A'}"``
    is syntactically parsed as ``f"{None:.4f if ... else ...}"`` which
    applies format spec ``.4f`` to NoneType → TypeError.
    """

    def test_repr_works_with_ema_none(self):
        """repr() must not crash when ema_trend is None."""
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=35.0, minus_di=10.0,
            price=100.0, ema_trend=None,
        )
        # Should not raise TypeError
        result = repr(signal)
        assert "RegimeSignal" in result
        assert "bull" in result or "bear" in result

    def test_repr_works_with_ema_present(self):
        """repr() works normally when ema_trend is set."""
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=35.0, minus_di=10.0,
            price=100.0, ema_trend=95.0,
        )
        result = repr(signal)
        assert "RegimeSignal" in result


# ═══════════════════════════════════════════════════════════════════════════════
#  TOTAL EXPOSURE BOUNDARY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestExposureBoundary:
    """Test exact boundary conditions in _total_exposure_allows_entry."""

    def test_entry_allowed_at_exact_limit(self):
        """When new_ratio == limit exactly, entry should be allowed (not > limit)."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = 100.0
        strategy._compute_total_exposure_ratio.return_value = 1.0
        strategy.logger = MagicMock()
        # Adding 50 → ratio = 1.0 + 0.5 = 1.5, which is NOT > 1.5
        assert rt.Strategy._total_exposure_allows_entry(strategy, 50.0) is True

    def test_entry_blocked_just_over_limit(self):
        """When new_ratio is infinitesimally over limit, entry is blocked."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = 100.0
        strategy._compute_total_exposure_ratio.return_value = 1.0
        strategy.logger = MagicMock()
        # Adding 51 → ratio = 1.0 + 0.51 = 1.51 > 1.5
        assert rt.Strategy._total_exposure_allows_entry(strategy, 51.0) is False

    def test_entry_allowed_with_zero_additional(self):
        """Zero additional notional is always allowed when under limit."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = 100.0
        strategy._compute_total_exposure_ratio.return_value = 1.0
        strategy.logger = MagicMock()
        assert rt.Strategy._total_exposure_allows_entry(strategy, 0.0) is True


# ═══════════════════════════════════════════════════════════════════════════════
#  COMPUTE TOTAL EXPOSURE EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeExposureEdgeCases:
    """Test _compute_total_exposure_ratio edge cases."""

    def test_zero_equity_returns_none(self):
        """Zero equity → None (can't compute)."""
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = 0.0
        strategy.db.get_current_coin.return_value = None
        strategy.futures_manager._open_position = None
        assert rt.Strategy._compute_total_exposure_ratio(strategy) is None

    def test_negative_equity_returns_none(self):
        """Negative equity → None."""
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = -100.0
        assert rt.Strategy._compute_total_exposure_ratio(strategy) is None

    def test_none_equity_returns_none(self):
        """None equity → None."""
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = None
        assert rt.Strategy._compute_total_exposure_ratio(strategy) is None

    def test_no_current_coin_spot_value_zero(self):
        """When no current coin, spot_value = 0."""
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = 100.0
        strategy.db.get_current_coin.return_value = None
        strategy.futures_manager._open_position = None
        ratio = rt.Strategy._compute_total_exposure_ratio(strategy)
        assert ratio == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  REGIME TRANSITION EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeTransitionEdges:
    """Test _handle_regime_transition edge cases."""

    def test_no_transition_when_same_regime(self):
        """_handle_regime_transition should noop when old == new."""
        strategy = MagicMock()
        strategy._bear_action = "short"
        strategy._paper_mode = True
        rt.Strategy._handle_regime_transition(strategy, "bull", "bull")
        # Should not call any exit/entry methods
        strategy._exit_to_cash.assert_not_called()
        strategy._prepare_bear_short.assert_not_called()
        strategy._exit_bear_mode.assert_not_called()

    def test_bull_to_sideways_resets_grid(self):
        """Moving from bull to sideways should null out grid state."""
        strategy = MagicMock()
        strategy._paper_mode = True
        strategy._grid_state = "old_grid"
        rt.Strategy._handle_regime_transition(strategy, "bull", "sideways")
        assert strategy._grid_state is None

    def test_bull_to_bear_triggers_bear_prep(self):
        """Moving from bull to bear with short action should call _prepare_bear_short."""
        strategy = MagicMock()
        strategy._bear_action = "short"
        strategy._paper_mode = False
        rt.Strategy._handle_regime_transition(strategy, "bull", "bear")
        strategy._prepare_bear_short.assert_called_once()

    def test_bull_to_bear_cash_mode_triggers_exit(self):
        """Moving from bull to bear with cash action should call _exit_to_cash."""
        strategy = MagicMock()
        strategy._bear_action = "cash"
        strategy._paper_mode = False
        rt.Strategy._handle_regime_transition(strategy, "bull", "bear")
        strategy._exit_to_cash.assert_called_once()
        strategy._prepare_bear_short.assert_not_called()

    def test_bear_to_bull_exits_bear_mode(self):
        """Moving from bear to bull should call _exit_bear_mode."""
        strategy = MagicMock()
        strategy._paper_mode = False
        rt.Strategy._handle_regime_transition(strategy, "bear", "bull")
        strategy._exit_bear_mode.assert_called_once()

    def test_bear_to_sideways_exits_bear_mode(self):
        """Moving from bear to sideways should also call _exit_bear_mode."""
        strategy = MagicMock()
        strategy._paper_mode = False
        rt.Strategy._handle_regime_transition(strategy, "bear", "sideways")
        strategy._exit_bear_mode.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
#  GRID STATE EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestGridStateEdgeCases:
    """Test GridState with unusual parameters."""

    def test_grid_with_zero_levels(self):
        """GridState with 0 levels produces 0 orders (no crash)."""
        grid = rt.GridState(levels=0, spacing_pct=0.025, mid_price=100.0)
        assert len(grid.orders) == 0
        assert grid.unfilled_count == 0

    def test_grid_with_one_level(self):
        """GridState with 1 level produces exactly 2 orders."""
        grid = rt.GridState(levels=1, spacing_pct=0.05, mid_price=100.0)
        assert len(grid.orders) == 2
        assert grid.orders[0]["price"] == 95.0  # buy at 5% below
        assert grid.orders[1]["price"] == 105.0  # sell at 5% above

    def test_grid_with_large_spacing(self):
        """GridState with 50% spacing still works."""
        grid = rt.GridState(levels=2, spacing_pct=0.5, mid_price=100.0)
        buy_orders = [o for o in grid.orders if o["side"] == "buy"]
        # First buy at 50, second at 0
        assert buy_orders[0]["price"] == pytest.approx(50.0)
        assert buy_orders[1]["price"] == pytest.approx(0.0)

    def test_grid_reset_preserves_config(self):
        """Resetting grid should keep same levels/spacing."""
        grid = rt.GridState(levels=3, spacing_pct=0.02, mid_price=100.0)
        grid.reset(200.0)
        assert grid.levels == 3
        assert grid.spacing_pct == 0.02
        assert grid.mid_price == 200.0

    def test_grid_does_not_refill_already_filled(self):
        """Already filled orders should not fill again."""
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        fills1 = grid.check_fills(97.0)
        assert len(fills1) >= 1
        fills2 = grid.check_fills(97.0)
        # The same orders should not fill again
        assert len(fills2) == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  HYSTERESIS INTERACTION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestHysteresisInteraction:
    """Test that RegimeHysteresis prevents whipsaw transitions."""

    def test_single_noise_reading_does_not_change_regime(self):
        """A single contradictory reading should NOT change the active regime."""
        hyst = rt.RegimeHysteresis(active="bull", confirmations=3)
        obs = hyst.observe("bear")
        assert obs.active == "bull"
        assert obs.changed is False
        assert obs.pending == "bear"

    def test_consecutive_readings_change_regime(self):
        """Three consecutive readings of the same candidate change regime."""
        hyst = rt.RegimeHysteresis(active="bull", confirmations=3)
        hyst.observe("bear")  # pending_count=1
        hyst.observe("bear")  # pending_count=2
        obs = hyst.observe("bear")  # pending_count=3 → change!
        assert obs.active == "bear"
        assert obs.changed is True
        assert obs.previous == "bull"

    def test_interleaved_noise_resets_counter(self):
        """If the candidate changes before confirmations, counter resets."""
        hyst = rt.RegimeHysteresis(active="bull", confirmations=3)
        hyst.observe("bear")  # pending=bear, count=1
        hyst.observe("sideways")  # pending=sideways, count=1 (reset)
        obs = hyst.observe("bear")  # pending=bear, count=1 (reset again)
        assert obs.active == "bull"
        assert obs.changed is False

    def test_observing_active_regime_clears_pending(self):
        """Observing the active regime clears pending state."""
        hyst = rt.RegimeHysteresis(active="bull", confirmations=3)
        hyst.observe("bear")  # pending=bear
        obs = hyst.observe("bull")  # clears pending
        assert obs.active == "bull"
        assert obs.pending is None
        assert obs.pending_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerEdges:
    """Test circuit breaker edge cases."""

    def test_fail_open_alert_rate_limited(self):
        """_alert_circuit_breaker_fail_open should rate-limit at 10 min intervals."""
        strategy = MagicMock()
        strategy.CIRCUIT_BREAKER_FAIL_OPEN_ALERT_INTERVAL = 600
        strategy._cb_fail_open_last_alert_ts = time.time()  # Just alerted

        # Should NOT send notification (rate limited)
        rt.Strategy._alert_circuit_breaker_fail_open(strategy)
        # Verify it used notification=False path (rate-limited warning)
        strategy.logger.warning.assert_called()
        args = strategy.logger.warning.call_args
        assert args[1].get("notification") is False or "notification" not in args[1]

    def test_fail_open_alert_not_rate_limited_first_time(self):
        """First call to _alert_circuit_breaker_fail_open sends notification."""
        strategy = MagicMock()
        strategy.CIRCUIT_BREAKER_FAIL_OPEN_ALERT_INTERVAL = 600
        strategy._cb_fail_open_last_alert_ts = 0.0  # Never alerted

        rt.Strategy._alert_circuit_breaker_fail_open(strategy)
        # Should have set the timestamp
        assert strategy._cb_fail_open_last_alert_ts > 0
        # Should have sent at least one warning
        strategy.logger.warning.assert_called()

    def test_new_risk_blocked_returns_true_during_cooldown(self):
        """When circuit breaker is cooling down, new risk is blocked."""
        strategy = MagicMock()
        strategy.config = SimpleNamespace(PORTFOLIO_CIRCUIT_BREAKER_ENABLED=True)
        strategy._estimate_spot_equity.return_value = 1000.0
        strategy._get_float_state.return_value = time.time()  # Just triggered
        strategy.logger = MagicMock()

        # Patch the cooling-down check to return True
        with patch.object(rt, "is_circuit_breaker_cooling_down", return_value=True):
            result = rt.Strategy._new_spot_risk_blocked(strategy)
        assert result is True


# ═══════════════════════════════════════════════════════════════════════════════
#  CIRCUIT BREAKER PERIOD COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerPeriods:
    """Test UTC period computation for circuit breaker."""

    def test_daily_period_format(self):
        """Daily period should be YYYY-MM-DD format."""
        ts = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        daily, weekly = rt.Strategy._circuit_breaker_periods(ts)
        assert daily == "2026-06-27"

    def test_weekly_period_format(self):
        """Weekly period should be YYYY-WNN format."""
        ts = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        daily, weekly = rt.Strategy._circuit_breaker_periods(ts)
        assert weekly.startswith("2026-W")

    def test_different_days_produce_different_periods(self):
        """Two days apart should produce different daily periods."""
        ts1 = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        ts2 = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        d1, w1 = rt.Strategy._circuit_breaker_periods(ts1)
        d2, w2 = rt.Strategy._circuit_breaker_periods(ts2)
        assert d1 != d2
        assert w1 == w2  # Same week

    def test_utc_timezone_used(self):
        """Period computation should use UTC, not local time."""
        # Midnight UTC on June 27 = 2026-06-27
        ts = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        daily, _ = rt.Strategy._circuit_breaker_periods(ts)
        assert daily == "2026-06-27"


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZING EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionSizingEdges:
    """Test compute_position_size with unusual inputs."""

    def test_negative_equity(self):
        """Negative equity shouldn't crash (returns the fraction, not sized)."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=-100.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(2.0)

    def test_very_small_equity(self):
        """Very small equity shouldn't crash."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=0.01)
        assert frac == pytest.approx(1.0)

    def test_nan_equity(self):
        """NaN equity shouldn't crash position sizing."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=float('nan'))
        assert frac == pytest.approx(1.0)

    def test_zero_leverage(self):
        """Zero leverage should return 0.0 effective leverage."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=1000.0, trend_leverage=0.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(0.0)

    def test_negative_leverage(self):
        """Negative leverage should pass through (no validation)."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=1000.0, trend_leverage=-1.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(-1.0)

    def test_grid_fraction_above_one(self):
        """Grid fraction > 1.0 should pass through (no clamping)."""
        frac, lev = rt.compute_position_size(rt.SIDEWAYS, equity=1000.0, grid_fraction=1.5)
        assert frac == pytest.approx(1.5)


# ═══════════════════════════════════════════════════════════════════════════════
#  _extract_ohlc FORMAT AGNOSTIC
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractOHLC:
    """Test _extract_ohlc with different kline formats."""

    def test_dict_format_klines(self):
        """Binance python client returns dict klines."""
        klines = [
            {"open": "100", "high": "110", "low": "90", "close": "105"},
            {"open": "105", "high": "115", "low": "95", "close": "110"},
        ]
        highs, lows, closes = rt.Strategy._extract_ohlc(klines)
        assert highs == [110.0, 115.0]
        assert lows == [90.0, 95.0]
        assert closes == [105.0, 110.0]

    def test_list_format_klines(self):
        """Binance REST API returns list klines."""
        klines = [
            [1000, "100", "110", "90", "105", "1000", 0, "110", "100", 0, 0, 0],
            [2000, "105", "115", "95", "110", "2000", 0, "115", "110", 0, 0, 0],
        ]
        highs, lows, closes = rt.Strategy._extract_ohlc(klines)
        assert highs == [110.0, 115.0]
        assert lows == [90.0, 95.0]
        assert closes == [105.0, 110.0]

    def test_single_kline(self):
        """Single kline should work."""
        klines = [{"open": "100", "high": "110", "low": "90", "close": "105"}]
        highs, lows, closes = rt.Strategy._extract_ohlc(klines)
        assert len(highs) == 1

    def test_empty_klines(self):
        """Empty klines should return empty lists."""
        highs, lows, closes = rt.Strategy._extract_ohlc([])
        assert highs == []
        assert lows == []
        assert closes == []


# ═══════════════════════════════════════════════════════════════════════════════
#  UNUSED IMPORTS CHECK (code quality)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCodeQuality:
    """Detect code quality issues (unused imports, etc.)."""

    def test_unused_json_import(self):
        """json is imported but never used — dead import."""
        src = STRATEGY_PATH.read_text()
        # json is imported on line 34 but never referenced via json.X
        assert "import json" in src
        # Verify it's never actually used as json.something
        lines = src.split("\n")
        json_usages = [
            line for line in lines
            if "json." in line and not line.strip().startswith("#") and "import" not in line
        ]
        assert len(json_usages) == 0, f"json is imported but never used. Remove `import json`."

    def test_unused_math_import(self):
        """math is imported but never used — dead import."""
        src = STRATEGY_PATH.read_text()
        assert "import math" in src
        lines = src.split("\n")
        math_usages = [
            line for line in lines
            if "math." in line and not line.strip().startswith("#") and "import" not in line
        ]
        assert len(math_usages) == 0, f"math is imported but never used. Remove `import math`."

    def test_unused_rsi_import(self):
        """compute_rsi is imported as _compute_rsi_func but never called."""
        src = STRATEGY_PATH.read_text()
        # Check _compute_rsi_func is only used in the import statement
        usages = [
            line.strip() for line in src.split("\n")
            if "_compute_rsi_func" in line
        ]
        # Should only appear in the import line
        assert len(usages) == 1, (
            "_compute_rsi_func is imported but never used in the code body."
            " Remove from imports or add RSI-based logic."
        )
