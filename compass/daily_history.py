"""Select completed trading sessions before limiting archived bar revisions."""
from datetime import datetime, timedelta
from sqlalchemy import case, func, select
from .market import NY, day
from .store import events

VERSION = 'completed-daily-sessions-v1'
RECOVERY_CACHE = 'secondary:daily:'


def current_context(value, now, recovery):
    """A cache is usable only for this session and this committed recovery proof."""
    return (value.get('version') == VERSION and value.get('day') == day(now)
            and 0 <= now-value.get('computed_at',0) < 300
            and value.get('history',{}).get('recovery',{}) == recovery)


def completed_rows(db, c, symbol, now):
    from .swing_signals import sessions_between
    today = datetime.fromisoformat(day(now)).replace(tzinfo=NY)
    dates = sessions_between((today-timedelta(days=160)).date().isoformat(),
                             (today-timedelta(days=1)).date().isoformat())
    bounds = []
    for date in dates:
        start = datetime.fromisoformat(date).replace(tzinfo=NY)
        bounds.append(((events.c.ts >= start.timestamp()) &
                       (events.c.ts < (start+timedelta(days=1)).timestamp()), date))
    label = case(*bounds, else_=None)
    ranked = select(events, label.label('session_day'),
        func.count().over(partition_by=label).label('revisions'),
        func.row_number().over(partition_by=label,
            order_by=events.c.id.desc()).label('revision_rank')).where(
        events.c.kind == 'daily', events.c.symbol == symbol,
        events.c.ts >= (today-timedelta(days=160)).timestamp(),
        events.c.ts < today.timestamp()).subquery()
    return [dict(r) for r in c.execute(select(ranked).where(
        ranked.c.revision_rank == 1, ranked.c.session_day.is_not(None))
        .order_by(ranked.c.session_day)).mappings()]
