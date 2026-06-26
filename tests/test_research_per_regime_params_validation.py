"""Tests for per-regime parameter validation helpers."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_per_regime_params.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("research_per_regime_params_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_expanded_param_grid_covers_sensitivity_surface():
    module = load_module()

    grid = module.build_param_grid()

    assert (4, 3.0) in grid
    assert (48, 12.0) in grid
    assert (18, 8.0) in grid
    assert len(grid) == 9 * 5


def test_plateau_assessment_rejects_sharp_single_parameter_spike():
    module = load_module()
    results = {
        (6, 5.0): {"return_pct": 100.0},
        (4, 5.0): {"return_pct": 10.0},
        (8, 5.0): {"return_pct": 15.0},
        (6, 3.0): {"return_pct": 12.0},
        (6, 8.0): {"return_pct": 8.0},
    }

    assessment = module.assess_parameter_plateau(results, 6, 5.0, min_neighbor_ratio=0.70)

    assert assessment["robust"] is False
    assert assessment["candidate_return_pct"] == 100.0
    assert assessment["robust_neighbor_count"] == 0


def test_plateau_assessment_accepts_broad_robust_region():
    module = load_module()
    results = {
        (6, 5.0): {"return_pct": 100.0},
        (4, 5.0): {"return_pct": 78.0},
        (8, 5.0): {"return_pct": 85.0},
        (6, 3.0): {"return_pct": 72.0},
        (6, 8.0): {"return_pct": 70.0},
    }

    assessment = module.assess_parameter_plateau(results, 6, 5.0, min_neighbor_ratio=0.70)

    assert assessment["robust"] is True
    assert assessment["robust_neighbor_count"] >= 3
    assert assessment["worst_neighbor_return_pct"] == 70.0


def test_walk_forward_windows_are_chronological_and_non_overlapping():
    module = load_module()
    timestamps = [i * HOUR_MS for i in range(100)]

    windows = module.build_walk_forward_windows(
        timestamps,
        train_hours=24,
        test_hours=12,
        step_hours=12,
        count=3,
    )

    assert len(windows) == 3
    assert [w["label"] for w in windows] == ["w1", "w2", "w3"]
    assert windows == sorted(windows, key=lambda w: w["test_start"])
    for prev, cur in zip(windows, windows[1:]):
        assert prev["test_end"] < cur["test_start"]
    for window in windows:
        assert window["train_start"] <= window["train_end"] < window["test_start"] <= window["test_end"]


def test_plateau_assessment_fails_closed_for_losing_candidate():
    module = load_module()
    results = {
        (6, 5.0): {"return_pct": -10.0},
        (4, 5.0): {"return_pct": -5.0},
        (8, 5.0): {"return_pct": -4.0},
        (6, 3.0): {"return_pct": -3.0},
    }

    assessment = module.assess_parameter_plateau(results, 6, 5.0)

    assert assessment["robust"] is False
    assert assessment["robust_neighbor_count"] == 0


def test_select_best_result_requires_minimum_trade_count():
    module = load_module()
    results = [
        (6, 12.0, {"return_pct": 120.0, "trades": 2}),
        (36, 3.0, {"return_pct": 70.0, "trades": 35}),
        (18, 8.0, {"return_pct": 12.0, "trades": 17}),
    ]

    best = module.select_best_result(results, min_trades=15)

    assert best[0] == 36
    assert best[1] == 3.0
    assert best[2]["return_pct"] == 70.0


def test_format_best_line_exposes_trade_count_and_floor_warning():
    module = load_module()
    best = (6, 12.0, {"return_pct": 120.0, "trades": 2})
    plateau = {"robust": False}

    line = module.format_best_line(best, plateau, min_trades=15)

    assert "trades=2" in line
    assert "BELOW_MIN_TRADES" in line
    assert "SPIKE" in line


def test_format_best_line_marks_robust_plateau():
    module = load_module()
    best = (18, 8.0, {"return_pct": 42.0, "trades": 22})
    plateau = {"robust": True}

    line = module.format_best_line(best, plateau, min_trades=15)

    assert "trades=22" in line
    assert "PLATEAU" in line
    assert "BELOW_MIN_TRADES" not in line
