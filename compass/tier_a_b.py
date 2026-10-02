"""ICT confluence tiering (Phase 1): Tier A/B from detector snapshots.

Evidence only. This module never alerts, never trades, and never re-runs
detection -- it only reads each detector's ':latest' snapshot defensively
and groups event rows by (symbol, direction) whose signal_ts fall within
ICT_TIER_WINDOW_S (default 1800s) of each other (chain-link).

Tier A = 3+ distinct concepts, Tier B = 2, anything below is kept in
`unranked`, never dropped.

Snapshot contract (shared with all ICT detectors):
    '<key>:latest' -> {'at': now, 'rows': {symbol: [row, ...]}}
'rows' maps symbol -> LIST of event-schema rows (list even for one row).
tier_a_b ALSO tolerates a bare dict row per symbol (wraps it in a list)
for older/foreign snapshots, but new detectors must write the list shape.

session_liquidity and htf_levels contribute nothing (levels only);
aoi_zones contribute zones with a bias, converted to directional rows.
"""
from .market import number
from .ict_common import ict_bars

TIER_WINDOW_S = 1800
TIER_A_MIN = 3
TIER_B_MIN = 2
FRESH_ATR_MULT = 1.0   # entry within 1.0*ATR of a level -> fresh shift
SCAN_THROTTLE = 120

# (snapshot key, default concept label, row-consumption mode)
# mode 'levels' -> levels only, contributes nothing to tiering.
# mode 'zones'  -> aoi zone rows, converted via bias to directional rows.
DETECTOR_SOURCES = (
    ('ict_turtle_soup:latest', 'turtle_soup', 'rows'),
    ('ict_smt_divergence:latest', 'smt_divergence', 'rows'),
    ('ict_session_liquidity:latest', 'session_liquidity', 'levels'),
    ('ict_htf_levels:latest', 'htf_levels', 'levels'),
    ('ict_aoi_zones:latest', 'aoi', 'zones'),
    ('ict_continuation:latest', 'continuation', 'rows'),
)


def _bars_from_window(window):
    """Normalize bar_window rows ([ts,o,h,l,c,v,...]) to dicts."""
    out = []
    for row in window or []:
        try:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        except (IndexError, TypeError):
            continue
        if None in (ts, o, h, l, c):
            continue
        out.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c})
    return out


def _bars_from_recent(rows):
    """Normalize db.recent('bar') dicts to the same shape."""
    out = []
    for r in rows or []:
        p = r.get('payload') or {}
        if None in (r.get('ts'), p.get('o'), p.get('h'), p.get('l'), p.get('c')):
            continue
        out.append({'ts': r['ts'], 'o': p['o'], 'h': p['h'], 'l': p['l'], 'c': p['c']})
    return sorted(out, key=lambda b: b['ts'])


def _atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        trs.append(max(b['h'] - b['l'], abs(b['h'] - p['c']), abs(b['l'] - p['c'])))
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else None


def _as_list(val):
    """Accept {symbol: [row, ...]} (the contract) or {symbol: row}.

    Also unwraps continuation's {symbol: {'signals': [...]}} wrapper.
    """
    if val is None:
        return []
    if isinstance(val, list):
        return [r for r in val if isinstance(r, dict)]
    if isinstance(val, dict):
        sigs = val.get('signals')
        if isinstance(sigs, list):
            return [r for r in sigs if isinstance(r, dict)]
        return [val]
    return []


def _normalize(symbol, row, default_concept, now):
    """Event-schema row -> tiering candidate. None when unusable."""
    if not isinstance(row, dict):
        return None
    direction = row.get('direction')
    if direction not in ('long', 'short'):
        return None
    ts = number(row.get('signal_ts'))
    if ts is None:
        ts = number(row.get('at'))
    if ts is None:
        ts = now
    out = dict(row)
    out['symbol'] = symbol
    out['concept'] = row.get('concept') or default_concept
    out['direction'] = direction
    out['signal_ts'] = ts
    return out


def _aoi_to_row(symbol, row, concept, now):
    """AOI zone rows carry a bias, not a direction; convert to a
    directional candidate at the zone midpoint."""
    if not isinstance(row, dict):
        return None
    bias = row.get('bias')
    if bias not in ('long', 'short'):
        return None
    top, bottom = number(row.get('top')), number(row.get('bottom'))
    mid = None
    if top is not None and bottom is not None:
        mid = (top + bottom) / 2
    elif top is not None:
        mid = top
    elif bottom is not None:
        mid = bottom
    ts = number(row.get('formed_ts'))
    if ts is None:
        ts = number(row.get('at'))
    if ts is None:
        ts = now
    return {'symbol': symbol, 'concept': row.get('concept') or concept,
            'direction': bias, 'signal_ts': ts,
            'entry': mid, 'stop': None, 'target': None,
            'risk_pts': None, 'reward_pts': None, 'rr': None,
            'level_price': mid, 'level_kind': row.get('zone_kind'),
            'source': 'aoi_zone', 'at': now}


def _zone_rows(symbol, val):
    """Unwrap an AOI snapshot value into zone-shaped dicts.

    aoi_zones stores rows[symbol] = {'zones': [...], 'order_block': {...}, ...}.
    Accepts a bare list of zone dicts too.
    """
    if isinstance(val, dict):
        zones = [z for z in (val.get('zones') or []) if isinstance(z, dict)]
        ob = val.get('order_block')
        if isinstance(ob, dict):
            zones.append(ob)
        return zones
    if isinstance(val, list):
        return [z for z in val if isinstance(z, dict)]
    return []


def _collect(db, c, now):
    """Gather tiering candidates from every detector snapshot.

    Each snapshot may be absent or malformed; it contributes nothing and
    never raises.
    """
    rows = []
    for key, concept, mode in DETECTOR_SOURCES:
        if mode == 'levels':
            continue  # levels only -- contributes nothing to tiering
        try:
            snap = db.get(c, key, {}) or {}
        except Exception:
            continue
        data = snap.get('rows') or {}
        if not isinstance(data, dict):
            continue
        for symbol, val in data.items():
            if mode == 'zones':
                for row in _zone_rows(symbol, val):
                    r = _aoi_to_row(symbol, row, concept, now)
                    if r is not None:
                        rows.append(r)
                continue
            for row in _as_list(val):
                if not isinstance(row, dict):
                    continue
                # SMT rows are keyed by pair; tier on the divergent leg.
                sym = row.get('divergent_leg') or symbol \
                    if concept == 'smt_divergence' else symbol
                r = _normalize(sym, row, concept, now)
                if r is not None:
                    rows.append(r)
    return rows


def group_confluence(rows, window_s=TIER_WINDOW_S):
    """Group rows by (symbol, direction); a row joins the current group
    when its signal_ts is within window_s of the group's latest row
    (chain-link). Returns [{symbol, direction, rows, concepts, ts_min,
    ts_max}].
    """
    normed = [r for r in rows
              if isinstance(r, dict) and r.get('symbol') and r.get('direction')]
    normed.sort(key=lambda r: (str(r['symbol']), str(r['direction']),
                               number(r.get('signal_ts')) or 0))
    groups = []
    for r in normed:
        key = (r['symbol'], r['direction'])
        ts = number(r.get('signal_ts')) or 0
        g = groups[-1] if groups and groups[-1]['key'] == key else None
        if g is None or ts - g['ts_max'] > window_s:
            groups.append({'key': key, 'symbol': r['symbol'],
                           'direction': r['direction'], 'rows': [r],
                           'ts_min': ts, 'ts_max': ts})
        else:
            g['rows'].append(r)
            g['ts_min'] = min(g['ts_min'], ts)
            g['ts_max'] = max(g['ts_max'], ts)
    for g in groups:
        g['concepts'] = sorted({x['concept'] for x in g['rows']
                                if x.get('concept')})
    return groups


def _tier_for(n_concepts, a_min=TIER_A_MIN, b_min=TIER_B_MIN):
    if n_concepts >= a_min:
        return 'A'
    if n_concepts >= b_min:
        return 'B'
    return None


def _aggregate_entry_stop_target(group):
    entries = [number(r.get('entry')) for r in group['rows']]
    entries = [e for e in entries if e is not None]
    entry = sum(entries) / len(entries) if entries else None
    stops = [number(r.get('stop')) for r in group['rows']]
    stops = [s for s in stops if s is not None]
    tgts = [number(r.get('target')) for r in group['rows']]
    tgts = [t for t in tgts if t is not None]
    if group['direction'] == 'long':
        stop = min(stops) if stops else None      # conservative extreme
        target = min(tgts) if tgts else None      # nearest
    else:
        stop = max(stops) if stops else None
        target = max(tgts) if tgts else None
    return entry, stop, target


def _shift_for(db, c, group, entry, fresh_mult=FRESH_ATR_MULT):
    """Copy 'shift' from continuation rows when present; otherwise compute
    from entry-vs-nearest-level distance in ATR (fresh <= fresh_mult)."""
    for r in group['rows']:
        if r.get('concept') == 'continuation' and r.get('shift') in ('fresh', 'extended'):
            return r['shift']
    levels = []
    try:
        for key in ('ict_session_liquidity:latest', 'ict_htf_levels:latest'):
            snap = db.get(c, key, {}) or {}
            for row in snap.get('levels') or []:
                if not isinstance(row, dict) or row.get('symbol') != group['symbol']:
                    continue
                for k in ('price', 'high', 'low'):
                    px = number(row.get(k))
                    if px is not None:
                        levels.append(px)
    except Exception:
        levels = []
    a = None
    try:
        bars = ict_bars(db, c, group['symbol'], limit=60)
        if not bars:
            bars = _bars_from_recent(db.recent(c, 'bar', group['symbol'], limit=60))
        a = _atr(bars, 14)
    except Exception:
        a = None
    if entry is None or not levels or not a or a <= 0:
        return 'unknown'
    dist = min(abs(entry - px) for px in levels) / a
    return 'fresh' if dist <= fresh_mult else 'extended'


def scan(db, c, cfg, now):
    """Build Tier A/B from detector snapshots."""
    if not getattr(cfg, 'ict_tier_a_b', False):
        return {'ran': False, 'reason': 'disabled'}
    if now - (number(db.get(c, 'ict_tier_a_b:scanned_at', 0)) or 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    window_s = number(getattr(cfg, 'ict_tier_window_s', TIER_WINDOW_S)) or TIER_WINDOW_S
    a_min = int(getattr(cfg, 'ict_tier_a_min', TIER_A_MIN) or TIER_A_MIN)
    b_min = int(getattr(cfg, 'ict_tier_b_min', TIER_B_MIN) or TIER_B_MIN)
    fresh_mult = number(getattr(cfg, 'ict_fresh_atr_mult', FRESH_ATR_MULT)) or FRESH_ATR_MULT
    rows = _collect(db, c, now)
    groups = group_confluence(rows, window_s)
    tiers = {'A': [], 'B': []}
    unranked = []
    for g in groups:
        tier = _tier_for(len(g['concepts']), a_min, b_min)
        entry, stop, target = _aggregate_entry_stop_target(g)
        out = {'symbol': g['symbol'], 'direction': g['direction'],
               'tier': tier, 'concepts': g['concepts'],
               'n_concepts': len(g['concepts']),
               'shift': _shift_for(db, c, g, entry, fresh_mult),
               'entry': entry, 'stop': stop, 'target': target,
               'ts_min': g['ts_min'], 'ts_max': g['ts_max'],
               'component_rows': g['rows'], 'at': now}
        if tier:
            tiers[tier].append(out)
        else:
            unranked.append(out)
    db.put(c, 'ict_tier_a_b:latest',
           {'at': now, 'tiers': tiers, 'unranked': unranked,
            'n_groups': len(groups), 'n_rows': len(rows)})
    db.put(c, 'ict_tier_a_b:scanned_at', now)
    return {'ran': True, 'tier_a': len(tiers['A']), 'tier_b': len(tiers['B']),
            'unranked': len(unranked), 'n_groups': len(groups)}


def display(db, c, now, limit=200):
    """Dashboard payload: Tier A, Tier B, and the unranked remainder."""
    latest = db.get(c, 'ict_tier_a_b:latest', {}) or {}
    tiers = latest.get('tiers') or {}
    return {'asof': latest.get('at'),
            'n_groups': latest.get('n_groups'),
            'tier_a': (tiers.get('A') or [])[:limit],
            'tier_b': (tiers.get('B') or [])[:limit],
            'unranked': (latest.get('unranked') or [])[:limit],
            'note': 'Research evidence only. No auto-admission, no alerts, no trades.'}
