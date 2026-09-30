"""Daily status page aggregation: one trading day across every section.

Read-only helper for the dashboard Daily tab. Combines the paper-trade
summary (0DTE options, futures) with per-section daily counts: scanner
signals, the day-trading board, alerts, pick checks, and the Smoothers
week glance.
"""
from collections import Counter
from datetime import datetime, timedelta

from .market import CT
from . import summary as summary_mod


def _day_bounds(day):
    start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=CT).timestamp()
    nxt = (datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)).replace(tzinfo=CT)
    return start, nxt.timestamp()


def build(db, c, cfg, now, day=None):
    if day is None:
        day = datetime.fromtimestamp(now, CT).strftime('%Y-%m-%d')
    datetime.strptime(day, '%Y-%m-%d')  # ValueError -> 422 on bad input
    start, end = _day_bounds(day)
    noon = start + 12 * 3600

    paper = summary_mod.build(db, c, noon)

    # summary.build keeps every currently open trade regardless of risk_day;
    # for a historical day that would leak today's open positions in.
    today = datetime.fromtimestamp(now, CT).strftime('%Y-%m-%d')
    if day != today:
        from collections import defaultdict as _dd
        for bucket in paper.values():
            if not isinstance(bucket, dict):
                continue
            trades = [t for t in bucket.get('trades', [])
                      if t.get('status') != 'open']
            bucket['trades'] = trades
            bucket.update(summary_mod._rollup(trades))
        fut = paper.get('futures')
        if isinstance(fut, dict) and 'strategies' in fut:
            per = _dd(lambda: {'taken': 0, 'wins': 0, 'losses': 0, 'open': 0,
                               'realized': 0.0, 'long': 0, 'short': 0})
            for r in fut['trades']:
                s = per[r['strategy'] or 'unknown']
                s['taken'] += 1
                s['realized'] += r['pnl'] or 0
                if r['result'] == 'win':
                    s['wins'] += 1
                elif r['result'] == 'loss':
                    s['losses'] += 1
                if r['side'] == 'long':
                    s['long'] += 1
                elif r['side'] == 'short':
                    s['short'] += 1
            by_tag = {s['strategy']: s for s in fut['strategies']}
            fut['strategies'] = [
                {**by_tag.get(tag, {}), 'strategy': tag, **vals,
                 'realized': round(vals['realized'], 2)}
                for tag, vals in per.items()]
            fut['strategies'].sort(key=lambda s: s['realized'])

    # --- evidence scanners ---
    apex = [e for e in db.recent(c, 'apex_magnet_signal', limit=500, since=start)
            if e['ts'] < end]
    apex_states = Counter((e.get('payload') or {}).get('signal', '?') for e in apex)
    broke = [{'symbol': e['symbol'], 'ts': e['ts'],
              'spot': (e.get('payload') or {}).get('spot'),
              'level': (e.get('payload') or {}).get('magnet')}
             for e in apex
             if (e.get('payload') or {}).get('signal') == 'broke_through'][:10]

    tape = [e for e in db.recent(c, 'tape_confirmation', limit=500, since=start)
            if e['ts'] < end]

    gaps = [s for _k, s in db.prefix(c, 'gap_cont:').items()
            if isinstance(s, dict) and s.get('day') == day and s.get('qualified')]
    gaps.sort(key=lambda s: -(abs(s.get('gap_pct') or 0)))

    breaks = [e for _k, e in db.prefix(c, 'breakout:').items()
              if isinstance(e, dict) and e.get('day') == day
              and e.get('status') in ('fresh', 'firing')]

    # --- morning board ---
    board = db.get(c, 'board:' + day, {}) or {}
    board_rows = board.get('rows') or []
    board_rows = sorted(board_rows, key=lambda r: -((r.get('total') or 0)))

    # --- alerts ---
    alerts = [e for e in db.recent(c, 'alert', limit=1000, since=start)
              if e['ts'] < end]
    alert_by = Counter(
        (e.get('payload') or {}).get('strategy')
        or (e.get('payload') or {}).get('rule') or '?'
        for e in alerts)

    # --- pick checks ---
    checks = [e for e in db.recent(c, 'pick_check', limit=200, since=start)
              if e['ts'] < end]

    # --- smoothers week containing this day ---
    d = datetime.strptime(day, '%Y-%m-%d').date()
    week = (d - timedelta(days=d.weekday())).isoformat()
    smoothers = {'week': week, 'featured': [], 'total': 0, 'available': False}
    try:
        from . import native_reports
        rep = native_reports.report(db, c, now, 100, '', '', '', week)
        srows = (rep.get('smoothers') or {}).get('rows') or []
        smoothers['available'] = True
        smoothers['total'] = len(srows)
        smoothers['featured'] = [
            {'ticker': r['native'].get('ticker'),
             'direction': r['native'].get('direction'),
             'status': r['native'].get('status')}
            for r in srows if r['native'].get('is_featured')]
    except Exception:
        pass

    return {
        'day': day, 'asof': now,
        'paper': paper,
        'scanners': {
            'apex_signals': len(apex),
            'apex_by_state': dict(apex_states),
            'apex_broke_through': broke,
            'tape_confirmations': len(tape),
            'tape_symbols': sorted({e.get('symbol') for e in tape if e.get('symbol')})[:10],
            'gaps_qualified': len(gaps),
            'gaps': [{'symbol': g.get('symbol'),
                      'gap_pct': round((g.get('gap_pct') or 0) * 100, 2),
                      'direction': g.get('direction')}
                     for g in gaps[:10]],
            'breakouts_fresh': len(breaks),
            'breakouts': [{'symbol': b.get('symbol'), 'pattern': b.get('pattern'),
                           'direction': b.get('direction')}
                          for b in breaks[:10]],
        },
        'board': {
            'built': bool(board.get('built_at')),
            'frozen': bool(board.get('frozen')),
            'universe': board.get('universe'),
            'rows': len(board_rows),
            'top': [{'symbol': r.get('symbol'),
                     'score': round(r.get('total') or 0, 1),
                     'day_pct': (None if r.get('day_pct') is None
                                 else round(r['day_pct'] * 100, 2))}
                    for r in board_rows[:8]],
        },
        'alerts': {'sent': len(alerts), 'by_strategy': dict(alert_by)},
        'pick_checks': {'count': len(checks),
                        'tickers': sorted({e.get('symbol') for e in checks
                                           if e.get('symbol')})},
        'smoothers': smoothers,
    }
