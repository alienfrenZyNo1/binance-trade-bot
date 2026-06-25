"""Regression tests for deposit detection around internal futures transfers."""

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING_PATH = REPO_ROOT / "binance_trade_bot" / "accounting.py"


def load_accounting_module():
    spec = importlib.util.spec_from_file_location("accounting_test", ACCOUNTING_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_spot_balance_increase_is_deposit():
    module = load_accounting_module()

    result = module.evaluate_deposit_delta(
        last_balance=10.0,
        current_balance=66.25,
        suppress_once=False,
        min_threshold=1.0,
    )

    assert result.deposit_amount == 56.25
    assert result.new_baseline == 66.25
    assert result.suppression_consumed is False


def test_internal_futures_to_spot_transfer_updates_baseline_without_deposit():
    module = load_accounting_module()

    result = module.evaluate_deposit_delta(
        last_balance=0.0,
        current_balance=56.25,
        suppress_once=True,
        min_threshold=1.0,
    )

    assert result.deposit_amount == 0.0
    assert result.new_baseline == 56.25
    assert result.suppression_consumed is True


def test_first_observed_balance_seeds_baseline_without_deposit():
    module = load_accounting_module()

    result = module.evaluate_deposit_delta(
        last_balance=0.0,
        current_balance=54.87,
        suppress_once=False,
        min_threshold=1.0,
    )

    assert result.deposit_amount == 0.0
    assert result.new_baseline == 54.87
