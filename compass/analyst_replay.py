"""Analyst replay harness: score dated analyst picks against as-of scanner evidence.

Answers: "would Compass's scanners have caught this analyst pick at entry time?"

Evidence-only and read-only by construction:
  - this module never calls db.append / db.put; it only reads via db.recent / db.get
  - it never emits alerts, admits trades, or opens simulated positions
  - every evidence lookup is bounded by pick_ts (end of the pick date, UTC):
    any evidence row with ts > pick_ts is excluded. Anti-lookahead is the point.

Usage:
    python -m compass.analyst_replay --picks picks.json
    python -m compass.analyst_replay --foxy ~/workspace/analyst-picks/foxy-fills-2026-09.json

Database: the harness takes a Store the same way the engine does. By default it
reads the DATABASE_URL environment variable (the same value the dashboard,
collector, and engine services use in production) and falls back to
sqlite:///compass.db for local use:
    DATABASE_URL='postgresql+psycopg://user:pass@host/db' python -m compass.analyst_replay --picks picks.json
    python -m compass.analyst_replay --picks picks.json --db-url sqlite:///compass.db
"""

import argparse
import json
import os
from datetime import datetime, timezone

from .store import Store

SESSION_S = 86400          # one session ~= one day; replay windows are session-counted
LOOKBACK_S = 5 * SESSION_S  # evidence must be within 5 sessions before the pick
APEX_AGREE_SIGNALS = ('tested_holding', 'approaching')
UP = {'long': 'up', 'short': 'down'}  # breakout/gap direction vocabulary


# --- pick loading -----------------------------------------------------------

def load_picks(path):
    """Load a generic pick list: [{ticker, direction, date, source, entry?, pnl?}].

    direction must be 'long' or 'short' (call/put are NOT accepted here; the
    pick checker normalizes those, this harness does not guess).
    date is 'YYYY-MM-DD'. pnl, when present, is the realized outcome in dollars.
    """
    with open(path) as f:
        raw = json.load(f)
    picks = []
    for r in raw:
        direction = (r.get('direction') or '').lower()
        if direction not in ('long', 'short'):
            raise ValueError("pick direction must be long/short: %r" % (r,))
        picks.append({
            'ticker': r['ticker'], 'direction': direction, 'date': r['date'],
            'source': r.get('source', 'unknown'), 'entry': r.get('entry'),
            'pnl': r.get('pnl'), 'status': r.get('status', 'open'),
            'replayable': bool(r.get('replayable', True)),
        })
    return picks


def load_foxy_fills(path):
    """Map the foxy fills JSON (option fills) onto replayable picks.

    Bought call -> long, Bought put -> short (both replayable). Sold-to-close
    fills (status 'closed') are kept for outcome stats only: they carry a pnl
    but have no known entry date, so they are marked replayable=False and
    skipped by the entry replay. Opening option sales (e.g. covered calls,
    side Sold with status still open) are non-directional and skipped entirely.
    """
    with open(path) as f:
        data = json.load(f)
    picks = []
    for fill in data.get('fills', []):
        contract = fill.get('contract') or ''
        is_call, is_put = 'Call' in contract, 'Put' in contract
        if not (is_call ^ is_put):
            continue  # not a plain directional option fill
        direction = 'long' if is_call else 'short'
        side = fill.get('side')
        status = fill.get('status')
        note = (fill.get('note') or '').lower()
        if side == 'Sold' and status != 'closed':
            continue  # opening sale (e.g. covered call): non-directional, skip
        picks.append({
            'ticker': fill.get('ticker'), 'direction': direction,
            'date': fill.get('date'), 'source': 'foxy',
            'entry': fill.get('price') if side == 'Bought' else None,
            'pnl': fill.get('pnl'), 'status': status or 'unknown',
            'replayable': side == 'Bought',
        })
    return [p for p in picks if p['ticker'] and p['date']]


# --- as-of evidence replay --------------------------------------------------

def end_of_day_ts(date_str):
    """pick_ts: end of the pick date, UTC. Evidence must be ts <= this."""
    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return dt.timestamp() + SESSION_S - 1


def _rows_asof(db, c, kind, symbol, pick_ts, lookback=LOOKBACK_S, limit=200):
    """Time-bounded read: rows strictly at or before pick_ts, within lookback.

    db.recent returns newest-first; the filter preserves that order.
    """
    rows = db.recent(c, kind, symbol, limit=limit)
    return [r for r in rows
            if r.get('ts') is not None
            and r['ts'] <= pick_ts
            and r['ts'] >= pick_ts - lookback]


def _payload(row):
    return row.get('payload') or {}


def _replay_apex(db, c, pick, pick_ts):
    """Latest apex_magnet_signal at/below pick_ts, fresh within 5 sessions.

    Long agrees when the magnet is a support (below spot) that price tested
    and held or is approaching; short mirrors with resistance above spot.
    """
    rows = _rows_asof(db, c, 'apex_magnet_signal', pick['ticker'], pick_ts)
    if not rows:
        return {'state': 'no_data',
                'detail': {'reason': 'no_apex_signal_within_5_sessions'}}
    p = _payload(rows[0])
    want_role = 'support' if pick['direction'] == 'long' else 'resistance'
    detail = {'signal': p.get('signal'), 'role': p.get('role'),
              'magnet': p.get('magnet'), 'spot': p.get('spot'),
              'distance_pct': p.get('distance_pct'), 'vs_flip': p.get('vs_flip'),
              'signal_ts': rows[0]['ts']}
    if p.get('role') != want_role:
        detail['reason'] = 'role_mismatch'
        return {'state': 'disagrees', 'detail': detail}
    if p.get('signal') not in APEX_AGREE_SIGNALS:
        detail['reason'] = 'signal_not_supportive'
        return {'state': 'disagrees', 'detail': detail}
    return {'state': 'agrees', 'detail': detail}


def _replay_tape(db, c, pick, pick_ts):
    """Same-direction confirmed institutional flow within 5 sessions."""
    rows = _rows_asof(db, c, 'tape_confirmation', pick['ticker'], pick_ts)
    same = [r for r in rows if _payload(r).get('side') == pick['direction']]
    if same:
        scores = [(_payload(r).get('max_score') or 0) for r in same]
        return {'state': 'agrees',
                'detail': {'confirmations': len(same),
                           'latest_ts': same[0]['ts'], 'max_score': max(scores)}}
    if rows:
        return {'state': 'disagrees',
                'detail': {'reason': 'only_opposing_flow_confirmed',
                           'confirmations': len(rows)}}
    return {'state': 'no_data',
            'detail': {'reason': 'no_tape_confirmation_within_5_sessions'}}


def _replay_breakouts(db, c, pick, pick_ts):
    """Fresh range break or triangle fire in the pick direction within lookback.

    Note: forming (not yet fired) coils persist only in live 'triangle_state:'
    puts, not in a ts-bounded series, so the replay covers fired events only.
    """
    rows = _rows_asof(db, c, 'breakout_event', pick['ticker'], pick_ts)
    if not rows:
        return {'state': 'no_data',
                'detail': {'reason': 'no_breakout_event_within_5_sessions'}}
    want = UP[pick['direction']]
    hits = [r for r in rows if _payload(r).get('direction') == want]
    if hits:
        p = _payload(hits[0])
        return {'state': 'agrees',
                'detail': {'pattern': p.get('pattern'),
                           'direction': p.get('direction'),
                           'confirmation': p.get('confirmation'),
                           'event_ts': hits[0]['ts']}}
    return {'state': 'disagrees',
            'detail': {'reason': 'breakouts_only_opposite_direction',
                       'events': len(rows)}}


def _replay_gap(db, c, pick, pick_ts):
    """Qualified gap-continuation setup on/before the pick date, aligned."""
    rows = _rows_asof(db, c, 'gap_continuation', pick['ticker'], pick_ts)
    if not rows:
        return {'state': 'no_data',
                'detail': {'reason': 'no_gap_setup_within_5_sessions'}}
    want = UP[pick['direction']]
    hits = [r for r in rows if _payload(r).get('direction') == want]
    if hits:
        p = _payload(hits[0])
        return {'state': 'agrees',
                'detail': {'direction': p.get('direction'),
                           'gap_pct': p.get('gap_pct'), 'day': p.get('day'),
                           'pillars': p.get('pillars'), 'event_ts': hits[0]['ts']}}
    return {'state': 'disagrees',
            'detail': {'reason': 'gap_setups_only_opposite_direction',
                       'setups': len(rows)}}


PILLARS = (('apex', _replay_apex), ('tape', _replay_tape),
           ('breakouts', _replay_breakouts), ('gap', _replay_gap))


def outcome(pick):
    """'win' | 'loss' | 'unknown'. Never invents an outcome: no pnl -> unknown."""
    pnl = pick.get('pnl')
    if pnl is None:
        return 'unknown'
    return 'win' if pnl > 0 else 'loss'


def replay_pick(db, c, pick):
    """Score one pick against as-of evidence. Pure read; returns a result dict."""
    pick_ts = end_of_day_ts(pick['date'])
    pillars = {name: fn(db, c, pick, pick_ts) for name, fn in PILLARS}
    agreeing = sum(1 for p in pillars.values() if p['state'] == 'agrees')
    return {'pick': pick, 'pick_ts': pick_ts, 'pillars': pillars,
            'agreeing': agreeing, 'outcome': outcome(pick)}


def replay_picks(db, c, picks):
    """Replay every replayable pick. Non-replayable records (e.g. closes with
    no known entry date) are skipped here; keep them for outcome stats."""
    return [replay_pick(db, c, p) for p in picks if p.get('replayable', True)]


# --- scoring ----------------------------------------------------------------

def summarize(results, all_picks):
    """Aggregate replay results. Picks without a known pnl stay 'unknown'."""
    replayed = results
    n = len(replayed)
    dist = {}
    by_outcome = {'win': [], 'loss': [], 'unknown': []}
    for r in replayed:
        dist[r['agreeing']] = dist.get(r['agreeing'], 0) + 1
        by_outcome[r['outcome']].append(r['agreeing'])
    mean = (sum(r['agreeing'] for r in replayed) / n) if n else None
    closed = [p for p in all_picks if not p.get('replayable', True)]
    closed_pnl = [p for p in closed if p.get('pnl') is not None]
    return {
        'n_replayed': n,
        'mean_agreeing': mean,
        'agreement_rate': (mean / len(PILLARS)) if mean is not None else None,
        'distribution': dist,
        'by_outcome': {k: {'n': len(v), 'mean_agreeing':
                           (sum(v) / len(v)) if v else None}
                       for k, v in by_outcome.items()},
        'closed_without_entry_replay': {
            'n': len(closed),
            'with_pnl': len(closed_pnl),
            'wins': sum(1 for p in closed_pnl if p['pnl'] > 0),
            'losses': sum(1 for p in closed_pnl if p['pnl'] <= 0),
        },
    }


# --- reporting ---------------------------------------------------------------

def _fmt(v):
    return '—' if v is None else str(v)


def print_report(results, summary):
    print('Analyst replay: %d picks, %d entry-replayed'
          % (len(results), summary['n_replayed']))
    print('Evidence strictly ts <= end of pick date (UTC); later rows excluded.')
    print()
    hdr = '%-6s %-5s %-10s %-9s %-9s %-9s %-9s %-5s %-7s' % (
        'TICKER', 'DIR', 'DATE', 'APEX', 'TAPE', 'BREAKOUT', 'GAP', 'AGREE',
        'OUTCOME')
    print(hdr)
    for r in results:
        p = r['pick']
        print('%-6s %-5s %-10s %-9s %-9s %-9s %-9s %d/%d   %-7s' % (
            p['ticker'], p['direction'], p['date'],
            r['pillars']['apex']['state'], r['pillars']['tape']['state'],
            r['pillars']['breakouts']['state'], r['pillars']['gap']['state'],
            r['agreeing'], len(PILLARS), r['outcome']))
    print()
    print('Summary')
    print('  replayed: %d' % summary['n_replayed'])
    if summary['mean_agreeing'] is not None:
        print('  mean agreeing pillars: %.2f/%d (%.0f%%)' % (
            summary['mean_agreeing'], len(PILLARS),
            100 * summary['agreement_rate']))
    dist = summary['distribution']
    print('  distribution: ' + ' '.join('%d:%d' % (k, dist.get(k, 0))
                                        for k in range(len(PILLARS) + 1)))
    bo = summary['by_outcome']
    print('  by outcome (known pnl only): ' +
          'win n=%d mean=%s | loss n=%d mean=%s | unknown n=%d' % (
              bo['win']['n'], _fmt(bo['win']['mean_agreeing']),
              bo['loss']['n'], _fmt(bo['loss']['mean_agreeing']),
              bo['unknown']['n']))
    cw = summary['closed_without_entry_replay']
    print('  closed fills without entry replay: n=%d (%d with pnl: %d wins, %d losses)'
          % (cw['n'], cw['with_pnl'], cw['wins'], cw['losses']))


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Replay dated analyst picks against as-of Compass scanner '
                    'evidence (read-only, evidence-only).')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--picks', help='JSON list of {ticker, direction, date, ...}')
    src.add_argument('--foxy', help='foxy fills JSON (mapped to picks)')
    ap.add_argument('--db-url', default=None,
                    help='Store URL; defaults to $DATABASE_URL then sqlite:///compass.db')
    args = ap.parse_args(argv)

    picks = load_foxy_fills(args.foxy) if args.foxy else load_picks(args.picks)
    db_url = args.db_url or os.environ.get('DATABASE_URL', 'sqlite:///compass.db')
    db = Store(db_url)
    db.initialize()
    try:
        with db.tx() as c:  # read-only transaction; this module never writes
            results = replay_picks(db, c, picks)
    finally:
        db.engine.dispose()
    print_report(results, summarize(results, picks))


if __name__ == '__main__':
    main()
