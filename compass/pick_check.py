"""Single-pick evidence check: stack one external pick against Compass evidence.

Evidence only — no alerts, no auto-admission, no trades. Nothing here is a
trading signal until prospective measurement says otherwise. Checks are
logged so analyst hit rates can be measured later.
"""
import time

from . import apex_magnet, tape_confirmed, gap_continuation, breakouts

LONG = {'long', 'bullish', 'call', 'calls', 'c'}
SHORT = {'short', 'bearish', 'put', 'puts', 'p'}


def normalize_direction(raw):
    d = str(raw or '').strip().lower()
    if d in LONG:
        return 'long'
    if d in SHORT:
        return 'short'
    raise ValueError("direction must be long/short (call/put accepted)")


def _rows_for(rows, symbol):
    return [r for r in (rows or []) if str(r.get('symbol') or '').upper() == symbol]


def _apex_section(display, symbol, direction, spot, target):
    rows = _rows_for(display.get('rows'), symbol)
    above = sorted([r for r in rows if (r.get('magnet') or 0) > (spot or 0)],
                   key=lambda r: r['magnet'])
    below = sorted([r for r in rows if (r.get('magnet') or 0) < (spot or 0)],
                   key=lambda r: r['magnet'], reverse=True)
    detail = {'nearest_above': above[0] if above else None,
              'nearest_below': below[0] if below else None,
              'signal': rows[0].get('signal') if rows else None}
    note = None
    if target and spot:
        between = [r for r in (above if direction == 'long' else below)
                   if (r['magnet'] - spot) * (1 if direction == 'long' else -1) <
                   (target - spot) * (1 if direction == 'long' else -1)]
        if between:
            note = ('A magnet at $%.2f sits between spot and the target; '
                    'dealer positioning may pin or stall price there.' % between[0]['magnet'])
    return {'alignment': 'info' if rows else 'no_data', 'note': note,
            'detail': detail,
            'basis': 'Vendor magnet levels joined with minute bars; drift history not joined.'}


def _tape_section(display, symbol, direction):
    recent = _rows_for(display.get('recent_confirmed'), symbol)
    if not recent:
        return {'alignment': 'no_data', 'note': None, 'detail': {'confirmed': 0},
                'basis': 'No tape-confirmed trials for this symbol in the window.'}
    same = [r for r in recent
            if (str(r.get('side') or '').lower() in
                (LONG if direction == 'long' else SHORT))]
    opp = [r for r in recent if r not in same]
    if same and not opp:
        align, note = 'supports', '%d same-direction institutional print(s) confirmed within ±15 min.' % len(same)
    elif opp and not same:
        align, note = 'contradicts', '%d opposite-direction institutional print(s) confirmed.' % len(opp)
    else:
        align, note = 'neutral', 'Mixed institutional flow: %d with, %d against.' % (len(same), len(opp))
    return {'alignment': align, 'note': note,
            'detail': {'confirmed': len(recent), 'with_pick': len(same), 'against_pick': len(opp)},
            'basis': 'Setup trials joined with ≥$250k same-direction flow (±15 min, before invalidation).'}


def _gap_section(display, symbol, direction):
    states = _rows_for(display.get('board'), symbol)
    st = states[0] if states else None
    if not st:
        return {'alignment': 'no_data', 'note': None, 'detail': {},
                'basis': 'No gap record for this symbol today.'}
    want = 'up' if direction == 'long' else 'down'
    gap_dir = str(st.get('gap_direction') or st.get('direction') or '')
    brk_dir = str(st.get('break_direction') or '')
    stage, qualified = st.get('stage'), bool(st.get('qualified'))
    if qualified and brk_dir == want:
        align, note = 'supports', 'Gap %s with a qualified opening-range break and hold.' % gap_dir
    elif gap_dir and gap_dir != want:
        align, note = 'contradicts', 'Today\'s gap points %s, against the pick.' % gap_dir
    else:
        align, note = 'neutral', 'Gap %s, stage: %s.' % (gap_dir or 'n/a', stage or 'n/a')
    return {'alignment': align, 'note': note,
            'detail': {'gap_pct': st.get('gap_pct'), 'stage': stage, 'qualified': qualified},
            'basis': 'Gaps ≥1.5% in curated large caps; 09:30–09:45 break plus 10-minute hold.'}


def _breakout_section(display, symbol, direction):
    want = 'up' if direction == 'long' else 'down'
    events = _rows_for(display.get('fresh'), symbol) + _rows_for(display.get('forming'), symbol)
    if not events:
        return {'alignment': 'no_data', 'note': None, 'detail': {},
                'basis': 'No active triangle coil or fresh range break for this symbol.'}
    fresh = [e for e in events if e.get('status') in ('fresh', 'firing')]
    if fresh:
        d = str(fresh[0].get('direction') or '')
        align = 'supports' if d == want else 'contradicts' if d else 'neutral'
        note = 'Fresh %s range break at $%s.' % (d, fresh[0].get('level'))
    else:
        align, note = 'neutral', 'Triangle coil forming; no break yet.'
    return {'alignment': align, 'note': note,
            'detail': {'events': [{'direction': e.get('direction'), 'level': e.get('level'),
                                   'status': e.get('status')} for e in events[:3]]},
            'basis': 'Close-only 20/50/253-session range breaks and 15-session triangle coils, 1.5× volume.'}


def _exposure_section(db, c, symbol):
    raw = {k[9:]: v for k, v in db.prefix(c, 'exposure:').items()}
    hit = None
    for key, val in raw.items():
        if key.upper() == symbol or (isinstance(val, dict) and
                                     str(val.get('symbol') or '').upper() == symbol):
            hit = val
            break
    if not hit:
        return {'alignment': 'no_data', 'note': None, 'detail': {},
                'basis': 'No exposure snapshot stored for this symbol.'}
    return {'alignment': 'info', 'note': None,
            'detail': hit if isinstance(hit, dict) else {'value': hit},
            'basis': 'Latest stored exposure snapshot; check the GEX tab for freshness.'}


def _safe(fn, *args):
    """One scanner's display must never sink the whole check."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - evidence-only degradation
        return {'asof': None, 'rows': [], 'board': [], 'fresh': [], 'forming': [],
                'recent_confirmed': [], '_error': '%s: %s' % (type(e).__name__, e)}


def _pillar_error(name, err):
    return {'alignment': 'no_data', 'note': None, 'detail': {},
            'basis': 'Scanner unavailable for this check (%s).' % (err.get('_error') or 'no data')}


def check(db, c, now, ticker, direction, entry=None, target=None, source=''):
    symbol = str(ticker or '').strip().upper()
    if not symbol:
        raise ValueError('ticker is required')
    if len(symbol) > 12 or not symbol.replace('.', '').replace('-', '').isalnum():
        raise ValueError('ticker looks invalid')
    direction = normalize_direction(direction)
    try:
        entry = float(entry) if entry not in (None, '') else None
        target = float(target) if target not in (None, '') else None
    except (TypeError, ValueError):
        raise ValueError('entry and target must be numbers')
    if entry is not None and entry <= 0:
        raise ValueError('entry must be positive')
    if target is not None and target <= 0:
        raise ValueError('target must be positive')

    apex = _safe(apex_magnet.display, db, c, now)
    spot = None
    for r in _rows_for(apex.get('rows'), symbol):
        spot = r.get('spot')
        break

    tape_d = _safe(tape_confirmed.display, db, c, now)
    gap_d = _safe(gap_continuation.display, db, c, now)
    brk_d = _safe(breakouts.display, db, c, now)
    pillars = {
        'apex': _apex_section(apex, symbol, direction, spot or entry, target)
                if not apex.get('_error') else _pillar_error('apex', apex),
        'tape': _tape_section(tape_d, symbol, direction)
                if not tape_d.get('_error') else _pillar_error('tape', tape_d),
        'gap': _gap_section(gap_d, symbol, direction)
               if not gap_d.get('_error') else _pillar_error('gap', gap_d),
        'breakout': _breakout_section(brk_d, symbol, direction)
                    if not brk_d.get('_error') else _pillar_error('breakout', brk_d),
        'exposure': _exposure_section(db, c, symbol),
    }
    for_code = {'supports': 1, 'neutral': 0, 'info': 0, 'no_data': 0, 'contradicts': -1}
    score = sum(for_code[p['alignment']] for p in pillars.values())
    counted = sum(1 for p in pillars.values() if p['alignment'] not in ('no_data', 'info'))

    record = {'ticker': symbol, 'direction': direction, 'entry': entry, 'target': target,
              'source': str(source or '')[:120], 'at': now, 'spot': spot,
              'pillars': pillars, 'evidence_score': score, 'pillars_counted': counted,
              'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
    db.append(c, 'pick_check', 'dashboard', symbol, now, record)
    return record


def recent(db, c, limit=50):
    return db.recent(c, 'pick_check', limit=limit)
