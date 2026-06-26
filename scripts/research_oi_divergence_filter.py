#!/usr/bin/env python3
"""Research-only Open Interest divergence sweep for BEAR futures shorts.

This script never places orders and never reads private endpoints. It reuses the
public-data BEAR futures backtester to compare whether requiring rising/falling
open-interest improves short-entry quality.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research_bear_futures_backtester import (  # noqa: E402
    BRIDGE,
    BacktestConfig,
    DEFAULT_SYMBOLS,
    load_market_data,
    run_backtest,
)

PRODUCTION_SHORT_SYMBOLS = tuple(
    symbol for symbol in DEFAULT_SYMBOLS if symbol not in {"NEARUSDC", "TIAUSDC"}
)


@dataclass(frozen=True)
class SweepCell:
    lookback_hours: int
    min_oi_change_pct: float
    trades: int
    total_pnl_pct: float
    avg_pnl_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    stop_loss_exits: int
    trailing_exits: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get("records") or [])


def summarize_payload(payload: dict[str, Any], *, lookback_hours: int, min_oi_change_pct: float) -> SweepCell:
    records = _records(payload)
    trades = len(records)
    pnl_values = [float(row.get("pnl_pct", 0.0) or 0.0) for row in records]
    wins = sum(1 for pnl in pnl_values if pnl > 0)
    max_dd = max((float(row.get("max_drawdown_pct", 0.0) or 0.0) for row in records), default=0.0)
    return SweepCell(
        lookback_hours=lookback_hours,
        min_oi_change_pct=min_oi_change_pct,
        trades=trades,
        total_pnl_pct=sum(pnl_values),
        avg_pnl_pct=(sum(pnl_values) / trades) if trades else 0.0,
        win_rate_pct=(wins / trades * 100.0) if trades else 0.0,
        max_drawdown_pct=max_dd,
        stop_loss_exits=sum(int(row.get("stop_loss_exits", 0) or 0) for row in records),
        trailing_exits=sum(int(row.get("trailing_exits", 0) or 0) for row in records),
    )


def run_sweep(
    market_data: dict[str, dict[str, Any]],
    *,
    lookbacks: Iterable[int],
    oi_thresholds: Iterable[float],
    base_config: BacktestConfig | None = None,
) -> dict[str, Any]:
    base_config = base_config or BacktestConfig()
    cells: list[SweepCell] = []
    raw_payloads: dict[str, Any] = {}
    for lookback in lookbacks:
        for min_oi in oi_thresholds:
            cfg = BacktestConfig(
                initial_balance=base_config.initial_balance,
                leverage=base_config.leverage,
                max_margin_pct=base_config.max_margin_pct,
                fee_rate=base_config.fee_rate,
                slippage_pct=base_config.slippage_pct,
                stop_loss_pct=base_config.stop_loss_pct,
                trailing_activation_pct=base_config.trailing_activation_pct,
                trailing_callback_pct=base_config.trailing_callback_pct,
                lookback_hours=int(lookback),
                min_oi_change_pct=float(min_oi),
                cooldown_hours=base_config.cooldown_hours,
            )
            payload = run_backtest(market_data, cfg)
            key = f"lookback={lookback}:min_oi={min_oi}"
            raw_payloads[key] = payload
            cells.append(
                summarize_payload(
                    payload,
                    lookback_hours=int(lookback),
                    min_oi_change_pct=float(min_oi),
                )
            )

    # Prefer positive return with enough trades; tie-break on avg PnL and drawdown.
    ranked = sorted(
        cells,
        key=lambda c: (c.total_pnl_pct, c.avg_pnl_pct, -c.max_drawdown_pct, c.trades),
        reverse=True,
    )
    baseline = next(
        (c for c in cells if c.lookback_hours == base_config.lookback_hours and c.min_oi_change_pct == base_config.min_oi_change_pct),
        None,
    )
    return {
        "assumptions": {
            "data_source": "Binance public futures klines + funding + openInterestHist",
            "bridge": BRIDGE,
            "production_short_symbols": list(PRODUCTION_SHORT_SYMBOLS),
            "interpretation": "For shorts, price weakness plus rising OI can mean fresh short pressure; falling OI can mean liquidation/deleveraging. This sweep tests thresholds empirically before any live use.",
            "base_config": base_config.to_dict(),
        },
        "baseline": baseline.to_dict() if baseline else None,
        "ranked": [cell.to_dict() for cell in ranked],
        "raw_payloads": raw_payloads,
    }


def parse_csv_numbers(value: str, cast):
    return [cast(part.strip()) for part in value.split(",") if part.strip()]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--symbols", default=",".join(PRODUCTION_SHORT_SYMBOLS))
    parser.add_argument("--lookbacks", default="6,12,18,24")
    parser.add_argument("--oi-thresholds", default="-20,-10,0,5,10,20")
    parser.add_argument("--output", default="research_outputs/oi_divergence_sweep.json")
    parser.add_argument("--initial-balance", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--max-margin-pct", type=float, default=0.40)
    parser.add_argument("--stop-loss-pct", type=float, default=12.0)
    parser.add_argument("--trailing-activation-pct", type=float, default=3.0)
    parser.add_argument("--trailing-callback-pct", type=float, default=1.0)
    parser.add_argument("--cooldown-hours", type=int, default=4)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    symbols = [s if s.endswith(BRIDGE) else s + BRIDGE for s in symbols]
    lookbacks = parse_csv_numbers(args.lookbacks, int)
    thresholds = parse_csv_numbers(args.oi_thresholds, float)
    base_config = BacktestConfig(
        initial_balance=args.initial_balance,
        leverage=args.leverage,
        max_margin_pct=args.max_margin_pct,
        stop_loss_pct=args.stop_loss_pct,
        trailing_activation_pct=args.trailing_activation_pct,
        trailing_callback_pct=args.trailing_callback_pct,
        lookback_hours=24,
        min_oi_change_pct=0.0,
        cooldown_hours=args.cooldown_hours,
    )

    print(f"Fetching {args.days}d futures data for {len(symbols)} production short symbols...")
    market_data = load_market_data(symbols, days=args.days, interval="1h")
    result = run_sweep(market_data, lookbacks=lookbacks, oi_thresholds=thresholds, base_config=base_config)

    print("\nOI divergence sweep leaderboard:")
    for row in result["ranked"][:12]:
        print(
            f"  lookback={row['lookback_hours']:>2}h min_oi={row['min_oi_change_pct']:>6.1f}% "
            f"trades={row['trades']:>3} total={row['total_pnl_pct']:+7.2f}% "
            f"avg={row['avg_pnl_pct']:+6.2f}% win={row['win_rate_pct']:5.1f}% "
            f"dd={row['max_drawdown_pct']:5.2f}% stops={row['stop_loss_exits']:>2}"
        )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"\nSaved to {out}")
    return result


if __name__ == "__main__":
    main()
