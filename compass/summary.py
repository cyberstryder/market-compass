"""Summary-page aggregation: what we took, what we skipped, per bucket.

Read-only helper for the dashboard Summary tab. Buckets match the four
summary sections: 0DTE options, intraday stock setups, futures paper
strategies. Smoothers keeps its existing page; the summary only points
at it with a compact glance rendered client-side.
"""
from collections import defaultdict

from sqlalchemy import func, select

from .daily_results import events
from .futures import futures_session, risk_day


def _result(t):
    if t.get('status') != 'closed':
        return 'open'
    pnl = t.get('pnl') or 0
    if pnl > 0:
        return 'win'
    if pnl < 0:
        return 'loss'
    return 'breakeven'


def _trade_row(t):
    return {
        'symbol': t.get('symbol'),
        'side': t.get('side'),
        'strategy': t.get('strategy'),
        'qty': t.get('qty'),
        'entry': t.get('entry'),
        'exit': t.get('exit'),
        'pnl': t.get('pnl'),
        'status': t.get('status'),
        'result': _result(t),
        'exit_reason': t.get('exit_reason'),
        'entered_at': t.get('entered_at'),
        'exited_at': t.get('exited_at'),
    }


def _bucket(t):
    asset = t.get('asset')
    if asset == 'future':
        return 'futures'
    if asset == 'option':
        return 'zero_dte'
    if asset == 'stock' and t.get('track') == 'intraday':
        return 'intraday'
    return None


def _skip_bucket(strategy, track):
    if (strategy or '').startswith('0dte'):
        return 'zero_dte'
    if track == 'intraday':
        return 'intraday'
    return None


def _rollup(rows):
    return {
        'taken': len(rows),
        'wins': sum(1 for r in rows if r['result'] == 'win'),
        'losses': sum(1 for r in rows if r['result'] == 'loss'),
        'open': sum(1 for r in rows if r['status'] == 'open'),
        'net_pnl': round(sum(r['pnl'] or 0 for r in rows
                             if r['status'] == 'closed'), 2),
    }


def build(db, c, now):
    """Aggregate today's paper activity for the Summary tab."""
    day = risk_day(now)
    session = futures_session(now)
    taken = {'zero_dte': [], 'intraday': [], 'futures': []}
    for key, t in db.prefix(c, 'trade:').items():
        if not isinstance(t, dict):
            continue
        if t.get('risk_day') != day and t.get('status') != 'open':
            continue
        b = _bucket(t)
        if b:
            taken[b].append(_trade_row(t))
    for rows in taken.values():
        rows.sort(key=lambda r: r['entered_at'] or 0, reverse=True)

    p = events.c.payload
    strat = p['strategy'].as_string()
    track = p['track'].as_string()
    reason = p['reason'].as_string()
    q = (select(events.c.symbol, strat, track, reason, func.count(),
               func.max(events.c.ts))
         .where(events.c.ts >= session['open'], events.c.ts < now,
                events.c.source == 'engine',
                events.c.kind.in_(('alert', 'paper_decision')),
                p['status'].as_string().in_(('skipped', 'options_skipped')))
         .group_by(events.c.symbol, strat, track, reason))
    skipped = {'zero_dte': [], 'intraday': []}
    for symbol, s, tr, r, n, last in c.execute(q):
        b = _skip_bucket(s, tr)
        if b in skipped:
            skipped[b].append({'symbol': symbol, 'strategy': s,
                               'reason': r or 'No reason recorded',
                               'count': n, 'last_at': last})
    for rows in skipped.values():
        rows.sort(key=lambda r: r['last_at'] or 0, reverse=True)

    from .ict_paper import attribution
    attr = attribution(db, c) or {}
    per_strategy = defaultdict(lambda: {'taken': 0, 'wins': 0, 'losses': 0,
                                        'open': 0, 'realized': 0.0,
                                        'long': 0, 'short': 0})
    for r in taken['futures']:
        s = per_strategy[r['strategy'] or 'unknown']
        s['taken'] += 1
        if r['status'] == 'open':
            s['open'] += 1
        else:
            s['realized'] += r['pnl'] or 0
            if r['result'] == 'win':
                s['wins'] += 1
            elif r['result'] == 'loss':
                s['losses'] += 1
        if r['side'] == 'long':
            s['long'] += 1
        elif r['side'] == 'short':
            s['short'] += 1
    strategies = []
    for tag_, s in per_strategy.items():
        a = attr.get(tag_, {})
        strategies.append({'strategy': tag_, **s,
                           'realized': round(s['realized'], 2),
                           'all_time_realized': round(a.get('realized', 0.0), 2),
                           'all_time_trades': a.get('trades', 0)})
    strategies.sort(key=lambda s: s['realized'])

    return {
        'day': day,
        'asof': now,
        'zero_dte': {**_rollup(taken['zero_dte']),
                     'trades': taken['zero_dte'],
                     'skipped': skipped['zero_dte']},
        'intraday': {**_rollup(taken['intraday']),
                     'trades': taken['intraday'],
                     'skipped': skipped['intraday']},
        'futures': {**_rollup(taken['futures']),
                    'trades': taken['futures'],
                    'strategies': strategies},
    }
