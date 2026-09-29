"""ICC — Indication / Correction / Continuation (tradesbysci).

Pure price-action, dual-timeframe entry model:
  HTF structure on 1H bars (4H bias filter); entry confirmation on 15m.

  1. INDICATION: a 1H close beyond the highest high / lowest low of the
     prior LOOKBACK_1H (120) 1H bars = a genuine new high/low. Level L is
     that prior extreme.
  2. CORRECTION + LIQUIDITY GRAB: the FIRST 1H bar after indication that
     touches back to L is the grab — explicitly never traded.
  3. CONTINUATION: a later 1H bar closes back through L in the indication
     direction (the second push), confirmed by a 15m structure flip (micro
     break-of-structure in the indication direction, evaluated only on bars
     after the continuation bar closes). Only the second push is traded.
  4. STOP beyond the correction extreme (lowest low / highest high from
     indication through entry). TARGET at the nearest unswept opposing 1H
     swing level ("deeper liquidity"); EQH/EQL clusters (2+ swing points
     within EQ_TOL_ATR_FRAC x ATR) are indecision -> no trade. Falls back
     to 2R when no clean level exists.

CONVENTION NOTES / DEVIATIONS:
- The course cascades 4H -> 1H -> 15m/5m. Here 4H supplies a bias filter
  only (indication must agree with 4H swing structure); 1H carries the
  structure, 15m the entry flip. 5m precision is not modeled.
- "TP at the indication origin extended to deeper unswept liquidity" is
  implemented as nearest unswept opposing 1H swing level; the "final boss"
  reading (deepest pool) is not chased mechanically.
- Multi-day holds: paper positions are submitted with track='swing' so the
  engine exits() loop skips session flatten; flatten_at is set +14d as a
  backstop.
- The trader's performance claims are unaudited (and disputed); treat as
  unproven. Evidence only.

Evidence only. No alerts. Paper fills via ict_paper only when both
ICT_FUTURES_PAPER_ENABLED and the detector flag are on.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .market import number, day
from .ict_common import ict_bars, atr, fractal_swings, _bars_from_recent
from .instruments import future_root
from .ict_paper import submit as _paper_submit

NY = ZoneInfo("America/New_York")

SCAN_THROTTLE = 300
SUPPORTED_ROOTS = ('GC', 'CL', 'NQ')
LOOKBACK_1H_DEFAULT = 120
SWING_N_1H = 2
SWING_N_4H = 2
EQ_TOL_ATR_FRAC_DEFAULT = 0.15
MIN_RR_FALLBACK = 2.0
ATR_PERIOD = 14
BAR_LIMIT = 15000
FLATTEN_DAYS = 14


def resample(bars, minutes):
    """Resample ascending 1-min-ish bar dicts into N-minute NY-anchored bars."""
    out = []
    cur = None
    for b in bars:
        d = datetime.fromtimestamp(b['ts'], NY)
        if minutes >= 60:
            key = (d.date().isoformat(), d.hour // (minutes // 60))
        else:
            key = (d.date().isoformat(), d.hour, d.minute // minutes)
        if cur is None or cur['key'] != key:
            if cur:
                out.append({k: cur[k] for k in ('ts', 'o', 'h', 'l', 'c', 'v')})
            cur = {'key': key, 'ts': b['ts'], 'o': b['o'], 'h': b['h'],
                   'l': b['l'], 'c': b['c'], 'v': b.get('v') or 0}
        else:
            cur['h'] = max(cur['h'], b['h'])
            cur['l'] = min(cur['l'], b['l'])
            cur['c'] = b['c']
            cur['v'] += b.get('v') or 0
    if cur:
        out.append({k: cur[k] for k in ('ts', 'o', 'h', 'l', 'c', 'v')})
    return out


def htf_bias(h4):
    """'long'/'short'/None from 4H fractal swing structure.

    Compares the latest swing high/low against the most recent *distinct*
    predecessor (differences within 0.05 x ATR count as chop, not structure).
    Both highs and lows must agree; otherwise None.
    """
    highs, lows = fractal_swings(h4, SWING_N_4H)
    if len(highs) < 2 or len(lows) < 2:
        return None
    a = atr(h4, ATR_PERIOD)
    tol = 0.05 * (a or 0.0)

    def rising(pts):
        for k in range(len(pts) - 1, 0, -1):
            d = pts[k][1] - pts[k - 1][1]
            if abs(d) > tol:
                return d > 0
        return None

    rh, rl = rising(highs), rising(lows)
    if rh is True and rl is True:
        return 'long'
    if rh is False and rl is False:
        return 'short'
    return None


def detect_indication(h1, lookback=LOOKBACK_1H_DEFAULT):
    """Most recent 1H new-high/new-low close vs prior `lookback` bars.

    Returns (idx, direction, level) or (None, None, None). The level is the
    prior extreme that was broken.
    """
    if len(h1) < lookback + 2:
        return None, None, None
    for i in range(len(h1) - 1, lookback - 1, -1):
        window = h1[i - lookback:i]
        hi = max(b['h'] for b in window)
        lo = min(b['l'] for b in window)
        c = h1[i]['c']
        if c is not None and c > hi:
            return i, 'long', hi
        if c is not None and c < lo:
            return i, 'short', lo
    return None, None, None


def _touches(b, level, direction):
    if direction == 'long':
        return b['l'] is not None and b['l'] <= level
    return b['h'] is not None and b['h'] >= level


def detect_grab(h1, ind_idx, level, direction):
    """First 1H bar after indication touching back to the broken level."""
    for j in range(ind_idx + 1, len(h1)):
        if _touches(h1[j], level, direction):
            return j
    return None


def detect_continuation(h1, grab_idx, level, direction):
    """First 1H bar after the grab closing back through the level (second push)."""
    for j in range(grab_idx + 1, len(h1)):
        c = h1[j]['c']
        if c is None:
            continue
        if direction == 'long' and c > level:
            return j
        if direction == 'short' and c < level:
            return j
    return None


def micro_flip(m15, after_ts, direction):
    """15m structure flip after `after_ts` in `direction`.

    Long flip: a 15m close above the most recent confirmed 15m fractal
    swing high. Short: mirror. Returns (bar, price) or (None, None).
    """
    highs, lows = fractal_swings(m15, 2)
    for i, b in enumerate(m15):
        if b['ts'] <= after_ts:
            continue
        if direction == 'long':
            sh = [(idx, p) for idx, p in highs if idx < i - 1]
            if sh and b['c'] is not None and b['c'] > max(p for _, p in sh):
                return b, b['c']
        else:
            sl = [(idx, p) for idx, p in lows if idx < i - 1]
            if sl and b['c'] is not None and b['c'] < min(p for _, p in sl):
                return b, b['c']
    return None, None


def _eq_clusters(points, tol):
    """True when 2+ swing points cluster within `tol` (EQH/EQL indecision)."""
    pts = sorted(points)
    for a, b in zip(pts, pts[1:]):
        if abs(a - b) <= tol:
            return True
    return False


def pick_target(h1, entry, direction, atr_v, ind_idx,
                eq_tol_frac=EQ_TOL_ATR_FRAC_DEFAULT):
    """Nearest unswept opposing 1H swing level; (target, kind) or (None, ...).

    Only swings formed *before* the indication bar count — post-indication
    swings belong to the developing move, not to "deeper" pre-existing
    liquidity. Unswept = no bar after formation exceeded the level. Levels
    inside an EQH/EQL cluster (2+ swing points within tol) are skipped as
    indecision.
    """
    highs, lows = fractal_swings(h1, SWING_N_1H)
    tol = (eq_tol_frac or 0.0) * (atr_v or 0.0)
    if direction == 'long':
        swings = sorted([(i, p) for i, p in highs
                         if p > entry and i < ind_idx], key=lambda t: t[1])
        if tol and _eq_clusters([p for _, p in swings
                                 if p - entry <= 5 * (atr_v or 0)], tol):
            return None, 'eqh_eql'
        for fidx, lvl in swings:
            if not any(b['h'] > lvl for b in h1[fidx + 1:]):
                return lvl, 'liquidity'
    else:
        swings = sorted([(i, p) for i, p in lows
                         if p < entry and i < ind_idx], key=lambda t: -t[1])
        if tol and _eq_clusters([p for _, p in swings
                                 if entry - p <= 5 * (atr_v or 0)], tol):
            return None, 'eqh_eql'
        for fidx, lvl in swings:
            if not any(b['l'] < lvl for b in h1[fidx + 1:]):
                return lvl, 'liquidity'
    return None, 'no_level'


def detect_signal(bars_1m, lookback=LOOKBACK_1H_DEFAULT,
                  eq_tol_frac=EQ_TOL_ATR_FRAC_DEFAULT):
    """Full ICC pipeline on ascending 1-min-ish bar dicts.

    Returns the signal dict or None. Pure function (no db).
    """
    h1 = resample(bars_1m, 60)
    if len(h1) < lookback + 10:
        return None
    h4 = resample(bars_1m, 240)
    bias = htf_bias(h4)
    if bias is None:
        return None
    ind_idx, direction, level = detect_indication(h1, lookback)
    if ind_idx is None:
        return None
    want = 'long' if direction == 'long' else 'short'
    if bias != want:
        return None
    grab_idx = detect_grab(h1, ind_idx, level, direction)
    if grab_idx is None:
        return None
    cont_idx = detect_continuation(h1, grab_idx, level, direction)
    if cont_idx is None:
        return None
    m15 = resample(bars_1m, 15)
    flip_bar, entry = micro_flip(m15, h1[cont_idx]['ts'], direction)
    if flip_bar is None:
        return None
    seg = h1[ind_idx:cont_idx + 1]
    if direction == 'long':
        corr_extreme = min(b['l'] for b in seg)
        stop = corr_extreme
        risk = entry - stop
    else:
        corr_extreme = max(b['h'] for b in seg)
        stop = corr_extreme
        risk = stop - entry
    if risk is None or risk <= 0:
        return None
    atr_v = atr(h1, ATR_PERIOD)
    target, target_kind = pick_target(h1, entry, direction, atr_v, ind_idx,
                                      eq_tol_frac)
    if target_kind == 'eqh_eql':
        return None
    if target is None:
        target = entry + MIN_RR_FALLBACK * risk if direction == 'long' \
            else entry - MIN_RR_FALLBACK * risk
        target_kind = 'min_rr_fallback'
    rr = abs(target - entry) / risk
    return {'direction': direction, 'signal_ts': flip_bar['ts'],
            'level_price': level, 'level_kind': 'icc_continuation',
            'indication_ts': h1[ind_idx]['ts'], 'indication_level': level,
            'grab_ts': h1[grab_idx]['ts'], 'cont_ts': h1[cont_idx]['ts'],
            'htf_bias': bias, 'correction_extreme': corr_extreme,
            'entry': entry, 'stop': stop, 'target': target,
            'target_kind': target_kind, 'risk_pts': risk,
            'reward_pts': abs(target - entry), 'rr': rr, 'atr': atr_v}


def scan(db, c, cfg, now):
    """Throttled ICC scan over supported symbols."""
    if not getattr(cfg, 'ict_icc', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'icc:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    lookback = int(number(getattr(cfg, 'ict_icc_lookback_1h',
                                  LOOKBACK_1H_DEFAULT)) or LOOKBACK_1H_DEFAULT)
    eq_tol = number(getattr(cfg, 'ict_icc_eq_tol_atr_frac',
                            EQ_TOL_ATR_FRAC_DEFAULT))
    if eq_tol is None:
        eq_tol = EQ_TOL_ATR_FRAC_DEFAULT
    symbols = [s for s in (getattr(cfg, 'ict_symbols', ()) or ())
               if future_root(s) in SUPPORTED_ROOTS]
    rows, excluded = {}, {}
    n_signals = 0
    for symbol in symbols:
        window = ict_bars(db, c, symbol, limit=BAR_LIMIT)
        bars = window or _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))
        if not bars:
            excluded['no_bars'] = excluded.get('no_bars', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_bars', 'at': now}
            continue
        sig = detect_signal(bars, lookback=lookback, eq_tol_frac=eq_tol)
        if sig is None:
            excluded['no_setup'] = excluded.get('no_setup', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'no_setup', 'at': now}
            continue
        state = db.get(c, 'icc:signal:' + symbol, {}) or {}
        if state.get('indication_ts') == sig['indication_ts']:
            excluded['already_signaled'] = \
                excluded.get('already_signaled', 0) + 1
            rows[symbol] = {'symbol': symbol, 'excluded': 'already_signaled',
                            'at': now, **{k: sig[k] for k in
                                          ('direction', 'indication_ts')}}
            continue
        row = {'symbol': symbol, 'concept': 'icc', 'at': now, **sig}
        rows[symbol] = row
        payload = {k: v for k, v in row.items() if k != 'at'}
        db.append(c, 'icc_signal', 'yt_methods', symbol, now, payload,
                  key='icc:%s:%d' % (symbol, int(sig['indication_ts'])))
        db.put(c, 'icc:signal:' + symbol,
               {'indication_ts': sig['indication_ts'],
                'signal_ts': sig['signal_ts'], 'direction': sig['direction'],
                'at': now})
        _paper_submit(db, c, cfg, now, 'icc', symbol, sig,
                      track='swing', flatten_at=now + FLATTEN_DAYS * 86400)
        n_signals += 1
    db.put(c, 'icc:latest',
           {'at': now, 'rows': rows, 'excluded': excluded,
            'params': {'lookback_1h': lookback, 'eq_tol_atr_frac': eq_tol},
            'status': 'running' if rows else 'idle'})
    db.put(c, 'icc:scanned_at', now)
    return {'ran': True, 'symbols': len(rows), 'signals': n_signals,
            'excluded': excluded}


def display(db, c, now, limit=200):
    """Dashboard payload: latest snapshot plus recent ICC signals."""
    latest = db.get(c, 'icc:latest', {}) or {}
    all_rows = list((latest.get('rows') or {}).values())
    sig_rows = sorted([r for r in all_rows if 'excluded' not in r],
                      key=lambda r: r.get('signal_ts') or 0, reverse=True)
    excl_rows = [r for r in all_rows if 'excluded' in r]
    signals = db.recent(c, 'icc_signal', limit=50)
    return {'asof': latest.get('at'), 'params': latest.get('params', {}),
            'excluded': latest.get('excluded', {}),
            'rows': (sig_rows + excl_rows)[:limit],
            'signals': [{'symbol': s['symbol'], 'ts': s['ts'],
                         **(s.get('payload') or {})} for s in signals],
            'note': 'Research evidence only (trader claims unaudited and '
                    'disputed). No auto-admission, no alerts; simulated paper '
                    'fills only when ICT_FUTURES_PAPER_ENABLED is on; '
                    'multi-day holds skip session flatten. No live orders.'}
