#!/usr/bin/env python3
"""Research-only Regime v2 evaluator.

This script does not affect live trading. It evaluates an interpretable
strategy-utility regime scorecard against the existing research v1 classifier
and the legacy SOL-only live rule using walk-forward, next-window labels.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "research_regime_classifier", _REPO_ROOT / "scripts" / "research_regime_classifier.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load scripts/research_regime_classifier.py")
_regime = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _regime
_spec.loader.exec_module(_regime)

BULL = _regime.BULL
BEAR = _regime.BEAR
SIDEWAYS = _regime.SIDEWAYS
STORMY = _regime.STORMY
DEFAULT_REFERENCES = _regime.DEFAULT_REFERENCES
DEFAULT_BREADTH_COINS = _regime.DEFAULT_BREADTH_COINS
DEFAULT_FUTURES_SYMBOLS = _regime.DEFAULT_FUTURES_SYMBOLS
HOUR_MS = _regime.HOUR_MS
BRIDGE = _regime.BRIDGE


def _closes(rows: list[dict[str, float | int]]) -> list[float]:
    return [float(row["close"]) for row in rows]


def _safe_median(values: list[float], default: float = 0.0) -> float:
    return median(values) if values else default


def _pct_change_values(values: list[float]) -> float:
    values = [value for value in values if value > 0]
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] / values[0] - 1.0) * 100.0


def _downside_vol(closes: list[float], periods: int = 24) -> float:
    if len(closes) <= periods:
        return 0.0
    window = closes[-periods - 1 :]
    neg_returns = []
    for i in range(1, len(window)):
        if window[i - 1] <= 0:
            continue
        ret = window[i] / window[i - 1] - 1.0
        if ret < 0:
            neg_returns.append(ret)
    if len(neg_returns) < 2:
        return 0.0
    mean = sum(neg_returns) / len(neg_returns)
    variance = sum((ret - mean) ** 2 for ret in neg_returns) / len(neg_returns)
    return math.sqrt(variance) * math.sqrt(24) * 100.0


def _basket_return(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    coins: Iterable[str],
    periods: int,
) -> float:
    returns = []
    for coin in coins:
        closes = _closes(ohlcv_by_coin.get(coin, []))
        if len(closes) > periods and closes[-periods - 1] > 0:
            returns.append((closes[-1] / closes[-periods - 1] - 1.0) * 100.0)
    return _safe_median(returns)


def _future_return(rows: list[dict[str, float | int]], start_idx: int, forward_bars: int) -> float:
    end_idx = min(len(rows) - 1, start_idx + max(1, forward_bars))
    if start_idx < 0 or start_idx >= len(rows) or end_idx <= start_idx:
        return 0.0
    start = float(rows[start_idx]["close"])
    end = float(rows[end_idx]["close"])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _future_basket_stats(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    coins: Iterable[str],
    start_ts: int,
    forward_hours: int,
) -> dict[str, float]:
    future_returns = []
    all_forward_closes: list[float] = []
    for coin in coins:
        rows = ohlcv_by_coin.get(coin, [])
        idx = next((i for i, row in enumerate(rows) if int(row["ts"]) >= start_ts), None)
        if idx is None or idx + 1 >= len(rows):
            continue
        future_returns.append(_future_return(rows, idx, forward_hours))
        end_idx = min(len(rows), idx + forward_hours + 1)
        all_forward_closes.extend(float(row["close"]) for row in rows[idx:end_idx])
    return {
        "future_basket_ret": _safe_median(future_returns),
        "future_dispersion": _safe_median([abs(ret - _safe_median(future_returns)) for ret in future_returns]),
        "future_vol": _regime.realized_volatility(all_forward_closes, min(24, max(2, len(all_forward_closes) - 1))),
    }


def build_feature_snapshot(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
    futures_metrics: dict[str, Any] | None = None,
) -> dict[str, float | int]:
    """Build interpretable Regime v2 features from point-in-time OHLCV."""
    reference_votes = []
    ref24: dict[str, float] = {}
    for ref in references:
        metrics = _regime._reference_metrics(ohlcv_by_coin.get(ref, []))
        if not metrics:
            continue
        vote = 0.0
        if metrics["adx"] >= 20:
            if metrics["ema20"] > metrics["ema50"] and metrics["plus_di"] > metrics["minus_di"]:
                vote += 1.0
            elif metrics["ema20"] < metrics["ema50"] and metrics["minus_di"] > metrics["plus_di"]:
                vote -= 1.0
        vote += 0.5 if metrics["ret_24h"] > 2.0 else 0.0
        vote -= 0.5 if metrics["ret_24h"] < -2.0 else 0.0
        vote += 0.25 if metrics["price"] > metrics["ema50"] else -0.25
        reference_votes.append(vote)
        ref24[ref] = float(metrics["ret_24h"])

    breadth = _regime._breadth_metrics(ohlcv_by_coin, breadth_coins)
    basket_24h = _basket_return(ohlcv_by_coin, breadth_coins, 24)
    basket_4h = _basket_return(ohlcv_by_coin, breadth_coins, 4)
    btc_24h = ref24.get("BTC", ref24.get("SOL", 0.0))
    eth_24h = ref24.get("ETH", 0.0)
    sol_24h = ref24.get("SOL", 0.0)

    downside_vols = []
    volume_changes = []
    for coin in breadth_coins:
        rows = ohlcv_by_coin.get(coin, [])
        closes = _closes(rows)
        if len(closes) >= 30:
            downside_vols.append(_downside_vol(closes, 24))
        vols = [float(row.get("volume", 0.0)) for row in rows[-25:]]
        if len(vols) >= 2:
            volume_changes.append(_pct_change_values(vols))

    futures_metrics = futures_metrics or {"valid_symbols": 0}
    return {
        "reference_trend_score": sum(reference_votes) / len(reference_votes) if reference_votes else 0.0,
        "reference_votes": len(reference_votes),
        "valid_breadth_coins": int(breadth["valid_coins"]),
        "breadth_above_ema20_pct": float(breadth["above_ema20_pct"]),
        "breadth_above_ema50_pct": float(breadth["above_ema50_pct"]),
        "breadth_advancers_24h_pct": float(breadth["advancers_24h_pct"]),
        "basket_ret_4h": basket_4h,
        "basket_ret_24h": basket_24h,
        "basket_vs_btc_24h": basket_24h - btc_24h,
        "basket_vs_eth_24h": basket_24h - eth_24h,
        "basket_vs_sol_24h": basket_24h - sol_24h,
        "return_dispersion_24h": float(breadth["return_dispersion_24h"]),
        "median_vol_24h": float(breadth["median_vol_24h"]),
        "downside_vol_24h": _safe_median(downside_vols),
        "median_volume_change_24h": _safe_median(volume_changes),
        "futures_valid_symbols": int(futures_metrics.get("valid_symbols", 0) or 0),
        "futures_funding_pct": float(futures_metrics.get("avg_funding_pct", 0.0) or 0.0),
        "futures_basis_pct": float(futures_metrics.get("avg_basis_pct", 0.0) or 0.0),
        "futures_oi_change_pct": float(futures_metrics.get("median_oi_value_change_pct", 0.0) or 0.0),
        "futures_taker_ratio": float(futures_metrics.get("avg_taker_buy_sell_ratio", 0.0) or 0.0),
    }


def strategy_utility_label(
    *,
    future_basket_ret: float,
    future_btc_ret: float,
    future_vol: float,
    fee_bps: float,
) -> str:
    """Label future window by which strategy family had utility after costs."""
    fee_pct = fee_bps / 100.0
    edge_vs_cash = future_basket_ret - fee_pct
    edge_vs_btc = future_basket_ret - future_btc_ret - fee_pct
    if future_basket_ret <= -7.0 and future_vol >= 8.0:
        return STORMY
    if edge_vs_cash > 1.0 and edge_vs_btc > 0.25:
        return BULL
    if future_basket_ret < -2.0 or (future_btc_ret < -2.5 and future_basket_ret < 0.0):
        return BEAR
    return SIDEWAYS


def classify_v2_scorecard(features: dict[str, float | int]) -> dict[str, Any]:
    """Interpretable Regime v2 scorecard; research-only, no live routing."""
    score = 0.0
    reasons = []
    ref = float(features["reference_trend_score"])
    above50 = float(features["breadth_above_ema50_pct"])
    adv = float(features["breadth_advancers_24h_pct"])
    basket24 = float(features["basket_ret_24h"])
    basket4 = float(features["basket_ret_4h"])
    vs_btc = float(features["basket_vs_btc_24h"])
    vol = float(features["median_vol_24h"])
    down_vol = float(features["downside_vol_24h"])
    dispersion = float(features["return_dispersion_24h"])
    oi = float(features["futures_oi_change_pct"])
    basis = float(features["futures_basis_pct"])
    taker = float(features["futures_taker_ratio"])
    funding = float(features["futures_funding_pct"])

    score += ref * 2.0
    if ref > 0.4:
        reasons.append(f"reference trend risk-on score {ref:+.2f}")
    elif ref < -0.4:
        reasons.append(f"reference trend risk-off score {ref:+.2f}")

    if above50 >= 0.70:
        score += 1.25
        reasons.append(f"broad participation: {above50:.0%} above EMA50")
    elif above50 <= 0.35:
        score -= 1.25
        reasons.append(f"weak participation: {above50:.0%} above EMA50")

    if adv >= 0.65 and basket24 > 1.0:
        score += 1.0
        reasons.append(f"basket advancing: {adv:.0%} advancers, median {basket24:+.1f}%")
    elif adv <= 0.35 and basket24 < -1.0:
        score -= 1.0
        reasons.append(f"basket declining: {adv:.0%} advancers, median {basket24:+.1f}%")

    if basket4 <= -2.5:
        score -= 0.75
        reasons.append(f"fast breadth selloff {basket4:+.1f}%")
    elif basket4 >= 2.5:
        score += 0.75
        reasons.append(f"fast breadth lift {basket4:+.1f}%")

    if vs_btc >= 1.0 and basket24 > 0:
        score += 0.75
        reasons.append(f"alt basket outperforming BTC by {vs_btc:+.1f}%")
    elif vs_btc <= -1.0:
        score -= 0.5
        reasons.append(f"alt basket lagging BTC by {vs_btc:+.1f}%")

    if oi >= 4.0 and (basket24 < -1.0 or basket4 < -1.0):
        score -= 0.75
        reasons.append(f"OI rising into weakness {oi:+.1f}%")
    elif oi >= 4.0 and basket24 > 1.0:
        score += 0.5
        reasons.append(f"OI confirms strength {oi:+.1f}%")

    if basis <= -0.04:
        score -= 0.25
        reasons.append(f"negative futures basis {basis:+.3f}%")
    elif basis >= 0.04:
        score += 0.25
        reasons.append(f"positive futures basis {basis:+.3f}%")

    if taker and taker <= 0.92:
        score -= 0.5
        reasons.append(f"bearish taker flow {taker:.2f}")
    elif taker >= 1.08:
        score += 0.5
        reasons.append(f"bullish taker flow {taker:.2f}")

    if funding >= 0.03 and basket24 < 0:
        score -= 0.25
        reasons.append(f"crowded long stress funding {funding:+.3f}%")

    stormy = (
        (vol >= 7.0 and basket24 <= -3.0)
        or (down_vol >= 6.0 and basket4 <= -2.5)
        or (dispersion >= 8.0 and basket24 <= -2.0)
        or (oi >= 8.0 and basket4 <= -2.0)
    )
    if stormy:
        regime = STORMY
    elif score >= 2.0:
        regime = BULL
    elif score <= -2.0:
        regime = BEAR
    else:
        regime = SIDEWAYS
    confidence = min(0.95, 0.45 + abs(score) / 6.0)
    if not reasons:
        reasons.append("mixed/low-conviction features")
    return {"regime": regime, "score": score, "confidence": confidence, "reasons": reasons[:6]}


def _timestamps_for(ohlcv_by_coin: dict[str, list[dict[str, float | int]]], ref_coin: str = "SOL") -> list[int]:
    return [int(row["ts"]) for row in ohlcv_by_coin.get(ref_coin, [])]


def _truncate_to_ts(ohlcv_by_coin: dict[str, list[dict[str, float | int]]], ts: int) -> dict[str, list[dict[str, float | int]]]:
    return {coin: [row for row in rows if int(row["ts"]) <= ts] for coin, rows in ohlcv_by_coin.items()}


def _hysteresis(regimes: list[str], confidences: list[float], confirmation_samples: int, min_confidence: float) -> list[str]:
    if not regimes:
        return []
    active = regimes[0]
    candidate = None
    count = 0
    out = []
    for regime, confidence in zip(regimes, confidences):
        if regime == active or confidence < min_confidence:
            candidate = None
            count = 0
        elif regime == candidate:
            count += 1
        else:
            candidate = regime
            count = 1
        if candidate and count >= max(1, confirmation_samples):
            active = candidate
            candidate = None
            count = 0
        out.append(active)
    return out


def _seq(regimes: list[str]) -> dict[str, Any]:
    if not regimes:
        return {"count": 0, "flips": 0, "distribution": {}, "median_dwell": 0.0}
    flips = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i - 1])
    distribution = {regime: regimes.count(regime) for regime in sorted(set(regimes))}
    dwell = []
    cur = 1
    for i in range(1, len(regimes)):
        if regimes[i] == regimes[i - 1]:
            cur += 1
        else:
            dwell.append(cur)
            cur = 1
    dwell.append(cur)
    return {"count": len(regimes), "flips": flips, "distribution": distribution, "median_dwell": median(dwell)}


def _accuracy(records: list[dict[str, Any]], key: str) -> float:
    if not records:
        return 0.0
    return sum(1 for row in records if row[key] == row["label"]) / len(records) * 100.0


def _avg_regime_return(records: list[dict[str, Any]], key: str, regime: str) -> float:
    vals = [float(row["future_basket_ret"]) for row in records if row[key] == regime]
    return sum(vals) / len(vals) if vals else 0.0


def _build_leaderboard(records: list[dict[str, Any]]) -> dict[str, Any]:
    legacy_acc = _accuracy(records, "legacy_regime")
    v1_acc = _accuracy(records, "v1_regime")
    v2_acc = _accuracy(records, "v2_smoothed")
    legacy_flips = _seq([row["legacy_regime"] for row in records])["flips"]
    v1_flips = _seq([row["v1_regime"] for row in records])["flips"]
    v2_flips = _seq([row["v2_smoothed"] for row in records])["flips"]
    v2_bull_ret = _avg_regime_return(records, "v2_smoothed", BULL)
    legacy_bull_ret = _avg_regime_return(records, "legacy_regime", BULL)
    return {
        "summary": {"total": len(records), "passed": int(v2_acc >= legacy_acc and v2_flips <= legacy_flips), "failed": int(not (v2_acc >= legacy_acc and v2_flips <= legacy_flips))},
        "by_metric": {
            "label_accuracy": [
                {"name": "regime_v2", "value": v2_acc},
                {"name": "research_v1", "value": v1_acc},
                {"name": "legacy_sol", "value": legacy_acc},
            ],
            "switching": [
                {"name": "regime_v2", "flips": v2_flips},
                {"name": "research_v1", "flips": v1_flips},
                {"name": "legacy_sol", "flips": legacy_flips},
            ],
            "relative_performance": [
                {"name": "v2_bull_forward_avg_pct", "value": v2_bull_ret},
                {"name": "legacy_bull_forward_avg_pct", "value": legacy_bull_ret},
            ],
        },
    }


def evaluate_regime_v2_history(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
    step_hours: int = 6,
    warmup_hours: int = 72,
    forward_hours: int = 24,
    confirmation_samples: int = 3,
    min_confidence: float = 0.60,
    fee_bps: float = 10.0,
) -> dict[str, Any]:
    """Walk-forward evaluate v2 vs v1 and legacy, using next-window labels."""
    timestamps = _timestamps_for(ohlcv_by_coin, "SOL")
    selected = []
    next_ts = timestamps[0] + warmup_hours * HOUR_MS if timestamps else 0
    max_ts = timestamps[-1] - forward_hours * HOUR_MS if timestamps else 0
    for ts in timestamps:
        if ts > max_ts:
            break
        if ts >= next_ts:
            selected.append(ts)
            next_ts = ts + max(1, step_hours) * HOUR_MS

    raw_records = []
    for ts in selected:
        window = _truncate_to_ts(ohlcv_by_coin, ts)
        features = build_feature_snapshot(window, references=references, breadth_coins=breadth_coins)
        v2 = classify_v2_scorecard(features)
        v1 = _regime.classify_regime(window, references=references, breadth_coins=breadth_coins)
        legacy = _regime.legacy_sol_regime(window)
        future = _future_basket_stats(ohlcv_by_coin, breadth_coins, ts, forward_hours)
        btc_rows = ohlcv_by_coin.get("BTC") or ohlcv_by_coin.get("SOL") or []
        btc_idx = next((i for i, row in enumerate(btc_rows) if int(row["ts"]) >= ts), -1)
        future_btc_ret = _future_return(btc_rows, btc_idx, forward_hours) if btc_idx >= 0 else 0.0
        label = strategy_utility_label(
            future_basket_ret=future["future_basket_ret"],
            future_btc_ret=future_btc_ret,
            future_vol=future["future_vol"],
            fee_bps=fee_bps,
        )
        raw_records.append(
            {
                "ts": ts,
                "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "legacy_regime": legacy,
                "v1_regime": v1.regime,
                "v2_regime": v2["regime"],
                "label": label,
                "score": v2["score"],
                "confidence": v2["confidence"],
                "future_basket_ret": future["future_basket_ret"],
                "future_btc_ret": future_btc_ret,
                "future_vol": future["future_vol"],
                "features": features,
                "reasons": v2["reasons"],
            }
        )

    smoothed = _hysteresis(
        [row["v2_regime"] for row in raw_records],
        [float(row["confidence"]) for row in raw_records],
        confirmation_samples,
        min_confidence,
    )
    for row, regime in zip(raw_records, smoothed):
        row["v2_smoothed"] = regime

    data_hash = hashlib.sha256(
        json.dumps({coin: rows[-5:] for coin, rows in sorted(ohlcv_by_coin.items())}, sort_keys=True).encode()
    ).hexdigest()
    return {
        "manifest": {
            "script": "research_regime_v2_evaluator.py",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_hash": data_hash,
            "assumptions": {
                "fee_bps": fee_bps,
                "step_hours": step_hours,
                "warmup_hours": warmup_hours,
                "forward_hours": forward_hours,
                "confirmation_samples": confirmation_samples,
                "min_confidence": min_confidence,
                "research_only": True,
            },
        },
        "records": raw_records,
        "sequence": {
            "legacy": _seq([row["legacy_regime"] for row in raw_records]),
            "research_v1": _seq([row["v1_regime"] for row in raw_records]),
            "regime_v2_raw": _seq([row["v2_regime"] for row in raw_records]),
            "regime_v2_smoothed": _seq([row["v2_smoothed"] for row in raw_records]),
            "labels": _seq([row["label"] for row in raw_records]),
        },
        "leaderboard": _build_leaderboard(raw_records),
    }


def _parse_csv(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Research-only Regime v2 evaluator")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--coins", default=",".join(DEFAULT_BREADTH_COINS))
    parser.add_argument("--references", default=",".join(DEFAULT_REFERENCES))
    parser.add_argument("--step-hours", type=int, default=6)
    parser.add_argument("--warmup-hours", type=int, default=72)
    parser.add_argument("--forward-hours", type=int, default=24)
    parser.add_argument("--confirmation-samples", type=int, default=3)
    parser.add_argument("--min-confidence", type=float, default=0.60)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    coins = _parse_csv(args.coins)
    references = _parse_csv(args.references)
    data = _regime.fetch_market_data(coins, references=references, days=args.days)
    output = evaluate_regime_v2_history(
        data,
        references=references,
        breadth_coins=coins,
        step_hours=args.step_hours,
        warmup_hours=args.warmup_hours,
        forward_hours=args.forward_hours,
        confirmation_samples=args.confirmation_samples,
        min_confidence=args.min_confidence,
        fee_bps=args.fee_bps,
    )

    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2))
    lb = output["leaderboard"]
    seq = output["sequence"]
    print(
        f"Regime v2 samples={lb['summary']['total']} "
        f"accuracy(v2/v1/legacy)="
        f"{lb['by_metric']['label_accuracy'][0]['value']:.1f}/"
        f"{lb['by_metric']['label_accuracy'][1]['value']:.1f}/"
        f"{lb['by_metric']['label_accuracy'][2]['value']:.1f}% "
        f"flips(v2/legacy)={seq['regime_v2_smoothed']['flips']}/{seq['legacy']['flips']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
