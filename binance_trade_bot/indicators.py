"""
Technical indicators for the adaptive strategy.
Standalone module — no DB/API dependencies, safe to import anywhere.
"""

import math


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


def compute_sma(values, period):
    """Simple Moving Average."""
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def compute_std(values, period):
    """Standard deviation over the last N values."""
    if not values or len(values) < period:
        return 0.0
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return math.sqrt(variance)


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


def compute_rsi(closes, period=14):
    """
    Compute RSI (Relative Strength Index).
    Returns value 0-100, or None if insufficient data.
    Uses Wilder's smoothing method.
    """
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    # Initial average (simple average of first 'period' changes)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_bollinger_bands(closes, period=20, num_std=2.0):
    """
    Compute Bollinger Bands.
    Returns (middle, upper, lower, bandwidth) or None if insufficient data.
    - middle: SMA(period)
    - upper: middle + num_std * std
    - lower: middle - num_std * std
    - bandwidth: (upper - lower) / middle (relative width)
    """
    if len(closes) < period:
        return None

    middle = compute_sma(closes, period)
    if middle is None:
        return None
    std = compute_std(closes, period)
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle if middle > 0 else 0.0

    return middle, upper, lower, bandwidth


def detect_bollinger_squeeze(closes, period=20, squeeze_lookback=50, num_std=2.0):
    """
    Detect Bollinger Band squeeze — a period of low volatility that
    typically precedes a breakout.

    Returns (is_squeeze, current_bandwidth, percentile) where:
    - is_squeeze: True if current bandwidth is in the bottom 20% of recent history
    - current_bandwidth: the latest bandwidth value
    - percentile: where current bandwidth ranks (0-100) vs recent history

    A squeeze is a signal that a big move is coming (but doesn't indicate direction).
    Combined with other signals (momentum, RSI), it helps time entries.
    """
    if len(closes) < max(period, squeeze_lookback):
        return False, 0.0, 50.0

    # Calculate bandwidth over the lookback window
    bandwidths = []
    for i in range(len(closes) - squeeze_lookback, len(closes)):
        if i < period:
            continue
        window = closes[i - period + 1: i + 1]
        if len(window) < period:
            continue
        result = compute_bollinger_bands(window, period, num_std)
        if result:
            bandwidths.append(result[3])

    if not bandwidths:
        return False, 0.0, 50.0

    current_bw = bandwidths[-1]
    sorted_bw = sorted(bandwidths)
    rank = sum(1 for b in sorted_bw if b <= current_bw)
    percentile = (rank / len(sorted_bw)) * 100

    # Squeeze if in the bottom 20th percentile of recent bandwidths
    is_squeeze = percentile <= 20

    return is_squeeze, current_bw, percentile


def compute_correlation(series_a, series_b):
    """
    Compute Pearson correlation coefficient between two return series.
    Returns value between -1 and 1, or 0.0 if insufficient data.
    """
    n = min(len(series_a), len(series_b))
    if n < 5:
        return 0.0

    a = series_a[-n:]
    b = series_b[-n:]

    mean_a = sum(a) / n
    mean_b = sum(b) / n

    numerator = sum((ai - mean_a) * (bi - mean_b) for ai, bi in zip(a, b))
    denom_a = math.sqrt(sum((ai - mean_a) ** 2 for ai in a))
    denom_b = math.sqrt(sum((bi - mean_b) ** 2 for bi in b))

    if denom_a == 0 or denom_b == 0:
        return 0.0

    return numerator / (denom_a * denom_b)


def compute_returns(prices):
    """
    Compute period-over-period returns from a price series.
    Returns list of percentage returns.
    """
    if len(prices) < 2:
        return []
    returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] == 0:
            continue
        returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
    return returns


def compute_correlation_matrix(price_series_dict, min_periods=10):
    """
    Compute correlation matrix between multiple assets.

    Args:
        price_series_dict: {symbol: [prices]} — each list is a price series
        min_periods: minimum number of overlapping data points required

    Returns:
        {symbol: {symbol: correlation}} nested dict
    """
    symbols = list(price_series_dict.keys())
    # Convert to returns
    returns_dict = {s: compute_returns(prices) for s, prices in price_series_dict.items()}

    matrix = {}
    for s1 in symbols:
        matrix[s1] = {}
        for s2 in symbols:
            r1 = returns_dict.get(s1, [])
            r2 = returns_dict.get(s2, [])
            if len(r1) >= min_periods and len(r2) >= min_periods:
                matrix[s1][s2] = compute_correlation(r1, r2)
            else:
                matrix[s1][s2] = 0.0

    return matrix
