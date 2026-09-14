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
    for row in rows[:LIMIT]:
        q = row.payload
        # Historical backfills and future-stamped observations cannot repair a
        # live path. Retained quotes must have been fresh when actually stored.
        if q.get('ts') == row.ts and row.ts <= row.received and fresh(q, row.received):
            observations.append({**q, 'recorded_at': row.received})
    return observations, len(rows)>LIMIT
