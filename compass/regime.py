"""Market regime filter ported from the Quill Trend Kit Pine indicator.

Classifies the market as TREND UP, TREND DOWN, RANGE, or SQUEEZE using:
- ADX(14) >= 20 for trend strength
- Bollinger Band width percentile (100-bar lookback) < 20 for squeeze
- EMA 8 > EMA 21 for trend direction

Used as a gate in ict_paper.submit(): trend-continuation detectors are
blocked in RANGE/SQUEEZE (they chop), fade/reversion detectors are blocked
in strong TREND (they get run over).
"""

ADX_LEN = 14
ADX_THRESHOLD = 20.0
SQUEEZE_LOOKBACK = 100
SQUEEZE_THRESHOLD = 20.0
EMA_FAST = 8
EMA_SLOW = 21
BB_LEN = 20


def ema(values, length):
    """Simple EMA calculation over a list of closes."""
    if not values or len(values) < length:
        return None
    k = 2.0 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars, length=14):
    """Average True Range over bar dicts with h/l/c keys."""
    if len(bars) < length + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, c_prev = bars[i]['h'], bars[i]['l'], bars[i-1]['c']
        trs.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    if len(trs) < length:
        return None
    return sum(trs[-length:]) / length


def adx(bars, length=14):
    """ADX via Wilder's smoothing. Returns None if insufficient bars."""
    if len(bars) < 2 * length + 1:
        return None
    # True range, +DM, -DM
    trs, pdms, mdms = [], [], []
    for i in range(1, len(bars)):
        h, l = bars[i]['h'], bars[i]['l']
        ph, pl = bars[i-1]['h'], bars[i-1]['l']
        trs.append(max(h - l, abs(h - bars[i-1]['c']), abs(l - bars[i-1]['c'])))
        up = h - ph
        dn = pl - l
        pdms.append(up if up > dn and up > 0 else 0.0)
        mdms.append(dn if dn > up and dn > 0 else 0.0)
    # Wilder's smoothing
    def wilder(vals):
        s = sum(vals[:length])
        out = [s]
        for v in vals[length:]:
            s = s - s / length + v
            out.append(s)
        return out
    str_ = wilder(trs)
    spdm = wilder(pdms)
    smdm = wilder(mdms)
    # DX series
    dx = []
    for i in range(len(str_)):
        if str_[i] == 0:
            dx.append(0.0)
            continue
        pdi = 100 * spdm[i] / str_[i]
        mdi = 100 * smdm[i] / str_[i]
        denom = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dx) < length:
        return None
    # ADX is Wilder-smoothed DX
    a = sum(dx[:length]) / length
    for v in dx[length:]:
        a = (a * (length - 1) + v) / length
    return a


def bollinger_width_pct_rank(closes, bb_len=20, lookback=100, pct_threshold=20.0):
    """Bollinger Band width percent-rank. Returns (is_squeeze, rank)."""
    if len(closes) < lookback + bb_len:
        return False, 50.0
    widths = []
    for i in range(len(closes) - lookback, len(closes)):
        window = closes[max(0, i - bb_len + 1):i + 1]
        if len(window) < bb_len:
            continue
        sma = sum(window) / len(window)
        if sma == 0:
            continue
        var = sum((x - sma) ** 2 for x in window) / len(window)
        widths.append(100 * (var ** 0.5) / sma)
    if not widths:
        return False, 50.0
    cur = widths[-1]
    rank = 100.0 * sum(1 for w in widths if w < cur) / len(widths)
    return rank < pct_threshold, rank


def classify(bars):
    """Classify market regime from bar dicts (o/h/l/c/v).

    Returns dict with 'regime' in ('trend_up','trend_down','range','squeeze'),
    plus 'adx', 'bbw_rank', 'ema_fast', 'ema_slow'.
    Returns 'unknown' regime if insufficient data.
    """
    closes = [b['c'] for b in bars]
    result = {'regime': 'unknown', 'adx': None, 'bbw_rank': None,
              'ema_fast': None, 'ema_slow': None}
    if len(closes) < SQUEEZE_LOOKBACK + BB_LEN:
        return result
    ef = ema(closes, EMA_FAST)
    es = ema(closes, EMA_SLOW)
    ax = adx(bars, ADX_LEN)
    is_sqz, rank = bollinger_width_pct_rank(closes, BB_LEN, SQUEEZE_LOOKBACK,
                                            SQUEEZE_THRESHOLD)
    result.update({'adx': ax, 'bbw_rank': rank,
                   'ema_fast': ef, 'ema_slow': es})
    if is_sqz:
        result['regime'] = 'squeeze'
    elif ax is not None and ax >= ADX_THRESHOLD:
        if ef is not None and es is not None:
            result['regime'] = 'trend_up' if ef > es else 'trend_down'
        else:
            result['regime'] = 'trend_up'  # trending but direction unclear
    else:
        result['regime'] = 'range'
    return result


# Detector classification: which regime each detector wants.
# 'trend' = continuation/momentum (blocked in range/squeeze)
# 'fade' = mean-reversion (blocked in strong trend)
# 'any' = no regime filter applied (truly regime-agnostic)
# Map every detector to its style; 'any' only for those with no directional bias
DETECTOR_STYLE = {
    'bos_fvg': 'trend',
    'bos_gz_vwap': 'trend',
    'continuation': 'trend',
    'morning_drive': 'trend',
    'icc': 'trend',
    'rumers_box': 'trend',
    'golden_zone': 'trend',
    'gz_ema_cross': 'trend',
    'aoi_zones': 'fade',
    'aoi_fade': 'fade',
    'turtle_soup': 'fade',
    'smt_divergence': 'fade',  # SMT divergence is a reversal signal
    'session_liquidity': 'trend',  # Liquidity sweeps continue the trend
    'trend_rider': 'trend',  # Trend rider: captures long moves with trailing stops
    'htf_levels': 'any',  # No mechanical entry; context only
    'tier_a_b': 'any',  # Tier system, not directional
    'trend_bias': 'any',  # Bias indicator, not a trigger
}


def allowed(detector, regime, direction=None):
    """Check if a detector is allowed to fire in the given regime.
    
    direction: 'long', 'short', or None. When provided, trend detectors
    require direction to align with regime direction (longs in trend_up,
    shorts in trend_down).
    """
    style = DETECTOR_STYLE.get(detector, 'any')
    if style == 'any' or regime == 'unknown':
        return True
    if style == 'trend':
        if regime not in ('trend_up', 'trend_down'):
            return False
        # Direction alignment: longs need uptrend, shorts need downtrend
        if direction == 'long':
            return regime == 'trend_up'
        if direction == 'short':
            return regime == 'trend_down'
        return True
    if style == 'fade':
        return regime in ('range', 'squeeze')
    return True
