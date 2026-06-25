"""Regression tests for standard Wilder ADX calculations."""

import importlib.util
import math
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INDICATORS_PATH = REPO_ROOT / "binance_trade_bot" / "indicators.py"


def load_indicators_module():
    spec = importlib.util.spec_from_file_location("indicators_adx_test", INDICATORS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_wilder_adx(highs, lows, closes, period=14):
    """Independent reference implementation of Wilder's ADX smoothing."""
    if len(closes) < period * 2 + 1:
        return 0.0, 0.0, 0.0

    true_ranges = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(closes)):
        true_ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    atr = sum(true_ranges[:period])
    smooth_plus = sum(plus_dm[:period])
    smooth_minus = sum(minus_dm[:period])
    dx_values = []

    for i in range(period, len(true_ranges)):
        if i > period:
            atr = atr - (atr / period) + true_ranges[i]
            smooth_plus = smooth_plus - (smooth_plus / period) + plus_dm[i]
            smooth_minus = smooth_minus - (smooth_minus / period) + minus_dm[i]

        if atr == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * smooth_plus / atr
        minus_di = 100 * smooth_minus / atr
        denominator = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)

    if not dx_values:
        return 0.0, 0.0, 0.0

    adx = sum(dx_values[:period]) / min(period, len(dx_values))
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    plus_di = 100 * smooth_plus / atr if atr else 0.0
    minus_di = 100 * smooth_minus / atr if atr else 0.0
    return adx, plus_di, minus_di


def test_compute_adx_uses_wilders_smoothed_dx_not_chunk_average():
    module = load_indicators_module()
    closes = [100 + i * 0.5 for i in range(30)]
    closes += [115 + ((-1) ** i) * (i % 5) * 0.3 for i in range(40)]
    highs = [close + 1 + (i % 4) * 0.1 for i, close in enumerate(closes)]
    lows = [close - 1 - (i % 5) * 0.1 for i, close in enumerate(closes)]

    actual = module.compute_adx(highs, lows, closes, period=14)
    expected = canonical_wilder_adx(highs, lows, closes, period=14)

    assert math.isclose(actual[0], expected[0], abs_tol=0.001)
    assert math.isclose(actual[1], expected[1], abs_tol=0.001)
    assert math.isclose(actual[2], expected[2], abs_tol=0.001)
    assert actual[0] < 20, "choppy post-trend fixture should not be classified as strongly trending"


def test_compute_adx_returns_zero_for_flat_market():
    module = load_indicators_module()
    closes = [100.0] * 40
    highs = [100.0] * 40
    lows = [100.0] * 40

    assert module.compute_adx(highs, lows, closes, period=14) == (0.0, 0.0, 0.0)
