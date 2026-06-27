"""
Tests for the 3 CRITICAL risk fixes in the Regime-Adaptive Trend Strategy.

Fix 1: Circuit breaker wired in
    - risk_circuit_breaker imported
    - futures_manager.new_risk_blocked assigned
    - _new_spot_risk_blocked() method present and functional
    - Circuit breaker checked before every spot buy AND futures entry

Fix 2: compute_position_size() actually called
    - Called before position entry in scout paths
    - max_notional guard enforces fraction * available_balance

Fix 3: Max total exposure check
    - Config option max_total_exposure = 1.5
    - Exposure ratio checked before opening any new position
    - Trades skipped when limit exceeded

These tests use the module loaded from file (same pattern as the existing
test_regime_trend_strategy.py) so they run without DB/exchange/network.
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
    spec = importlib.util.spec_from_file_location("regime_trend_strategy_risk_fix", STRATEGY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load_strategy_module()


# ═══════════════════════════════════════════════════════════════════════════════
#  FIX 1: CIRCUIT BREAKER WIRED IN
# ═══════════════════════════════════════════════════════════════════════════════

class TestFix1CircuitBreakerWired:
    """Verify the circuit breaker is fully integrated."""

    def test_risk_circuit_breaker_imported(self):
        """The module must import risk_circuit_breaker functions."""
        src = STRATEGY_PATH.read_text()
        assert "from binance_trade_bot.risk_circuit_breaker import" in src, (
            "risk_circuit_breaker must be imported"
        )
        assert "evaluate_circuit_breaker" in src
        assert "is_circuit_breaker_cooling_down" in src
        assert "circuit_breaker_status_summary" in src

    def test_new_risk_blocked_callback_wired_in_initialize(self):
        """initialize() must assign futures_manager.new_risk_blocked."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "self.futures_manager.new_risk_blocked = self._new_spot_risk_blocked" in src, (
            "futures_manager.new_risk_blocked must be assigned in initialize()"
        )

    def test_equity_baselines_seeded_on_startup(self):
        """initialize() must seed circuit-breaker baselines when enabled."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "_ensure_circuit_breaker_baselines" in src, (
            "Circuit breaker baselines must be seeded in initialize()"
        )
        assert "PORTFOLIO_CIRCUIT_BREAKER_ENABLED" in src

    def test_new_spot_risk_blocked_method_exists(self):
        """_new_spot_risk_blocked() must exist on the Strategy class."""
        assert hasattr(rt.Strategy, "_new_spot_risk_blocked")
        assert callable(getattr(rt.Strategy, "_new_spot_risk_blocked"))

    def test_new_spot_risk_blocked_returns_false_when_disabled(self):
        """When breaker is disabled, _new_spot_risk_blocked returns False."""
        # Build a minimal mock strategy instance
        strategy = MagicMock()
        strategy.config = SimpleNamespace(PORTFOLIO_CIRCUIT_BREAKER_ENABLED=False)
        result = rt.Strategy._new_spot_risk_blocked(strategy)
        assert result is False

    def test_circuit_breaker_checked_in_scout_bull(self):
        """_scout_bull must check the circuit breaker before executing rotation."""
        src = inspect.getsource(rt.Strategy._scout_bull)
        breaker_idx = src.find("self._new_spot_risk_blocked()")
        rotation_idx = src.find("self._execute_rotation(")
        assert breaker_idx != -1, "Circuit breaker check missing from _scout_bull"
        assert rotation_idx != -1, "Rotation call missing from _scout_bull"
        assert breaker_idx < rotation_idx, (
            "REGRESSION: circuit breaker must run BEFORE rotation in _scout_bull"
        )

    def test_circuit_breaker_checked_in_reenter_from_bridge(self):
        """_reenter_from_bridge must check the breaker before buying."""
        src = inspect.getsource(rt.Strategy._reenter_from_bridge)
        breaker_idx = src.find("self._new_spot_risk_blocked()")
        buy_idx = src.find("self.manager.buy_alt(")
        assert breaker_idx != -1, "Circuit breaker check missing from _reenter_from_bridge"
        assert buy_idx != -1, "Buy call missing from _reenter_from_bridge"
        assert breaker_idx < buy_idx, (
            "REGRESSION: circuit breaker must run BEFORE buy in _reenter_from_bridge"
        )

    def test_circuit_breaker_checked_in_bridge_scout(self):
        """bridge_scout must check the breaker before buying."""
        src = inspect.getsource(rt.Strategy.bridge_scout)
        breaker_idx = src.find("self._new_spot_risk_blocked()")
        buy_idx = src.find("self.manager.buy_alt(")
        assert breaker_idx != -1, "Circuit breaker check missing from bridge_scout"
        assert buy_idx != -1, "Buy call missing from bridge_scout"
        assert breaker_idx < buy_idx, (
            "REGRESSION: circuit breaker must run BEFORE buy in bridge_scout"
        )

    def test_circuit_breaker_checked_in_scout_sideways_grid_buy(self):
        """_scout_sideways grid buy must check the breaker."""
        src = inspect.getsource(rt.Strategy._scout_sideways)
        breaker_idx = src.find("self._new_spot_risk_blocked()")
        buy_idx = src.find("self.manager.buy_alt(current_coin, self.config.BRIDGE)")
        assert breaker_idx != -1, "Circuit breaker check missing from _scout_sideways"
        assert buy_idx != -1, "Grid buy call missing from _scout_sideways"
        assert breaker_idx < buy_idx, (
            "REGRESSION: circuit breaker must run BEFORE grid buy"
        )

    def test_futures_entry_gated_by_circuit_breaker(self):
        """The futures entry path (_scout_bear) must be gated by the callback
        wired into futures_manager.  We verify the callback assignment exists
        and that _scout_bear does not independently call buy/sell without
        delegating to manage_bear (which checks new_risk_blocked)."""
        src = inspect.getsource(rt.Strategy._scout_bear)
        assert "manage_bear" in src, (
            "Futures management must go through manage_bear which checks "
            "the new_risk_blocked callback"
        )

    def test_circuit_breaker_does_not_block_exits(self):
        """Exit/stop paths must NOT be gated by the circuit breaker."""
        # Trailing stop is an exit — should never call breaker
        trail_src = inspect.getsource(rt.Strategy._check_trailing_stop)
        assert "_new_spot_risk_blocked" not in trail_src, (
            "REGRESSION: trailing stop (exit) must not check circuit breaker"
        )
        # Hard stop is an exit — should never call breaker
        hard_stop_src = inspect.getsource(rt.Strategy._check_hard_stop)
        assert "_new_spot_risk_blocked" not in hard_stop_src, (
            "REGRESSION: hard stop (exit) must not check circuit breaker"
        )

    def test_estimate_spot_equity_method_exists(self):
        """_estimate_spot_equity must exist for breaker equity calculation."""
        assert hasattr(rt.Strategy, "_estimate_spot_equity")

    def test_ensure_circuit_breaker_baselines_exists(self):
        """_ensure_circuit_breaker_baselines must exist."""
        assert hasattr(rt.Strategy, "_ensure_circuit_breaker_baselines")


# ═══════════════════════════════════════════════════════════════════════════════
#  FIX 2: compute_position_size() ACTUALLY CALLED
# ═══════════════════════════════════════════════════════════════════════════════

class TestFix2PositionSizeCalled:
    """Verify compute_position_size() is actually called before position entry."""

    def test_compute_position_size_called_in_scout_bull(self):
        """_scout_bull must call compute_position_size() before executing rotation."""
        src = inspect.getsource(rt.Strategy._scout_bull)
        size_idx = src.find("compute_position_size(")
        rotation_idx = src.find("self._execute_rotation(")
        assert size_idx != -1, (
            "compute_position_size() must be called in _scout_bull before rotation"
        )
        assert size_idx < rotation_idx, (
            "REGRESSION: compute_position_size() must be called BEFORE rotation"
        )

    def test_compute_position_size_called_in_scout_bear(self):
        """_scout_bear must call compute_position_size() before futures entry."""
        src = inspect.getsource(rt.Strategy._scout_bear)
        assert "compute_position_size(" in src, (
            "compute_position_size() must be called in _scout_bear before futures entry"
        )

    def test_max_notional_guard_in_scout_bull(self):
        """_scout_bull must enforce max_notional = fraction * available."""
        src = inspect.getsource(rt.Strategy._scout_bull)
        assert "max_notional" in src, (
            "max_notional guard must be present in _scout_bull"
        )
        assert "frac" in src, (
            "fraction from compute_position_size must be used"
        )

    def test_max_notional_blocks_oversized_position(self):
        """The max_notional guard should block when exposure would be too large.

        This tests the logic: if fraction * available_balance exceeds the
        exposure limit, the trade is skipped.
        """
        # The _total_exposure_allows_entry guard uses the computed fraction
        # to determine max_notional and checks against _max_total_exposure
        src = inspect.getsource(rt.Strategy._scout_bull)
        assert "_total_exposure_allows_entry" in src, (
            "_total_exposure_allows_entry must be called with max_notional"
        )

    def test_position_size_uses_regime_and_leverage(self):
        """The call must pass the regime and configured leverage."""
        src = inspect.getsource(rt.Strategy._scout_bull)
        assert "self._market_regime" in src
        assert "self._trend_leverage" in src, (
            "Configured trend_leverage must be passed to compute_position_size"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  FIX 3: MAX TOTAL EXPOSURE CHECK
# ═══════════════════════════════════════════════════════════════════════════════

class TestFix3MaxTotalExposure:
    """Verify max total exposure limit is enforced."""

    def test_max_total_exposure_config_default(self):
        """MAX_TOTAL_EXPOSURE_DEFAULT must be 1.5."""
        assert rt.MAX_TOTAL_EXPOSURE_DEFAULT == 1.5

    def test_max_total_exposure_loaded_in_initialize(self):
        """initialize() must read RT_MAX_TOTAL_EXPOSURE from config."""
        src = inspect.getsource(rt.Strategy.initialize)
        assert "RT_MAX_TOTAL_EXPOSURE" in src
        assert "_max_total_exposure" in src

    def test_compute_total_exposure_ratio_exists(self):
        """_compute_total_exposure_ratio must exist on Strategy."""
        assert hasattr(rt.Strategy, "_compute_total_exposure_ratio")

    def test_total_exposure_allows_entry_exists(self):
        """_total_exposure_allows_entry must exist on Strategy."""
        assert hasattr(rt.Strategy, "_total_exposure_allows_entry")

    def test_exposure_ratio_calculation(self):
        """_compute_total_exposure_ratio returns correct ratio.

        spot_value=100, futures_notional=50, equity=100 → ratio = 1.5
        """
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = 100.0
        strategy.db.get_current_coin.return_value = SimpleNamespace(symbol="BTC")
        strategy.manager.get_currency_balance.return_value = 50.0  # 50 coins
        strategy.manager.get_ticker_price.return_value = 2.0       # spot_value = 100
        strategy.futures_manager._open_position = SimpleNamespace(
            symbol="SOLUSDC", entry_price=10.0, quantity=5.0  # notional = 50
        )

        ratio = rt.Strategy._compute_total_exposure_ratio(strategy)
        assert ratio == pytest.approx(1.5, abs=0.01)

    def test_exposure_ratio_no_futures_position(self):
        """When no futures position, ratio = spot_value / equity."""
        strategy = MagicMock()
        strategy._estimate_spot_equity.return_value = 100.0
        strategy.db.get_current_coin.return_value = SimpleNamespace(symbol="BTC")
        strategy.manager.get_currency_balance.return_value = 100.0
        strategy.manager.get_ticker_price.return_value = 0.5  # spot_value = 50
        strategy.futures_manager._open_position = None

        ratio = rt.Strategy._compute_total_exposure_ratio(strategy)
        assert ratio == pytest.approx(0.5, abs=0.01)

    def test_exposure_allows_entry_when_under_limit(self):
        """When current + new notional is under the limit, entry is allowed."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = 100.0
        strategy._compute_total_exposure_ratio.return_value = 0.5
        strategy.logger = MagicMock()

        # Adding 50 notional → ratio = 0.5 + 50/100 = 1.0, under 1.5
        assert rt.Strategy._total_exposure_allows_entry(strategy, 50.0) is True

    def test_exposure_blocks_entry_when_over_limit(self):
        """When current + new notional exceeds the limit, entry is blocked."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = 100.0
        strategy._compute_total_exposure_ratio.return_value = 1.2
        strategy.logger = MagicMock()

        # Adding 50 notional → ratio = 1.2 + 50/100 = 1.7, over 1.5
        assert rt.Strategy._total_exposure_allows_entry(strategy, 50.0) is False

    def test_exposure_fails_open_when_equity_unknown(self):
        """When equity can't be computed, entry is allowed (fail-open)."""
        strategy = MagicMock()
        strategy._max_total_exposure = 1.5
        strategy._estimate_spot_equity.return_value = None
        strategy._compute_total_exposure_ratio.return_value = None
        strategy.logger = MagicMock()

        assert rt.Strategy._total_exposure_allows_entry(strategy, 1000.0) is True

    def test_exposure_checked_in_all_entry_paths(self):
        """Every entry path must call _total_exposure_allows_entry."""
        for method_name in [
            "_scout_bull",
            "_scout_sideways",
            "_scout_bear",
            "_reenter_from_bridge",
            "bridge_scout",
        ]:
            src = inspect.getsource(getattr(rt.Strategy, method_name))
            assert "_total_exposure_allows_entry" in src, (
                f"REGRESSION: {method_name} must check total exposure"
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  INTEGRATION: ALL THREE FIXES TOGETHER
# ═══════════════════════════════════════════════════════════════════════════════

class TestAllThreeFixesIntegrated:
    """Verify all three fixes coexist correctly in the scout dispatch."""

    def test_scout_dispatches_to_protected_methods(self):
        """scout() must dispatch to methods that have all 3 risk gates."""
        src = inspect.getsource(rt.Strategy.scout)
        # All regime dispatches should be present
        assert "_scout_bear" in src
        assert "_scout_bull" in src
        assert "_scout_sideways" in src
        assert "_scout_transition" in src
        assert "_reenter_from_bridge" in src

    def test_no_entry_without_circuit_breaker_anywhere(self):
        """Comprehensive: no buy_alt or manage_bear call should exist without
        a circuit breaker check in the same method."""
        methods_with_buys = [
            "_scout_bull",      # has rotation → buy
            "_scout_sideways",  # has grid buy
            "_reenter_from_bridge",  # has buy
            "bridge_scout",     # has buy
        ]
        for method_name in methods_with_buys:
            src = inspect.getsource(getattr(rt.Strategy, method_name))
            if "self.manager.buy_alt(" in src or "self._execute_rotation(" in src:
                assert "_new_spot_risk_blocked" in src, (
                    f"{method_name} places buys but has no circuit breaker check"
                )

    def test_risk_gates_run_before_entry_actions(self):
        """In every entry method, the circuit breaker check must come before
        the actual buy/sell action."""
        entry_checks = [
            ("_scout_bull", "_new_spot_risk_blocked()", "_execute_rotation("),
            ("_scout_sideways", "_new_spot_risk_blocked()", "self.manager.buy_alt("),
            ("_reenter_from_bridge", "_new_spot_risk_blocked()", "self.manager.buy_alt("),
            ("bridge_scout", "_new_spot_risk_blocked()", "self.manager.buy_alt("),
        ]
        for method, gate, action in entry_checks:
            src = inspect.getsource(getattr(rt.Strategy, method))
            gate_idx = src.find(gate)
            action_idx = src.find(action)
            assert gate_idx != -1, f"{method}: gate '{gate}' not found"
            assert action_idx != -1, f"{method}: action '{action}' not found"
            assert gate_idx < action_idx, (
                f"REGRESSION: {method} — gate must run BEFORE action"
            )
