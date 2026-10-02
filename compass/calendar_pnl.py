"""Monthly P&L calendar: realized paper P&L per Chicago day per strategy bucket.

A trade lands on the Chicago calendar day of its exit (exited_at, falling
back to closed_at, then entered_at). Only closed trades with a recorded P&L
count; open positions are excluded. Days before cfg.dashboard_cutoff are
omitted — the calendar reflects the current regime, not deleted history.
"""
from collections import defaultdict
from datetime import datetime, timezone

from .market import CT, number

BUCKETS = [
    ('futures', 'Futures'),
    ('0dte', '0DTE'),
    ('trap-spread', 'Trap Spreads'),
    ('smoothers', 'Smoothers'),
    ('swings', 'Swings'),
    ('other', 'Other'),
]
BUCKET_IDS = [b[0] for b in BUCKETS]


def bucket_for(t):
    """Strategy bucket for one trade record."""
    asset = t.get('asset')
    strategy = str(t.get('strategy') or '')
    if asset == 'future':
        return 'futures'
    if strategy == 'trap-spread':
        return 'trap-spread'
    if strategy.startswith('0dte'):
        return '0dte'
    if 'smoother' in strategy:
        return 'smoothers'
    if strategy.startswith('swing'):
        return 'swings'
    return 'other'


def exit_day(t):
    """Chicago calendar day (YYYY-MM-DD) the trade's P&L was realized."""
    ts = t.get('exited_at') or t.get('closed_at') or t.get('entered_at')
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(number(ts), tz=timezone.utc) \
            .astimezone(CT).date().isoformat()
    except Exception:
        return None


def _blank_stats():
    return {'pnl': 0.0, 'trades': 0, 'wins': 0}


def month_calendar(db, c, month=None, cutoff_day='2026-09-30'):
    """Realized P&L grouped by day and bucket for one month.

    month: 'YYYY-MM'; defaults to the current Chicago month.
    Returns {month, cutoff_day, buckets, days, month_total}.
    """
    if not month:
        month = datetime.now(CT).strftime('%Y-%m')
    days = defaultdict(lambda: defaultdict(_blank_stats))
    totals = defaultdict(_blank_stats)

    for key, t in db.prefix(c, 'trade:').items():
        if not isinstance(t, dict) or t.get('status') != 'closed':
            continue
        pnl = t.get('pnl')
        if pnl is None:
            continue
        day = exit_day(t)
        if not day or not day.startswith(month) or day < cutoff_day:
            continue
        b = bucket_for(t)
        pnl = number(pnl) or 0.0
        for stats in (days[day][b], totals[b]):
            stats['pnl'] += pnl
            stats['trades'] += 1
            if pnl > 0:
                stats['wins'] += 1

    def finalize(stats):
        return {b: {'pnl': round(s['pnl'], 2), 'trades': s['trades'],
                    'wins': s['wins']}
                for b, s in stats.items()}

    out_days = {}
    for day in sorted(days):
        per = finalize(days[day])
        tot = _blank_stats()
        for s in per.values():
            tot['pnl'] += s['pnl']
            tot['trades'] += s['trades']
            tot['wins'] += s['wins']
        tot['pnl'] = round(tot['pnl'], 2)
        out_days[day] = {'buckets': per, 'total': tot}

    month_total = finalize(totals)
    grand = _blank_stats()
    for s in month_total.values():
        grand['pnl'] += s['pnl']
        grand['trades'] += s['trades']
        grand['wins'] += s['wins']
    grand['pnl'] = round(grand['pnl'], 2)

    return {
        'month': month,
        'cutoff_day': cutoff_day,
        'buckets': [{'id': bid, 'label': label} for bid, label in BUCKETS],
        'days': out_days,
        'month_total': {'by_bucket': month_total, 'total': grand},
    }
