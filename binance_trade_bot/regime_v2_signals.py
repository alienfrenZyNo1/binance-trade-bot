"""Regime v2 candidate multi-signal detector.

CANDIDATE / RESEARCH MODULE — issue #102.
-----------------------------------------
This is NON-LIVE research code feeding the Regime v2 promotion pipeline
(issue #72). It is intentionally NOT wired into the live trading path
(`momentum_strategy.py`, `binance_api_manager.py`, `futures_manager.py`).
No function here performs any Binance API call: every detector accepts its
data as parameters so it can be unit-tested offline and replayed against
cached history.

The detectors implement the five signals recommended in
``research/strategy-hypotheses-2026-06.md`` §2.2:

1. Multi-coin breadth          (§2.2.5) — % of universe above EMA20/EMA50
2. BTC confirmation            (§2.2.1) — BTC vs its 50-EMA regime anchor
3. Realized volatility regime  (§2.2.2) — 24h realized vol classification
4. Funding-rate signal         (§2.2.3) — derivatives positioning
5. Composite regime score      (§2.3)   — weighted blend → BULL/SIDEWAYS/BEAR/STORMY

All numeric thresholds are module-level constants so they can be tuned by
the promotion pipeline without touching detector logic.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .indicators import compute_ema

# ──────────────────────────────────────────────────────────────────────────
# Regime labels — kept identical to the live strategy so a v2 classification
# can be compared apples-to-apples with the SOL-only classifier output.
# (binance_trade_bot/strategies/momentum_strategy.py:47-50)
# ──────────────────────────────────────────────────────────────────────────
BULL = "bull"
BEAR = "bear"
SIDEWAYS = "sideways"
STORMY = "stormy"

# ──────────────────────────────────────────────────────────────────────────
# Tunable thresholds (deliberately overridable for the promotion sweep).
# Defaults match the rationale in strategy-hypotheses-2026-06.md §2.2.
# ──────────────────────────────────────────────────────────────────────────

# Breadth (% of universe above an EMA).
BREADTH_BULL_THRESHOLD = 0.70   # >70% above EMA50 ⇒ broad bull
BREADTH_BEAR_THRESHOLD = 0.30   # <30% above EMA50 ⇒ broad bear

# BTC confirmation.
BTC_EMA_PERIOD = 50

# Realized volatility classification (24h stdev of hourly returns).
# Daily (24-bar) stdev of per-bar returns expressed as a fraction (0.01 = 1%).
VOL_LOW_MAX = 0.015      # <1.5%/bar stdev  → low
VOL_NORMAL_MAX = 0.035   # <3.5%/bar stdev  → normal
VOL_HIGH_MAX = 0.07      # <7%/bar stdev    → high
# anything ≥ VOL_HIGH_MAX → extreme (stormy territory)

# Funding-rate thresholds (fractional funding rate per interval, e.g. 0.001 = 0.1%).
FUNDING_OVERHEATED_LONG = 0.0010   # >+0.10%  ⇒ overheated longs (bull exhaustion)
FUNDING_BEAR_CAPITULATION = -0.0005  # <-0.05% ⇒ bear capitulation

# Composite score weights — must sum to 1.0 for the score to stay in [-1, 1].
DEFAULT_WEIGHTS = {
    "breadth": 0.40,
    "btc": 0.30,
    "volatility": 0.15,
    "funding": 0.15,
}

# Composite score → regime cut points.
SCORE_BULL_MIN = 0.35    # ≥ +0.35 ⇒ BULL
SCORE_BEAR_MAX = -0.35   # ≤ -0.35 ⇒ BEAR
# anything in (SCORE_BEAR_MAX, SCORE_BULL_MIN) is SIDEWAYS, unless the
# volatility sub-detector reads EXTREME, in which case it is promoted to STORMY.


# ──────────────────────────────────────────────────────────────────────────
# 1. Multi-coin breadth
# ──────────────────────────────────────────────────────────────────────────

def breadth_signal(
    coin_closes: Dict[str, Sequence[float]],
    ema_short_period: int = 20,
    ema_long_period: int = 50,
) -> Dict[str, float]:
    """Percentage of coins trading above their own EMA20 and EMA50.

    This is the breadth thrust signal from §2.2.5: participation narrows at
    tops and broadens at bottoms, so breadth turns before any single-coin ADX.

    Args:
        coin_closes: mapping of ``{symbol: [close prices, oldest→newest]}``.
            Each series must be long enough to compute its EMA (>= period);
            coins with insufficient history are counted as "not above".
        ema_short_period: short EMA window (default 20).
        ema_long_period: long EMA window (default 50).

    Returns:
        Dict with keys:
            - ``pct_above_ema20``: fraction of coins above their EMA20 ∈ [0, 1]
            - ``pct_above_ema50``: fraction of coins above their EMA50 ∈ [0, 1]
            - ``score``: composite breadth score ∈ [-1, 1]; driven primarily by
              the EMA50 breadth (the §2.2.5 anchor) with EMA20 as confirmation
            - ``valid_coins``: number of coins with enough history to evaluate
            - ``signal``: ``"bull"`` / ``"bear"`` / ``"neutral"`` from EMA50 thresholds
    """
    if not coin_closes:
        return _empty_breadth()

    above_ema20 = 0
    above_ema50 = 0
    valid = 0

    for symbol, closes in coin_closes.items():
        closes = list(closes)
        if len(closes) < max(ema_short_period, ema_long_period):
            # Not enough history — count the coin as "not above" (conservative).
            continue
        valid += 1
        price = closes[-1]
        ema20 = compute_ema(closes, ema_short_period)
        ema50 = compute_ema(closes, ema_long_period)
        if ema20 is not None and price > ema20:
            above_ema20 += 1
        if ema50 is not None and price > ema50:
            above_ema50 += 1

    if valid == 0:
        return _empty_breadth()

    pct20 = above_ema20 / valid
    pct50 = above_ema50 / valid

    # Map EMA50 breadth into [-1, 1] around its 50% midpoint, then nudge by the
    # EMA20 confirmation so a strong short-term breadth thrust is reflected.
    breadth50_score = 2.0 * (pct50 - 0.5)
    breadth20_score = 2.0 * (pct20 - 0.5)
    score = 0.7 * breadth50_score + 0.3 * breadth20_score
    score = max(-1.0, min(1.0, score))

    if pct50 >= BREADTH_BULL_THRESHOLD:
        signal = BULL
    elif pct50 <= BREADTH_BEAR_THRESHOLD:
        signal = BEAR
    else:
        signal = "neutral"

    return {
        "pct_above_ema20": pct20,
        "pct_above_ema50": pct50,
        "score": score,
        "valid_coins": valid,
        "signal": signal,
    }


def _empty_breadth() -> Dict[str, float]:
    return {
        "pct_above_ema20": 0.0,
        "pct_above_ema50": 0.0,
        "score": 0.0,
        "valid_coins": 0,
        "signal": "neutral",
    }


# ──────────────────────────────────────────────────────────────────────────
# 2. BTC confirmation
# ──────────────────────────────────────────────────────────────────────────

def btc_confirmation(
    btc_closes: Sequence[float],
    ema_period: int = BTC_EMA_PERIOD,
) -> Dict[str, object]:
    """BTC trend confirmation (§2.2.1).

    BTC drives ~60-75% of crypto directional variance. Requiring BTC to agree
    with the reference-coin signal filters SOL-specific false breaks that the
    current SOL-only classifier cannot see.

    Args:
        btc_closes: BTC close prices, oldest→newest.
        ema_period: EMA window for the trend anchor (default 50).

    Returns:
        Dict with keys:
            - ``above_ema``: bool, BTC above its EMA
            - ``price``: latest close
            - ``ema``: the EMA value (or None if insufficient data)
            - ``score``: +1 (above, bull-confirmation), -1 (below, bear-confirmation),
              0 if indeterminate
            - ``signal``: ``"bull"`` / ``"bear"`` / ``"neutral"``
    """
    btc_closes = list(btc_closes)
    if len(btc_closes) < ema_period:
        return {
            "above_ema": False,
            "price": btc_closes[-1] if btc_closes else None,
            "ema": None,
            "score": 0.0,
            "signal": "neutral",
        }

    price = btc_closes[-1]
    ema = compute_ema(btc_closes, ema_period)
    above = price > ema if ema is not None else False
    score = 1.0 if above else -1.0
    signal = BULL if above else BEAR
    return {
        "above_ema": above,
        "price": price,
        "ema": ema,
        "score": score,
        "signal": signal,
    }


# ──────────────────────────────────────────────────────────────────────────
# 3. Volatility regime
# ──────────────────────────────────────────────────────────────────────────

VOL_LOW = "low"
VOL_NORMAL = "normal"
VOL_HIGH = "high"
VOL_EXTREME = "extreme"


def volatility_regime(
    closes: Sequence[float],
    window: int = 24,
) -> Dict[str, object]:
    """24h realized volatility regime (§2.2.2).

    Realized volatility = stdev of hourly log returns over ``window`` bars.
    Spikes precede/accompany crashes; classifying the regime re-enables the
    STORMY branch that the live path currently dead-codes
    (`momentum_strategy.py` logs ``avg_volatility=0.0``).

    Args:
        closes: close prices, oldest→newest (ideally hourly bars).
        window: number of bars to compute stdev over (default 24 ⇒ 24h).

    Returns:
        Dict with keys:
            - ``realized_vol``: stdev of log returns over the window
            - ``regime``: one of ``low``/``normal``/``high``/``extreme``
            - ``score``: ∈ [-1, 1]; higher vol ⇒ more defensive (negative)
            - ``is_extreme``: bool convenience flag
    """
    closes = list(closes)
    if len(closes) < window + 1 or window < 2:
        return {
            "realized_vol": 0.0,
            "regime": VOL_NORMAL,
            "score": 0.0,
            "is_extreme": False,
        }

    log_rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - window, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(log_rets) < 2:
        return {
            "realized_vol": 0.0,
            "regime": VOL_NORMAL,
            "score": 0.0,
            "is_extreme": False,
        }

    mean = sum(log_rets) / len(log_rets)
    variance = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    stdev = math.sqrt(variance)

    if stdev < VOL_LOW_MAX:
        regime = VOL_LOW
    elif stdev < VOL_NORMAL_MAX:
        regime = VOL_NORMAL
    elif stdev < VOL_HIGH_MAX:
        regime = VOL_HIGH
    else:
        regime = VOL_EXTREME

    # Score: defensive (negative) as vol rises. Linear interpolation between
    # the LOW and EXTREME anchors, clamped to [-1, 1].
    if regime == VOL_LOW:
        score = 0.5
    elif regime == VOL_NORMAL:
        score = 0.0
    elif regime == VOL_HIGH:
        score = -0.5
    else:
        score = -1.0

    return {
        "realized_vol": stdev,
        "regime": regime,
        "score": score,
        "is_extreme": regime == VOL_EXTREME,
    }


# ──────────────────────────────────────────────────────────────────────────
# 4. Funding rate signal
# ──────────────────────────────────────────────────────────────────────────

def funding_rate_signal(
    funding_rates: Sequence[float],
) -> Dict[str, object]:
    """Derivatives positioning from per-interval funding rates (§2.2.3).

    Funding reflects what leveraged traders are already doing — something
    price-only indicators (ADX/EMA) cannot see. High positive funding ⇒
    overheated longs (bull exhaustion / liquidation risk); persistently
    negative funding ⇒ bear capitulation (squeeze risk).

    **Does NOT call Binance.** Callers pass already-fetched funding rates;
    the futures manager already fetches per-symbol funding
    (`futures_manager.py:979`) and that path can feed this detector.

    Args:
        funding_rates: per-interval funding rates (fractions, e.g. 0.001 = 0.1%).
            A single value or a list of recent values (median is used).

    Returns:
        Dict with keys:
            - ``funding_rate``: the (median) funding rate used
            - ``score``: ∈ [-1, 1]; positive funding ⇒ overheated ⇒ negative score
            - ``signal``: ``"overheated_bull"`` / ``"bear_capitulation"`` / ``"neutral"``
    """
    rates = [float(r) for r in funding_rates if r is not None]
    if not rates:
        return {
            "funding_rate": 0.0,
            "score": 0.0,
            "signal": "neutral",
        }

    rates_sorted = sorted(rates)
    mid = len(rates_sorted) // 2
    median = (
        rates_sorted[mid]
        if len(rates_sorted) % 2 == 1
        else 0.5 * (rates_sorted[mid - 1] + rates_sorted[mid])
    )

    if median >= FUNDING_OVERHEATED_LONG:
        signal = "overheated_bull"
        # Scale the score from 0 (at threshold) toward -1 (at ~5× threshold).
        score = max(-1.0, -(median - FUNDING_OVERHEATED_LONG) / (FUNDING_OVERHEATED_LONG * 4.0 + 1e-12))
    elif median <= FUNDING_BEAR_CAPITULATION:
        signal = "bear_capitulation"
        # Negative funding ⇒ capitulation ⇒ contrarian-bullish lean, but the
        # primary read is "bear positioning". Map magnitude to a mild positive
        # score so it nudges toward STORMY/BEAR resolution in the composite.
        score = min(1.0, (FUNDING_BEAR_CAPITULATION - median) / (abs(FUNDING_BEAR_CAPITULATION) * 4.0 + 1e-12))
    else:
        signal = "neutral"
        score = 0.0

    return {
        "funding_rate": median,
        "score": max(-1.0, min(1.0, score)),
        "signal": signal,
    }


# ──────────────────────────────────────────────────────────────────────────
# 5. Composite regime score
# ──────────────────────────────────────────────────────────────────────────

def composite_regime(
    coin_closes: Optional[Dict[str, Sequence[float]]] = None,
    btc_closes: Optional[Sequence[float]] = None,
    vol_closes: Optional[Sequence[float]] = None,
    funding_rates: Optional[Sequence[float]] = None,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Weighted composite of the four sub-detectors → regime classification.

    Implements the §2.3 composite regime score in priority order
    (breadth + BTC agreement, then volatility, then funding). All inputs are
    optional — any detector without data contributes a neutral 0.0 score but
    its weight is renormalized away so the remaining signals still decide.

    Args:
        coin_closes: universe closes for the breadth detector (1).
        btc_closes: BTC closes for the confirmation detector (2).
        vol_closes: closes for the volatility detector (3); if omitted, BTC
            closes are reused when available.
        funding_rates: funding rates for the funding detector (4).
        weights: override of ``DEFAULT_WEIGHTS`` (must cover the same keys).

    Returns:
        Dict with keys:
            - ``regime``: BULL / SIDEWAYS / BEAR / STORMY
            - ``score``: composite score ∈ [-1, 1]
            - ``components``: per-detector result dicts (debug / logging)
            - ``weights``: the effective (renormalized) weights used
            - ``reasons``: human-readable list explaining the classification
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights is not None:
        w.update(weights)

    components: Dict[str, Dict[str, object]] = {}
    subscores: Dict[str, float] = {}

    # 1. Breadth
    if coin_closes:
        b = breadth_signal(coin_closes)
        components["breadth"] = b
        subscores["breadth"] = b["score"]
    else:
        components["breadth"] = _empty_breadth()

    # 2. BTC confirmation
    if btc_closes:
        btc = btc_confirmation(btc_closes)
        components["btc"] = btc
        subscores["btc"] = btc["score"]
    else:
        components["btc"] = {
            "above_ema": False,
            "price": None,
            "ema": None,
            "score": 0.0,
            "signal": "neutral",
        }

    # 3. Volatility (reuse BTC closes as the vol reference if none supplied)
    vol_series = vol_closes if vol_closes is not None else btc_closes
    if vol_series:
        vol = volatility_regime(vol_series)
        components["volatility"] = vol
        subscores["volatility"] = vol["score"]
    else:
        components["volatility"] = {
            "realized_vol": 0.0,
            "regime": VOL_NORMAL,
            "score": 0.0,
            "is_extreme": False,
        }

    # 4. Funding
    if funding_rates:
        fund = funding_rate_signal(funding_rates)
        components["funding"] = fund
        subscores["funding"] = fund["score"]
    else:
        components["funding"] = {
            "funding_rate": 0.0,
            "score": 0.0,
            "signal": "neutral",
        }

    # Renormalize weights across detectors that actually produced a subscore
    # so a missing detector does not silently drag the composite to 0.
    active = {k: w[k] for k in subscores if w.get(k, 0.0) > 0}
    weight_sum = sum(active.values())
    if weight_sum <= 0:
        # Fall back to defaults evenly over whatever produced a subscore.
        active = {k: 1.0 for k in subscores}
        weight_sum = float(len(active)) or 1.0

    score = 0.0
    for k, sub in subscores.items():
        score += sub * active.get(k, 0.0)
    score = score / weight_sum if weight_sum else 0.0
    score = max(-1.0, min(1.0, score))

    regime, reasons = _classify_composite(score, components)
    effective_weights = {k: (v / weight_sum if weight_sum else 0.0) for k, v in active.items()}

    return {
        "regime": regime,
        "score": score,
        "components": components,
        "weights": effective_weights,
        "reasons": reasons,
    }


def _classify_composite(
    score: float,
    components: Dict[str, Dict[str, object]],
) -> tuple:
    """Resolve a composite score + sub-detector reads into a regime label."""
    reasons: List[str] = []
    vol = components.get("volatility", {})
    is_extreme = bool(vol.get("is_extreme", False))

    breadth = components.get("breadth", {})
    btc = components.get("btc", {})
    breadth_signal_lbl = breadth.get("signal", "neutral")
    btc_signal_lbl = btc.get("signal", "neutral")

    # Agreement notes for explainability.
    if breadth_signal_lbl != "neutral" and btc_signal_lbl != "neutral":
        if breadth_signal_lbl == btc_signal_lbl:
            reasons.append(
                f"breadth+BTC agreement on {breadth_signal_lbl.upper()}"
            )
        else:
            reasons.append(
                f"breadth({breadth_signal_lbl}) vs BTC({btc_signal_lbl}) divergence"
            )

    # STORMY overrides whenever realized vol is extreme — defense-first.
    if is_extreme:
        reasons.append(
            f"extreme realized vol ({vol.get('realized_vol', 0.0):.4f}) → STORMY"
        )
        return STORMY, reasons

    if score >= SCORE_BULL_MIN:
        reasons.append(f"composite score {score:+.2f} ≥ {SCORE_BULL_MIN} → BULL")
        return BULL, reasons
    if score <= SCORE_BEAR_MAX:
        reasons.append(f"composite score {score:+.2f} ≤ {SCORE_BEAR_MAX} → BEAR")
        return BEAR, reasons

    reasons.append(
        f"composite score {score:+.2f} within sideways band → SIDEWAYS"
    )
    return SIDEWAYS, reasons


__all__ = [
    # labels
    "BULL",
    "BEAR",
    "SIDEWAYS",
    "STORMY",
    "VOL_LOW",
    "VOL_NORMAL",
    "VOL_HIGH",
    "VOL_EXTREME",
    # thresholds
    "BREADTH_BULL_THRESHOLD",
    "BREADTH_BEAR_THRESHOLD",
    "BTC_EMA_PERIOD",
    "VOL_LOW_MAX",
    "VOL_NORMAL_MAX",
    "VOL_HIGH_MAX",
    "FUNDING_OVERHEATED_LONG",
    "FUNDING_BEAR_CAPITULATION",
    "DEFAULT_WEIGHTS",
    "SCORE_BULL_MIN",
    "SCORE_BEAR_MAX",
    # detectors
    "breadth_signal",
    "btc_confirmation",
    "volatility_regime",
    "funding_rate_signal",
    "composite_regime",
]
