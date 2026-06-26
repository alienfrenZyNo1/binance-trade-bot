"""Tests for the research-only BEAR futures backtester."""

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "research_bear_futures_backtester.py"
HOUR_MS = 3600 * 1000


def load_module():
    spec = importlib.util.spec_from_file_location("bear_futures_backtester_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candle(i, close, high=None, low=None):
    high = high if high is not None else close * 1.01
    low = low if low is not None else close * 0.99
    return {
        "ts": i * HOUR_MS,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
    }


def test_rank_short_candidates_prefers_weakest_negative_momentum_with_oi_context():
    module = load_module()
    market = {
        "ENAUSDC": {
            "candles": [candle(0, 100), candle(1, 96), candle(2, 92)],
            "open_interest": [
                {"timestamp": 0, "sumOpenInterestValue": "1000"},
                {"timestamp": HOUR_MS, "sumOpenInterestValue": "1120"},
            ],
        },
        "SOLUSDC": {
            "candles": [candle(0, 100), candle(1, 99), candle(2, 101)],
            "open_interest": [
                {"timestamp": 0, "sumOpenInterestValue": "1000"},
                {"timestamp": HOUR_MS, "sumOpenInterestValue": "980"},
            ],
        },
    }

    ranked = module.rank_short_candidates(market, lookback_hours=2)

    assert ranked[0]["symbol"] == "ENAUSDC"
    assert ranked[0]["momentum_pct"] < ranked[1]["momentum_pct"]
    assert ranked[0]["oi_value_change_pct"] == 12.0
    assert ranked[0]["eligible"] is True
    assert ranked[1]["eligible"] is False


def test_simulate_short_counts_positive_funding_as_income_and_trailing_exit():
    module = load_module()
    candles = [
        candle(0, 100, high=101, low=99),
        candle(1, 96, high=97, low=94),  # activates 3% profit trailing
        candle(2, 94, high=95, low=92),  # new best price
        candle(3, 95, high=96, low=94),  # 1% callback from 92 triggers near 92.92
    ]
    funding = [
        {"fundingTime": HOUR_MS, "fundingRate": "0.0001"},
        {"fundingTime": 2 * HOUR_MS, "fundingRate": "0.0001"},
    ]
    cfg = module.BacktestConfig(
        initial_balance=1000,
        leverage=2,
        max_margin_pct=0.5,
        fee_rate=0.0,
        slippage_pct=0.0,
        stop_loss_pct=15.0,
        trailing_activation_pct=3.0,
        trailing_callback_pct=1.0,
    )

    result = module.simulate_short("ENAUSDC", candles, funding_rates=funding, config=cfg)

    assert result["exit_reason"] == "trailing_stop"
    assert result["pnl_pct"] > 0
    assert result["funding_pnl"] > 0
    assert result["fees"] == 0
    assert result["trailing_exits"] == 1
    assert result["min_liquidation_buffer_pct"] > 0


def test_run_backtest_enters_after_point_in_time_bearish_signal_not_future_lookahead():
    module = load_module()
    market = {
        "ENAUSDC": {
            # First valid 2h lookback at t=2 is bullish: 100 -> 120.
            # Bearish signal only appears at t=3: 110 -> 80.
            "candles": [
                candle(0, 100, high=101, low=99),
                candle(1, 110, high=111, low=109),
                candle(2, 120, high=121, low=119),
                candle(3, 80, high=82, low=75),
                candle(4, 75, high=76, low=70),
            ],
            "open_interest": [
                {"timestamp": 0, "sumOpenInterestValue": "1000"},
                {"timestamp": HOUR_MS, "sumOpenInterestValue": "1000"},
                {"timestamp": 2 * HOUR_MS, "sumOpenInterestValue": "1000"},
                {"timestamp": 3 * HOUR_MS, "sumOpenInterestValue": "1100"},
            ],
            "funding": [],
        }
    }
    cfg = module.BacktestConfig(
        initial_balance=1000,
        leverage=1,
        max_margin_pct=0.5,
        fee_rate=0.0,
        slippage_pct=0.0,
        lookback_hours=2,
    )

    payload = module.run_backtest(market, cfg)

    assert payload["records"]
    assert payload["records"][0]["entry_ts"] == 3 * HOUR_MS
    assert payload["records"][0]["candidate"]["momentum_pct"] < 0


def test_simulate_short_hard_stop_limits_rising_market_loss_after_fees_and_slippage():
    module = load_module()
    candles = [
        candle(0, 100, high=101, low=99),
        candle(1, 112, high=116, low=110),  # crosses 10% hard stop
        candle(2, 120, high=121, low=118),
    ]
    cfg = module.BacktestConfig(
        initial_balance=1000,
        leverage=2,
        max_margin_pct=0.4,
        fee_rate=0.001,
        slippage_pct=0.001,
        stop_loss_pct=10.0,
        trailing_activation_pct=3.0,
        trailing_callback_pct=1.0,
    )

    result = module.simulate_short("SOLUSDC", candles, funding_rates=[], config=cfg)

    assert result["exit_reason"] == "stop_loss"
    assert result["pnl_pct"] < 0
    assert result["stop_loss_exits"] == 1
    assert result["fees"] > 0
    assert result["max_drawdown_pct"] > 0
    assert result["account_stop_risk_pct"] == 8.0


def test_missing_oi_is_unknown_and_does_not_pass_real_filter():
    module = load_module()
    candles = [candle(0, 100), candle(1, 96), candle(2, 92)]

    real_filter = module._candidate_from_window(
        "ENAUSDC",
        candles,
        [],
        lookback_hours=2,
        min_oi_change_pct=0.0,
    )
    no_filter = module._candidate_from_window(
        "ENAUSDC",
        candles,
        [],
        lookback_hours=2,
        min_oi_change_pct=-1000.0,
    )

    assert real_filter["oi_known"] is False
    assert real_filter["eligible"] is False
    assert no_filter["eligible"] is True
