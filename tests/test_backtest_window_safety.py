"""Regression tests for research backtest walk-forward window safety."""

from strategy_optimizer import BacktestBase


HOUR_MS = 3600 * 1000


def candle(ts, close):
    return {
        "ts": ts,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1.0,
    }


def synthetic_ohlcv():
    timestamps = [0, HOUR_MS, 2 * HOUR_MS, 3 * HOUR_MS]
    ref = [candle(ts, 1.0) for ts in timestamps]
    # Starting coin rises 8x across the full dataset. A windowed test should not
    # get to size at the first price or value at the final price by accident.
    tia = [candle(ts, price) for ts, price in zip(timestamps, [10.0, 20.0, 40.0, 80.0])]
    return {"SOL": ref, "TIA": tia}, ref


class HoldOnlyBacktest(BacktestBase):
    def scout(self, ts):
        return None


def test_windowed_backtest_sizes_at_start_and_values_at_end():
    ohlcv, btc = synthetic_ohlcv()
    bt = HoldOnlyBacktest(ohlcv, btc, params={}, initial_balance=100.0, starting_coin="TIA")

    result = bt.run(start_ts=HOUR_MS, end_ts=2 * HOUR_MS)

    # At the window start, TIA is $20, so $100 buys 5 TIA. At the window end,
    # TIA is $40, so final value should be $200. The old bug sized at $10 and
    # valued at the dataset end $80, incorrectly returning $800.
    assert result["final_value"] == 200.0
    assert result["pnl_pct"] == 100.0
    assert bt.equity_curve[0] == (HOUR_MS, 100.0)
    assert bt.equity_curve[-1] == (2 * HOUR_MS, 200.0)


def test_full_backtest_still_uses_full_dataset_window():
    ohlcv, btc = synthetic_ohlcv()
    bt = HoldOnlyBacktest(ohlcv, btc, params={}, initial_balance=100.0, starting_coin="TIA")

    result = bt.run()

    assert result["final_value"] == 800.0
    assert result["pnl_pct"] == 700.0
    assert bt.equity_curve[0] == (0, 100.0)
    assert bt.equity_curve[-1] == (3 * HOUR_MS, 800.0)
