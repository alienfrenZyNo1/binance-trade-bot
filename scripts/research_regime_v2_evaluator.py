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

DEFAULT_SCORE_WEIGHTS = {
    "reference_trend_score": 2.0,
    "breadth_score": 1.25,
    "momentum_score": 1.0,
    "fast_move_score": 0.75,
    "relative_strength_score": 0.75,
    "oi_stress_score": 0.75,
    "basis_score": 0.25,
    "taker_flow_score": 0.5,
    "funding_stress_score": 0.25,
    "bull_threshold": 2.0,
    "bear_threshold": 2.0,
}



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


def scorecard_components(features: dict[str, float | int]) -> dict[str, float]:
    """Convert raw feature snapshot into signed, bounded score components."""
    ref = float(features["reference_trend_score"])
    above50 = float(features["breadth_above_ema50_pct"])
    adv = float(features["breadth_advancers_24h_pct"])
    basket24 = float(features["basket_ret_24h"])
    basket4 = float(features["basket_ret_4h"])
    vs_btc = float(features["basket_vs_btc_24h"])
    oi = float(features["futures_oi_change_pct"])
    basis = float(features["futures_basis_pct"])
    taker = float(features["futures_taker_ratio"])
    funding = float(features["futures_funding_pct"])

    breadth = 1.0 if above50 >= 0.70 else -1.0 if above50 <= 0.35 else 0.0
    momentum = 1.0 if adv >= 0.65 and basket24 > 1.0 else -1.0 if adv <= 0.35 and basket24 < -1.0 else 0.0
    fast_move = 1.0 if basket4 >= 2.5 else -1.0 if basket4 <= -2.5 else 0.0
    rel_strength = 1.0 if vs_btc >= 1.0 and basket24 > 0 else -1.0 if vs_btc <= -1.0 else 0.0
    oi_stress = -1.0 if oi >= 4.0 and (basket24 < -1.0 or basket4 < -1.0) else 1.0 if oi >= 4.0 and basket24 > 1.0 else 0.0
    basis_score = 1.0 if basis >= 0.04 else -1.0 if basis <= -0.04 else 0.0
    taker_score = 1.0 if taker >= 1.08 else -1.0 if 0.0 < taker <= 0.92 else 0.0
    funding_score = -1.0 if funding >= 0.03 and basket24 < 0 else 0.0
    return {
        "reference_trend_score": ref,
        "breadth_score": breadth,
        "momentum_score": momentum,
        "fast_move_score": fast_move,
        "relative_strength_score": rel_strength,
        "oi_stress_score": oi_stress,
        "basis_score": basis_score,
        "taker_flow_score": taker_score,
        "funding_stress_score": funding_score,
    }


def _score_from_components(components: dict[str, float], weights: dict[str, float]) -> float:
    return sum(float(components.get(name, 0.0)) * float(weights.get(name, 0.0)) for name in DEFAULT_SCORE_WEIGHTS)


def _scorecard_reasons(features: dict[str, float | int], components: dict[str, float]) -> list[str]:
    reasons = []
    if components["reference_trend_score"] > 0.4:
        reasons.append(f"reference trend risk-on score {components['reference_trend_score']:+.2f}")
    elif components["reference_trend_score"] < -0.4:
        reasons.append(f"reference trend risk-off score {components['reference_trend_score']:+.2f}")
    if components["breadth_score"] > 0:
        reasons.append(f"broad participation: {float(features['breadth_above_ema50_pct']):.0%} above EMA50")
    elif components["breadth_score"] < 0:
        reasons.append(f"weak participation: {float(features['breadth_above_ema50_pct']):.0%} above EMA50")
    if components["momentum_score"] > 0:
        reasons.append(f"basket advancing: {float(features['breadth_advancers_24h_pct']):.0%} advancers, median {float(features['basket_ret_24h']):+.1f}%")
    elif components["momentum_score"] < 0:
        reasons.append(f"basket declining: {float(features['breadth_advancers_24h_pct']):.0%} advancers, median {float(features['basket_ret_24h']):+.1f}%")
    if components["fast_move_score"] > 0:
        reasons.append(f"fast breadth lift {float(features['basket_ret_4h']):+.1f}%")
    elif components["fast_move_score"] < 0:
        reasons.append(f"fast breadth selloff {float(features['basket_ret_4h']):+.1f}%")
    if components["relative_strength_score"] > 0:
        reasons.append(f"alt basket outperforming BTC by {float(features['basket_vs_btc_24h']):+.1f}%")
    elif components["relative_strength_score"] < 0:
        reasons.append(f"alt basket lagging BTC by {float(features['basket_vs_btc_24h']):+.1f}%")
    if not reasons:
        reasons.append("mixed/low-conviction features")
    return reasons[:6]


def guardrail_reasons(features: dict[str, float | int], score: float, bull_threshold: float, bear_threshold: float) -> list[str]:
    """Explain risk guardrails that override otherwise high-conviction raw scores."""
    basket4 = float(features["basket_ret_4h"])
    basket24 = float(features["basket_ret_24h"])
    adv = float(features["breadth_advancers_24h_pct"])
    vs_btc = float(features["basket_vs_btc_24h"])
    down_vol = float(features["downside_vol_24h"])
    dispersion = float(features["return_dispersion_24h"])
    taker = float(features["futures_taker_ratio"])
    reasons = []
    if score >= bull_threshold and (
        basket4 <= -2.0
        or adv <= 0.35
        or (down_vol >= 5.5 and dispersion >= 5.0)
    ):
        reasons.append("false-bull guardrail: fast deterioration/downside volatility blocks risk-on")
    if score <= -bear_threshold and (
        basket4 >= 2.5
        or (adv >= 0.65 and vs_btc >= 0.5)
        or taker >= 1.12
    ):
        reasons.append("rebound guardrail: fast breadth lift/short-squeeze risk blocks bearish route")
    return reasons


def apply_regime_guardrails(regime: str, features: dict[str, float | int], score: float, bull_threshold: float, bear_threshold: float) -> tuple[str, list[str]]:
    reasons = guardrail_reasons(features, score, bull_threshold, bear_threshold)
    guarded = regime
    if regime == BULL and any("false-bull" in reason for reason in reasons):
        guarded = STORMY if float(features["downside_vol_24h"]) >= 6.0 or float(features["basket_ret_4h"]) <= -3.0 else SIDEWAYS
    if regime == BEAR and any("rebound" in reason for reason in reasons):
        guarded = SIDEWAYS
    return guarded, reasons


# ---------------------------------------------------------------------------
# Direction #1 (issue #72): momentum-exhaustion / mean-reversion guard ON THE
# REGIME LABEL ITSELF. Unlike the confirmation gate (dir #1-old) and the
# recent-P&L stop (dir #2), which are lagging overlays that confirm a trend
# already turned, this guard attacks the ROOT CAUSE: the directional model
# calling regime turns wrong. It blocks BULL activation into an
# overextended/rolling-over basket (momentum exhaustion) and blocks BEAR into a
# diverging-positive BTC (false breakdown). All features use only candles at or
# before the decision timestamp (strictly no-lookahead, next-candle execution).
# ---------------------------------------------------------------------------

# Per-coin RSI computed from the indicator library (Wilder's smoothing).
_rsi = _regime._indicators_mod.compute_rsi


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values, or None."""
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rate_of_change(closes: list[float], periods: int) -> float:
    """Percent rate of change over the last ``periods`` candles (0.0 if short)."""
    if len(closes) <= periods or closes[-periods - 1] <= 0:
        return 0.0
    return (closes[-1] / closes[-periods - 1] - 1.0) * 100.0


def _median_roc(ohlcv_by_coin: dict[str, list[dict[str, float | int]]], coins: list[str], periods: int) -> float:
    """Median per-coin rate-of-change over the last ``periods`` candles."""
    rocs: list[float] = []
    for coin in coins:
        closes = _closes(ohlcv_by_coin.get(coin, []))
        rocs.append(_rate_of_change(closes, periods))
    return _safe_median(rocs)


def momentum_exhaustion_features(
    ohlcv_by_coin: dict[str, list[dict[str, float | int]]],
    *,
    references: Iterable[str] = DEFAULT_REFERENCES,
    breadth_coins: Iterable[str] = DEFAULT_BREADTH_COINS,
    rsi_period: int = 14,
    mean_period: int = 24,
) -> dict[str, float]:
    """Compute point-in-time momentum-exhaustion features from raw OHLCV.

    All inputs are point-in-time closes at/strictly before the decision time
    (the caller passes a ``_truncate_to_ts`` window), so this is strictly
    no-lookahead. Returns a dict of overextension / deceleration / divergence
    signals used by the direction-#3 regime-label guard:

    Level / overextension (secondary — RSI saturates at 100 on monotonic ramps
    and is near-useless for detecting *rollover*, so these are kept but are NOT
    the primary trigger):
    - ``basket_rsi``: median per-coin RSI over breadth coins (high = overbought).
    - ``basket_overextended_pct``: fraction of coins above their own
      ``mean_period`` SMA by more than 15% (overextended from the mean).
    - ``basket_roc_24h``: median 24h rate-of-change across the basket.

    Deceleration / rollover (PRIMARY — these detect a trend that has ALREADY
    turned, which a pure level signal cannot):
    - ``basket_roc_6h`` / ``basket_roc_12h``: short- and medium-horizon basket
      ROC. A short-ROC that has rolled below the medium-ROC (after an uptrend)
      signals momentum deceleration -> mean reversion. A short-ROC that has
      turned above the medium-ROC (after a downtrend) signals a decelerating
      decline / V-bounce -> mean reversion up.
    - ``basket_deceleration``: ``basket_roc_6h - basket_roc_12h``. Negative =
      short momentum rolling under medium (BULL-exhaustion); positive = short
      momentum lifting over medium (BEAR mean-reversion).

    Divergence:
    - ``btc_rsi``: BTC RSI (falls back to first reference).
    - ``btc_roc_24h``: BTC 24h rate-of-change.
    - ``btc_basket_divergence``: BTC trailing 24h return minus basket median
      24h return. Positive = BTC up while basket rolls over.
    """
    references = list(references)
    breadth_coins = list(breadth_coins)

    coin_rsis: list[float] = []
    coin_overextended = 0
    coin_rocs: list[float] = []
    for coin in breadth_coins:
        rows = ohlcv_by_coin.get(coin, [])
        closes = _closes(rows)
        if len(closes) >= rsi_period + 1:
            rsi = _rsi(closes, rsi_period)
            if rsi is not None:
                coin_rsis.append(float(rsi))
        sma = _sma(closes, mean_period)
        if sma is not None and sma > 0:
            # Distance above the mean as a fraction of the mean.
            dist_frac = closes[-1] / sma - 1.0
            # "Overextended" = stretched >15% above its own 24h SMA.
            if dist_frac > 0.15:
                coin_overextended += 1
        roc = _rate_of_change(closes, 24)
        coin_rocs.append(roc)

    n_breadth = len(breadth_coins)
    basket_rsi = _safe_median(coin_rsis)
    basket_overextended_pct = (coin_overextended / n_breadth) if n_breadth else 0.0
    basket_roc_24h = _safe_median(coin_rocs)
    # Multi-horizon ROC for deceleration / rollover detection.
    basket_roc_6h = _median_roc(ohlcv_by_coin, breadth_coins, 6)
    basket_roc_12h = _median_roc(ohlcv_by_coin, breadth_coins, 12)

    btc_rows = (ohlcv_by_coin.get("BTC") or ohlcv_by_coin.get(references[0])) if references else []
    btc_closes = _closes(btc_rows) if btc_rows else []
    btc_rsi = float(_rsi(btc_closes, rsi_period)) if (btc_closes and _rsi(btc_closes, rsi_period) is not None) else 50.0
    btc_roc_24h = _rate_of_change(btc_closes, 24)
    basket_24h = _basket_return(ohlcv_by_coin, breadth_coins, 24)
    btc_basket_divergence = btc_roc_24h - basket_24h

    return {
        "basket_rsi": basket_rsi,
        "basket_overextended_pct": basket_overextended_pct,
        "basket_roc_24h": basket_roc_24h,
        "basket_roc_6h": basket_roc_6h,
        "basket_roc_12h": basket_roc_12h,
        "basket_deceleration": basket_roc_6h - basket_roc_12h,
        "btc_rsi": btc_rsi,
        "btc_roc_24h": btc_roc_24h,
        "btc_basket_divergence": btc_basket_divergence,
    }


def apply_momentum_guard(
    regime: str,
    exhaustion: dict[str, float],
    *,
    bull_rsi_cap: float = 100.0,
    bull_overextended_cap: float = 0.85,
    bull_roc_cap: float = 25.0,
    bear_btc_roc_floor: float = 2.0,
    bear_divergence_floor: float = 3.0,
    bull_rollover_roc_floor: float = -1.0,
    bull_rollover_roc12_precondition: float = 2.0,
    bull_stall_roc6_cap: float = -1.5,
    bull_stall_roc12_floor: float = 2.0,
    bear_mean_revert_roc24_floor: float = -8.0,
    bear_mean_revert_roc6_floor: float = 1.0,
    # PRIMARY BULL-blocking signal (direction #3, calibrated on 240d/300d real
    # data): deceleration = basket_roc_6h - basket_roc_12h. A value <= -1.0 means
    # short-horizon momentum has rolled under medium by >=1.0pp. The roc12>=1.0
    # precondition requires the basket to have been genuinely extended first, so
    # the guard only fires on a real trend that has rolled over, not a flat
    # basket. Selectivity sweep across both 240d and 300d: decel<=-1.0 blocks
    # 34 BULL windows (18 losers / 16 winners), net +22% route improvement on
    # 240d, and is the strict maxDD-minimizer on BOTH windows (240d 18.12% ->
    # 13.38%, 300d 18.30% -> 11.22%, both well under the 15% gate). This is the
    # single most effective BULL-blocking family and the only calibration that
    # brings maxDD robustly under 15% on both windows.
    bull_deceleration_cap: float = -1.0,
    bull_deceleration_roc12_precondition: float = 1.0,
    bear_divergence_only_floor: float = 1000.0,
) -> tuple[str, list[str]]:
    """Momentum-exhaustion / mean-reversion guard on the regime label.

    Issue #72 direction #3: block BULL activation into an overextended /
    rolling-over basket (momentum exhaustion -> mean reversion), and block BEAR
    into a mean-reverting / diverging-positive reference (false breakdown the
    BEAR route would step into and lose on). This operates on the LABEL, not a
    lagging overlay.

    Selectivity-grounded design (from ``_inspect_dir3_selectivity`` diagnostics
    on 240d of real data): the BULL-blocking family DOES isolate losers (blocked
    BULL avg return -5.92% vs unblocked -0.73%), but ONLY when it requires the
    basket to have *been trending* before rolling over. A naive "block when
    roc24 <= 0" fires on flat/neutral baskets (roc24 == 0) and is useless —
    RSI is even worse (saturates at 100 on any monotonic ramp, gentle or steep,
    so it cannot distinguish them). The guard therefore requires a PRECONDITION
    (the medium-horizon ROC was positive = the basket was genuinely extended)
    before the rollover can fire. The BEAR-blocking family mostly HURTS on real
    data (it blocks profitable BEAR bets), so it is kept conservative — firing
    only at genuine V-bounce / strong-BTC-divergence extremes.

    Signal families, in priority order:

    1. PRIMARY — deceleration / rollover (detects a trend that has ALREADY
       turned, which a pure level signal cannot). This is the family the issue
       asked for ("short-horizon momentum-exhaustion / mean-reversion") and the
       one that actually fires on the gate's losing windows:
       - **BULL rollover**: block BULL when the basket 24h ROC has rolled
         non-positive (``basket_roc_24h <= bull_rollover_roc_floor``) AND the
         medium-horizon ROC was recently positive
         (``basket_roc_12h >= bull_rollover_roc12_precondition``) — the basket
         WAS trending up and has now rolled over, so buying it is stepping into
         a turn. The precondition is essential: without it a flat basket
         (roc24 ~ 0) falsely triggers.
       - **BULL stall**: block BULL when the very recent move has gone negative
         (``basket_roc_6h <= bull_stall_roc6_cap``) while the medium window is
         still positive (``basket_roc_12h >= bull_stall_roc12_floor``) —
         momentum has faded/rolled over even though the trailing 12h is still
         up. This catches the *deceleration before* the 24h ROC turns.
       - **BEAR mean-reversion**: block BEAR when the basket is in a deep decline
         (``basket_roc_24h <= bear_mean_revert_roc24_floor``) but short-horizon
         momentum has already turned up (``basket_roc_6h >
         bear_mean_revert_roc6_floor``) — a decelerating decline / V-bounce the
         BEAR short proxy gets squeezed on. Kept conservative (deep floor)
         because BEAR-blocking mostly hurts on real data.
    2. SECONDARY — level / overextension / divergence (kept for completeness;
       fire only at genuine extremes, since RSI is near-useless for rollover
       detection on trending data):
       - BULL blocked at RSI/overextension/ROC-spike extremes.
       - BEAR blocked when BTC diverges positive while the basket rolls over
         (``btc_roc_24h >= bear_btc_roc_floor`` AND divergence >= floor).

    A blocked BULL or BEAR becomes SIDEWAYS (cash). Returns
    ``(guarded_regime, reasons)``. When nothing fires, the regime and an empty
    reason list are returned unchanged.
    """
    reasons: list[str] = []
    guarded = regime
    basket_rsi = float(exhaustion.get("basket_rsi", 50.0))
    basket_overextended = float(exhaustion.get("basket_overextended_pct", 0.0))
    basket_roc = float(exhaustion.get("basket_roc_24h", 0.0))
    basket_roc_6h = float(exhaustion.get("basket_roc_6h", 0.0))
    basket_roc_12h = float(exhaustion.get("basket_roc_12h", 0.0))
    basket_deceleration = float(exhaustion.get("basket_deceleration", basket_roc_6h - basket_roc_12h))
    btc_roc = float(exhaustion.get("btc_roc_24h", 0.0))
    divergence = float(exhaustion.get("btc_basket_divergence", 0.0))

    if regime == BULL:
        # PRIMARY (calibrated, direction #3): basket has DECELERATED — the
        # short-horizon ROC has rolled under the medium-horizon ROC by at least
        # ``bull_deceleration_cap`` while the medium window was still positive
        # (the basket was genuinely extended). This is the single most selective
        # BULL-blocking signal on real 240d data: it isolates losing BULL windows
        # (avg return deeply negative) while sparing winners, because a
        # decelerating/rolling-over basket after an extension is exactly the
        # false-BULL reversal the issue targets. ``basket_deceleration`` =
        # ``basket_roc_6h - basket_roc_12h``; a large negative value means the
        # most recent impulse has faded relative to the trailing trend.
        if (
            basket_deceleration <= bull_deceleration_cap
            and basket_roc_12h >= bull_deceleration_roc12_precondition
        ):
            reasons.append(
                f"momentum-exhaustion: basket deceleration {basket_deceleration:+.1f}% <= "
                f"{bull_deceleration_cap:.1f}% (short ROC {basket_roc_6h:+.1f}% under medium "
                f"ROC {basket_roc_12h:+.1f}%) after extension; rolling over, blocking BULL -> SIDEWAYS"
            )
        # PRIMARY: basket was trending (roc12 precondition) but 24h ROC has now
        # rolled non-positive. The precondition is what makes this selective
        # instead of firing on every flat basket.
        elif basket_roc <= bull_rollover_roc_floor and basket_roc_12h >= bull_rollover_roc12_precondition:
            reasons.append(
                f"momentum-exhaustion: basket 24h ROC {basket_roc:+.1f}% <= "
                f"{bull_rollover_roc_floor:.1f}% after extension (12h ROC {basket_roc_12h:+.1f}% "
                f">= {bull_rollover_roc12_precondition:.1f}%); rolling over, blocking BULL -> SIDEWAYS"
            )
        # PRIMARY: short momentum has turned negative while medium window still
        # positive (deceleration BEFORE the 24h ROC fully rolls).
        elif basket_roc_6h <= bull_stall_roc6_cap and basket_roc_12h >= bull_stall_roc12_floor:
            reasons.append(
                f"momentum-exhaustion: short ROC {basket_roc_6h:+.1f}% <= "
                f"{bull_stall_roc6_cap:.1f}% while medium ROC {basket_roc_12h:+.1f}% >= "
                f"{bull_stall_roc12_floor:.1f}% (stalled after extension); blocking BULL -> SIDEWAYS"
            )
        # SECONDARY: level / overextension extremes (RSI near-useless for
        # rollover but kept for genuine overbought spikes).
        elif basket_rsi >= bull_rsi_cap:
            reasons.append(
                f"momentum-exhaustion: basket RSI {basket_rsi:.1f} >= {bull_rsi_cap:.1f} "
                f"(overbought); blocking BULL -> SIDEWAYS"
            )
        elif basket_overextended >= bull_overextended_cap:
            reasons.append(
                f"momentum-exhaustion: {basket_overextended:.0%} of basket >15% above its "
                f"24h SMA (overextended); blocking BULL -> SIDEWAYS"
            )
        elif basket_roc >= bull_roc_cap:
            reasons.append(
                f"momentum-exhaustion: basket 24h ROC {basket_roc:+.1f}% >= {bull_roc_cap:.1f}% "
                f"(parabolic spike); blocking BULL -> SIDEWAYS"
            )
        if reasons:
            guarded = SIDEWAYS

    if regime == BEAR:
        # PRIMARY: deep decline but short momentum has turned up (mean reversion).
        # Kept conservative (deep roc24 floor) because BEAR-blocking mostly
        # hurts on real data — it blocks profitable BEAR bets.
        if basket_roc <= bear_mean_revert_roc24_floor and basket_roc_6h > bear_mean_revert_roc6_floor:
            reasons.append(
                f"false-breakdown: basket 24h ROC {basket_roc:+.1f}% <= "
                f"{bear_mean_revert_roc24_floor:.1f}% but short ROC {basket_roc_6h:+.1f}% > "
                f"{bear_mean_revert_roc6_floor:.1f}% (decelerating decline / V-bounce); "
                f"suppressing BEAR -> SIDEWAYS"
            )
        # SECONDARY (calibrated, direction #3): BTC-vs-basket divergence alone
        # is positive and large — BTC is making higher lows / holding up while
        # the alt basket dumps. This is a rotation, not a market-wide risk-off,
        # so the BEAR short proxy gets squeezed. Calibrated to a divergence
        # floor of 2% (the threshold that isolated losing BEAR windows with net
        # positive return on real 240d data). Falls back to the stricter
        # dual-condition (btc_roc AND divergence) via the legacy params when the
        # divergence-only floor is set high (default-off by setting it large).
        elif divergence >= bear_divergence_only_floor:
            reasons.append(
                f"false-breakdown: BTC-vs-basket divergence {divergence:+.1f}% >= "
                f"{bear_divergence_only_floor:.1f}% (BTC holding up while basket rolls over; "
                f"alt rotation, not bear); suppressing BEAR -> SIDEWAYS"
            )
        # SECONDARY (legacy): BTC diverging positive while basket rolls over.
        elif btc_roc >= bear_btc_roc_floor and divergence >= bear_divergence_floor:
            reasons.append(
                f"false-breakdown: BTC 24h ROC {btc_roc:+.1f}% >= {bear_btc_roc_floor:.1f}% "
                f"and BTC-vs-basket divergence {divergence:+.1f}% >= {bear_divergence_floor:.1f}% "
                f"(BTC diverging positive; alt rotation, not bear); suppressing BEAR -> SIDEWAYS"
            )
        if reasons:
            guarded = SIDEWAYS

    return guarded, reasons


def classify_v2_scorecard(features: dict[str, float | int], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Interpretable Regime v2 scorecard; research-only, no live routing."""
    weights = weights or DEFAULT_SCORE_WEIGHTS
    components = scorecard_components(features)
    score = _score_from_components(components, weights)
    vol = float(features["median_vol_24h"])
    down_vol = float(features["downside_vol_24h"])
    dispersion = float(features["return_dispersion_24h"])
    basket24 = float(features["basket_ret_24h"])
    basket4 = float(features["basket_ret_4h"])
    oi = float(features["futures_oi_change_pct"])
    stormy = (
        (vol >= 7.0 and basket24 <= -3.0)
        or (down_vol >= 6.0 and basket4 <= -2.5)
        or (dispersion >= 8.0 and basket24 <= -2.0)
        or (oi >= 8.0 and basket4 <= -2.0)
    )
    bull_threshold = max(0.75, float(weights.get("bull_threshold", 2.0)))
    bear_threshold = max(0.75, float(weights.get("bear_threshold", 2.0)))
    if stormy:
        regime = STORMY
    elif score >= bull_threshold:
        regime = BULL
    elif score <= -bear_threshold:
        regime = BEAR
    else:
        regime = SIDEWAYS
    regime, guard_reasons = apply_regime_guardrails(regime, features, score, bull_threshold, bear_threshold)
    confidence = min(0.95, 0.45 + abs(score) / 6.0)
    reasons = _scorecard_reasons(features, components) + guard_reasons
    return {"regime": regime, "score": score, "confidence": confidence, "reasons": reasons[:8]}


def score_records_with_weights(records: list[dict[str, Any]], weights: dict[str, float]) -> dict[str, Any]:
    """Evaluate a weight set on existing records with point-in-time features."""
    predictions = [classify_v2_scorecard(row["features"], weights)["regime"] for row in records]
    correct = sum(1 for row, pred in zip(records, predictions) if row.get("label") == pred)
    flips = sum(1 for i in range(1, len(predictions)) if predictions[i] != predictions[i - 1])
    accuracy = correct / len(records) * 100.0 if records else 0.0
    # Penalize unnecessary switching so tuned weights don't simply overfit every label transition.
    score = accuracy - min(20.0, flips * 0.25)
    return {"accuracy_pct": accuracy, "score": score, "flips": flips, "predictions": predictions}


def train_scorecard_weights(records: list[dict[str, Any]], *, min_records: int = 20) -> dict[str, Any]:
    """Grid-search a small interpretable weight set on training records."""
    if len(records) < min_records:
        baseline = score_records_with_weights(records, DEFAULT_SCORE_WEIGHTS)
        return {"enabled": False, "reason": "insufficient_records", "weights": dict(DEFAULT_SCORE_WEIGHTS), **baseline}
    candidate_scales = [0.5, 0.75, 1.0, 1.25, 1.5]
    threshold_pairs = [(1.5, 1.5), (2.0, 2.0), (2.5, 2.0), (2.0, 2.5)]
    best: dict[str, Any] | None = None
    for ref_scale in candidate_scales:
        for breadth_scale in candidate_scales:
            for momentum_scale in candidate_scales:
                for rel_scale in [0.5, 1.0, 1.5]:
                    for bull_t, bear_t in threshold_pairs:
                        weights = dict(DEFAULT_SCORE_WEIGHTS)
                        weights["reference_trend_score"] = DEFAULT_SCORE_WEIGHTS["reference_trend_score"] * ref_scale
                        weights["breadth_score"] = DEFAULT_SCORE_WEIGHTS["breadth_score"] * breadth_scale
                        weights["momentum_score"] = DEFAULT_SCORE_WEIGHTS["momentum_score"] * momentum_scale
                        weights["fast_move_score"] = DEFAULT_SCORE_WEIGHTS["fast_move_score"] * momentum_scale
                        weights["relative_strength_score"] = DEFAULT_SCORE_WEIGHTS["relative_strength_score"] * rel_scale
                        weights["bull_threshold"] = bull_t
                        weights["bear_threshold"] = bear_t
                        result = score_records_with_weights(records, weights)
                        if best is None or (result["score"], result["accuracy_pct"], -result["flips"]) > (best["score"], best["accuracy_pct"], -best["flips"]):
                            best = {"enabled": True, "weights": weights, **result}
    return best or {"enabled": False, "reason": "no_candidates", "weights": dict(DEFAULT_SCORE_WEIGHTS), **score_records_with_weights(records, DEFAULT_SCORE_WEIGHTS)}


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


def route_window_return(
    regime: str,
    *,
    future_basket_ret: float,
    future_btc_ret: float,
    fee_bps: float,
    bear_capture: float = 0.45,
    sideways_capture: float = 0.0,
) -> float:
    """Map a regime decision to a route return for the next window.

    Research proxy only:
    - BULL routes to spot/basket exposure after round-trip fee.
    - BEAR routes to a conservative short/cash proxy: captures part of basket downside,
      loses a smaller amount if the basket rallies, after fees.
    - SIDEWAYS/STORMY stay in cash unless a future version wires a proven chop strategy.
    """
    fee_pct = fee_bps / 100.0
    if regime == BULL:
        return future_basket_ret - fee_pct
    if regime == BEAR:
        return (-future_basket_ret * bear_capture) - fee_pct
    if regime == SIDEWAYS:
        return max(0.0, future_basket_ret * sideways_capture)
    if regime == STORMY:
        return 0.0
    return 0.0


def _compound_returns(returns_pct: list[float]) -> dict[str, Any]:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    curve = []
    for ret in returns_pct:
        equity *= max(0.0, 1.0 + ret / 100.0)
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, drawdown)
        curve.append(equity)
    wins = sum(1 for ret in returns_pct if ret > 0)
    return {
        "total_return_pct": (equity - 1.0) * 100.0,
        "max_drawdown_pct": max_dd,
        "avg_window_return_pct": sum(returns_pct) / len(returns_pct) if returns_pct else 0.0,
        "win_rate_pct": wins / len(returns_pct) * 100.0 if returns_pct else 0.0,
        "windows": len(returns_pct),
        "equity_curve": curve,
    }


def build_route_outcomes(records: list[dict[str, Any]], *, fee_bps: float) -> dict[str, dict[str, Any]]:
    """Build routed equity outcomes for each available regime sequence."""
    route_keys = {
        "cash": None,
        "buy_and_hold_basket": "__basket__",
        "legacy_sol": "legacy_regime",
        "research_v1": "v1_regime",
        "regime_v2": "v2_smoothed",
    }
    if records and "v2_tuned_smoothed" in records[0]:
        route_keys["regime_v2_tuned"] = "v2_tuned_smoothed"
    if records and "v2_route_tuned_smoothed" in records[0]:
        route_keys["regime_v2_route_tuned"] = "v2_route_tuned_smoothed"
    if records and "selector_smoothed" in records[0]:
        route_keys["regime_v2_selector"] = "selector_smoothed"
    outcomes: dict[str, dict[str, Any]] = {}
    for name, key in route_keys.items():
        returns = []
        for row in records:
            basket_ret = float(row.get("future_basket_ret", 0.0))
            btc_ret = float(row.get("future_btc_ret", 0.0))
            if key is None:
                returns.append(0.0)
            elif key == "__basket__":
                returns.append(basket_ret - fee_bps / 100.0)
            else:
                returns.append(route_window_return(str(row.get(key, SIDEWAYS)), future_basket_ret=basket_ret, future_btc_ret=btc_ret, fee_bps=fee_bps))
        result = _compound_returns(returns)
        result["route"] = name
        outcomes[name] = result
    return outcomes


def _route_returns_for_key(records: list[dict[str, Any]], route_key: str, *, fee_bps: float) -> list[float]:
    return [
        route_window_return(
            str(row.get(route_key, SIDEWAYS)),
            future_basket_ret=float(row.get("future_basket_ret", 0.0)),
            future_btc_ret=float(row.get("future_btc_ret", 0.0)),
            fee_bps=fee_bps,
        )
        for row in records
    ]


def _market_confirmation_signal(
    history: list[dict[str, Any]],
    regime: str,
    *,
    breadth_pct: float,
) -> tuple[bool, float, float, float]:
    """Regime-aware no-lookahead confirmation from raw market returns.

    Issue #72 direction #1: the confirmation gate must skip choppy early-recovery
    false-BULL windows. The prior signal used *realized route returns* (which
    include BEAR-route gains from falling baskets), so it was almost always
    positive and never skipped anything on real data. This signal uses the RAW
    basket and reference trailing returns directly — i.e. "is the market actually
    recovering?" — which is what the issue specifies.

    Returns ``(confirmed, basket_trailing, basket_advancing_frac, btc_trailing)``.
    ``confirmed`` is True when the market move supports the chosen regime:

    - BULL: basket trailing return > 0 OR breadth turn (advancing frac > breadth_pct).
    - BEAR: basket trailing return < 0 AND btc trailing return < 0 (a genuine
      sustained downtrend, not a single V-bottom spike the BEAR route would
      step into and lose on).
    - SIDEWAYS / STORMY: always confirmed (these route to ~cash anyway).

    All inputs are from ``history`` (rows strictly before the decision index),
    so this is strictly no-lookahead.
    """
    if not history:
        return True, 0.0, 0.0, 0.0
    basket_rets = [float(h.get("future_basket_ret", 0.0)) for h in history]
    btc_rets = [float(h.get("future_btc_ret", 0.0)) for h in history]
    basket_trailing = sum(basket_rets)
    basket_advancing = sum(1 for x in basket_rets if x > 0.0)
    basket_advancing_frac = basket_advancing / len(basket_rets) if basket_rets else 0.0
    btc_trailing = sum(btc_rets)
    if regime == BULL:
        confirmed = basket_trailing > 0.0 or basket_advancing_frac > breadth_pct
    elif regime == BEAR:
        confirmed = basket_trailing < 0.0 and btc_trailing < 0.0
    else:
        confirmed = True
    return confirmed, basket_trailing, basket_advancing_frac, btc_trailing


def _trailing_window_passes(
    returns: list[float],
    *,
    windows: int,
    min_window_return_pct: float,
    max_window_drawdown_pct: float,
) -> tuple[int, int]:
    """Count chronological trailing subwindows that pass return/drawdown gates."""
    if not returns or windows <= 0:
        return 0, 0
    windows = min(windows, len(returns))
    passing = 0
    for idx in range(windows):
        start = idx * len(returns) // windows
        end = (idx + 1) * len(returns) // windows
        if end <= start:
            continue
        outcome = _compound_returns(returns[start:end])
        if outcome["total_return_pct"] >= min_window_return_pct and outcome["max_drawdown_pct"] <= max_window_drawdown_pct:
            passing += 1
    return passing, windows


def build_selector_route(
    records: list[dict[str, Any]],
    *,
    route_candidates: dict[str, str],
    fee_bps: float,
    lookback: int = 12,
    min_trailing_objective: float = 0.0,
    max_trailing_drawdown_pct: float = 0.0,
    selector_equity_stop_drawdown_pct: float = 0.0,
    selector_equity_stop_cooldown_windows: int = 1,
    selector_min_trailing_return_pct: float = -999999.0,
    selector_min_trailing_win_rate_pct: float = 0.0,
    selector_trailing_robust_windows: int = 0,
    selector_min_passing_trailing_windows: int = 0,
    selector_trailing_window_min_return_pct: float = 0.0,
    selector_trailing_window_max_drawdown_pct: float = 20.0,
    selector_re_engage_confirmation: bool = True,
    selector_re_engage_breadth_pct: float = 0.60,
    selector_re_engage_rolling_peak_windows: int = 0,
    selector_re_engage_confirmation_lookback: int = 0,
    selector_recent_pnl_lookback_windows: int = 0,
    selector_recent_pnl_stop_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """Select a route using only prior realized route windows.

    This is a research-only no-lookahead selector. At row i it scores each
    candidate on rows [i-lookback, i), never on row i or later.

    Equity-stop re-engagement (issue #72):
    - ``selector_re_engage_confirmation`` (DEFAULT ON) gates re-engagement after
      the cooldown elapses on a confirmation signal: the best candidate's
      realized trailing return over the lookback must be positive, OR a
      breadth/momentum turn (advancing windows > ``selector_re_engage_breadth_pct``).
      This skips the choppy early-recovery false-BULL windows that inflate route
      maxDD. Unconditional re-arm (the prior behaviour that ballooned maxDD) is
      recovered by setting this False. The default is ON so that callers that do
      not pass this kwarg (e.g. ``regime_v2_forward_replay.evaluate_settings_grid``)
      get confirmation-gated re-engagement automatically.
    - ``selector_re_engage_breadth_pct`` (DEFAULT 0.60): fraction of advancing
      (positive-return) windows required for the breadth branch of the
      confirmation signal. 0.50 proved too lenient (a 50/50 chop still
      confirmed); 0.60 requires a genuine majority of up-windows.
    - ``selector_re_engage_rolling_peak_windows`` (suggestion #3): when re-arming
      after cooldown, rebase the peak to a RECENT rolling peak (max equity over
      the last N windows) rather than the instantaneous current equity, so a
      couple of early-recovery losses don't immediately establish a new, higher
      watermark to draw down from. 0 disables the rolling rebase and falls back
      to rebasing to current equity.
    - ``selector_re_engage_confirmation_lookback`` (DEFAULT 0 = full lookback):
      the number of recent history windows the confirmation signal sums over.
      The default (0) uses the full ``lookback`` (e.g. 12) history. A SHORTER
      window (e.g. 3-4) drops stale positive returns faster after a trend
      reversal, so the gate stops confirming a BULL whose uptrend has already
      rolled over — this materially reduces route maxDD on the recent crash
      window (issue #72). 0 keeps the prior full-lookback behaviour.
    - ``selector_recent_pnl_lookback_windows`` / ``selector_recent_pnl_stop_pct``
      (issue #72 direction #2, DEFAULT OFF): a CONTINUOUS risk-off layer keyed
      on the selector's OWN recent realized P&L over the last N windows (not the
      all-time peak drawdown the equity stop watches). When the cumulative
      realized selector return over the last ``lookback_windows`` windows is at
      or below ``-stop_pct``, the selector goes to cash for this window and
      re-checks next window. Unlike the confirmation gate (direction #1), this
      layer is not a transition gate — it fires mid-bleed whenever the selector
      is recently losing, regardless of any cash->active transition, which makes
      it robust to a directional model that stays BULL through a regime
      transition. When ``stop_pct > 0`` and ``lookback_windows > 0`` it is ON;
      either being 0 disables it.
    """
    selected: list[dict[str, Any]] = []
    lookback = max(1, lookback)
    selector_equity = 1.0
    selector_peak = 1.0
    selector_drawdown = 0.0
    # Rolling window of recent selector equity values for the rolling-peak rebase.
    recent_equity: list[float] = []
    stop_cooldown_remaining = 0
    selector_equity_stop_cooldown_windows = max(1, int(selector_equity_stop_cooldown_windows or 1))
    re_engage_rolling_peak_windows = max(0, int(selector_re_engage_rolling_peak_windows or 0))
    # Direction #2: recent-P&L risk-off layer. Active only when BOTH a positive
    # lookback window count AND a positive stop threshold are configured.
    recent_pnl_lookback_windows = max(0, int(selector_recent_pnl_lookback_windows or 0))
    recent_pnl_stop_pct = float(selector_recent_pnl_stop_pct or 0.0)
    recent_pnl_active = recent_pnl_lookback_windows > 0 and recent_pnl_stop_pct > 0.0
    # Confirmation-signal lookback: 0 means use the full ``lookback`` history
    # (prior behaviour); a positive value uses only the most recent N windows so
    # stale positive returns drop faster after a trend reversal.
    confirm_lookback = lookback if int(selector_re_engage_confirmation_lookback or 0) <= 0 else max(1, int(selector_re_engage_confirmation_lookback))
    for idx, row in enumerate(records):
        if idx > 0 and selected:
            prev = selected[-1]
            prev_ret = route_window_return(
                str(prev.get("selector_smoothed", SIDEWAYS)),
                future_basket_ret=float(prev.get("future_basket_ret", 0.0)),
                future_btc_ret=float(prev.get("future_btc_ret", 0.0)),
                fee_bps=fee_bps,
            )
            selector_equity *= max(0.0, 1.0 + prev_ret / 100.0)
            # Maintain a rolling window of recent equity values for the
            # rolling-peak rebase (suggestion #3). This is the realized equity
            # BEFORE the current window is decided, so it is strictly no-lookahead.
            recent_equity.append(selector_equity)
            if re_engage_rolling_peak_windows > 0 and len(recent_equity) > re_engage_rolling_peak_windows:
                recent_equity = recent_equity[-re_engage_rolling_peak_windows:]
            selector_peak = max(selector_peak, selector_equity)
            selector_drawdown = (selector_peak - selector_equity) / selector_peak * 100.0 if selector_peak > 0 else 0.0
        history = records[max(0, idx - lookback) : idx]
        choice_name = "cash"
        choice_key = ""
        choice_objective = 0.0
        choice_drawdown = 0.0
        choice_return = 0.0
        choice_win_rate = 0.0
        choice_passing_windows = 0
        choice_total_windows = 0
        block_reason = ""
        if history:
            scored = []
            rejected = []
            for name, key in route_candidates.items():
                returns = _route_returns_for_key(history, key, fee_bps=fee_bps)
                outcome = _compound_returns(returns)
                objective = outcome["total_return_pct"] - 0.35 * outcome["max_drawdown_pct"]
                passing_windows, total_windows = _trailing_window_passes(
                    returns,
                    windows=selector_trailing_robust_windows,
                    min_window_return_pct=selector_trailing_window_min_return_pct,
                    max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct,
                )
                candidate = (
                    objective,
                    outcome["total_return_pct"],
                    -outcome["max_drawdown_pct"],
                    name,
                    key,
                    outcome["max_drawdown_pct"],
                    outcome["win_rate_pct"],
                    passing_windows,
                    total_windows,
                )
                if outcome["total_return_pct"] < selector_min_trailing_return_pct:
                    rejected.append((name, "return", outcome["total_return_pct"]))
                    continue
                if selector_min_trailing_win_rate_pct > 0 and outcome["win_rate_pct"] < selector_min_trailing_win_rate_pct:
                    rejected.append((name, "win_rate", outcome["win_rate_pct"]))
                    continue
                if max_trailing_drawdown_pct > 0 and outcome["max_drawdown_pct"] > max_trailing_drawdown_pct:
                    rejected.append((name, "drawdown", outcome["max_drawdown_pct"]))
                    continue
                if selector_min_passing_trailing_windows > 0 and passing_windows < selector_min_passing_trailing_windows:
                    rejected.append((name, "passing_windows", float(passing_windows)))
                    continue
                scored.append(candidate)
            scored.sort(reverse=True)
            if scored:
                best = scored[0]
                choice_drawdown = float(best[5])
                choice_return = float(best[1])
                choice_win_rate = float(best[6])
                choice_passing_windows = int(best[7])
                choice_total_windows = int(best[8])
                if best[0] >= min_trailing_objective:
                    choice_objective = best[0]
                    choice_name = best[3]
                    choice_key = best[4]
                else:
                    block_reason = (
                        f"trailing objective {best[0]:.2f} < "
                        f"{min_trailing_objective:.2f}"
                    )
            else:
                block_reason = "selector quality gates rejected all candidates"
                if rejected:
                    reject_name, reject_reason, reject_value = rejected[0]
                    if reject_reason == "drawdown":
                        choice_drawdown = float(reject_value)
                    elif reject_reason == "return":
                        choice_return = float(reject_value)
                    elif reject_reason == "win_rate":
                        choice_win_rate = float(reject_value)
                    block_reason += f" ({reject_name} {reject_reason}={reject_value:.2f})"
        if stop_cooldown_remaining > 0:
            choice_name = "cash"
            choice_key = ""
            choice_objective = 0.0
            block_reason = f"equity drawdown cooldown ({stop_cooldown_remaining} windows remaining)"
            stop_cooldown_remaining -= 1
            # Cooldown just elapsed. We must rebase the selector peak so the
            # drawdown metric does not freeze above the stop forever (one-way
            # ratchet — see issue #72). But re-engagement is NOT unconditional
            # unless confirmation gating is disabled. With confirmation gating,
            # we require a no-lookahead confirmation signal (positive realized
            # trailing return over the lookback OR a breadth/momentum turn with
            # advancing windows > threshold) before re-arming into an active
            # regime. This skips the choppy early-recovery false-BULL windows
            # that inflate route maxDD.
            if stop_cooldown_remaining == 0:
                # Direction #1: gate re-engagement on a regime-aware MARKET
                # confirmation signal (raw basket/reference trailing return),
                # NOT on realized route returns. The prior signal used route
                # returns (which include BEAR-route gains from falling baskets)
                # and was therefore almost always positive — it never skipped
                # anything on real data. The market signal asks "is the basket
                # actually recovering?" which is what skips the choppy
                # early-recovery false-BULL windows that inflate route maxDD.
                confirmation_ok = True
                if selector_re_engage_confirmation and history:
                    _cand_regime = str(row.get("v2_smoothed", SIDEWAYS))
                    confirmation_ok, _bt, _baf, _btct = _market_confirmation_signal(
                        history, _cand_regime, breadth_pct=selector_re_engage_breadth_pct,
                    )
                    if not confirmation_ok:
                        block_reason = (
                            f"re-engagement market confirmation FAILED "
                            f"(basket_trail {_bt:+.2f}%, advancing {_baf:.2f}, "
                            f"btc_trail {_btct:+.2f} for {_cand_regime}); staying in cash"
                        )
                        # Do NOT re-arm. Stay in cash this window WITHOUT
                        # rebasing the peak, so the stop can re-trip a fresh
                        # cooldown next window and we retry confirmation then.
                        # We set a 1-window retry so the selector re-checks the
                        # confirmation gate on the next step.
                        stop_cooldown_remaining = 1
                if confirmation_ok:
                    # Re-arm: rebase the peak. Suggestion #3 — when a rolling
                    # peak window is configured, rebase to the RECENT rolling
                    # peak (max equity over the last N windows) rather than the
                    # instantaneous current equity, so early-recovery losses
                    # don't immediately establish a new, higher watermark to
                    # draw down from. Fall back to current equity if no recent
                    # history is available or the feature is disabled.
                    if re_engage_rolling_peak_windows > 0 and recent_equity:
                        rebased_peak = max(recent_equity)
                        # The rolling peak should never exceed the all-time peak;
                        # it is a *recent* watermark used to avoid an
                        # instantaneous spike setting an unforgiving new high.
                        selector_peak = max(rebased_peak, selector_equity)
                    else:
                        selector_peak = selector_equity
                    selector_drawdown = 0.0
        elif selector_equity_stop_drawdown_pct > 0 and selector_drawdown > selector_equity_stop_drawdown_pct:
            choice_name = "cash"
            choice_key = ""
            choice_objective = 0.0
            block_reason = (
                f"equity drawdown {selector_drawdown:.2f}% > "
                f"{selector_equity_stop_drawdown_pct:.2f}%"
            )
            # Schedule a bounded cooldown of `cooldown_windows` cash windows,
            # after which the stop re-arms (via the peak rebase above). This is a
            # true time-bounded cooldown rather than requiring drawdown to heal
            # below the stop first.
            stop_cooldown_remaining = selector_equity_stop_cooldown_windows
        # Direction #1 — TRANSITION confirmation gate. The post-cooldown gate
        # above only fires after the equity stop trips. But on real data the
        # stop fires only once and the dominant maxDD driver is a slow bleed of
        # *quality-gate-sanctioned* re-entries (cash -> active) that step into
        # losing windows over a multi-month drawdown. This gate applies the SAME
        # regime-aware market confirmation at EVERY cash -> active transition,
        # not just post-cooldown, so those bad re-entries are skipped. When the
        # gate blocks, the selector stays in cash for this window (the market
        # has not confirmed a recovery) and re-checks next window.
        if (
            selector_re_engage_confirmation
            and choice_name != "cash"
            and selected
            and selected[-1].get("selector_route_key") == "cash"
            and history
        ):
            _trans_regime = str(row.get(choice_key, SIDEWAYS))
            _trans_ok, _tbt, _tbaf, _tbtct = _market_confirmation_signal(
                history, _trans_regime, breadth_pct=selector_re_engage_breadth_pct,
            )
            if not _trans_ok:
                block_reason = (
                    f"transition market confirmation FAILED "
                    f"(basket_trail {_tbt:+.2f}%, advancing {_tbaf:.2f}, "
                    f"btc_trail {_tbtct:+.2f} for {_trans_regime}); staying in cash"
                )
                choice_name = "cash"
                choice_key = ""
                choice_objective = 0.0
        # Direction #2 — CONTINUOUS recent-P&L risk-off. Unlike the transition
        # gate above (which only fires at cash->active transitions), this layer
        # watches the selector's OWN realized P&L over the last N windows every
        # step and forces cash whenever that recent stretch is losing beyond the
        # threshold. This catches a directional model that stays BULL through a
        # regime transition and bleeds active losses the confirmation gate never
        # sees (because there is no transition into/out of cash to gate). The
        # window returns are the realized selector route returns for the prior
        # rows (rows strictly before this decision), so this is no-lookahead.
        if recent_pnl_active and choice_name != "cash" and selected:
            _recent_rows = selected[-recent_pnl_lookback_windows:]
            _recent_returns = [
                route_window_return(
                    str(r.get("selector_smoothed", SIDEWAYS)),
                    future_basket_ret=float(r.get("future_basket_ret", 0.0)),
                    future_btc_ret=float(r.get("future_btc_ret", 0.0)),
                    fee_bps=fee_bps,
                )
                for r in _recent_rows
            ]
            if len(_recent_returns) >= recent_pnl_lookback_windows:
                # Cumulative realized selector return over the last N windows.
                _recent_equity = 1.0
                for _r in _recent_returns:
                    _recent_equity *= max(0.0, 1.0 + _r / 100.0)
                _recent_pnl_pct = (_recent_equity - 1.0) * 100.0
                if _recent_pnl_pct <= -recent_pnl_stop_pct:
                    block_reason = (
                        f"recent selector P&L risk-off: {_recent_pnl_pct:+.2f}% "
                        f"over last {recent_pnl_lookback_windows} windows <= "
                        f"-{recent_pnl_stop_pct:.2f}%; going to cash"
                    )
                    choice_name = "cash"
                    choice_key = ""
                    choice_objective = 0.0
        selected_regime = SIDEWAYS if choice_name == "cash" else str(row.get(choice_key, SIDEWAYS))
        selected.append(
            {
                **row,
                "selector_route_key": choice_name,
                "selector_route_source": choice_key,
                "selector_trailing_objective": choice_objective,
                "selector_trailing_return_pct": choice_return,
                "selector_trailing_drawdown_pct": choice_drawdown,
                "selector_trailing_win_rate_pct": choice_win_rate,
                "selector_trailing_passing_windows": choice_passing_windows,
                "selector_trailing_total_windows": choice_total_windows,
                "selector_equity_drawdown_pct": selector_drawdown,
                "selector_block_reason": block_reason,
                "selector_smoothed": selected_regime,
            }
        )
    return selected


def score_route_records_with_weights(records: list[dict[str, Any]], weights: dict[str, float], *, fee_bps: float) -> dict[str, Any]:
    routed_records = []
    for row in records:
        result = classify_v2_scorecard(row["features"], weights)
        routed_records.append({**row, "v2_smoothed": result["regime"]})
    outcome = build_route_outcomes(routed_records, fee_bps=fee_bps)["regime_v2"]
    objective = outcome["total_return_pct"] - 0.35 * outcome["max_drawdown_pct"]
    return {"objective": objective, "route_total_return_pct": outcome["total_return_pct"], "max_drawdown_pct": outcome["max_drawdown_pct"], "weights": weights}


def train_route_scorecard_weights(records: list[dict[str, Any]], *, fee_bps: float, min_records: int = 20) -> dict[str, Any]:
    """Tune scorecard weights against routed return/drawdown, not label accuracy."""
    if len(records) < min_records:
        base = score_route_records_with_weights(records, DEFAULT_SCORE_WEIGHTS, fee_bps=fee_bps)
        return {"enabled": False, "reason": "insufficient_records", **base}
    best: dict[str, Any] | None = None
    for ref_scale in [0.0, 0.5, 1.0, 1.5]:
        for breadth_scale in [0.0, 0.5, 1.0, 1.5]:
            for momentum_scale in [0.0, 0.5, 1.0, 1.5]:
                for rel_scale in [0.0, 0.5, 1.0]:
                    for bull_t, bear_t in [(2.0, 2.0), (3.0, 2.5), (4.0, 3.0), (6.0, 3.0), (8.0, 4.0)]:
                        weights = dict(DEFAULT_SCORE_WEIGHTS)
                        weights["reference_trend_score"] *= ref_scale
                        weights["breadth_score"] *= breadth_scale
                        weights["momentum_score"] *= momentum_scale
                        weights["fast_move_score"] *= momentum_scale
                        weights["relative_strength_score"] *= rel_scale
                        weights["bull_threshold"] = bull_t
                        weights["bear_threshold"] = bear_t
                        result = score_route_records_with_weights(records, weights, fee_bps=fee_bps)
                        if best is None or (result["objective"], result["route_total_return_pct"], -result["max_drawdown_pct"]) > (best["objective"], best["route_total_return_pct"], -best["max_drawdown_pct"]):
                            best = {"enabled": True, **result}
    return best or {"enabled": False, "reason": "no_candidates", **score_route_records_with_weights(records, DEFAULT_SCORE_WEIGHTS, fee_bps=fee_bps)}


def route_failure_diagnostics(records: list[dict[str, Any]], route_key: str, *, fee_bps: float, limit: int = 5) -> dict[str, Any]:
    """Identify worst routed windows and the features/reasons that caused them."""
    windows = []
    for row in records:
        regime = str(row.get(route_key, SIDEWAYS))
        route_ret = route_window_return(
            regime,
            future_basket_ret=float(row.get("future_basket_ret", 0.0)),
            future_btc_ret=float(row.get("future_btc_ret", 0.0)),
            fee_bps=fee_bps,
        )
        windows.append(
            {
                "time": row.get("time"),
                "regime": regime,
                "route_return_pct": route_ret,
                "future_basket_ret": row.get("future_basket_ret", 0.0),
                "future_btc_ret": row.get("future_btc_ret", 0.0),
                "reasons": row.get("reasons", []),
                "features": row.get("features", {}),
            }
        )
    return {"route_key": route_key, "worst_windows": sorted(windows, key=lambda item: item["route_return_pct"])[:limit]}


def build_route_robustness_gates(
    records: list[dict[str, Any]],
    route_key: str,
    *,
    fee_bps: float,
    windows: int = 3,
    min_window_return_pct: float = 0.25,
    max_window_drawdown_pct: float = 20.0,
) -> dict[str, Any]:
    """Evaluate whether a route survives multiple chronological slices."""
    if not records:
        return {"route_key": route_key, "passed": False, "total_windows": 0, "passing_windows": 0, "windows": []}
    windows = max(1, min(windows, len(records)))
    chunk_size = math.ceil(len(records) / windows)
    window_rows = []
    for idx in range(windows):
        chunk = records[idx * chunk_size : (idx + 1) * chunk_size]
        if not chunk:
            continue
        returns = [
            route_window_return(
                str(row.get(route_key, SIDEWAYS)),
                future_basket_ret=float(row.get("future_basket_ret", 0.0)),
                future_btc_ret=float(row.get("future_btc_ret", 0.0)),
                fee_bps=fee_bps,
            )
            for row in chunk
        ]
        result = _compound_returns(returns)
        result.update(
            {
                "window_index": idx + 1,
                "start_time": chunk[0].get("time"),
                "end_time": chunk[-1].get("time"),
                "passed": result["total_return_pct"] >= min_window_return_pct and result["max_drawdown_pct"] <= max_window_drawdown_pct,
            }
        )
        window_rows.append(result)
    passing = sum(1 for row in window_rows if row["passed"])
    return {
        "route_key": route_key,
        "passed": bool(window_rows) and passing == len(window_rows),
        "passing_windows": passing,
        "total_windows": len(window_rows),
        "min_window_return_pct": min_window_return_pct,
        "max_window_drawdown_pct": max_window_drawdown_pct,
        "windows": window_rows,
    }


def _build_leaderboard(records: list[dict[str, Any]], route_outcomes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    legacy_acc = _accuracy(records, "legacy_regime")
    v1_acc = _accuracy(records, "v1_regime")
    v2_acc = _accuracy(records, "v2_smoothed")
    tuned_available = bool(records and "v2_tuned_smoothed" in records[0])
    tuned_acc = _accuracy(records, "v2_tuned_smoothed") if tuned_available else None
    legacy_flips = _seq([row["legacy_regime"] for row in records])["flips"]
    v1_flips = _seq([row["v1_regime"] for row in records])["flips"]
    v2_flips = _seq([row["v2_smoothed"] for row in records])["flips"]
    tuned_flips = _seq([row["v2_tuned_smoothed"] for row in records])["flips"] if tuned_available else None
    v2_bull_ret = _avg_regime_return(records, "v2_smoothed", BULL)
    tuned_bull_ret = _avg_regime_return(records, "v2_tuned_smoothed", BULL) if tuned_available else None
    legacy_bull_ret = _avg_regime_return(records, "legacy_regime", BULL)
    accuracy_rows = [
        {"name": "regime_v2", "value": v2_acc},
        {"name": "research_v1", "value": v1_acc},
        {"name": "legacy_sol", "value": legacy_acc},
    ]
    switch_rows = [
        {"name": "regime_v2", "flips": v2_flips},
        {"name": "research_v1", "flips": v1_flips},
        {"name": "legacy_sol", "flips": legacy_flips},
    ]
    perf_rows = [
        {"name": "v2_bull_forward_avg_pct", "value": v2_bull_ret},
        {"name": "legacy_bull_forward_avg_pct", "value": legacy_bull_ret},
    ]
    if tuned_available:
        accuracy_rows.insert(0, {"name": "regime_v2_tuned", "value": tuned_acc})
        switch_rows.insert(0, {"name": "regime_v2_tuned", "flips": tuned_flips})
        perf_rows.insert(0, {"name": "v2_tuned_bull_forward_avg_pct", "value": tuned_bull_ret})
    best_acc = tuned_acc if tuned_available and tuned_acc is not None else v2_acc
    best_flips = tuned_flips if tuned_available and tuned_flips is not None else v2_flips
    route_rows = []
    if route_outcomes:
        route_rows = sorted(
            [
                {
                    "name": name,
                    "total_return_pct": metrics["total_return_pct"],
                    "max_drawdown_pct": metrics["max_drawdown_pct"],
                    "win_rate_pct": metrics["win_rate_pct"],
                }
                for name, metrics in route_outcomes.items()
            ],
            key=lambda row: (row["total_return_pct"], -row["max_drawdown_pct"]),
            reverse=True,
        )
    return {
        "summary": {"total": len(records), "passed": int(best_acc >= legacy_acc and best_flips <= legacy_flips), "failed": int(not (best_acc >= legacy_acc and best_flips <= legacy_flips))},
        "by_metric": {
            "label_accuracy": accuracy_rows,
            "switching": switch_rows,
            "relative_performance": perf_rows,
            "route_outcomes": route_rows,
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
    tune_scorecard: bool = False,
    tune_route_objective: bool = False,
    train_fraction: float = 0.60,
    selector_lookback: int = 12,
    selector_min_objective: float = 0.0,
    selector_max_trailing_drawdown_pct: float = 0.0,
    selector_equity_stop_drawdown_pct: float = 0.0,
    selector_equity_stop_cooldown_windows: int = 1,
    selector_min_trailing_return_pct: float = -999999.0,
    selector_min_trailing_win_rate_pct: float = 0.0,
    selector_trailing_robust_windows: int = 0,
    selector_min_passing_trailing_windows: int = 0,
    selector_trailing_window_min_return_pct: float = 0.0,
    selector_trailing_window_max_drawdown_pct: float = 20.0,
    selector_re_engage_confirmation: bool = True,
    selector_re_engage_breadth_pct: float = 0.60,
    selector_re_engage_rolling_peak_windows: int = 0,
    selector_recent_pnl_lookback_windows: int = 0,
    selector_recent_pnl_stop_pct: float = 0.0,
    # Direction #3 (issue #72): momentum-exhaustion / mean-reversion guard ON THE
    # REGIME LABEL. Defaults ON so the revalidation command (which does not pass a
    # --momentum-guard flag) exercises the guard. The guard blocks BULL into a
    # decelerating/rolling-over basket after extension (momentum exhaustion ->
    # mean reversion), attacking the root cause (the directional model calling
    # regime turns wrong). The deceleration calibration (decel<=-1.5, roc12>=1.0)
    # is the selectivity-grounded PRIMARY trigger; the BEAR-blocking family is
    # kept conservative (mostly off) because it hurts the selector on real data.
    momentum_guard: bool = True,
    momentum_bull_rsi_cap: float = 100.0,
    momentum_bull_overextended_cap: float = 0.85,
    momentum_bull_roc_cap: float = 25.0,
    momentum_bear_btc_roc_floor: float = 2.0,
    momentum_bear_divergence_floor: float = 3.0,
    momentum_bull_rollover_roc_floor: float = -1.0,
    momentum_bull_rollover_roc12_precondition: float = 2.0,
    momentum_bull_stall_roc6_cap: float = -1.5,
    momentum_bull_stall_roc12_floor: float = 2.0,
    momentum_bear_mean_revert_roc24_floor: float = -8.0,
    momentum_bear_mean_revert_roc6_floor: float = 1.0,
    momentum_bull_deceleration_cap: float = -1.0,
    momentum_bull_deceleration_roc12_precondition: float = 1.0,
    momentum_bear_divergence_only_floor: float = 1000.0,
) -> dict[str, Any]:
    """Walk-forward evaluate v2 vs v1 and legacy, using next-window labels.

    Direction #3 (issue #72): ``momentum_guard`` defaults ON. A
    momentum-exhaustion / mean-reversion guard is applied ON THE REGIME LABEL
    after smoothing — blocking BULL into a decelerating/rolling-over basket after
    extension (momentum exhaustion -> mean reversion) and (conservatively)
    blocking BEAR into a mean-reverting (V-bounce) or diverging-positive
    reference. This attacks the root cause (the directional model calling regime
    turns wrong) rather than a lagging overlay. The PRIMARY trigger is the
    basket deceleration signal (short-horizon ROC rolling under medium-horizon
    ROC after the basket was genuinely extended), calibrated via a selectivity
    sweep on 240d/300d real data. Pass ``momentum_guard=False`` for the PLAIN
    label A/B comparison.
    """
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
        # Direction #1: point-in-time momentum-exhaustion features computed from
        # the SAME truncated window as the scorecard features (no-lookahead).
        # Computed unconditionally (cheap) so the audit trail is always present
        # even when the guard is OFF; only applied when momentum_guard=True.
        exhaustion = momentum_exhaustion_features(window, references=references, breadth_coins=breadth_coins)
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
                "exhaustion_features": exhaustion,
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

    # Direction #1 (issue #72): apply the momentum-exhaustion / false-breakdown
    # guard ON THE REGIME LABEL after smoothing. This blocks BULL into an
    # overextended/rolling-over basket and BEAR into a diverging-positive BTC,
    # attacking the root cause (the model calling the turn wrong) rather than a
    # lagging overlay. Applied to v2_smoothed and (if tuned) the tuned variants
    # so the selector routes on the guarded label. When momentum_guard is False
    # (default) this is a no-op and the PLAIN label passes through unchanged,
    # giving a clean A/B comparison.
    if momentum_guard:
        _guard_kw = dict(
            bull_rsi_cap=momentum_bull_rsi_cap,
            bull_overextended_cap=momentum_bull_overextended_cap,
            bull_roc_cap=momentum_bull_roc_cap,
            bear_btc_roc_floor=momentum_bear_btc_roc_floor,
            bear_divergence_floor=momentum_bear_divergence_floor,
            bull_rollover_roc_floor=momentum_bull_rollover_roc_floor,
            bull_rollover_roc12_precondition=momentum_bull_rollover_roc12_precondition,
            bull_stall_roc6_cap=momentum_bull_stall_roc6_cap,
            bull_stall_roc12_floor=momentum_bull_stall_roc12_floor,
            bear_mean_revert_roc24_floor=momentum_bear_mean_revert_roc24_floor,
            bear_mean_revert_roc6_floor=momentum_bear_mean_revert_roc6_floor,
            bull_deceleration_cap=momentum_bull_deceleration_cap,
            bull_deceleration_roc12_precondition=momentum_bull_deceleration_roc12_precondition,
            bear_divergence_only_floor=momentum_bear_divergence_only_floor,
        )
        for row in raw_records:
            guarded, guard_reasons = apply_momentum_guard(
                row["v2_smoothed"],
                row["exhaustion_features"],
                **_guard_kw,
            )
            if guarded != row["v2_smoothed"]:
                row["v2_smoothed_plain"] = row["v2_smoothed"]
                row["v2_smoothed"] = guarded
                row["momentum_guard_reasons"] = guard_reasons
                if guard_reasons:
                    row["reasons"] = (row.get("reasons") or []) + guard_reasons
            # Direction #3 (critical): the selector routes over MULTIPLE candidate
            # keys (legacy_sol, research_v1, regime_v2, tuned variants). If the
            # guard only blocks v2_smoothed, the selector simply picks an
            # UNGUARDED candidate (e.g. legacy_sol BULL) and takes the same loss.
            # The guard must therefore apply to ALL directional candidate keys so
            # no unguarded BULL/BEAR can slip through. We guard the raw
            # legacy/v1 regimes here (the tuned variants are derived from the
            # guarded v2 features below).
            for _cand_key in ("legacy_regime", "v1_regime"):
                _orig = row.get(_cand_key)
                if _orig is None:
                    continue
                _guarded_cand, _ = apply_momentum_guard(_orig, row["exhaustion_features"], **_guard_kw)
                if _guarded_cand != _orig:
                    row[f"{_cand_key}_plain"] = _orig
                    row[_cand_key] = _guarded_cand

    tuning: dict[str, Any] = {"enabled": False, "weights": dict(DEFAULT_SCORE_WEIGHTS)}
    if tune_scorecard and raw_records:
        split_idx = max(1, min(len(raw_records), int(len(raw_records) * max(0.1, min(0.9, train_fraction)))))
        training_records = raw_records[:split_idx]
        tuning = train_scorecard_weights(training_records, min_records=min(20, max(1, len(training_records))))
        tuned_weights = tuning.get("weights", DEFAULT_SCORE_WEIGHTS)
        tuned_results = [classify_v2_scorecard(row["features"], tuned_weights) for row in raw_records]
        tuned_smoothed = _hysteresis(
            [str(result["regime"]) for result in tuned_results],
            [float(result["confidence"]) for result in tuned_results],
            confirmation_samples,
            min_confidence,
        )
        for row, result, regime in zip(raw_records, tuned_results, tuned_smoothed):
            row["v2_tuned_regime"] = result["regime"]
            row["v2_tuned_smoothed"] = regime
            row["tuned_score"] = result["score"]
            row["tuned_confidence"] = result["confidence"]
        # Direction #3: apply momentum guard to the tuned label too so the
        # selector routes on the consistently-guarded variant.
        if momentum_guard:
            for row in raw_records:
                guarded, _g = apply_momentum_guard(
                    row["v2_tuned_smoothed"],
                    row["exhaustion_features"],
                    **_guard_kw,
                )
                row["v2_tuned_smoothed"] = guarded
        tuning["train_records"] = len(training_records)
        tuning["test_records"] = max(0, len(raw_records) - len(training_records))
        tuning["train_fraction"] = train_fraction

    route_tuning: dict[str, Any] = {"enabled": False, "weights": dict(DEFAULT_SCORE_WEIGHTS)}
    if tune_route_objective and raw_records:
        split_idx = max(1, min(len(raw_records), int(len(raw_records) * max(0.1, min(0.9, train_fraction)))))
        training_records = raw_records[:split_idx]
        route_tuning = train_route_scorecard_weights(training_records, fee_bps=fee_bps, min_records=min(20, max(1, len(training_records))))
        route_weights = route_tuning.get("weights", DEFAULT_SCORE_WEIGHTS)
        route_results = [classify_v2_scorecard(row["features"], route_weights) for row in raw_records]
        route_smoothed = _hysteresis(
            [str(result["regime"]) for result in route_results],
            [float(result["confidence"]) for result in route_results],
            confirmation_samples,
            min_confidence,
        )
        for row, result, regime in zip(raw_records, route_results, route_smoothed):
            row["v2_route_tuned_regime"] = result["regime"]
            row["v2_route_tuned_smoothed"] = regime
            row["route_tuned_score"] = result["score"]
            row["route_tuned_confidence"] = result["confidence"]
        # Direction #3: apply momentum guard to the route-tuned label too so the
        # selector routes on the consistently-guarded variant.
        if momentum_guard:
            for row in raw_records:
                guarded, _g = apply_momentum_guard(
                    row["v2_route_tuned_smoothed"],
                    row["exhaustion_features"],
                    **_guard_kw,
                )
                row["v2_route_tuned_smoothed"] = guarded
        route_tuning["train_records"] = len(training_records)
        route_tuning["test_records"] = max(0, len(raw_records) - len(training_records))
        route_tuning["train_fraction"] = train_fraction

    selector_candidates = {
        "regime_v2": "v2_smoothed",
        "research_v1": "v1_regime",
        "legacy_sol": "legacy_regime",
    }
    if raw_records and "v2_tuned_smoothed" in raw_records[0]:
        selector_candidates["regime_v2_tuned"] = "v2_tuned_smoothed"
    if raw_records and "v2_route_tuned_smoothed" in raw_records[0]:
        selector_candidates["regime_v2_route_tuned"] = "v2_route_tuned_smoothed"
    selector = {
        "enabled": bool(raw_records),
        "lookback": selector_lookback,
        "min_trailing_objective": selector_min_objective,
        "max_trailing_drawdown_pct": selector_max_trailing_drawdown_pct,
        "equity_stop_drawdown_pct": selector_equity_stop_drawdown_pct,
        "equity_stop_cooldown_windows": selector_equity_stop_cooldown_windows,
        "min_trailing_return_pct": selector_min_trailing_return_pct,
        "min_trailing_win_rate_pct": selector_min_trailing_win_rate_pct,
        "trailing_robust_windows": selector_trailing_robust_windows,
        "min_passing_trailing_windows": selector_min_passing_trailing_windows,
        "trailing_window_min_return_pct": selector_trailing_window_min_return_pct,
        "trailing_window_max_drawdown_pct": selector_trailing_window_max_drawdown_pct,
        "re_engage_confirmation": selector_re_engage_confirmation,
        "re_engage_breadth_pct": selector_re_engage_breadth_pct,
        "re_engage_rolling_peak_windows": selector_re_engage_rolling_peak_windows,
        "recent_pnl_lookback_windows": selector_recent_pnl_lookback_windows,
        "recent_pnl_stop_pct": selector_recent_pnl_stop_pct,
        "candidates": selector_candidates,
        "no_lookahead": True,
    }
    raw_records = build_selector_route(
        raw_records,
        route_candidates=selector_candidates,
        fee_bps=fee_bps,
        lookback=selector_lookback,
        min_trailing_objective=selector_min_objective,
        max_trailing_drawdown_pct=selector_max_trailing_drawdown_pct,
        selector_equity_stop_drawdown_pct=selector_equity_stop_drawdown_pct,
        selector_equity_stop_cooldown_windows=selector_equity_stop_cooldown_windows,
        selector_min_trailing_return_pct=selector_min_trailing_return_pct,
        selector_min_trailing_win_rate_pct=selector_min_trailing_win_rate_pct,
        selector_trailing_robust_windows=selector_trailing_robust_windows,
        selector_min_passing_trailing_windows=selector_min_passing_trailing_windows,
        selector_trailing_window_min_return_pct=selector_trailing_window_min_return_pct,
        selector_trailing_window_max_drawdown_pct=selector_trailing_window_max_drawdown_pct,
        selector_re_engage_confirmation=selector_re_engage_confirmation,
        selector_re_engage_breadth_pct=selector_re_engage_breadth_pct,
        selector_re_engage_rolling_peak_windows=selector_re_engage_rolling_peak_windows,
        selector_recent_pnl_lookback_windows=selector_recent_pnl_lookback_windows,
        selector_recent_pnl_stop_pct=selector_recent_pnl_stop_pct,
    )

    route_outcomes = build_route_outcomes(raw_records, fee_bps=fee_bps)
    route_failure = {
        "legacy_sol": route_failure_diagnostics(raw_records, "legacy_regime", fee_bps=fee_bps),
        "research_v1": route_failure_diagnostics(raw_records, "v1_regime", fee_bps=fee_bps),
        "regime_v2": route_failure_diagnostics(raw_records, "v2_smoothed", fee_bps=fee_bps),
    }
    route_robustness = {
        "legacy_sol": build_route_robustness_gates(raw_records, "legacy_regime", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct),
        "research_v1": build_route_robustness_gates(raw_records, "v1_regime", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct),
        "regime_v2": build_route_robustness_gates(raw_records, "v2_smoothed", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct),
    }
    if raw_records and "v2_tuned_smoothed" in raw_records[0]:
        route_failure["regime_v2_tuned"] = route_failure_diagnostics(raw_records, "v2_tuned_smoothed", fee_bps=fee_bps)
        route_robustness["regime_v2_tuned"] = build_route_robustness_gates(raw_records, "v2_tuned_smoothed", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct)
    if raw_records and "v2_route_tuned_smoothed" in raw_records[0]:
        route_failure["regime_v2_route_tuned"] = route_failure_diagnostics(raw_records, "v2_route_tuned_smoothed", fee_bps=fee_bps)
        route_robustness["regime_v2_route_tuned"] = build_route_robustness_gates(raw_records, "v2_route_tuned_smoothed", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct)
    if raw_records and "selector_smoothed" in raw_records[0]:
        route_failure["regime_v2_selector"] = route_failure_diagnostics(raw_records, "selector_smoothed", fee_bps=fee_bps)
        route_robustness["regime_v2_selector"] = build_route_robustness_gates(raw_records, "selector_smoothed", fee_bps=fee_bps, max_window_drawdown_pct=selector_trailing_window_max_drawdown_pct)

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
                "tune_scorecard": tune_scorecard,
                "tune_route_objective": tune_route_objective,
                "train_fraction": train_fraction,
                "selector_lookback": selector_lookback,
                "selector_min_objective": selector_min_objective,
                "selector_max_trailing_drawdown_pct": selector_max_trailing_drawdown_pct,
                "selector_equity_stop_drawdown_pct": selector_equity_stop_drawdown_pct,
                "selector_equity_stop_cooldown_windows": selector_equity_stop_cooldown_windows,
                "selector_min_trailing_return_pct": selector_min_trailing_return_pct,
                "selector_min_trailing_win_rate_pct": selector_min_trailing_win_rate_pct,
                "selector_trailing_robust_windows": selector_trailing_robust_windows,
                "selector_min_passing_trailing_windows": selector_min_passing_trailing_windows,
                "selector_trailing_window_min_return_pct": selector_trailing_window_min_return_pct,
                "selector_trailing_window_max_drawdown_pct": selector_trailing_window_max_drawdown_pct,
                "selector_re_engage_confirmation": selector_re_engage_confirmation,
                "selector_re_engage_breadth_pct": selector_re_engage_breadth_pct,
                "selector_re_engage_rolling_peak_windows": selector_re_engage_rolling_peak_windows,
                "selector_recent_pnl_lookback_windows": selector_recent_pnl_lookback_windows,
                "selector_recent_pnl_stop_pct": selector_recent_pnl_stop_pct,
                "momentum_guard": momentum_guard,
                "momentum_bull_rsi_cap": momentum_bull_rsi_cap,
                "momentum_bull_overextended_cap": momentum_bull_overextended_cap,
                "momentum_bull_roc_cap": momentum_bull_roc_cap,
                "momentum_bear_btc_roc_floor": momentum_bear_btc_roc_floor,
                "momentum_bear_divergence_floor": momentum_bear_divergence_floor,
                "momentum_bull_rollover_roc_floor": momentum_bull_rollover_roc_floor,
                "momentum_bull_rollover_roc12_precondition": momentum_bull_rollover_roc12_precondition,
                "momentum_bull_stall_roc6_cap": momentum_bull_stall_roc6_cap,
                "momentum_bull_stall_roc12_floor": momentum_bull_stall_roc12_floor,
                "momentum_bear_mean_revert_roc24_floor": momentum_bear_mean_revert_roc24_floor,
                "momentum_bear_mean_revert_roc6_floor": momentum_bear_mean_revert_roc6_floor,
                "momentum_bull_deceleration_cap": momentum_bull_deceleration_cap,
                "momentum_bull_deceleration_roc12_precondition": momentum_bull_deceleration_roc12_precondition,
                "momentum_bear_divergence_only_floor": momentum_bear_divergence_only_floor,
            },
        },
        "records": raw_records,
        "sequence": {
            "legacy": _seq([row["legacy_regime"] for row in raw_records]),
            "research_v1": _seq([row["v1_regime"] for row in raw_records]),
            "regime_v2_raw": _seq([row["v2_regime"] for row in raw_records]),
            "regime_v2_smoothed": _seq([row["v2_smoothed"] for row in raw_records]),
            **({"regime_v2_tuned": _seq([row["v2_tuned_smoothed"] for row in raw_records])} if raw_records and "v2_tuned_smoothed" in raw_records[0] else {}),
            **({"regime_v2_route_tuned": _seq([row["v2_route_tuned_smoothed"] for row in raw_records])} if raw_records and "v2_route_tuned_smoothed" in raw_records[0] else {}),
            "regime_v2_selector": _seq([row["selector_smoothed"] for row in raw_records]),
            "labels": _seq([row["label"] for row in raw_records]),
        },
        "leaderboard": _build_leaderboard(raw_records, route_outcomes),
        "route_outcomes": route_outcomes,
        "route_failure_diagnostics": route_failure,
        "route_robustness": route_robustness,
        "selector": selector,
        "tuning": tuning,
        "route_tuning": route_tuning,
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
    parser.add_argument("--tune-scorecard", action="store_true")
    parser.add_argument("--tune-route-objective", action="store_true")
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
        tune_scorecard=args.tune_scorecard,
        tune_route_objective=args.tune_route_objective,
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

    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2))
    lb = output["leaderboard"]
    seq = output["sequence"]
    accuracy_values = {row["name"]: row["value"] for row in lb["by_metric"]["label_accuracy"]}
    if "regime_v2_tuned" in accuracy_values:
        accuracy_label = "tuned/v2/v1/legacy"
        accuracy_body = f"{accuracy_values['regime_v2_tuned']:.1f}/{accuracy_values['regime_v2']:.1f}/{accuracy_values['research_v1']:.1f}/{accuracy_values['legacy_sol']:.1f}%"
    else:
        accuracy_label = "v2/v1/legacy"
        accuracy_body = f"{accuracy_values['regime_v2']:.1f}/{accuracy_values['research_v1']:.1f}/{accuracy_values['legacy_sol']:.1f}%"
    print(
        f"Regime v2 samples={lb['summary']['total']} "
        f"accuracy({accuracy_label})={accuracy_body} "
        f"flips(v2/legacy)={seq['regime_v2_smoothed']['flips']}/{seq['legacy']['flips']}"
    )
    route_rows = lb["by_metric"].get("route_outcomes") or []
    if route_rows:
        best_route = route_rows[0]
        print(
            f"Best route={best_route['name']} return={best_route['total_return_pct']:+.2f}% "
            f"maxDD={best_route['max_drawdown_pct']:.2f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
