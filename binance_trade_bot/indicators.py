"""
Technical indicators for the adaptive strategy.
Standalone module — no DB/API dependencies, safe to import anywhere.
"""


def compute_ema(values, period):
    """Compute Exponential Moving Average."""
    if not values:
        return None
    period = min(period, len(values))
    alpha = 2.0 / (period + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def compute_adx(highs, lows, closes, period=14):
    """
    Compute ADX (Average Directional Index) — measures trend strength.
    Returns (ADX, +DI, -DI).
    ADX > 25 = strong trend, < 20 = sideways.
    """
    if len(closes) < period * 2:
        return 0.0, 0.0, 0.0

    tr_list = []
    plus_dm_list = []
    minus_dm_list = []

    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        prev_high = highs[i - 1]
        prev_low = lows[i - 1]
        prev_close = closes[i - 1]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        tr_list.append(tr)

        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    def wilder_smooth(values, period):
        if len(values) < period:
            return values[-1] if values else 0.0
        smoothed = sum(values[:period])
        for v in values[period:]:
            smoothed = (smoothed - smoothed / period) + v
        return smoothed

    atr = wilder_smooth(tr_list, period)
    if atr == 0:
        return 0.0, 0.0, 0.0

    plus_di = 100 * wilder_smooth(plus_dm_list, period) / atr
    minus_di = 100 * wilder_smooth(minus_dm_list, period) / atr

    dx_list = []
    for i in range(0, len(tr_list), period):
        chunk_tr = tr_list[i:i + period]
        chunk_plus = plus_dm_list[i:i + period]
        chunk_minus = minus_dm_list[i:i + period]
        if not chunk_tr:
            continue
        s_tr = sum(chunk_tr)
        if s_tr == 0:
            continue
        s_plus = sum(chunk_plus)
        s_minus = sum(chunk_minus)
        denom = s_plus + s_minus
        if denom == 0:
            dx_list.append(0.0)
        else:
            dx_list.append(100 * abs(s_plus - s_minus) / denom)

    adx = sum(dx_list) / len(dx_list) if dx_list else 0.0
    return adx, plus_di, minus_di
