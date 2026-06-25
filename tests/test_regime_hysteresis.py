"""Regression tests for live regime-change hysteresis."""

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REGIME_PATH = REPO_ROOT / "binance_trade_bot" / "regime_hysteresis.py"


def load_regime_module():
    spec = importlib.util.spec_from_file_location("regime_hysteresis_test", REGIME_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_regime_change_requires_consecutive_confirmations():
    module = load_regime_module()
    hysteresis = module.RegimeHysteresis(active="sideways", confirmations=3)

    first = hysteresis.observe("bear")
    second = hysteresis.observe("bear")
    third = hysteresis.observe("bear")

    assert first.active == "sideways"
    assert first.changed is False
    assert first.pending == "bear"
    assert first.pending_count == 1

    assert second.active == "sideways"
    assert second.changed is False
    assert second.pending_count == 2

    assert third.active == "bear"
    assert third.changed is True
    assert third.previous == "sideways"
    assert third.pending is None


def test_regime_candidate_reset_prevents_one_reading_whipsaw():
    module = load_regime_module()
    hysteresis = module.RegimeHysteresis(active="bear", confirmations=2)

    sideways_probe = hysteresis.observe("sideways")
    back_to_bear = hysteresis.observe("bear")
    second_sideways = hysteresis.observe("sideways")

    assert sideways_probe.active == "bear"
    assert sideways_probe.pending == "sideways"
    assert back_to_bear.active == "bear"
    assert back_to_bear.pending is None
    assert second_sideways.active == "bear"
    assert second_sideways.pending_count == 1


def test_one_confirmation_preserves_immediate_transition_for_tests_or_manual_override():
    module = load_regime_module()
    hysteresis = module.RegimeHysteresis(active="sideways", confirmations=1)

    result = hysteresis.observe("bull")

    assert result.changed is True
    assert result.previous == "sideways"
    assert result.active == "bull"
