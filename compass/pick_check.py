"""Single-pick evidence check: stack one external pick against Compass evidence.

Evidence only — no alerts, no auto-admission, no trades. Nothing here is a
trading signal until prospective measurement says otherwise. Checks are
logged so analyst hit rates can be measured later.
"""
import time

from . import apex_magnet, tape_confirmed, gap_continuation, breakouts
from .instruments import FUTURES, future_root

LONG = {'long', 'bullish', 'call', 'calls', 'c'}
SHORT = {'short', 'bearish', 'put', 'puts', 'p'}

# detector key, :latest snapshot key, display label
ICT_DETECTORS = (
    ('golden_zone', 'ict_golden_zone:latest', 'Golden Zone + VWAP'),
    ('bos_fvg', 'ict_bos_fvg:latest', 'BOS + FVG'),
    ('bos_gz_vwap', 'ict_bos_gz_vwap:latest', 'BOS + GZ/VWAP'),
    ('turtle_soup', 'ict_turtle_soup:latest', 'Turtle Soup'),
    ('smt_divergence', 'ict_smt_divergence:latest', 'SMT Divergence'),
    ('aoi_zones', 'ict_aoi_zones:latest', 'AOI Zones'),
    ('continuation', 'ict_continuation:latest', 'Continuation'),
    ('morning_drive', 'morning_drive:latest', 'Morning-Drive Fade'),
    ('icc', 'icc:latest', 'ICC'),
    ('rumers_box', 'rumers_box:latest', 'Rumers Box'),
)

# A detector snapshot older than this is too stale to judge a live pick.
ICT_STALE_AFTER = 24 * 3600


def normalize_direction(raw):
    d = str(raw or '').strip().lower()
    if d in LONG:
        return 'long'
    if d in SHORT:
        return 'short'
    raise ValueError("direction must be long/short (call/put accepted)")


def _rows_for(rows, symbol):
    return [r for r in (rows or []) if str(r.get('symbol') or '').upper() == symbol]


def _futures_root(symbol):
    """Contract root for a futures pick symbol, or None for non-futures."""
    root = future_root(symbol or '')
    if root:
        return root
    bare = str(symbol or '').split('.')[0].strip().upper()
    return bare if bare in FUTURES else None


def _ict_row_for(rows, root):
    """Find the detector row covering `root`. SMT rows are keyed by pair."""
    for r in (rows or {}).values() if isinstance(rows, dict) else (rows or []):
        sym = str(r.get('symbol') or '')
        if _futures_root(sym) == root:
            return r
        pair = str(r.get('pair') or '')
        if pair and root in {_futures_root(p) for p in pair.split(':')}:
            return r
    return None


def _ict_verdict(det_key, label, latest_key, db, c, root, direction, now):
    """One detector's verdict on a futures pick.

    Returns (verdict, note) with verdict in
    supports/contradicts/neutral/no_data.
    """
    latest = db.get(c, latest_key, {}) or {}
    rows = latest.get('rows') or {}
    at = latest.get('at') or 0
    if not rows or now - at > ICT_STALE_AFTER:
        return 'no_data', 'detector has not run recently'
    row = _ict_row_for(rows, root)
    if row is None:
        return 'no_data', 'symbol not in detector universe'
    if row.get('excluded') == 'no_bars':
        return 'no_data', 'no bar data for symbol'
    if row.get('excluded'):
        return 'neutral', 'scanned, no setup (%s)' % row['excluded']
    sig_dir = str(row.get('direction') or '').lower()
    sig_ts = row.get('signal_ts') or row.get('at') or at
    age_h = (now - sig_ts) / 3600 if sig_ts else None
    age = '' if age_h is None else ' %.1fh ago' % age_h
    if sig_dir == direction:
        return 'supports', 'fired %s%s' % (sig_dir, age)
    if sig_dir in ('long', 'short'):
        return 'contradicts', 'fired %s%s' % (sig_dir, age)
    return 'neutral', 'signal without direction%s' % age


def _ict_section(db, c, symbol, direction, now):
    """Aggregate ICT-detector evidence for a futures pick.

    Reads each detector's precomputed :latest snapshot (no new scanning);
    never raises.
    """
    root = _futures_root(symbol)
    verdicts = {}
    for det_key, latest_key, label in ICT_DETECTORS:
        try:
            v, note = _ict_verdict(det_key, label, latest_key, db, c, root,
                                  direction, now)
        except Exception:  # noqa: BLE001 - one bad detector never breaks the check
            v, note = 'no_data', 'read error'
        verdicts[det_key] = {'label': label, 'verdict': v, 'note': note}
    n_sup = sum(1 for v in verdicts.values() if v['verdict'] == 'supports')
    n_con = sum(1 for v in verdicts.values() if v['verdict'] == 'contradicts')
    n_dat = sum(1 for v in verdicts.values() if v['verdict'] != 'no_data')
    if n_sup and not n_con:
        alignment, note = 'supports', \
            '%d detector(s) fired with the pick' % n_sup
    elif n_con and not n_sup:
        alignment, note = 'contradicts', \
            '%d detector(s) fired against the pick' % n_con
    elif n_sup and n_con:
        alignment, note = 'neutral', \
            'detectors split: %d with, %d against' % (n_sup, n_con)
    elif n_dat:
        alignment, note = 'neutral', 'detectors scanned, no setup fired'
    else:
        alignment, note = 'no_data', \
            'no ICT detector has usable data for this symbol'
    tb_verdict, tb_note = _trend_bias_note(db, c, root, direction, now)
    if tb_verdict:
        verdicts['trend_bias'] = tb_verdict
    if tb_note:
        note = (note + ' ' + tb_note) if note else tb_note
    return {'alignment': alignment, 'note': note, 'detail': verdicts,
            'basis': 'Precomputed ICT futures detector snapshots (latest scan '
                     'per method); a fired signal in the pick direction '
                     'supports, an opposite signal contradicts.'}


def _trend_bias_note(db, c, root, direction, now):
    """Daily-regime framing for a futures pick. Returns (verdict_dict, note).

    Never moves the score (verdict is 'info'); it only frames the trade as
    with-trend (runner framing) or counter-trend (scalp framing), per the
    3R rule's trend-bias component.
    """
    try:
        latest = db.get(c, 'trend_bias:latest', {}) or {}
        if now - (latest.get('at') or 0) > ICT_STALE_AFTER:
            return None, None
        row = _ict_row_for(latest.get('rows') or {}, root)
        if not row or row.get('excluded'):
            return None, None
        regime = str(row.get('regime') or '')
        if regime not in ('bullish', 'bearish'):
            return None, None
        with_trend = (regime == 'bullish' and direction == 'long') or \
                     (regime == 'bearish' and direction == 'short')
        framing = ('with-trend — runner framing'
                   if with_trend else
                   'counter-trend — scalp framing (take profits quickly)')
        note = '%s regime (close %s %d-day SMA); pick is %s' % (
            regime, 'above' if regime == 'bullish' else 'below',
            int(row.get('sma_len') or 50), framing)
        return {'label': 'Trend bias (daily SMA)', 'verdict': 'info',
                'note': note}, (None if with_trend else
                                'Counter-trend vs daily regime: scalp framing.')
    except Exception:  # noqa: BLE001 - regime context never breaks the check
        return None, None
def _apex_section(display, symbol, direction, spot, target):
    rows = _rows_for(display.get('rows'), symbol)
    above = sorted([r for r in rows if (r.get('magnet') or 0) > (spot or 0)],
                   key=lambda r: r['magnet'])
    below = sorted([r for r in rows if (r.get('magnet') or 0) < (spot or 0)],
                   key=lambda r: r['magnet'], reverse=True)
    detail = {'nearest_above': above[0] if above else None,
              'nearest_below': below[0] if below else None,
              'signal': rows[0].get('signal') if rows else None,
              'role': rows[0].get('role') if rows else None,
              'vs_flip': rows[0].get('vs_flip') if rows else None,
              'gamma_flip': rows[0].get('gamma_flip') if rows else None}
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
            'basis': 'Vendor magnet levels joined with minute bars; magnet drift from level first-seen history.'}


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


def check(db, c, now, ticker, direction, entry=None, target=None, source='',
          cfg=None):
    """Evaluate one analyst pick against Compass evidence.

    cfg is optional. When provided (and cfg.pick_backfill is not False),
    pillars that report no_data because the underlying data was never
    ingested get one bounded on-demand backfill attempt, then one
    re-evaluation. The backfill can never raise; the check always completes.
    """
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
    if _futures_root(symbol):
        pillars['ict'] = _ict_section(db, c, symbol, direction, now)
    backfilled = []
    if cfg is not None and getattr(cfg, 'pick_backfill', True):
        pillars, backfilled, bf_spot = _maybe_backfill(
            db, c, cfg, now, symbol, direction, spot or entry, target, pillars)
        if spot is None and bf_spot:
            spot = bf_spot
    # Fractional evidence scoring (2026-10-01): 'info' contributes +0.25 so
    # real-but-unconfirmed evidence (institutional flow, GEX data) separates
    # from true no-data. 'supports' stays +1, 'contradicts' stays -1.
    for_code = {'supports': 1, 'neutral': 0, 'info': 0.25, 'no_data': 0, 'contradicts': -1}
    score = sum(for_code[p['alignment']] for p in pillars.values())
    counted = sum(1 for p in pillars.values() if p['alignment'] not in ('no_data', 'info'))

    record = {'ticker': symbol, 'direction': direction, 'entry': entry, 'target': target,
              'source': str(source or '')[:120], 'at': now, 'spot': spot,
              'pillars': pillars, 'evidence_score': score, 'pillars_counted': counted,
              'backfilled': backfilled,
              'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
    db.append(c, 'pick_check', 'dashboard', symbol, now, record)
    return record


def _maybe_backfill(db, c, cfg, now, symbol, direction, spot, target, pillars):
    """One bounded on-demand backfill pass for no_data pillars. Never raises.

    Returns (pillars, backfilled_names, spot_or_None).
    """
    try:
        from . import pick_backfill as bf
        wants, reeval = set(), set()
        if pillars['apex']['alignment'] == 'no_data' and bf.apex_needs(db, c, symbol, now):
            wants.update(('apex', 'bars'))
            reeval.add('apex')
        if pillars['tape']['alignment'] == 'no_data' and bf.flow_needs(db, c, now):
            wants.add('flow')
            reeval.add('tape')
        if pillars['gap']['alignment'] == 'no_data' and bf.gap_needs(db, c, symbol, now):
            wants.add('bars')
            reeval.add('gap')
        if pillars['breakout']['alignment'] == 'no_data' and bf.breakout_needs(db, c, symbol):
            wants.add('bars')
            reeval.add('breakout')
        if not wants and pillars['tape']['alignment'] != 'no_data':
            return pillars, [], None
        result = bf.backfill(db, c, cfg, symbol, wants, now) if wants else \
            {'fetched': [], 'errors': {}}
        fetched = set(result.get('fetched') or [])
        sections, row_spot = bf.reevaluate(db, c, cfg, symbol, direction,
                                           spot, target, now, reeval)
        # The backfill marker means "this section rests on data fetched by
        # this pass". Sections recomputed from standing data (e.g. flow
        # context from an already-fresh feed) still update the pillar, but
        # are not marked as backfilled.
        sources = {'apex': 'apex', 'gap': 'minute_bars',
                   'breakout': 'daily_bars', 'tape': 'flow'}
        done = []
        for name, section in sections.items():
            pillars[name] = section
            if sources.get(name) in fetched:
                detail = section.get('detail')
                if isinstance(detail, dict):
                    detail['backfilled'] = True
                done.append(name)
        return pillars, sorted(done), row_spot
    except Exception:  # noqa: BLE001 - the check always completes
        return pillars, [], None


def recent(db, c, limit=50):
    return db.recent(c, 'pick_check', limit=limit)
