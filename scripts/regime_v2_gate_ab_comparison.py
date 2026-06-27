#!/usr/bin/env python3
"""Issue #72 research (lever #1): A/B comparison of robustness gate definitions.

Research-only. Runs the momentum-guarded regime v2 forward replay ONCE per
guard-state (ON / OFF) and per day-window, extracting the routed records, then
re-applies every candidate window-robustness gate (absolute, relative,
maxdd-only, segment-aware) to the SAME records. This is correct because
``build_route_robustness_gates`` is a PURE verdict function over already-routed
records (it reads only route_key + future returns) — the gate mode does not
influence routing — so we avoid 4x redundant full evaluations and also directly
prove the gate introduces no routing/lookahead side-effects.

For each gate mode we report:
  - selector robustness (does the momentum-guarded selector achieve 3/3-equiv?)
  - legacy_sol robustness under the SAME gate (sanity: rejects bad strategy?)
  - PLAIN (guard OFF) selector robustness under the SAME gate (sanity)
  - selector vs legacy_sol OOS comparison (return, maxDD)
  - cash_also_passes diagnostic (anti-cash guarantee)

No live config, DB, Docker, or order changes. Leaves code uncommitted on disk.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "regime_v2_forward_replay", REPO_ROOT / "scripts" / "regime_v2_forward_replay.py"
)
assert _spec is not None and _spec.loader is not None
_replay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _replay
_spec.loader.exec_module(_replay)
_eval = _replay._regime_v2

GATE_MODES = ("absolute", "relative", "maxdd-only", "segment-aware")


def _evaluate_once(
    *, data: dict[str, list[dict[str, Any]]], day: int, coins: list[str],
    references: list[str], momentum_guard: bool,
) -> dict[str, Any]:
    """One full forward-replay evaluation; returns records + route outcomes.

    gate_mode here is irrelevant to routing; we pass 'absolute' just to seed the
    default robustness dict, then re-derive every gate below from the records.
    """
    run_data = _replay._slice_last_days(data, day) if day else data
    output = _eval.evaluate_regime_v2_history(
        run_data,
        references=references,
        breadth_coins=coins,
        step_hours=12,
        warmup_hours=120,
        forward_hours=24,
        confirmation_samples=2,
        min_confidence=0.60,
        tune_scorecard=True,
        tune_route_objective=True,
        train_fraction=0.60,
        selector_lookback=12,
        selector_min_objective=0.0,
        selector_max_trailing_drawdown_pct=15.0,
        selector_equity_stop_drawdown_pct=15.0,
        selector_equity_stop_cooldown_windows=1,
        selector_min_trailing_win_rate_pct=50.0,
        selector_trailing_robust_windows=3,
        selector_min_passing_trailing_windows=3,
        selector_trailing_window_max_drawdown_pct=15.0,
        selector_re_engage_confirmation=True,
        selector_re_engage_breadth_pct=0.60,
        selector_re_engage_rolling_peak_windows=0,
        selector_recent_pnl_lookback_windows=0,
        selector_recent_pnl_stop_pct=0.0,
        momentum_guard=momentum_guard,
        robustness_gate_mode="absolute",  # placeholder; re-derived below
    )
    return output


def _gate_for_all_routes(
    records: list[dict[str, Any]], *, gate_mode: str, fee_bps: float, max_dd: float,
) -> dict[str, dict[str, Any]]:
    """Re-apply one gate mode to every route key present on the records."""
    routes = {
        "legacy_sol": "legacy_regime",
        "regime_v2": "v2_smoothed",
        "regime_v2_selector": "selector_smoothed",
    }
    if records and "v2_tuned_smoothed" in records[0]:
        routes["regime_v2_tuned"] = "v2_tuned_smoothed"
    if records and "v2_route_tuned_smoothed" in records[0]:
        routes["regime_v2_route_tuned"] = "v2_route_tuned_smoothed"
    out = {}
    for name, key in routes.items():
        out[name] = _eval.build_route_robustness_gates(
            records, key, fee_bps=fee_bps, windows=3,
            min_window_return_pct=0.25, max_window_drawdown_pct=max_dd,
            gate_mode=gate_mode, benchmark="buy_and_hold_basket",
            require_positive_total_return=None,  # mode-specific default
            segment_min_passing_frac=2.0 / 3.0, bleed_min_run=6,
        )
    return out


def _fmt_pct(x: Any) -> str:
    try:
        return f"{float(x):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def run_one(data, *, day, coins, references, momentum_guard) -> dict[str, Any]:
    output = _evaluate_once(
        data=data, day=day, coins=coins, references=references, momentum_guard=momentum_guard,
    )
    records = output["records"]
    route_outcomes = output.get("route_outcomes", {}) or {}
    fee_bps = float(output["manifest"]["assumptions"].get("fee_bps", 10.0))
    result: dict[str, Any] = {"day": day, "momentum_guard": momentum_guard, "by_gate": {}}
    for gate_mode in GATE_MODES:
        gates = _gate_for_all_routes(records, gate_mode=gate_mode, fee_bps=fee_bps, max_dd=15.0)
        def _m(name):
            oc = route_outcomes.get(name, {}) or {}
            g = gates.get(name, {}) or {}
            return {
                "return_pct": oc.get("total_return_pct", 0.0),
                "max_drawdown_pct": oc.get("max_drawdown_pct", 0.0),
                "win_rate_pct": oc.get("win_rate_pct", 0.0),
                "robust_passed": g.get("passed", False),
                "passing_windows": g.get("passing_windows", 0),
                "total_windows": g.get("total_windows", 0),
                "cash_also_passes": g.get("cash_also_passes", False),
                "windows": g.get("windows", []),
                "monotone_bleed_tail": g.get("monotone_bleed_tail", False),
            }
        result["by_gate"][gate_mode] = {
            "selector": _m("regime_v2_selector"),
            "legacy_sol": _m("legacy_sol"),
            "buy_and_hold": _m("buy_and_hold_basket"),
        }
    return result


def _robust_str(m: dict[str, Any]) -> str:
    pw, tw = m["passing_windows"], m["total_windows"]
    flag = "PASS" if m["robust_passed"] else "fail"
    cash = " ⚠️CASH-PASS" if m["cash_also_passes"] else ""
    return f"{pw}/{tw} {flag}{cash}"


def print_block(label: str, guard: str, runs: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 112}")
    print(f"  {label}  |  momentum_guard={guard}")
    print(f"{'=' * 112}")
    for r in runs:
        day = r["day"]
        print(f"\n--- {day}d ---")
        print(f"  {'gate':<14} | {'route':<18} | {'return':>9} | {'maxDD':>8} | {'robustness':<24} | {'cash_also':<9}")
        print(f"  {'-'*14} | {'-'*18} | {'-'*9} | {'-'*8} | {'-'*24} | {'-'*9}")
        for gate_mode, gdata in r["by_gate"].items():
            for rn in ("selector", "legacy_sol", "buy_and_hold"):
                m = gdata[rn]
                rob = _robust_str(m) if rn in ("selector", "legacy_sol") else "-"
                cash = ("Y" if m.get("cash_also_passes") else "N") if rn == "selector" else "-"
                print(
                    f"  {gate_mode:<14} | {rn:<18} | {_fmt_pct(m['return_pct']):>9} | "
                    f"{m['max_drawdown_pct']:7.2f}% | {rob:<24} | {cash:<9}"
                )
            print()
        # Per-window selector detail under each gate
        print(f"  per-window SELECTOR detail ({day}d):")
        for gate_mode, gdata in r["by_gate"].items():
            wins = gdata["selector"].get("windows", [])
            parts = []
            for i, w in enumerate(wins):
                s = f"w{i+1}: ret={_fmt_pct(w.get('total_return_pct'))} maxDD={w.get('max_drawdown_pct',0):.1f}% {'✓' if w.get('passed') else '✗'}"
                if gate_mode == "relative":
                    s += f" excess={_fmt_pct(w.get('excess_return_pct'))} (vs B&H {_fmt_pct(w.get('benchmark_total_return_pct'))})"
                parts.append(s)
            print(f"    {gate_mode:<14}: {' | '.join(parts)}")
        # legacy_sol per-window detail (sanity: should the gate reject it?)
        print(f"  per-window LEGACY_SOL detail ({day}d) [sanity — gate must reject]:")
        for gate_mode, gdata in r["by_gate"].items():
            wins = gdata["legacy_sol"].get("windows", [])
            parts = []
            for i, w in enumerate(wins):
                s = f"w{i+1}: ret={_fmt_pct(w.get('total_return_pct'))} maxDD={w.get('max_drawdown_pct',0):.1f}% {'✓' if w.get('passed') else '✗'}"
                parts.append(s)
            print(f"    {gate_mode:<14}: {' | '.join(parts)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue #72 gate A/B comparison (research-only)")
    parser.add_argument("--days", default="240,300")
    parser.add_argument("--fetch-days", type=int, default=365)
    parser.add_argument("--coins", default="SOL,SUI,AAVE,LINK,AVAX,JUP,ENA,TIA,APT")
    parser.add_argument("--references", default="BTC,ETH,SOL")
    parser.add_argument("--cache-dir", default=str(_replay.DEFAULT_CACHE_DIR))
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    days = [int(d) for d in args.days.split(",") if d.strip()]
    coins = _replay._parse_csv(args.coins)
    references = _replay._parse_csv(args.references)
    fetch_days = max([args.fetch_days, *days])

    label = "FRESH" if args.force_refresh else "CACHED"
    print(f"[{label}] Loading market data (force_refresh={args.force_refresh}, fetch_days={fetch_days})...", flush=True)
    data, cache_meta = _replay.load_or_fetch_market_data(
        cache_dir=Path(args.cache_dir).expanduser(),
        days=fetch_days, coins=coins, references=references,
        force_refresh=args.force_refresh,
    )
    print(f"  cache_hit={cache_meta['cache_hit']} path={cache_meta['cache_path']}", flush=True)

    all_runs: dict[str, Any] = {"label": label}
    on_runs: list[dict[str, Any]] = []
    off_runs: list[dict[str, Any]] = []
    for day in days:
        print(f"[{label}] Evaluating {day}d momentum_guard=ON ...", flush=True)
        on_runs.append(run_one(data, day=day, coins=coins, references=references, momentum_guard=True))
        print(f"[{label}] Evaluating {day}d momentum_guard=OFF (PLAIN) ...", flush=True)
        off_runs.append(run_one(data, day=day, coins=coins, references=references, momentum_guard=False))

    print_block(f"{label} momentum_guard=ON", "ON", on_runs)
    print_block(f"{label} momentum_guard=OFF", "OFF (PLAIN)", off_runs)

    all_runs["on"] = on_runs
    all_runs["off"] = off_runs
    if args.output:
        Path(args.output).write_text(json.dumps(all_runs, indent=2, default=str) + "\n")
        print(f"\n[{label}] Full JSON written to {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
