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
BINANCE_FAPI = "https://fapi.binance.com"
BRIDGE = "USDC"
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"
DEFAULT_REFERENCES = ("BTC", "ETH", "SOL")
DEFAULT_FUTURES_SYMBOLS = ("BTCUSDC", "ETHUSDC", "SOLUSDC")
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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_futures_json(path: str, params: dict[str, Any]) -> Any:
    """Fetch a Binance USDC-M/USDT-M public futures endpoint."""
    resp = requests.get(f"{BINANCE_FAPI}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_futures_signals(
    symbols: Iterable[str] = DEFAULT_FUTURES_SYMBOLS,
    *,
    period: str = "1h",
    limit: int = 24,
) -> dict[str, dict[str, Any]]:
    """Fetch public futures sentiment/risk signals for the given symbols.

    Uses free Binance endpoints only. Missing symbols/endpoints are skipped so a
    single unavailable futures market does not break the research classifier.
    """
    out: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        payload: dict[str, Any] = {}
        endpoint_specs = {
            "premium": ("/fapi/v1/premiumIndex", {"symbol": symbol}),
            "funding": ("/fapi/v1/fundingRate", {"symbol": symbol, "limit": min(limit, 100)}),
            "open_interest_hist": (
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": period, "limit": min(limit, 500)},
            ),
            "global_long_short": (
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": period, "limit": min(limit, 500)},
            ),
            "top_long_short": (
                "/futures/data/topLongShortAccountRatio",
                {"symbol": symbol, "period": period, "limit": min(limit, 500)},
            ),
            "taker_long_short": (
                "/futures/data/takerlongshortRatio",
                {"symbol": symbol, "period": period, "limit": min(limit, 500)},
            ),
        }
        for key, (path, params) in endpoint_specs.items():
            try:
                payload[key] = fetch_futures_json(path, params)
            except requests.RequestException as exc:
                payload.setdefault("errors", {})[key] = str(exc)
            time.sleep(0.05)
        out[symbol] = payload
    return out


def _change_pct(values: list[float]) -> float:
    values = [value for value in values if value > 0]
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] / values[0] - 1.0) * 100.0


def _futures_metrics(futures_data: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Aggregate public futures signals into compact sentiment metrics."""
    if not futures_data:
        return {"valid_symbols": 0}

    basis_values: list[float] = []
    funding_values: list[float] = []
    oi_value_changes: list[float] = []
    global_lsr_values: list[float] = []
    top_lsr_values: list[float] = []
    taker_buy_sell_values: list[float] = []
    per_symbol: dict[str, dict[str, float]] = {}

    for symbol, payload in futures_data.items():
        metrics: dict[str, float] = {}

        premium = payload.get("premium") or {}
        mark = _safe_float(premium.get("markPrice"))
        index = _safe_float(premium.get("indexPrice"))
        if mark and index:
            basis_pct = (mark / index - 1.0) * 100.0
            basis_values.append(basis_pct)
            metrics["basis_pct"] = basis_pct
        if premium.get("lastFundingRate") is not None:
            funding_pct = _safe_float(premium.get("lastFundingRate")) * 100.0
            funding_values.append(funding_pct)
            metrics["funding_pct"] = funding_pct

        funding = payload.get("funding") or []
        if funding:
            latest_funding_pct = _safe_float(funding[-1].get("fundingRate")) * 100.0
            metrics["latest_funding_pct"] = latest_funding_pct
            if "funding_pct" not in metrics:
                funding_values.append(latest_funding_pct)

        oi_hist = payload.get("open_interest_hist") or []
        oi_values = [_safe_float(row.get("sumOpenInterestValue")) for row in oi_hist]
        oi_change = _change_pct(oi_values)
        if oi_values:
            oi_value_changes.append(oi_change)
            metrics["oi_value_change_pct"] = oi_change

        global_lsr = payload.get("global_long_short") or []
        if global_lsr:
            value = _safe_float(global_lsr[-1].get("longShortRatio"))
            global_lsr_values.append(value)
            metrics["global_long_short_ratio"] = value

        top_lsr = payload.get("top_long_short") or []
        if top_lsr:
            value = _safe_float(top_lsr[-1].get("longShortRatio"))
            top_lsr_values.append(value)
            metrics["top_long_short_ratio"] = value

        taker_lsr = payload.get("taker_long_short") or []
        if taker_lsr:
            value = _safe_float(taker_lsr[-1].get("buySellRatio"))
            taker_buy_sell_values.append(value)
            metrics["taker_buy_sell_ratio"] = value

        if metrics:
            per_symbol[symbol] = metrics

    return {
        "valid_symbols": len(per_symbol),
        "avg_basis_pct": sum(basis_values) / len(basis_values) if basis_values else 0.0,
        "avg_funding_pct": sum(funding_values) / len(funding_values) if funding_values else 0.0,
        "median_oi_value_change_pct": median(oi_value_changes) if oi_value_changes else 0.0,
        "avg_global_long_short_ratio": sum(global_lsr_values) / len(global_lsr_values) if global_lsr_values else 0.0,
        "avg_top_long_short_ratio": sum(top_lsr_values) / len(top_lsr_values) if top_lsr_values else 0.0,
        "avg_taker_buy_sell_ratio": sum(taker_buy_sell_values) / len(taker_buy_sell_values) if taker_buy_sell_values else 0.0,
        "symbols": per_symbol,
    }


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


def _timestamps_for(ohlcv_by_coin: dict[str, list[dict[str, float | int]]], ref_coin: str = "SOL") -> list[int]:
    return [int(row["ts"]) for row in ohlcv_by_coin.get(ref_coin, [])]


def _truncate_to_ts(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    ts: int,
) -> dict[str, list[dict[str, float | int]]]:
    return {
        coin: [row for row in candles if int(row["ts"]) <= ts]
        for coin, candles in ohlcv_by_coin.items()
    }


def legacy_sol_regime(ohlcv_by_coin: dict[str, list[dict[str, float | int]]]) -> str:
    """Approximate the current SOL-only ADX/EMA live regime rule for comparison."""
    candles = ohlcv_by_coin.get("SOL", [])
    if len(candles) < 60:
        return SIDEWAYS
    recent = candles[-60:]
    highs = [float(row["high"]) for row in recent]
    lows = [float(row["low"]) for row in recent]
    closes = [float(row["close"]) for row in recent]
    adx, plus_di, minus_di = _adx(highs, lows, closes, 14)
    ema_long = _ema(closes, 50)
    current_price = closes[-1]
    if adx >= 25:
        if current_price > ema_long and plus_di > minus_di:
            return BULL
        if current_price < ema_long and minus_di > plus_di:
            return BEAR
    return SIDEWAYS


def _sequence_summary(regimes: list[str]) -> dict[str, Any]:
    if not regimes:
        return {"count": 0, "flips": 0, "distribution": {}, "median_dwell": 0.0}
    distribution = {regime: regimes.count(regime) for regime in sorted(set(regimes))}
    flips = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i - 1])
    dwell_lengths: list[int] = []
    current_len = 1
    for i in range(1, len(regimes)):
        if regimes[i] == regimes[i - 1]:
            current_len += 1
        else:
            dwell_lengths.append(current_len)
            current_len = 1
    dwell_lengths.append(current_len)
    return {
        "count": len(regimes),
        "flips": flips,
        "flip_rate": flips / max(1, len(regimes) - 1),
        "distribution": distribution,
        "median_dwell": median(dwell_lengths),
    }


def compare_regime_history(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
    step_hours: int = 4,
    warmup_hours: int = 60,
) -> dict[str, Any]:
    """Compare multi-signal regimes vs the current SOL-only classifier over history."""
    timestamps = _timestamps_for(ohlcv_by_coin, "SOL")
    if not timestamps:
        return {"samples": [], "multi": _sequence_summary([]), "legacy": _sequence_summary([]), "disagreement_pct": 0.0}

    step_ms = max(1, step_hours) * HOUR_MS
    warmup_ms = warmup_hours * HOUR_MS
    start_ts = timestamps[0] + warmup_ms
    selected: list[int] = []
    next_ts = start_ts
    for ts in timestamps:
        if ts >= next_ts:
            selected.append(ts)
            next_ts = ts + step_ms

    samples = []
    for ts in selected:
        window = _truncate_to_ts(ohlcv_by_coin, ts)
        multi = classify_regime(window, references=references, breadth_coins=breadth_coins)
        legacy = legacy_sol_regime(window)
        samples.append(
            {
                "ts": ts,
                "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "multi": multi.regime,
                "legacy": legacy,
                "confidence": multi.confidence,
                "score": multi.score,
            }
        )

    multi_regimes = [row["multi"] for row in samples]
    legacy_regimes = [row["legacy"] for row in samples]
    disagreements = sum(1 for row in samples if row["multi"] != row["legacy"])
    return {
        "samples": samples,
        "multi": _sequence_summary(multi_regimes),
        "legacy": _sequence_summary(legacy_regimes),
        "disagreement_pct": disagreements / len(samples) * 100.0 if samples else 0.0,
    }


def classify_regime(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
    futures_data: dict[str, dict[str, Any]] | None = None,
) -> RegimeResult:
    """Classify market regime using spot trend/breadth plus optional futures signals."""
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

    futures = _futures_metrics(futures_data)
    futures_valid = int(futures.get("valid_symbols", 0) or 0)
    avg_basis = float(futures.get("avg_basis_pct", 0.0) or 0.0)
    avg_funding = float(futures.get("avg_funding_pct", 0.0) or 0.0)
    oi_change = float(futures.get("median_oi_value_change_pct", 0.0) or 0.0)
    global_lsr = float(futures.get("avg_global_long_short_ratio", 0.0) or 0.0)
    top_lsr = float(futures.get("avg_top_long_short_ratio", 0.0) or 0.0)
    taker_ratio = float(futures.get("avg_taker_buy_sell_ratio", 0.0) or 0.0)

    if futures_valid:
        # Rising open interest confirms moves when it agrees with spot/breadth;
        # it raises storm risk when it builds into a falling market.
        if oi_change >= 3.0 and (median24 > 1.0 or btc_ret24 > 1.0):
            score += 0.5
            reasons.append(f"futures confirmation: OI value +{oi_change:.1f}% into rising spot")
        elif oi_change >= 3.0 and (median24 < -1.0 or btc_ret24 < -1.0):
            score -= 0.5
            reasons.append(f"futures risk-off confirmation: OI value +{oi_change:.1f}% into falling spot")

        if avg_basis >= 0.03:
            score += 0.25
            reasons.append(f"futures basis positive: mark/index premium {avg_basis:+.3f}%")
        elif avg_basis <= -0.03:
            score -= 0.25
            reasons.append(f"futures basis negative: mark/index discount {avg_basis:+.3f}%")

        if taker_ratio >= 1.08:
            score += 0.5
            reasons.append(f"futures taker flow bullish: buy/sell ratio {taker_ratio:.2f}")
        elif 0.0 < taker_ratio <= 0.92:
            score -= 0.5
            reasons.append(f"futures taker flow bearish: buy/sell ratio {taker_ratio:.2f}")

        if avg_funding >= 0.02 and max(global_lsr, top_lsr) >= 1.8:
            score -= 0.25
            reasons.append(
                f"crowded long risk: funding {avg_funding:+.3f}% and long/short ratio {max(global_lsr, top_lsr):.2f}"
            )
        elif avg_funding <= -0.02 and 0 < min(global_lsr or 99, top_lsr or 99) <= 0.8:
            score += 0.25
            reasons.append(
                f"crowded short squeeze risk: funding {avg_funding:+.3f}% and long/short ratio {min(global_lsr or 99, top_lsr or 99):.2f}"
            )

    futures_storm_risk = bool(
        futures_valid
        and (
            (oi_change >= 8.0 and (median24 <= -2.0 or btc_ret4 <= -2.0) and (taker_ratio == 0.0 or taker_ratio <= 0.95))
            or (abs(avg_basis) >= 0.12 and oi_change >= 5.0)
        )
    )

    stormy_condition = (
        (vol24 >= 5.0 and median24 <= -3.0 and advancers <= 0.35)
        or (dispersion >= 8.0 and median24 <= -2.0)
        or (btc_ret4 <= -3.0 and advancers <= 0.35)
        or (btc_ret24 <= -5.0 and above50 <= 0.35)
        or futures_storm_risk
    )

    metrics_out = {
        "score": score,
        "reference_votes": ref_votes,
        "references": ref_metrics,
        "breadth": breadth,
        "futures": futures,
        "btc_ret_4h": btc_ret4,
        "btc_ret_24h": btc_ret24,
    }

    if stormy_condition:
        reasons.append(
            f"storm risk: vol {vol24:.1f}%, dispersion {dispersion:.1f}%, "
            f"median24 {median24:+.1f}%, advancers {advancers:.0%}"
        )
        if futures_storm_risk:
            reasons.append(
                f"futures storm risk: OI {oi_change:+.1f}%, basis {avg_basis:+.3f}%, taker buy/sell {taker_ratio:.2f}"
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
    parser.add_argument("--include-futures", action="store_true", help="Fetch and include Binance public futures sentiment signals")
    parser.add_argument("--futures-symbols", default=",".join(DEFAULT_FUTURES_SYMBOLS), help="Comma-separated futures symbols, e.g. BTCUSDC,ETHUSDC")
    parser.add_argument("--futures-period", default="1h", help="Futures data period for OI/ratio endpoints")
    parser.add_argument("--futures-limit", type=int, default=24, help="Rows to fetch from futures history endpoints")
    parser.add_argument("--history-compare", action="store_true", help="Compare multi-signal history with the current SOL-only classifier")
    parser.add_argument("--history-step-hours", type=int, default=4, help="Sample spacing for --history-compare")
    parser.add_argument("--history-warmup-hours", type=int, default=60, help="Initial warmup before --history-compare sampling")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    coins = [coin.strip().upper() for coin in args.coins.split(",") if coin.strip()]
    references = [coin.strip().upper() for coin in args.references.split(",") if coin.strip()]
    futures_symbols = [symbol.strip().upper() for symbol in args.futures_symbols.split(",") if symbol.strip()]
    data = fetch_market_data(coins, references=references, interval=args.interval, days=args.days)
    futures_data = None
    if args.include_futures:
        futures_data = fetch_futures_signals(
            futures_symbols,
            period=args.futures_period,
            limit=args.futures_limit,
        )
    result = classify_regime(data, references=references, breadth_coins=coins, futures_data=futures_data)
    history = None
    if args.history_compare:
        history = compare_regime_history(
            data,
            references=references,
            breadth_coins=coins,
            step_hours=args.history_step_hours,
            warmup_hours=args.history_warmup_hours,
        )
    if args.json:
        payload = result.to_dict()
        if history is not None:
            payload = {"current": payload, "history_compare": history}
        print(json.dumps(payload, indent=2, sort_keys=True))
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
    futures = result.metrics.get("futures", {})
    if futures.get("valid_symbols", 0):
        print("\nFutures:")
        print(
            f"  symbols={futures['valid_symbols']} basis={futures['avg_basis_pct']:+.3f}% "
            f"funding={futures['avg_funding_pct']:+.3f}% "
            f"OI={futures['median_oi_value_change_pct']:+.1f}% "
            f"taker={futures['avg_taker_buy_sell_ratio']:.2f}"
        )
    if history is not None:
        print("\nHistory comparison vs current SOL-only classifier:")
        print(
            f"  samples={history['multi']['count']} disagreement={history['disagreement_pct']:.1f}% "
            f"multi_flips={history['multi']['flips']} legacy_flips={history['legacy']['flips']}"
        )
        print(f"  multi_distribution={history['multi']['distribution']}")
        print(f"  legacy_distribution={history['legacy']['distribution']}")
    return result


if __name__ == "__main__":
    main()
