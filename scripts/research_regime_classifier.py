#!/usr/bin/env python3
"""Research-only multi-signal crypto regime classifier.

This module does not affect live trading. It is a research foundation for the
all-weather strategy roadmap: combine free Binance spot data into a regime,
confidence, and explainable reasons before any future live integration.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

import importlib.util
import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "indicators", _REPO_ROOT / "binance_trade_bot" / "indicators.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Could not load binance_trade_bot/indicators.py")
_indicators_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_indicators_mod)
_ema = _indicators_mod.compute_ema
_adx = _indicators_mod.compute_adx

BINANCE_API = "https://api.binance.com/api/v3"
BRIDGE = "USDC"
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"
DEFAULT_REFERENCES = ("BTC", "ETH", "SOL")
DEFAULT_BREADTH_COINS = (
    "SOL", "SUI", "XRP", "ADA", "DOGE", "NEAR", "LINK", "AAVE", "AVAX",
    "APT", "INJ", "TIA", "ENA", "PEPE", "JUP",
)
HOUR_MS = 3600 * 1000
DAY_MS = 86400 * 1000


@dataclass
class RegimeResult:
    regime: str
    confidence: float
    score: float
    reasons: list[str]
    metrics: dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


def parse_klines(raw_klines: list[list]) -> list[dict[str, float | int]]:
    return [
        {
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw_klines
    ]


def fetch_klines(symbol: str, *, interval: str = "1h", days: int = 14) -> list[dict[str, float | int]]:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * DAY_MS
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cur,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        rows.extend(data)
        cur = data[-1][0] + 1
        if len(data) < 1000:
            break
        time.sleep(0.12)
    return parse_klines(rows)


def fetch_market_data(
    coins: Iterable[str],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    bridge: str = BRIDGE,
    interval: str = "1h",
    days: int = 14,
) -> dict[str, list[dict[str, float | int]]]:
    symbols = []
    for coin in [*references, *coins]:
        if coin not in symbols:
            symbols.append(coin)

    out = {}
    for coin in symbols:
        out[coin] = fetch_klines(f"{coin}{bridge}", interval=interval, days=days)
        time.sleep(0.05)
    return out


def pct_change(closes: list[float], periods: int) -> float:
    if len(closes) <= periods or closes[-periods - 1] == 0:
        return 0.0
    return (closes[-1] / closes[-periods - 1] - 1.0) * 100.0


def realized_volatility(closes: list[float], periods: int = 24) -> float:
    if len(closes) <= periods:
        return 0.0
    window = closes[-periods - 1 :]
    returns = [
        (window[i] / window[i - 1] - 1.0)
        for i in range(1, len(window))
        if window[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((ret - mean) ** 2 for ret in returns) / len(returns)
    # Return dailyized percent volatility from hourly candles.
    return math.sqrt(variance) * math.sqrt(24) * 100.0


def _reference_metrics(candles: list[dict[str, float | int]]) -> dict[str, float]:
    closes = [float(row["close"]) for row in candles]
    highs = [float(row["high"]) for row in candles]
    lows = [float(row["low"]) for row in candles]
    if len(closes) < 60:
        return {}

    recent_highs = highs[-60:]
    recent_lows = lows[-60:]
    recent_closes = closes[-60:]
    adx, plus_di, minus_di = _adx(recent_highs, recent_lows, recent_closes, 14)
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    price = closes[-1]
    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "ret_1h": pct_change(closes, 1),
        "ret_4h": pct_change(closes, 4),
        "ret_24h": pct_change(closes, 24),
        "vol_24h": realized_volatility(closes, 24),
    }


def _breadth_metrics(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    breadth_coins: Iterable[str],
) -> dict[str, float | int]:
    above_ema20 = 0
    above_ema50 = 0
    advancers_24h = 0
    returns_4h: list[float] = []
    returns_24h: list[float] = []
    vols_24h: list[float] = []
    valid = 0

    for coin in breadth_coins:
        candles = ohlcv_by_coin.get(coin, [])
        closes = [float(row["close"]) for row in candles]
        if len(closes) < 60:
            continue
        valid += 1
        price = closes[-1]
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        ret4 = pct_change(closes, 4)
        ret24 = pct_change(closes, 24)
        vol24 = realized_volatility(closes, 24)
        above_ema20 += int(price > ema20)
        above_ema50 += int(price > ema50)
        advancers_24h += int(ret24 > 0)
        returns_4h.append(ret4)
        returns_24h.append(ret24)
        vols_24h.append(vol24)

    if valid == 0:
        return {
            "valid_coins": 0,
            "above_ema20_pct": 0.0,
            "above_ema50_pct": 0.0,
            "advancers_24h_pct": 0.0,
            "median_ret_4h": 0.0,
            "median_ret_24h": 0.0,
            "return_dispersion_24h": 0.0,
            "median_vol_24h": 0.0,
        }

    med24 = median(returns_24h)
    dispersion = median([abs(ret - med24) for ret in returns_24h]) if returns_24h else 0.0
    return {
        "valid_coins": valid,
        "above_ema20_pct": above_ema20 / valid,
        "above_ema50_pct": above_ema50 / valid,
        "advancers_24h_pct": advancers_24h / valid,
        "median_ret_4h": median(returns_4h) if returns_4h else 0.0,
        "median_ret_24h": med24,
        "return_dispersion_24h": dispersion,
        "median_vol_24h": median(vols_24h) if vols_24h else 0.0,
    }


def classify_regime(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
) -> RegimeResult:
    """Classify market regime using trend, breadth, return, and volatility signals."""
    reasons: list[str] = []
    score = 0.0
    ref_metrics: dict[str, dict[str, float]] = {}
    ref_votes: list[float] = []

    for ref in references:
        metrics = _reference_metrics(ohlcv_by_coin.get(ref, []))
        if not metrics:
            continue
        ref_metrics[ref] = metrics
        vote = 0.0
        if metrics["adx"] >= 20:
            if metrics["ema20"] > metrics["ema50"] and metrics["plus_di"] > metrics["minus_di"]:
                vote += 1.0
                reasons.append(f"{ref} trend up: EMA20>EMA50, +DI>-DI, ADX {metrics['adx']:.1f}")
            elif metrics["ema20"] < metrics["ema50"] and metrics["minus_di"] > metrics["plus_di"]:
                vote -= 1.0
                reasons.append(f"{ref} trend down: EMA20<EMA50, -DI>+DI, ADX {metrics['adx']:.1f}")
        vote += 0.5 if metrics["ret_24h"] > 2.0 else 0.0
        vote -= 0.5 if metrics["ret_24h"] < -2.0 else 0.0
        vote += 0.25 if metrics["price"] > metrics["ema50"] else -0.25
        ref_votes.append(vote)

    if ref_votes:
        score += sum(ref_votes) / len(ref_votes) * 2.0

    breadth = _breadth_metrics(ohlcv_by_coin, breadth_coins)
    above50 = float(breadth["above_ema50_pct"])
    advancers = float(breadth["advancers_24h_pct"])
    median24 = float(breadth["median_ret_24h"])
    median4 = float(breadth["median_ret_4h"])
    vol24 = float(breadth["median_vol_24h"])
    dispersion = float(breadth["return_dispersion_24h"])

    if above50 >= 0.65:
        score += 1.0
        reasons.append(f"breadth risk-on: {above50:.0%} of tracked coins above EMA50")
    elif above50 <= 0.35:
        score -= 1.0
        reasons.append(f"breadth risk-off: only {above50:.0%} of tracked coins above EMA50")

    if advancers >= 0.65 and median24 > 1.0:
        score += 1.0
        reasons.append(f"broad 24h advance: {advancers:.0%} advancers, median {median24:+.1f}%")
    elif advancers <= 0.35 and median24 < -1.0:
        score -= 1.0
        reasons.append(f"broad 24h decline: {advancers:.0%} advancers, median {median24:+.1f}%")

    if median4 <= -2.5:
        score -= 0.5
        reasons.append(f"fast 4h breadth drop: median {median4:+.1f}%")
    elif median4 >= 2.5:
        score += 0.5
        reasons.append(f"fast 4h breadth lift: median {median4:+.1f}%")

    btc = ref_metrics.get("BTC") or ref_metrics.get("SOL") or {}
    btc_ret4 = float(btc.get("ret_4h", 0.0))
    btc_ret24 = float(btc.get("ret_24h", 0.0))

    stormy_condition = (
        (vol24 >= 5.0 and median24 <= -3.0 and advancers <= 0.35)
        or (dispersion >= 8.0 and median24 <= -2.0)
        or (btc_ret4 <= -3.0 and advancers <= 0.35)
        or (btc_ret24 <= -5.0 and above50 <= 0.35)
    )

    metrics_out = {
        "score": score,
        "reference_votes": ref_votes,
        "references": ref_metrics,
        "breadth": breadth,
        "btc_ret_4h": btc_ret4,
        "btc_ret_24h": btc_ret24,
    }

    if stormy_condition:
        reasons.append(
            f"storm risk: vol {vol24:.1f}%, dispersion {dispersion:.1f}%, "
            f"median24 {median24:+.1f}%, advancers {advancers:.0%}"
        )
        confidence = min(0.95, 0.65 + min(0.25, abs(median24) / 20.0) + min(0.10, vol24 / 100.0))
        return RegimeResult(STORMY, round(confidence, 3), round(score, 3), reasons, metrics_out)

    if score >= 2.0:
        regime = BULL
    elif score <= -2.0:
        regime = BEAR
    else:
        regime = SIDEWAYS
        if not reasons:
            reasons.append("mixed/low-conviction signals: no strong trend or breadth agreement")
        else:
            reasons.append("mixed signals: score stayed inside sideways band")

    agreement = abs(sum(1 for vote in ref_votes if vote > 0) - sum(1 for vote in ref_votes if vote < 0))
    agreement_factor = agreement / max(1, len(ref_votes))
    confidence = 0.45 + min(0.35, abs(score) / 6.0) + 0.15 * agreement_factor
    if regime == SIDEWAYS:
        confidence = 0.50 + min(0.25, (2.0 - abs(score)) / 8.0)
    return RegimeResult(regime, round(min(0.95, confidence), 3), round(score, 3), reasons, metrics_out)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14, help="History length in days")
    parser.add_argument("--interval", default="1h", help="Binance kline interval")
    parser.add_argument("--coins", default=",".join(DEFAULT_BREADTH_COINS), help="Comma-separated breadth coins")
    parser.add_argument("--references", default=",".join(DEFAULT_REFERENCES), help="Comma-separated reference coins")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    references = [coin.strip().upper() for coin in args.references.split(",") if coin.strip()]
    data = fetch_market_data(coins, references=references, interval=args.interval, days=args.days)
    result = classify_regime(data, references=references, breadth_coins=coins)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return result

    print(f"Regime: {result.regime.upper()}  confidence={result.confidence:.0%}  score={result.score:+.2f}")
    print("Reasons:")
    for reason in result.reasons:
        print(f"- {reason}")
    breadth = result.metrics["breadth"]
    print("\nBreadth:")
    print(
        f"  valid={breadth['valid_coins']} aboveEMA50={breadth['above_ema50_pct']:.0%} "
        f"advancers24h={breadth['advancers_24h_pct']:.0%} "
        f"median24h={breadth['median_ret_24h']:+.1f}% vol24h={breadth['median_vol_24h']:.1f}%"
    )
    return result


if __name__ == "__main__":
    main()
