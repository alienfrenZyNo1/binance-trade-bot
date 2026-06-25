#!/usr/bin/env python3
"""Evidence gates and per-regime leaderboard for strategy research results.

Research-only helper for the all-weather strategy roadmap. It turns backtest or
optimizer JSON records into mechanical pass/fail decisions so strategies are not
promoted to shadow/live mode on vibes or one lucky headline P&L.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_GATES = {
    "min_oos_pnl_pct": 0.0,
    "min_vs_baseline_pct": 0.0,
    "max_drawdown_pct": 35.0,
    "min_trades": 3,
    "max_fee_pct": 15.0,
    "min_sharpe": 0.0,
    "min_passing_windows": 0,
    "min_window_pass_rate_pct": 0.0,
}


@dataclass
class GateResult:
    name: str
    regime: str
    passed: bool
    score: float
    metrics: dict[str, float | int | str]
    failures: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(record: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in record and record[key] is not None:
            try:
                return float(record[key])
            except (TypeError, ValueError):
                return default
    return default


def _name(record: dict[str, Any]) -> str:
    if record.get("name"):
        return str(record["name"])
    if record.get("strategy"):
        return str(record["strategy"])
    params = record.get("params")
    if isinstance(params, dict) and params:
        compact = ",".join(f"{k}={v}" for k, v in sorted(params.items())[:4])
        return f"params:{compact}"
    return "unnamed"


def normalize_record(record: dict[str, Any]) -> dict[str, float | int | str]:
    """Normalize common result shapes from project research scripts."""
    initial = _num(record, "initial_balance", "initial", default=62.0)
    fees = _num(record, "total_fees", "fees", default=0.0)
    explicit_fee_pct = _num(record, "fee_pct", default=float("nan"))
    fee_pct = explicit_fee_pct if explicit_fee_pct == explicit_fee_pct else (fees / initial * 100.0 if initial else 0.0)

    pnl_pct = _num(record, "oos_pnl", "oos_pnl_pct", "pnl_pct", "pnl", default=0.0)
    baseline_pct = _num(record, "baseline_pnl", "baseline_pnl_pct", "cash_pnl", default=0.0)
    trades = int(_num(record, "trade_count", "trades", default=0.0))
    max_drawdown = _num(record, "max_drawdown", "max_dd", default=100.0)
    sharpe = _num(record, "sharpe", default=0.0)
    robustness_any = record.get("robustness")
    robustness: dict[str, Any] = robustness_any if isinstance(robustness_any, dict) else {}
    passing_windows = int(_num(robustness, "passing_windows", default=0.0))
    window_count = int(_num(robustness, "window_count", default=0.0))
    window_pass_rate = _num(robustness, "pass_rate_pct", default=0.0)

    return {
        "name": _name(record),
        "regime": str(record.get("regime", "all")).lower(),
        "pnl_pct": pnl_pct,
        "baseline_pnl_pct": baseline_pct,
        "vs_baseline_pct": pnl_pct - baseline_pct,
        "max_drawdown_pct": max_drawdown,
        "trades": trades,
        "fee_pct": fee_pct,
        "sharpe": sharpe,
        "passing_windows": passing_windows,
        "window_count": window_count,
        "window_pass_rate_pct": window_pass_rate,
    }


def evaluate_strategy(record: dict[str, Any], gates: dict[str, float] | None = None) -> GateResult:
    gates = {**DEFAULT_GATES, **(gates or {})}
    metrics = normalize_record(record)
    failures: list[str] = []

    if float(metrics["pnl_pct"]) < gates["min_oos_pnl_pct"]:
        failures.append(f"OOS P&L {metrics['pnl_pct']:+.2f}% < {gates['min_oos_pnl_pct']:+.2f}%")
    if float(metrics["vs_baseline_pct"]) < gates["min_vs_baseline_pct"]:
        failures.append(
            f"baseline edge {metrics['vs_baseline_pct']:+.2f}% < {gates['min_vs_baseline_pct']:+.2f}%"
        )
    if float(metrics["max_drawdown_pct"]) > gates["max_drawdown_pct"]:
        failures.append(f"max drawdown {metrics['max_drawdown_pct']:.2f}% > {gates['max_drawdown_pct']:.2f}%")
    if int(metrics["trades"]) < gates["min_trades"]:
        failures.append(f"trades {metrics['trades']} < {int(gates['min_trades'])}")
    if float(metrics["fee_pct"]) > gates["max_fee_pct"]:
        failures.append(f"fees {metrics['fee_pct']:.2f}% > {gates['max_fee_pct']:.2f}%")
    if float(metrics["sharpe"]) < gates["min_sharpe"]:
        failures.append(f"sharpe {metrics['sharpe']:.2f} < {gates['min_sharpe']:.2f}")
    if int(metrics["passing_windows"]) < int(gates["min_passing_windows"]):
        failures.append(
            f"passing windows {metrics['passing_windows']} < {int(gates['min_passing_windows'])}"
        )
    if float(metrics["window_pass_rate_pct"]) < gates["min_window_pass_rate_pct"]:
        failures.append(
            f"window pass rate {metrics['window_pass_rate_pct']:.2f}% < "
            f"{gates['min_window_pass_rate_pct']:.2f}%"
        )

    # Ranking score deliberately penalizes drawdown/fees so a high-P&L but ugly
    # strategy doesn't automatically top the leaderboard.
    score = (
        float(metrics["pnl_pct"])
        + float(metrics["vs_baseline_pct"]) * 0.5
        + float(metrics["sharpe"]) * 5.0
        + float(metrics["window_pass_rate_pct"]) * 0.05
        - float(metrics["max_drawdown_pct"]) * 0.35
        - float(metrics["fee_pct"]) * 0.5
    )
    return GateResult(
        name=str(metrics["name"]),
        regime=str(metrics["regime"]),
        passed=not failures,
        score=round(score, 4),
        metrics=metrics,
        failures=failures,
    )


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "strategies", "leaderboard", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    # optimize_momentum.py result shape: treat the top-level object as one record.
    return [payload]


def build_leaderboard(records: list[dict[str, Any]], gates: dict[str, float] | None = None) -> dict[str, Any]:
    evaluated = [evaluate_strategy(record, gates) for record in records]
    evaluated.sort(key=lambda row: (row.passed, row.score), reverse=True)

    by_regime: dict[str, list[dict[str, Any]]] = {}
    for row in evaluated:
        by_regime.setdefault(row.regime, []).append(row.to_dict())

    return {
        "gates": {**DEFAULT_GATES, **(gates or {})},
        "overall": [row.to_dict() for row in evaluated],
        "by_regime": by_regime,
        "summary": {
            "total": len(evaluated),
            "passed": sum(1 for row in evaluated if row.passed),
            "failed": sum(1 for row in evaluated if not row.passed),
        },
    }


def _iso(ts_ms: int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def build_data_manifest(
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    *,
    interval: str = "1h",
    bridge: str = "USDC",
    assumptions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a reproducible manifest for the market data behind research output."""
    normalized: dict[str, list[dict[str, float | int]]] = {}
    timestamps: list[int] = []
    candle_counts: dict[str, int] = {}

    for coin in sorted(ohlcv_by_coin):
        rows = sorted(ohlcv_by_coin.get(coin) or [], key=lambda row: int(row.get("ts", 0)))
        candle_counts[coin] = len(rows)
        normalized[coin] = []
        for row in rows:
            ts = int(row.get("ts", 0))
            timestamps.append(ts)
            normalized[coin].append(
                {
                    "ts": ts,
                    "open": float(row.get("open", 0.0)),
                    "high": float(row.get("high", 0.0)),
                    "low": float(row.get("low", 0.0)),
                    "close": float(row.get("close", 0.0)),
                    "volume": float(row.get("volume", 0.0)),
                }
            )

    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    start_ts = min(timestamps) if timestamps else None
    end_ts = max(timestamps) if timestamps else None
    return {
        "bridge": bridge,
        "interval": interval,
        "symbols": [f"{coin}{bridge}" for coin in sorted(ohlcv_by_coin)],
        "date_range": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start": _iso(start_ts),
            "end": _iso(end_ts),
        },
        "candle_counts": candle_counts,
        "assumptions": assumptions or {},
        "data_hash": hashlib.sha256(encoded).hexdigest(),
    }


def build_research_output(
    records: list[dict[str, Any]],
    *,
    ohlcv_by_coin: dict[str, list[dict[str, Any]]],
    interval: str = "1h",
    bridge: str = "USDC",
    assumptions: dict[str, Any] | None = None,
    gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Package raw research records with a manifest and gated leaderboard."""
    return {
        "manifest": build_data_manifest(
            ohlcv_by_coin,
            interval=interval,
            bridge=bridge,
            assumptions=assumptions,
        ),
        "records": records,
        "leaderboard": build_leaderboard(records, gates),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="JSON file containing strategy result records")
    parser.add_argument("--output", help="Optional JSON output path")
    parser.add_argument("--min-oos-pnl-pct", type=float, default=DEFAULT_GATES["min_oos_pnl_pct"])
    parser.add_argument("--min-vs-baseline-pct", type=float, default=DEFAULT_GATES["min_vs_baseline_pct"])
    parser.add_argument("--max-drawdown-pct", type=float, default=DEFAULT_GATES["max_drawdown_pct"])
    parser.add_argument("--min-trades", type=int, default=int(DEFAULT_GATES["min_trades"]))
    parser.add_argument("--max-fee-pct", type=float, default=DEFAULT_GATES["max_fee_pct"])
    parser.add_argument("--min-sharpe", type=float, default=DEFAULT_GATES["min_sharpe"])
    parser.add_argument("--min-passing-windows", type=int, default=int(DEFAULT_GATES["min_passing_windows"]))
    parser.add_argument(
        "--min-window-pass-rate-pct",
        type=float,
        default=DEFAULT_GATES["min_window_pass_rate_pct"],
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.input:
        payload = json.loads(Path(args.input).read_text())
        records = _extract_records(payload)
    else:
        raise SystemExit("input JSON file is required")

    gates = {
        "min_oos_pnl_pct": args.min_oos_pnl_pct,
        "min_vs_baseline_pct": args.min_vs_baseline_pct,
        "max_drawdown_pct": args.max_drawdown_pct,
        "min_trades": args.min_trades,
        "max_fee_pct": args.max_fee_pct,
        "min_sharpe": args.min_sharpe,
        "min_passing_windows": args.min_passing_windows,
        "min_window_pass_rate_pct": args.min_window_pass_rate_pct,
    }
    leaderboard = build_leaderboard(records, gates)
    text = json.dumps(leaderboard, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)
    return leaderboard


if __name__ == "__main__":
    main()
