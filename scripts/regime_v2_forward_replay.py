#!/usr/bin/env python3
"""Cached forward replay harness for Regime v2 research.

Research-only. Fetches historical Binance public data once, caches it locally,
and replays many Regime v2 settings against the same point-in-time history.
It never edits live config, places orders, restarts services, or promotes a
strategy automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_regime_v2_evaluator", _REPO_ROOT / "scripts" / "research_regime_v2_evaluator.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load scripts/research_regime_v2_evaluator.py")
_regime_v2 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _regime_v2
_spec.loader.exec_module(_regime_v2)

HOUR_MS = 3600 * 1000
DEFAULT_CACHE_DIR = _REPO_ROOT / ".cache" / "regime_v2_forward_replay"


def _normalize_symbols(values: Iterable[str]) -> list[str]:
    return sorted({str(value).strip().upper() for value in values if str(value).strip()})


def cache_key(*, days: int, coins: Iterable[str], references: Iterable[str]) -> str:
    """Return a stable order-insensitive cache key for market-history inputs."""
    payload = {
        "days": int(days),
        "coins": _normalize_symbols(coins),
        "references": _normalize_symbols(references),
        "source": "binance-public-spot-klines",
        "version": 1,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    return f"regime-v2-history-{digest}"


def _cache_path(cache_dir: Path, *, days: int, coins: Iterable[str], references: Iterable[str]) -> Path:
    return cache_dir / f"{cache_key(days=days, coins=coins, references=references)}.json"


def load_or_fetch_market_data(
    *,
    cache_dir: Path,
    days: int,
    coins: list[str],
    references: list[str],
    fetcher: Callable[..., dict[str, list[dict[str, Any]]]] | None = None,
    force_refresh: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load cached market history or fetch it once and cache it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, days=days, coins=coins, references=references)
    if path.exists() and not force_refresh:
        payload = json.loads(path.read_text())
        meta = dict(payload.get("meta", {}))
        meta.update({"cache_hit": True, "cache_path": str(path)})
        return payload["data"], meta

    fetch = fetcher or _regime_v2._regime.fetch_market_data
    data = fetch(coins, references=references, days=days)
    meta = {
        "cache_hit": False,
        "cache_path": str(path),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "coins": _normalize_symbols(coins),
        "references": _normalize_symbols(references),
    }
    path.write_text(json.dumps({"meta": meta, "data": data}, indent=2, sort_keys=True) + "\n")
    return data, meta


def _slice_last_days(data: dict[str, list[dict[str, Any]]], days: int) -> dict[str, list[dict[str, Any]]]:
    all_ts = [int(row["ts"]) for rows in data.values() for row in rows]
    if not all_ts:
        return data
    end_ts = max(all_ts)
    start_ts = end_ts - days * 24 * HOUR_MS
    return {coin: [row for row in rows if int(row["ts"]) >= start_ts] for coin, rows in data.items()}


def build_default_settings(*, days: list[int], step_hours: list[int], selector_lookbacks: list[int]) -> list[dict[str, Any]]:
    """Build a compact grid of replay settings."""
    settings = []
    for day in days:
        for step in step_hours:
            for selector in selector_lookbacks:
                settings.append(
                    {
                        "name": f"{day}d_step{step}_sel{selector}",
                        "days": day,
                        "step_hours": step,
                        "warmup_hours": 72 if day <= 30 else 120,
                        "forward_hours": 24,
                        "confirmation_samples": 3 if day <= 30 else 2,
                        "min_confidence": 0.60,
                        "tune_scorecard": True,
                        "tune_route_objective": True,
                        "train_fraction": 0.60,
                        "selector_lookback": selector,
                        "selector_min_objective": 0.0,
                    }
                )
    return settings


def _best_route(output: dict[str, Any]) -> dict[str, Any]:
    rows = output.get("leaderboard", {}).get("by_metric", {}).get("route_outcomes", []) or []
    for row in rows:
        if row.get("name") not in {"cash", "buy_and_hold_basket"}:
            return row
    return {"name": "none", "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "win_rate_pct": 0.0}


def _score_candidate(route: dict[str, Any], robustness: dict[str, Any]) -> float:
    robust_bonus = 10.0 if robustness.get("passed") else 2.0 * float(robustness.get("passing_windows", 0) or 0)
    return float(route.get("total_return_pct", 0.0) or 0.0) - 0.5 * float(route.get("max_drawdown_pct", 0.0) or 0.0) + robust_bonus


def evaluate_settings_grid(
    data: dict[str, list[dict[str, Any]]],
    settings: list[dict[str, Any]],
    *,
    references: list[str],
    breadth_coins: list[str],
) -> dict[str, Any]:
    """Replay many settings against one already-fetched dataset."""
    candidates = []
    for setting in settings:
        run_data = _slice_last_days(data, int(setting.get("days", 99999))) if setting.get("days") else data
        output = _regime_v2.evaluate_regime_v2_history(
            run_data,
            references=references,
            breadth_coins=breadth_coins,
            step_hours=int(setting.get("step_hours", 12)),
            warmup_hours=int(setting.get("warmup_hours", 72)),
            forward_hours=int(setting.get("forward_hours", 24)),
            confirmation_samples=int(setting.get("confirmation_samples", 3)),
            min_confidence=float(setting.get("min_confidence", 0.60)),
            tune_scorecard=bool(setting.get("tune_scorecard", True)),
            tune_route_objective=bool(setting.get("tune_route_objective", True)),
            train_fraction=float(setting.get("train_fraction", 0.60)),
            selector_lookback=int(setting.get("selector_lookback", 12)),
            selector_min_objective=float(setting.get("selector_min_objective", 0.0)),
        )
        route = _best_route(output)
        robustness = output.get("route_robustness", {}).get(route.get("name"), {}) or {}
        score = _score_candidate(route, robustness)
        candidates.append(
            {
                "name": setting.get("name", f"candidate_{len(candidates) + 1}"),
                "settings": setting,
                "samples": output.get("leaderboard", {}).get("summary", {}).get("total", 0),
                "best_route": route.get("name"),
                "best_return_pct": route.get("total_return_pct", 0.0),
                "best_max_drawdown_pct": route.get("max_drawdown_pct", 0.0),
                "best_win_rate_pct": route.get("win_rate_pct", 0.0),
                "robustness_passed": bool(robustness.get("passed", False)),
                "passing_windows": int(robustness.get("passing_windows", 0) or 0),
                "total_windows": int(robustness.get("total_windows", 0) or 0),
                "score": score,
                "artifact": output,
            }
        )
    leaderboard = sorted(
        [
            {key: value for key, value in row.items() if key != "artifact"}
            for row in candidates
        ],
        key=lambda row: (row["score"], row["best_return_pct"], -row["best_max_drawdown_pct"]),
        reverse=True,
    )
    return {
        "manifest": {
            "script": "regime_v2_forward_replay.py",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "research_only": True,
            "no_live_orders": True,
            "settings_count": len(settings),
        },
        "summary": {"total_candidates": len(candidates)},
        "leaderboard": leaderboard,
        "candidates": candidates,
    }


def _parse_csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def _parse_int_csv(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Cached Regime v2 forward replay research harness")
    parser.add_argument("--days", default="30,60,90", help="Comma-separated replay windows")
    parser.add_argument("--fetch-days", type=int, default=90, help="History days to fetch/cache once")
    parser.add_argument("--coins", default="SOL,SUI,AAVE,LINK,AVAX,JUP,ENA,TIA,APT")
    parser.add_argument("--references", default="BTC,ETH,SOL")
    parser.add_argument("--step-hours", default="12", help="Comma-separated step-hours grid")
    parser.add_argument("--selector-lookbacks", default="6,12,24", help="Comma-separated selector lookback grid")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    days = _parse_int_csv(args.days)
    step_hours = _parse_int_csv(args.step_hours)
    selector_lookbacks = _parse_int_csv(args.selector_lookbacks)
    coins = _parse_csv(args.coins)
    references = _parse_csv(args.references)
    fetch_days = max([args.fetch_days, *days])

    data, cache_meta = load_or_fetch_market_data(
        cache_dir=Path(args.cache_dir).expanduser(),
        days=fetch_days,
        coins=coins,
        references=references,
        force_refresh=args.force_refresh,
    )
    settings = build_default_settings(days=days, step_hours=step_hours, selector_lookbacks=selector_lookbacks)
    payload = evaluate_settings_grid(data, settings, references=references, breadth_coins=coins)
    payload["cache"] = cache_meta
    payload["manifest"].update({"days": days, "fetch_days": fetch_days, "coins": coins, "references": references})

    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    print(
        f"Regime v2 forward replay candidates={payload['summary']['total_candidates']} "
        f"cache_hit={cache_meta['cache_hit']} cache={cache_meta['cache_path']}"
    )
    for row in payload["leaderboard"][:10]:
        robust = "✅" if row["robustness_passed"] else f"❌ {row['passing_windows']}/{row['total_windows']}"
        print(
            f"{row['name']}: route={row['best_route']} return={row['best_return_pct']:+.2f}% "
            f"maxDD={row['best_max_drawdown_pct']:.2f}% score={row['score']:.2f} robust={robust}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
