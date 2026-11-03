"""Fill-only daily-bar history backfill (2026-11-03).

The production daily-bar archive (events kind='daily', written by the
Alpaca SIP recovery in secondary_data) starts only ~117 sessions back:
daily collection was gated behind the retired SWING_IDEAS_ENABLED flag
until the 2026-11-02 decoupling, so the archive's depth is an artifact
of when collection resumed, not of what exists. The Connors RSI(2) SSC
scorer's frozen signal needs SMA(200); its 2026-11-03 bootstrap census
found 162 symbols and ZERO eligible. This module is the write path for
backfilling the missing history from the owned Desk Task 008 1-minute
panel (Alpaca SIP, adjustment=all), aggregated to daily bars with the
gauntlet's own aggregation and rescaled per symbol onto the archive's
split-only basis at the splice (see the build note,
files/connors-daily-backfill-2026-11-03.md, for the overlap audit and
the factor table).

Hard rules, enforced here server-side (the caller cannot widen them):

- FILL ONLY. A (symbol, session) that already has ANY 'daily' event —
  whatever its source or payload — is skipped untouched. The
  collector's bars always win; this path only adds sessions the
  archive lacks entirely. (daily_series resolves a session by its
  latest event id, so appending to an occupied session would silently
  REPLACE the collector's bar; the existence check is what makes the
  load safe, and it runs inside the same transaction as the inserts.)
- Completed XNYS sessions only: a bar dated today (ET) or later, or
  dated on a non-session, is refused. Backfilled history can therefore
  never rewrite a live session or pre-empt the collector.
- Every bar must pass spy_daily.valid_daily — the same validity bar
  the standing recovery enforces on its own writes.
- Idempotent: each insert carries the deterministic key
  identity('daily-backfill-v1', symbol, date), and a re-run finds
  every session already present and inserts nothing.
- Provenance: payloads are stamped backfilled='panel-1m-v1' with the
  per-symbol splice factor used, and each call appends one
  'daily_backfill' summary event with its counts.

One transaction per call (per symbol): the existence read and the
inserts commit or roll back together.
"""
from datetime import datetime

from sqlalchemy import select

from .market import NY, day, number, session
from .spy_daily import valid_daily
from .store import events, identity
from .swing_signals import sessions_between

VERSION = 'daily-backfill-v1'
SOURCE = 'panel_backfill'
MARKER = 'panel-1m-v1'
MAX_BARS_PER_CALL = 1000


def _session_ts(date_str):
    """A timestamp inside the session's NY-midnight window (the
    daily_series labeling convention): one minute before the XNYS
    close. None when date_str is not an XNYS session."""
    bounds = session(date_str)
    if not bounds:
        return None
    return bounds[1] - 60


def existing_sessions(db, c, symbol, first, last):
    """Session dates in [first, last] that already hold at least one
    'daily' event for the symbol, labeled exactly as daily_series
    labels them (the NY-midnight window containing the event ts)."""
    labels = list(sessions_between(first, last))
    if not labels:
        return set()
    lo = datetime.fromisoformat(labels[0]).replace(tzinfo=NY).timestamp()
    hi = datetime.fromisoformat(labels[-1]).replace(tzinfo=NY).timestamp() + 86400
    rows = c.execute(select(events.c.ts).where(
        events.c.kind == 'daily', events.c.symbol == symbol,
        events.c.ts >= lo, events.c.ts < hi)).scalars()
    starts = [(datetime.fromisoformat(d).replace(tzinfo=NY).timestamp(), d)
              for d in labels]
    found = set()
    for ts in rows:
        for start, d in starts:
            if start <= ts < start + 86400:
                found.add(d)
                break
    return found


def fill_bars(db, c, symbol, bars, now, factor=None):
    """Insert derived daily bars for the sessions the archive lacks.

    bars: [{date, o, h, l, c, v}] (any order; duplicates within the
    call are refused after the first). Returns
    {inserted: [dates], skipped_existing: [dates],
     refused: [{date, reason}]}. Runs inside the caller's transaction.
    """
    today = day(now)
    inserted, skipped, refused = [], [], []
    candidates = {}
    for b in bars or []:
        d = str((b or {}).get('date') or '')
        if d in candidates:
            refused.append({'date': d, 'reason': 'duplicate_in_request'})
            continue
        candidates[d] = b
    if len(candidates) > MAX_BARS_PER_CALL:
        raise ValueError(f'too many bars in one call ({len(candidates)} '
                         f'> {MAX_BARS_PER_CALL})')
    if candidates:
        existing = existing_sessions(db, c, symbol,
                                     min(candidates), max(candidates))
    else:
        existing = set()
    for d in sorted(candidates):
        b = candidates[d]
        if d >= today:
            refused.append({'date': d, 'reason': 'not_a_completed_session'})
            continue
        ts = _session_ts(d)
        if ts is None:
            refused.append({'date': d, 'reason': 'not_an_xnys_session'})
            continue
        payload = {k: number(b.get(k)) for k in ('o', 'h', 'l', 'c', 'v')}
        if not valid_daily(payload):
            refused.append({'date': d, 'reason': 'invalid_bar'})
            continue
        if d in existing:
            skipped.append(d)
            continue
        payload = {**payload, 'backfilled': MARKER,
                   'backfill_factor': factor}
        if db.append(c, 'daily', SOURCE, symbol, ts, payload,
                     key=identity(VERSION, symbol, d)):
            inserted.append(d)
        else:
            skipped.append(d)  # deterministic-key dedupe: already loaded
    return {'inserted': inserted, 'skipped_existing': skipped,
            'refused': refused}
