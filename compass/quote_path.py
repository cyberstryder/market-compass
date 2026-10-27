"""Replay only sampled quotes recorded by the live collector, never OHLC fills."""
from sqlalchemy import select
from .store import events
from .market import fresh

LIMIT = 1200
DISQUALIFIED_LIMIT = 100


def _receipt(q):
    """When the system actually read this quote, before storage.

    socket_read_at is the stream's read stamp; recovery_fetched_at is the
    recovery fetcher's read stamp. Both are receipt times and are treated
    identically (ruling, 2026-10-26): a quote is judged against the moment
    it was read, whichever path read it."""
    receipt = q.get('socket_read_at', q.get('recovery_fetched_at'))
    return receipt


def recorded_path(db, c, symbol, after, until):
    # The received<=until bound stays (desk verdict correction D): a row
    # counts only once it is actually in storage, so a replay never "sees"
    # a quote earlier than storage had it. Late-stored rows become usable
    # on the first replay after they land — which is what the pause (not
    # terminate) gap semantics are for.
    rows = c.execute(select(events.c.ts, events.c.received, events.c.payload)
        .where(events.c.kind == 'quote', events.c.symbol == symbol,
               events.c.ts > after, events.c.ts <= until, events.c.received <= until)
        .order_by(events.c.ts, events.c.id).limit(LIMIT+1)).all()
    observations = []
    check = dict(read_rows=min(len(rows),LIMIT), usable_rows=0, timestamp_mismatches=0,
                 future_when_recorded=0, stale_or_invalid_when_recorded=0, late_storage=0,
                 late_storage_accepted=0, invalid_quote=0, disqualified=[], checked_at=until)
    for row in rows[:LIMIT]:
        q = row.payload
        if q.get('ts') != row.ts:
            check['timestamp_mismatches'] += 1
            check['disqualified'].append(dict(ts=row.ts, bucket='timestamp_mismatch'))
        elif row.ts > row.received:
            # Historical backfills and future-stamped observations cannot
            # repair a live path; a receipt stamp cannot rescue them either
            # (receipt would have to precede the source time).
            check['future_when_recorded'] += 1
            check['disqualified'].append(dict(ts=row.ts, bucket='future_when_recorded'))
        elif fresh(q, row.received):
            observations.append({**q, 'recorded_at': row.received})
        else:
            # Fix 3 (2026-10-26): fresh when actually stored is no longer
            # the only acceptance. A quote persisted seconds late was
            # rejected even though it was fresh when the socket read it —
            # a stored-late hole that never existed on the wire. Judge it
            # by its receipt stamp instead; recorded_at becomes the
            # receipt time (when the system knew the quote), so downstream
            # freshness checks stay coherent.
            receipt = _receipt(q)
            if receipt is not None and row.ts <= receipt <= row.received and fresh(q, receipt):
                observations.append({**q, 'recorded_at': receipt})
                # Counted as late-storage evidence (it WAS stored late —
                # gap classification still sees it) even though usable now.
                check['late_storage'] += 1
                check['late_storage_accepted'] += 1
            else:
                check['stale_or_invalid_when_recorded'] += 1
                check['invalid_quote'] += 1
                check['disqualified'].append(dict(ts=row.ts, bucket='invalid_quote'))
    check['usable_rows'] = len(observations)
    del check['disqualified'][DISQUALIFIED_LIMIT:]
    if not rows:
        latest = c.execute(select(events.c.ts,events.c.received).where(events.c.kind=='quote',
            events.c.symbol==symbol).order_by(events.c.ts.desc()).limit(1)).first()
        check.update(latest_archived_ts=latest.ts if latest else None,
                     latest_archived_received=latest.received if latest else None)
    return observations, len(rows)>LIMIT, check
