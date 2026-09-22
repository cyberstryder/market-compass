"""Replay only sampled quotes recorded by the live collector, never OHLC fills."""
from sqlalchemy import select
from .store import events
from .market import fresh

LIMIT = 1200


def recorded_path(db, c, symbol, after, until):
    rows = c.execute(select(events.c.ts, events.c.received, events.c.payload)
        .where(events.c.kind == 'quote', events.c.symbol == symbol,
               events.c.ts > after, events.c.ts <= until, events.c.received <= until)
        .order_by(events.c.ts, events.c.id).limit(LIMIT+1)).all()
    observations = []
    check = dict(read_rows=min(len(rows),LIMIT), usable_rows=0, timestamp_mismatches=0,
                 future_when_recorded=0, stale_or_invalid_when_recorded=0, late_storage=0, invalid_quote=0, checked_at=until)
    for row in rows[:LIMIT]:
        q = row.payload
        # Historical backfills and future-stamped observations cannot repair a
        # live path. Retained quotes must have been fresh when actually stored.
        if q.get('ts') == row.ts and row.ts <= row.received and fresh(q, row.received):
            observations.append({**q, 'recorded_at': row.received})
        elif q.get('ts') != row.ts:
            check['timestamp_mismatches'] += 1
        elif row.ts > row.received:
            check['future_when_recorded'] += 1
        else:
            check['stale_or_invalid_when_recorded'] += 1
            receipt=q.get('socket_read_at',q.get('recovery_fetched_at'))
            check['late_storage' if receipt is not None and row.ts<=receipt<=row.received and fresh(q,receipt) else 'invalid_quote'] += 1
    check['usable_rows'] = len(observations)
    if not rows:
        latest = c.execute(select(events.c.ts,events.c.received).where(events.c.kind=='quote',
            events.c.symbol==symbol).order_by(events.c.ts.desc()).limit(1)).first()
        check.update(latest_archived_ts=latest.ts if latest else None,
                     latest_archived_received=latest.received if latest else None)
    return observations, len(rows)>LIMIT, check
