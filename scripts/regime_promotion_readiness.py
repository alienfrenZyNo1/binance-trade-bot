#!/usr/bin/env python3
"""Report-only Regime v2 promotion-readiness summary.

This script is intentionally read-only. It summarizes research artifacts and
prints a human-scannable readiness verdict. It never edits config, restarts
services, deploys, or places orders.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_regime_v2_evaluator", _REPO_ROOT / "scripts" / "research_regime_v2_evaluator.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load scripts/research_regime_v2_evaluator.py")
_regime_v2 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _regime_v2
_spec.loader.exec_module(_regime_v2)

NON_ACTIONABLE_ROUTES = {"cash", "buy_and_hold_basket"}


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _route_rows(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return list(artifact.get("leaderboard", {}).get("by_metric", {}).get("route_outcomes", []) or [])


def _best_non_cash_route(artifact: dict[str, Any]) -> dict[str, Any]:
    for row in _route_rows(artifact):
        if row.get("name") not in NON_ACTIONABLE_ROUTES:
            return row
    return {"name": "none", "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "win_rate_pct": 0.0}


def _route_by_name(artifact: dict[str, Any], route_name: str) -> dict[str, Any]:
    for row in _route_rows(artifact):
        if row.get("name") == route_name:
            return row
    return {"name": route_name, "total_return_pct": 0.0, "max_drawdown_pct": 999.0, "win_rate_pct": 0.0}


def summarize_window(window: str, artifact: dict[str, Any], *, required_route: str = "") -> dict[str, Any]:
    best = _route_by_name(artifact, required_route) if required_route else _best_non_cash_route(artifact)
    route_name = str(best.get("name", "none"))
    robustness = artifact.get("route_robustness", {}).get(route_name, {}) or {}
    sequence = artifact.get("sequence", {}) or {}
    current_regime = "unknown"
    if artifact.get("records"):
        current_regime = str(artifact["records"][-1].get("v2_smoothed", "unknown"))
    elif "regime_v2_smoothed" in sequence:
        dist = sequence["regime_v2_smoothed"].get("distribution", {}) or {}
        current_regime = max(dist, key=lambda key: dist[key]) if dist else "unknown"
    return {
        "window": window,
        "best_route": route_name,
        "best_return_pct": float(best.get("total_return_pct", 0.0) or 0.0),
        "best_max_drawdown_pct": float(best.get("max_drawdown_pct", 0.0) or 0.0),
        "best_win_rate_pct": float(best.get("win_rate_pct", 0.0) or 0.0),
        "best_route_robust": bool(robustness.get("passed", False)),
        "passing_windows": int(robustness.get("passing_windows", 0) or 0),
        "total_windows": int(robustness.get("total_windows", 0) or 0),
        "current_regime": current_regime,
    }


def evaluate_readiness(
    artifacts: dict[str, dict[str, Any]],
    *,
    max_allowed_drawdown_pct: float = 18.0,
    required_route: str = "",
) -> dict[str, Any]:
    ordered = sorted(artifacts.items(), key=lambda item: int("".join(ch for ch in item[0] if ch.isdigit()) or 0))
    windows = [summarize_window(name, artifact, required_route=required_route) for name, artifact in ordered]
    blockers: list[str] = []
    if not windows:
        blockers.append("No research artifacts supplied")
    non_positive = [row["window"] for row in windows if row["best_return_pct"] <= 0]
    weak_robustness = [row["window"] for row in windows if not row["best_route_robust"]]
    high_drawdown = [row["window"] for row in windows if row["best_max_drawdown_pct"] > max_allowed_drawdown_pct]
    if non_positive:
        blockers.append(f"Non-positive best route in: {', '.join(non_positive)}")
    if weak_robustness:
        blockers.append(f"Route robustness failed in: {', '.join(weak_robustness)}")
    if high_drawdown:
        blockers.append(f"Max drawdown above {max_allowed_drawdown_pct:.1f}% in: {', '.join(high_drawdown)}")

    positive_count = sum(1 for row in windows if row["best_return_pct"] > 0)
    robust_count = sum(1 for row in windows if row["best_route_robust"])
    drawdown_ok_count = sum(1 for row in windows if row["best_max_drawdown_pct"] <= max_allowed_drawdown_pct)
    if windows and positive_count == len(windows) and robust_count == len(windows) and drawdown_ok_count == len(windows):
        verdict = "🟢"
        status = "Eligible to draft promotion PR"
        next_action = "Draft a small explicit promotion PR only after fresh shadow observation confirms the same route/regime."
    elif positive_count > 0:
        verdict = "🟡"
        status = "Keep shadowing"
        next_action = "Continue report-only shadowing; do not promote live until every required window passes robustness and drawdown gates."
    else:
        verdict = "🔴"
        status = "Not ready"
        next_action = "Do not promote; improve research route or stay in cash/shadow mode."

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "status": status,
        "next_action": next_action,
        "blockers": blockers,
        "windows": windows,
        "max_allowed_drawdown_pct": max_allowed_drawdown_pct,
        "required_route": required_route,
        "research_only": True,
    }


def render_markdown_report(result: dict[str, Any]) -> str:
    lines = [
        "## Regime v2 Promotion Readiness",
        "REPORT ONLY — NO LIVE ORDERS — NO CONFIG CHANGES",
        "",
        f"Verdict: {result['verdict']} **{result['status']}**",
        f"Next: {result['next_action']}",
    ]
    if result.get("required_route"):
        lines.append(f"Required route: `{result['required_route']}`")
    lines.extend([
        "",
        "| Window | Best route | Return | Max DD | Robust |",
        "|---|---|---:|---:|---|",
    ])
    for row in result.get("windows", []):
        robust = "✅" if row["best_route_robust"] else f"❌ {row['passing_windows']}/{row['total_windows']}"
        lines.append(
            f"| {row['window']} | {row['best_route']} | {_fmt_pct(row['best_return_pct'])} | "
            f"{row['best_max_drawdown_pct']:.2f}% | {robust} |"
        )
    lines.append("")
    if result.get("blockers"):
        lines.append("### Blockers")
        for blocker in result["blockers"]:
            lines.append(f"- {blocker}")
        lines.append("")
    lines.append("Safety: this report is read-only and must never trigger orders, config edits, restarts, commits, merges, or deployments automatically.")
    return "\n".join(lines)


def parse_artifact_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("artifact must be WINDOW=PATH, e.g. 30d=/tmp/regime30.json")
    window, path = value.split("=", 1)
    return window.strip(), Path(path).expanduser()


def load_artifacts(pairs: list[tuple[str, Path]]) -> dict[str, dict[str, Any]]:
    artifacts = {}
    for window, path in pairs:
        artifacts[window] = json.loads(path.read_text())
    return artifacts


def _parse_csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def run_fresh_artifacts(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    windows = [int(item.strip()) for item in args.days.split(",") if item.strip()]
    coins = _parse_csv(args.coins)
    references = _parse_csv(args.references)
    for days in windows:
        warmup = 72 if days <= 30 else 120
        confirmation = 3 if days <= 30 else 2
        data = _regime_v2._regime.fetch_market_data(coins, references=references, days=days)
        artifacts[f"{days}d"] = _regime_v2.evaluate_regime_v2_history(
            data,
            references=references,
            breadth_coins=coins,
            step_hours=args.step_hours,
            warmup_hours=warmup,
            forward_hours=args.forward_hours,
            confirmation_samples=confirmation,
            min_confidence=args.min_confidence,
            tune_scorecard=True,
            tune_route_objective=True,
            train_fraction=args.train_fraction,
            selector_lookback=args.selector_lookback,
            selector_min_objective=args.selector_min_objective,
            selector_max_trailing_drawdown_pct=args.selector_max_trailing_drawdown_pct,
            selector_equity_stop_drawdown_pct=args.selector_equity_stop_drawdown_pct,
            selector_equity_stop_cooldown_windows=args.selector_equity_stop_cooldown_windows,
            selector_min_trailing_return_pct=args.selector_min_trailing_return_pct,
            selector_min_trailing_win_rate_pct=args.selector_min_trailing_win_rate_pct,
            selector_trailing_robust_windows=args.selector_trailing_robust_windows,
            selector_min_passing_trailing_windows=args.selector_min_passing_trailing_windows,
            selector_trailing_window_min_return_pct=args.selector_trailing_window_min_return_pct,
            selector_trailing_window_max_drawdown_pct=args.selector_trailing_window_max_drawdown_pct,
        )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description="Report-only Regime v2 promotion readiness")
    parser.add_argument("--artifact", action="append", type=parse_artifact_arg, default=[], help="WINDOW=PATH artifact, repeatable")
    parser.add_argument("--fresh", action="store_true", help="Run fresh public-data Regime v2 artifacts instead of reading files")
    parser.add_argument("--days", default="30,60,90")
    parser.add_argument("--coins", default="SOL,SUI,AAVE,LINK,AVAX,JUP,ENA,TIA,APT")
    parser.add_argument("--references", default="BTC,ETH,SOL")
    parser.add_argument("--step-hours", type=int, default=12)
    parser.add_argument("--forward-hours", type=int, default=24)
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--train-fraction", type=float, default=0.60)
    parser.add_argument("--selector-lookback", type=int, default=12)
    parser.add_argument("--selector-min-objective", type=float, default=0.0)
    parser.add_argument("--selector-max-trailing-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--selector-equity-stop-drawdown-pct", type=float, default=0.0)
    parser.add_argument("--selector-equity-stop-cooldown-windows", type=int, default=1)
    parser.add_argument("--selector-min-trailing-return-pct", type=float, default=-999999.0)
    parser.add_argument("--selector-min-trailing-win-rate-pct", type=float, default=0.0)
    parser.add_argument("--selector-trailing-robust-windows", type=int, default=0)
    parser.add_argument("--selector-min-passing-trailing-windows", type=int, default=0)
    parser.add_argument("--selector-trailing-window-min-return-pct", type=float, default=0.0)
    parser.add_argument("--selector-trailing-window-max-drawdown-pct", type=float, default=20.0)
    parser.add_argument("--max-allowed-drawdown-pct", type=float, default=18.0)
    parser.add_argument("--required-route", default="", help="Evaluate this route instead of whichever non-cash route has highest return")
    parser.add_argument("--json-output", default="")
    args = parser.parse_args()

    if args.fresh:
        artifacts = run_fresh_artifacts(args)
    elif args.artifact:
        artifacts = load_artifacts(args.artifact)
    else:
        parser.error("provide --fresh or at least one --artifact WINDOW=PATH")

    result = evaluate_readiness(
        artifacts,
        max_allowed_drawdown_pct=args.max_allowed_drawdown_pct,
        required_route=args.required_route,
    )
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(result, indent=2))
    print(render_markdown_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
