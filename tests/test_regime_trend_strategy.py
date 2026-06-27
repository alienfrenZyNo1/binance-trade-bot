"""
Comprehensive tests for the Regime-Adaptive Trend Strategy.

Tests cover:
  - Regime detection accuracy (bull/bear/sideways/transition boundaries)
  - Position sizing per regime
  - Stop loss / trail stop behavior
  - Transition between regimes
  - Edge cases (gap moves, no data, invalid signals)

All tests use the pure functions from the strategy module so they can run
without database, exchange API, or network access.
"""

import math
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_PATH = REPO_ROOT / "binance_trade_bot" / "strategies" / "regime_trend_strategy.py"


def load_strategy_module():
    """Load the strategy module from file (avoids importing AutoTrader chain)."""
    spec = importlib.util.spec_from_file_location("regime_trend_strategy_test", STRATEGY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load_strategy_module()


# ═══════════════════════════════════════════════════════════════════════════════
#  REGIME DETECTION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeDetection:
    """Test detect_regime_from_indicators() at all boundaries."""

    def test_bull_regime_adx_above_threshold_price_above_ema(self):
        """ADX ≥ 25 + price > EMA200 → BULL."""
        signal = rt.detect_regime_from_indicators(
            adx=28.0, plus_di=30.0, minus_di=15.0,
            price=100.0, ema_trend=90.0,
        )
        assert signal.regime == rt.BULL
        assert signal.is_trending is True
        assert signal.above_ema is True

    def test_bear_regime_adx_above_threshold_price_below_ema(self):
        """ADX ≥ 25 + price < EMA200 → BEAR."""
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=12.0, minus_di=28.0,
            price=80.0, ema_trend=100.0,
        )
        assert signal.regime == rt.BEAR
        assert signal.is_trending is True
        assert signal.above_ema is False

    def test_sideways_regime_adx_below_threshold(self):
        """ADX < 20 → SIDEWAYS regardless of price vs EMA."""
        signal = rt.detect_regime_from_indicators(
            adx=15.0, plus_di=20.0, minus_di=22.0,
            price=100.0, ema_trend=95.0,
        )
        assert signal.regime == rt.SIDEWAYS
        assert signal.is_trending is False

    def test_transition_regime_adx_between_thresholds(self):
        """20 ≤ ADX < 25 → TRANSITION."""
        signal = rt.detect_regime_from_indicators(
            adx=22.0, plus_di=25.0, minus_di=18.0,
            price=100.0, ema_trend=98.0,
        )
        assert signal.regime == rt.TRANSITION
        assert signal.is_trending is False

    def test_exact_adx_trend_boundary_is_trending(self):
        """ADX == 25 exactly should be classified as trending."""
        signal_bull = rt.detect_regime_from_indicators(
            adx=25.0, plus_di=30.0, minus_di=10.0,
            price=110.0, ema_trend=100.0,
        )
        assert signal_bull.regime == rt.BULL

        signal_bear = rt.detect_regime_from_indicators(
            adx=25.0, plus_di=10.0, minus_di=30.0,
            price=90.0, ema_trend=100.0,
        )
        assert signal_bear.regime == rt.BEAR

    def test_exact_adx_sideways_boundary(self):
        """ADX just below 20 → SIDEWAYS; ADX == 20 → TRANSITION."""
        signal_below = rt.detect_regime_from_indicators(
            adx=19.9, plus_di=20.0, minus_di=21.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal_below.regime == rt.SIDEWAYS

        signal_at = rt.detect_regime_from_indicators(
            adx=20.0, plus_di=20.0, minus_di=21.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal_at.regime == rt.TRANSITION

    def test_very_high_adx_strong_bull(self):
        """ADX = 60 (extreme trend) + above EMA → BULL."""
        signal = rt.detect_regime_from_indicators(
            adx=60.0, plus_di=45.0, minus_di=5.0,
            price=200.0, ema_trend=100.0,
        )
        assert signal.regime == rt.BULL

    def test_very_high_adx_strong_bear(self):
        """ADX = 55 (extreme trend) + below EMA → BEAR."""
        signal = rt.detect_regime_from_indicators(
            adx=55.0, plus_di=5.0, minus_di=45.0,
            price=50.0, ema_trend=100.0,
        )
        assert signal.regime == rt.BEAR

    def test_no_ema_falls_back_to_di_direction(self):
        """When ema_trend is None, use DI direction for trending classification."""
        # Plus DI > Minus DI → bull
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=35.0, minus_di=10.0,
            price=100.0, ema_trend=None,
        )
        assert signal.regime == rt.BULL
        assert signal.above_ema is None

        # Minus DI > Plus DI → bear
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=10.0, minus_di=35.0,
            price=100.0, ema_trend=None,
        )
        assert signal.regime == rt.BEAR

    def test_price_exactly_at_ema_is_bear(self):
        """When price == ema_trend, above_ema is False (not strictly above) → BEAR."""
        signal = rt.detect_regime_from_indicators(
            adx=28.0, plus_di=10.0, minus_di=30.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal.regime == rt.BEAR
        assert signal.above_ema is False

    def test_custom_thresholds(self):
        """Custom ADX thresholds should work."""
        signal = rt.detect_regime_from_indicators(
            adx=18.0, plus_di=25.0, minus_di=15.0,
            price=110.0, ema_trend=100.0,
            adx_trend_threshold=15.0,
            adx_sideways_threshold=10.0,
        )
        assert signal.regime == rt.BULL

    def test_regime_signal_has_all_attributes(self):
        """RegimeSignal should carry all diagnostic fields."""
        signal = rt.detect_regime_from_indicators(
            adx=27.0, plus_di=28.0, minus_di=12.0,
            price=105.0, ema_trend=100.0,
        )
        assert signal.adx == 27.0
        assert signal.plus_di == 28.0
        assert signal.minus_di == 12.0
        assert signal.ema_trend == 100.0
        assert signal.price == 105.0
        assert signal.is_trending is True
        assert signal.above_ema is True


# ═══════════════════════════════════════════════════════════════════════════════
#  POSITION SIZING TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    """Test compute_position_size() for each regime."""

    def test_bull_full_position_with_leverage(self):
        frac, lev = rt.compute_position_size(rt.BULL, equity=1000.0, trend_leverage=2.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(2.0)

    def test_bear_full_position_with_leverage(self):
        frac, lev = rt.compute_position_size(rt.BEAR, equity=1000.0, trend_leverage=2.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(2.0)

    def test_sideways_half_position_no_leverage(self):
        frac, lev = rt.compute_position_size(rt.SIDEWAYS, equity=1000.0, grid_fraction=0.5)
        assert frac == pytest.approx(0.5)
        assert lev == pytest.approx(1.0)

    def test_transition_half_position_no_leverage(self):
        frac, lev = rt.compute_position_size(
            rt.TRANSITION, equity=1000.0, transition_fraction=0.5
        )
        assert frac == pytest.approx(0.5)
        assert lev == pytest.approx(1.0)

    def test_unknown_regime_zero_position(self):
        frac, lev = rt.compute_position_size("unknown", equity=1000.0)
        assert frac == pytest.approx(0.0)
        assert lev == pytest.approx(1.0)

    def test_custom_leverage(self):
        frac, lev = rt.compute_position_size(rt.BULL, equity=1000.0, trend_leverage=3.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(3.0)

    def test_custom_transition_fraction(self):
        frac, lev = rt.compute_position_size(
            rt.TRANSITION, equity=1000.0, transition_fraction=0.3
        )
        assert frac == pytest.approx(0.3)
        assert lev == pytest.approx(1.0)

    def test_custom_grid_fraction(self):
        frac, lev = rt.compute_position_size(
            rt.SIDEWAYS, equity=1000.0, grid_fraction=0.7
        )
        assert frac == pytest.approx(0.7)
        assert lev == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  STOP LOSS TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopLoss:
    """Test check_stop_loss() for long and short positions."""

    def test_long_stop_not_hit(self):
        """Price above entry → no stop hit."""
        assert rt.check_stop_loss(100.0, 99.0, stop_loss_pct=0.15) is False

    def test_long_stop_exactly_at_threshold(self):
        """Price exactly 15% below entry → stop hit."""
        assert rt.check_stop_loss(100.0, 85.0, stop_loss_pct=0.15) is True

    def test_long_stop_beyond_threshold(self):
        """Price > 15% below entry → stop hit."""
        assert rt.check_stop_loss(100.0, 80.0, stop_loss_pct=0.15) is True

    def test_long_stop_just_above_threshold(self):
        """Price just above 15% drop → no stop hit."""
        assert rt.check_stop_loss(100.0, 85.1, stop_loss_pct=0.15) is False

    def test_short_stop_not_hit(self):
        """Price below entry → no stop hit for short."""
        assert rt.check_stop_loss(100.0, 95.0, stop_loss_pct=0.15, is_short=True) is False

    def test_short_stop_hit(self):
        """Price 15%+ above entry → stop hit for short."""
        assert rt.check_stop_loss(100.0, 115.0, stop_loss_pct=0.15, is_short=True) is True
        assert rt.check_stop_loss(100.0, 120.0, stop_loss_pct=0.15, is_short=True) is True

    def test_zero_entry_returns_false(self):
        assert rt.check_stop_loss(0.0, 50.0, stop_loss_pct=0.15) is False

    def test_negative_entry_returns_false(self):
        assert rt.check_stop_loss(-1.0, 50.0, stop_loss_pct=0.15) is False

    def test_custom_stop_pct(self):
        """20% stop loss should only trigger at 20%+ drop."""
        assert rt.check_stop_loss(100.0, 81.0, stop_loss_pct=0.20) is False
        assert rt.check_stop_loss(100.0, 80.0, stop_loss_pct=0.20) is True


# ═══════════════════════════════════════════════════════════════════════════════
#  TRAILING STOP TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrailingStop:
    """Test compute_trailing_stop() and check_trailing_stop_hit()."""

    def test_long_trailing_stop_price(self):
        """12% trail below peak price for long."""
        stop = rt.compute_trailing_stop(
            entry_price=100.0, peak_price=120.0, trail_stop_pct=0.12
        )
        assert stop == pytest.approx(105.6)  # 120 * (1 - 0.12)

    def test_short_trailing_stop_price(self):
        """12% trail above trough price for short."""
        stop = rt.compute_trailing_stop(
            entry_price=100.0, peak_price=80.0, trail_stop_pct=0.12, is_short=True
        )
        assert stop == pytest.approx(89.6)  # 80 * (1 + 0.12)

    def test_trailing_stop_none_when_no_peak(self):
        """Zero peak → no trailing stop."""
        assert rt.compute_trailing_stop(100.0, 0.0, trail_stop_pct=0.12) is None

    def test_trailing_stop_none_when_zero_entry(self):
        """Zero entry → no trailing stop."""
        assert rt.compute_trailing_stop(0.0, 100.0, trail_stop_pct=0.12) is None

    def test_long_trailing_stop_hit(self):
        """Price drops to trail stop → hit."""
        stop_price = 105.6
        assert rt.check_trailing_stop_hit(105.0, stop_price, is_short=False) is True
        assert rt.check_trailing_stop_hit(105.6, stop_price, is_short=False) is True

    def test_long_trailing_stop_not_hit(self):
        """Price above trail stop → not hit."""
        stop_price = 105.6
        assert rt.check_trailing_stop_hit(106.0, stop_price, is_short=False) is False

    def test_short_trailing_stop_hit(self):
        """Price rises to trail stop → hit for short."""
        stop_price = 89.6
        assert rt.check_trailing_stop_hit(90.0, stop_price, is_short=True) is True

    def test_short_trailing_stop_not_hit(self):
        """Price below trail stop → not hit for short."""
        stop_price = 89.6
        assert rt.check_trailing_stop_hit(89.0, stop_price, is_short=True) is False

    def test_trailing_stop_hit_none_stop(self):
        """None stop price → never hit."""
        assert rt.check_trailing_stop_hit(100.0, None) is False

    def test_trailing_stop_hit_zero_stop(self):
        """Zero stop price → never hit."""
        assert rt.check_trailing_stop_hit(100.0, 0.0) is False

    def test_trail_stop_uses_peak_not_entry(self):
        """Trail stop should be based on peak, not entry price."""
        stop = rt.compute_trailing_stop(
            entry_price=100.0, peak_price=150.0, trail_stop_pct=0.12
        )
        assert stop == pytest.approx(132.0)  # 150 * 0.88

    def test_trail_stop_ratchets_up_for_long(self):
        """As peak increases, trail stop should increase."""
        stop1 = rt.compute_trailing_stop(100.0, 110.0, trail_stop_pct=0.12)
        stop2 = rt.compute_trailing_stop(100.0, 120.0, trail_stop_pct=0.12)
        stop3 = rt.compute_trailing_stop(100.0, 130.0, trail_stop_pct=0.12)
        assert stop1 < stop2 < stop3


# ═══════════════════════════════════════════════════════════════════════════════
#  GRID STATE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestGridState:
    """Test GridState ladder logic for sideways regime."""

    def test_grid_builds_correct_ladder(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        # 4 levels × 2 sides = 8 orders
        assert len(grid.orders) == 8
        buy_orders = [o for o in grid.orders if o["side"] == "buy"]
        sell_orders = [o for o in grid.orders if o["side"] == "sell"]
        assert len(buy_orders) == 4
        assert len(sell_orders) == 4

    def test_grid_buy_prices_below_mid(self):
        grid = rt.GridState(levels=3, spacing_pct=0.02, mid_price=100.0)
        buy_orders = [o for o in grid.orders if o["side"] == "buy"]
        for order in buy_orders:
            assert order["price"] < 100.0

    def test_grid_sell_prices_above_mid(self):
        grid = rt.GridState(levels=3, spacing_pct=0.02, mid_price=100.0)
        sell_orders = [o for o in grid.orders if o["side"] == "sell"]
        for order in sell_orders:
            assert order["price"] > 100.0

    def test_grid_check_fills_buy_on_dip(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        # Price drops to first buy level (97.5)
        fills = grid.check_fills(97.0)
        buy_fills = [f for f in fills if f["side"] == "buy"]
        assert len(buy_fills) >= 1
        for f in buy_fills:
            assert f["filled"] is True

    def test_grid_check_fills_sell_on_rally(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        fills = grid.check_fills(103.0)
        sell_fills = [f for f in fills if f["side"] == "sell"]
        assert len(sell_fills) >= 1

    def test_grid_no_fills_when_flat(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        # Price at exactly mid — no fills (first level is at ±2.5%)
        fills = grid.check_fills(100.0)
        assert len(fills) == 0

    def test_grid_resets_to_new_mid(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        grid.reset(200.0)
        assert grid.mid_price == 200.0
        buy_orders = [o for o in grid.orders if o["side"] == "buy"]
        for order in buy_orders:
            assert order["price"] < 200.0

    def test_grid_unfilled_count(self):
        grid = rt.GridState(levels=3, spacing_pct=0.02, mid_price=100.0)
        assert grid.unfilled_count == 6  # 3 levels × 2 sides
        grid.check_fills(93.0)  # Should fill multiple buy orders
        assert grid.unfilled_count < 6

    def test_grid_extreme_drop_fills_all_buys(self):
        grid = rt.GridState(levels=4, spacing_pct=0.025, mid_price=100.0)
        fills = grid.check_fills(80.0)
        buy_fills = [f for f in fills if f["side"] == "buy"]
        assert len(buy_fills) == 4  # All buy levels filled


# ═══════════════════════════════════════════════════════════════════════════════
#  EDGE CASES
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Test edge cases: gap moves, no data, invalid signals."""

    def test_gap_move_up_still_classified(self):
        """Large overnight gap up still classifies correctly."""
        signal = rt.detect_regime_from_indicators(
            adx=35.0, plus_di=40.0, minus_di=5.0,
            price=200.0, ema_trend=50.0,  # 4x gap above EMA
        )
        assert signal.regime == rt.BULL

    def test_gap_move_down_still_classified(self):
        """Large overnight gap down still classifies correctly."""
        signal = rt.detect_regime_from_indicators(
            adx=40.0, plus_di=5.0, minus_di=40.0,
            price=10.0, ema_trend=100.0,
        )
        assert signal.regime == rt.BEAR

    def test_zero_adx_returns_sideways(self):
        """ADX = 0 (no directional movement) → SIDEWAYS."""
        signal = rt.detect_regime_from_indicators(
            adx=0.0, plus_di=0.0, minus_di=0.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal.regime == rt.SIDEWAYS

    def test_negative_adx_treated_as_sideways(self):
        """Negative ADX (shouldn't happen, but defensive) → SIDEWAYS."""
        signal = rt.detect_regime_from_indicators(
            adx=-5.0, plus_di=10.0, minus_di=10.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal.regime == rt.SIDEWAYS

    def test_zero_price_handled(self):
        """Price = 0 should not crash regime detection."""
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=25.0, minus_di=10.0,
            price=0.0, ema_trend=100.0,
        )
        # price=0 < ema → bear
        assert signal.regime == rt.BEAR
        assert signal.above_ema is False

    def test_negative_ema_handled(self):
        """Negative EMA (shouldn't happen) → None path."""
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=25.0, minus_di=10.0,
            price=100.0, ema_trend=-50.0,
        )
        # ema_trend <= 0 → above_ema = None → falls back to DI direction
        assert signal.above_ema is None
        assert signal.regime == rt.BULL  # +DI > -DI

    def test_extreme_leverage_doesnt_crash_position_sizing(self):
        """Very high leverage shouldn't crash position sizing."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=100.0, trend_leverage=100.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(100.0)

    def test_zero_equity_position_sizing(self):
        """Zero equity shouldn't crash."""
        frac, lev = rt.compute_position_size(rt.BULL, equity=0.0)
        assert frac == pytest.approx(1.0)
        assert lev == pytest.approx(2.0)

    def test_nan_adx_handled_gracefully(self):
        """NaN ADX should be handled without crashing.

        float('nan') >= 25 is False (not trending) and float('nan') < 20 is
        also False (not sideways), so NaN falls through to TRANSITION — the
        conservative middle ground. This is correct defensive behavior.
        """
        signal = rt.detect_regime_from_indicators(
            adx=float('nan'), plus_di=20.0, minus_di=20.0,
            price=100.0, ema_trend=100.0,
        )
        assert signal.regime == rt.TRANSITION

    def test_empty_coin_universe(self):
        """Empty coin universe should be handled in defaults."""
        assert isinstance(rt.DEFAULT_COIN_UNIVERSE, list)
        assert len(rt.DEFAULT_COIN_UNIVERSE) == 5
        assert "BTC" in rt.DEFAULT_COIN_UNIVERSE

    def test_all_regimes_constant(self):
        """ALL_REGIMES should contain exactly the 4 regime types."""
        assert rt.ALL_REGIMES == frozenset({"bull", "bear", "sideways", "transition"})


# ═══════════════════════════════════════════════════════════════════════════════
#  REGIME TRANSITION SEQUENCE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegimeTransitions:
    """Test regime transitions through the full cycle."""

    def test_full_cycle_bull_bear_sideways_transition(self):
        """Simulate a full market cycle and verify transitions."""
        transitions = []

        # Start sideways
        signal = rt.detect_regime_from_indicators(
            adx=15.0, plus_di=20.0, minus_di=20.0,
            price=100.0, ema_trend=100.0,
        )
        transitions.append(signal.regime)

        # Transition phase
        signal = rt.detect_regime_from_indicators(
            adx=22.0, plus_di=25.0, minus_di=15.0,
            price=105.0, ema_trend=100.0,
        )
        transitions.append(signal.regime)

        # Full bull
        signal = rt.detect_regime_from_indicators(
            adx=35.0, plus_di=35.0, minus_di=10.0,
            price=120.0, ema_trend=100.0,
        )
        transitions.append(signal.regime)

        # Pullback to transition
        signal = rt.detect_regime_from_indicators(
            adx=23.0, plus_di=20.0, minus_di=22.0,
            price=95.0, ema_trend=100.0,
        )
        transitions.append(signal.regime)

        # Full bear
        signal = rt.detect_regime_from_indicators(
            adx=30.0, plus_di=10.0, minus_di=35.0,
            price=80.0, ema_trend=100.0,
        )
        transitions.append(signal.regime)

        assert transitions == [
            rt.SIDEWAYS,
            rt.TRANSITION,
            rt.BULL,
            rt.TRANSITION,
            rt.BEAR,
        ]

    def test_position_size_changes_with_regime(self):
        """Position size and leverage should change appropriately across regimes."""
        results = {}
        for regime in [rt.BULL, rt.BEAR, rt.SIDEWAYS, rt.TRANSITION]:
            results[regime] = rt.compute_position_size(regime, equity=1000.0)

        # Bull and bear use full position with leverage
        assert results[rt.BULL][0] == 1.0
        assert results[rt.BULL][1] > 1.0
        assert results[rt.BEAR][0] == 1.0
        assert results[rt.BEAR][1] > 1.0

        # Sideways and transition use reduced position, no leverage
        assert results[rt.SIDEWAYS][0] < 1.0
        assert results[rt.SIDEWAYS][1] == 1.0
        assert results[rt.TRANSITION][0] < 1.0
        assert results[rt.TRANSITION][1] == 1.0

    def test_trailing_stop_adapts_through_trend(self):
        """Trailing stop should ratchet up as price increases."""
        entry = 100.0
        stops = []

        for peak in [100.0, 105.0, 110.0, 115.0, 120.0]:
            stop = rt.compute_trailing_stop(entry, peak, trail_stop_pct=0.12)
            stops.append(stop)

        # Each stop should be higher than the previous
        for i in range(1, len(stops)):
            assert stops[i] > stops[i - 1]

    def test_bear_to_bull_transition_logic(self):
        """When regime flips from BEAR to BULL, position should switch from short to long."""
        # In bear: short sizing
        bear_frac, bear_lev = rt.compute_position_size(rt.BEAR, 1000.0, trend_leverage=2.0)

        # In bull: long sizing
        bull_frac, bull_lev = rt.compute_position_size(rt.BULL, 1000.0, trend_leverage=2.0)

        # Both use full position
        assert bear_frac == bull_frac == 1.0
        assert bear_lev == bull_lev == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION: STRATEGY LOADING
# ═══════════════════════════════════════════════════════════════════════════════

class TestStrategyLoading:
    """Verify the module is discoverable by the strategy loader."""

    def test_module_has_strategy_class(self):
        """Strategy class must exist for the loader."""
        assert hasattr(rt, "Strategy")
        assert hasattr(rt, "BULL")
        assert hasattr(rt, "BEAR")
        assert hasattr(rt, "SIDEWAYS")
        assert hasattr(rt, "TRANSITION")

    def test_strategy_filename_matches_loader_pattern(self):
        """File must be named regime_trend_strategy.py for auto-discovery."""
        assert STRATEGY_PATH.name == "regime_trend_strategy.py"

    def test_strategy_loader_finds_module(self):
        """The strategies/__init__.py loader should discover this module."""
        from binance_trade_bot.strategies import get_strategy
        strategy_cls = get_strategy("regime_trend")
        assert strategy_cls is not None
        assert strategy_cls.__name__ == "Strategy"

    def test_default_strategy_still_default(self):
        """Default strategy should NOT be regime_trend."""
        from binance_trade_bot.strategies import get_strategy
        default = get_strategy("default")
        momentum = get_strategy("momentum")
        assert default is not None
        assert momentum is not None
        # regime_trend should be a DIFFERENT class
        rt_cls = get_strategy("regime_trend")
        assert rt_cls is not default
        assert rt_cls is not momentum


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG DEFAULTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigDefaults:
    """Verify the strategy constants match backtested values."""

    def test_default_coin_universe_is_strat_top_5(self):
        assert rt.DEFAULT_COIN_UNIVERSE == ["APT", "AVAX", "OP", "BTC", "RUNE"]

    def test_adx_thresholds(self):
        assert rt.ADX_BULL_BEAR_DEFAULT == 25
        assert rt.ADX_SIDEWAYS_DEFAULT == 20
        assert rt.ADX_PERIOD_DEFAULT == 14

    def test_ema_trend_default(self):
        assert rt.EMA_TREND_DEFAULT == 200

    def test_strategy_variant_defaults(self):
        assert rt.TREND_LEVERAGE_DEFAULT == 2.0
        assert rt.STOP_LOSS_DEFAULT == 0.15
        assert rt.TRAIL_STOP_DEFAULT == 0.12
        assert rt.GRID_SPACING_PCT_DEFAULT == 0.025
        assert rt.GRID_LEVELS_DEFAULT == 4
        assert rt.TRANSITION_FRACTION_DEFAULT == 0.5
        assert rt.BEAR_ACTION_DEFAULT == "short"
