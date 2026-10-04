"""Volatility Squeeze: Bollinger Bands inside Keltner Channels.

MPhinance's Screen 3. When BB compresses inside KC, volatility is coiled.
When the squeeze FIRES (BB breaks outside KC), trade the expansion
direction. Fresh squeezes (1-5 bars) are watch-only; extended squeezes
(15+ bars) get larger size because long coils produce big moves.

States per symbol:
- none: no squeeze, not enough bars
- squeezed: BB inside KC for N bars (informational watch)
- fired: squeeze released this scan (trigger candidate)

Evidence first: dashboard panel + API + logged events. The Discord push
path follows the flow_pulse pattern, gated behind SQUEEZE_PUSH_ENABLED
(default false); Josh arms it himself.

A squeeze fire is a DIRECTIONAL trigger: the breakout direction comes from
the expansion (close above upper BB = bullish, below lower BB = bearish).
"""
import math
import time

VERSION = 'squeeze-v1'
SCAN_THROTTLE = 60
BB_PERIOD = 20
BB_MULT = 2.0
KC_PERIOD = 20
KC_ATR_PERIOD = 14
KC_MULT = 1.5
MIN_BARS = 30          # need enough history for both indicators
FRESH_MAX = 5          # 1-5 bars squeezed: watch, do not trade
EXTENDED_MIN = 15      # 15+ bars squeezed: larger size


def _sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _stdev(values, period):
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    return math.sqrt(sum((v - mean) ** 2 for v in window) / period)


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(bars, period):
    """bars: list of (high, low, close)."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, c = bars[i]
        pc = bars[i - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


def _bands(closes, hlc, end):
    """BB/KC at window ending at index `end` (exclusive). None if insufficient."""
    window = closes[:end]
    hlc_window = hlc[:end]
    mid = _sma(window, BB_PERIOD)
    sd = _stdev(window, BB_PERIOD)
    basis = _ema(window, KC_PERIOD)
    atr = _atr(hlc_window, KC_ATR_PERIOD)
    if mid is None or sd is None or basis is None or atr is None:
        return None
    bu, bl = mid + BB_MULT * sd, mid - BB_MULT * sd
    ku, kl = basis + KC_MULT * atr, basis - KC_MULT * atr
    return {"bb_upper": bu, "bb_lower": bl, "kc_upper": ku, "kc_lower": kl,
            "squeezed": bu < ku and bl > kl}


def _squeeze_run(closes, hlc, end):
    """Count consecutive squeezed bars ending at index `end` (exclusive)."""
    count = 0
    for e in range(end, MIN_BARS, -1):
        b = _bands(closes, hlc, e)
        if b is None or not b["squeezed"]:
            break
        count += 1
    return count


def compute_squeeze(bars):
    """Compute squeeze state from a list of bar dicts with o/h/l/c.

    bars: oldest-first list of {"o","h","l","c"} (floats).
    Returns dict with state, squeeze_bars, bb/kc values, direction on fire.
    """
    closes = [b["c"] for b in bars if b.get("c") is not None]
    hlc = [(b["h"], b["l"], b["c"]) for b in bars
           if b.get("h") is not None and b.get("l") is not None and b.get("c") is not None]
    n = len(closes)
    if n < MIN_BARS or len(hlc) < MIN_BARS:
        return {"state": "none", "reason": f"only {n} bars (need {MIN_BARS})",
                "squeeze_bars": 0}

    latest = _bands(closes, hlc, n)
    if latest is None:
        return {"state": "none", "reason": "indicator error", "squeeze_bars": 0}
    close = closes[-1]
    base = {"bb_upper": latest["bb_upper"], "bb_lower": latest["bb_lower"],
            "kc_upper": latest["kc_upper"], "kc_lower": latest["kc_lower"],
            "close": close}

    if latest["squeezed"]:
        run = _squeeze_run(closes, hlc, n)
        return {**base, "state": "squeezed", "squeeze_bars": run,
                "watch_only": True}

    # Not squeezed now: check for a fire. Walk back up to 5 bars to find the
    # most recent squeezed window (breakouts can span several bars).
    fired_run = 0
    for back in range(1, 6):
        run = _squeeze_run(closes, hlc, n - back)
        if run >= 1:
            fired_run = run
            break
    if fired_run >= 1:
        direction = ("bullish" if close > latest["bb_upper"] else
                     "bearish" if close < latest["bb_lower"] else None)
        size = "larger" if fired_run >= EXTENDED_MIN else "normal"
        return {**base, "state": "fired", "squeeze_bars": fired_run,
                "direction": direction, "size": size,
                "watch_only": fired_run <= FRESH_MAX}
    return {**base, "state": "none", "squeeze_bars": 0}


def detect(bars_by_symbol):
    """bars_by_symbol: {symbol: [bar dicts oldest-first]} -> {symbol: state}."""
    out = {}
    for symbol, bars in bars_by_symbol.items():
        try:
            out[symbol] = compute_squeeze(bars)
        except Exception:
            out[symbol] = {"state": "none", "reason": "compute error",
                           "squeeze_bars": 0}
    return out


def scan(db, c, cfg, now):
    """Throttled pass: detect squeezes/fires, persist, optionally queue push."""
    if not getattr(cfg, 'squeeze', True):
        return {'ran': False, 'reason': 'disabled'}
    if now - db.get(c, 'squeeze:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    symbols = list(getattr(cfg, 'watch_symbols', ()) or ())
    bars_by_symbol = {}
    for symbol in symbols:
        rows = db.recent(c, 'bar', symbol, limit=120)
        bars = [dict(r.get('payload') or {}) for r in reversed(rows)
                if isinstance(r.get('payload'), dict)]
        if bars:
            bars_by_symbol[symbol] = bars
    states = detect(bars_by_symbol)
    fired = []
    for symbol, state in states.items():
        if state.get('state') == 'fired' and not state.get('watch_only'):
            key = 'squeeze:%s:%s:%s' % (int(now // 3600), symbol,
                                        state.get('direction'))
            payload = {'kind': 'squeeze', 'symbol': symbol, 'at': now,
                       **{k: state.get(k) for k in (
                           'direction', 'squeeze_bars', 'size',
                           'bb_upper', 'bb_lower', 'kc_upper', 'kc_lower',
                           'close')}}
            if db.append(c, 'squeeze', 'squeeze', symbol, now, payload, key=key):
                fired.append(payload)
    db.put(c, 'squeeze:scanned_at', now)
    db.put(c, 'squeeze:latest', {'at': now, 'states': states,
                                'fired': len(fired)})
    return {'ran': True, 'symbols': len(states), 'fired': len(fired)}


def display(db, c, now, days=7):
    """Recent squeeze states for the dashboard panel."""
    latest = db.get(c, 'squeeze:latest', {})
    states = latest.get('states') or {}
    squeezed = {s: st for s, st in states.items()
                if st.get('state') in ('squeezed', 'fired')}
    return {'asof': now, 'version': VERSION,
            'states': squeezed, 'last_scan': latest.get('at'),
            'note': 'Evidence only. Fresh squeezes (<=5 bars) are watch-only.'}
