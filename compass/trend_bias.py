"""Daily trend-bias regime for ICT futures symbols (Rumers 3R rule, part 1).

Evidence-only context: for each configured ICT symbol, compare the latest
daily close against its N-day simple moving average (default 50, per Rumers'
"trend bias" rule). Price above the SMA -> buyers in control (bullish
regime); below -> sellers in control (bearish regime).

This never filters signals and never sizes positions. It answers one
question for trade framing: is a setup with-trend (runner framing) or
counter-trend (scalp framing)?

Data: TradingView daily bars ("1!" continuous symbols), cache-first. The
scan refreshes stale cache entries itself (bounded: only symbols whose
cache is older than 24h, failures degrade to no_data for that symbol).
"""

from .market import number

SCAN_THROTTLE = 24 * 3600
SMA_LEN_DEFAULT = 50
CACHE_MAX_AGE_S = 24 * 3600


def sma(values, length):
    """Simple moving average of the last `length` values, or None."""
    vals = [number(v) for v in (values or [])]
    vals = [v for v in vals if v is not None]
    if len(vals) < length or length < 1:
        return None
    return sum(vals[-length:]) / length


def classify(close, sma_v):
    """Regime label for a close vs its SMA. Pure function, no I/O."""
    close, sma_v = number(close), number(sma_v)
    if close is None or sma_v in (None, 0):
        return None
    return 'bullish' if close > sma_v else 'bearish'


def _daily_bars(symbol):
    """Cached TradingView daily bars, refreshing stale entries. Never raises."""
    try:
        from . import tradingview_daily as tv
    except Exception:
        return []
    try:
        if not tv.cache_fresh(symbol, max_age_s=CACHE_MAX_AGE_S):
            tv.refresh([symbol], max_age_s=CACHE_MAX_AGE_S)
        return tv.daily_bars_for_scan(symbol) or []
    except Exception:
        return []


def scan(db, c, cfg, now):
    """Throttled daily regime scan over configured ICT symbols."""
    if not getattr(cfg, 'ict_trend_bias', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'trend_bias:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    try:
        length = int(number(getattr(cfg, 'ict_trend_bias_sma_len',
                                    SMA_LEN_DEFAULT)) or SMA_LEN_DEFAULT)
    except (TypeError, ValueError):
        length = SMA_LEN_DEFAULT
    if length < 5:
        length = SMA_LEN_DEFAULT
    symbols = getattr(cfg, 'ict_symbols', ()) or ()
    rows = {}
    for symbol in symbols:
        bars = _daily_bars(symbol)
        closes = [b.get('c') for b in bars]
        s = sma(closes, length)
        close = number(closes[-1]) if closes else None
        regime = classify(close, s)
        if regime is None:
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_daily_bars',
                            'at': now}
            continue
        rows[symbol] = {'symbol': symbol, 'regime': regime, 'at': now,
                        'sma_len': length, 'sma': round(s, 2),
                        'close': round(close, 2),
                        'dist_pct': round((close - s) / s * 100, 2)}
    db.put(c, 'trend_bias:latest',
           {'at': now, 'rows': rows, 'sma_len': length,
            'status': 'running' if rows else 'idle'})
    db.put(c, 'trend_bias:scanned_at', now)
    return {'ran': True, 'symbols': len(rows)}


def display(db, c, now, limit=200):
    """Dashboard payload: latest regime snapshot."""
    latest = db.get(c, 'trend_bias:latest', {}) or {}
    rows = list((latest.get('rows') or {}).values())[:limit]
    return {'asof': latest.get('at'), 'params': {'sma_len': latest.get('sma_len')},
            'rows': rows, 'regime_rows': rows,
            'note': 'Daily trend-bias regime (close vs %d-day SMA). Context only: '
                    'with-trend setups get runner framing, counter-trend setups '
                    'get scalp framing. Never a filter, never a sizer.' %
                    (latest.get('sma_len') or SMA_LEN_DEFAULT)}
