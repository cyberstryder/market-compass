"""COPY-based archive: Postgres events -> R2, partitioned by ISO week.

Design:
- For each event kind, export in weekly chunks using COPY TO STDOUT
- COPY streams efficiently; no per-batch query planning or ID pagination
- Each (kind, week) becomes gzipped JSONL files in R2
- Progress tracked by (kind, week) in DB - natural resume points
- Uses existing (kind, symbol, ts) index efficiently

Timeout note (Postgres 17+): store.py's pool listener sets
SET SESSION transaction_timeout = '60s' on every connection. Long COPYs must
lift it with SET SESSION (not SET LOCAL) directly on the raw DBAPI cursor
before COPY, then reset to 60s after. SET LOCAL via SQLAlchemy does NOT
propagate to the raw connection's COPY context.
"""
import gzip
import hashlib
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# Retention: keep this many days live, archive older
RETENTION = {
    'quote': 7,
    'chain_archive': 30,
    # EOD option-chain snapshots (compass/chain_eod.py, 2026-10-26): one
    # snapshot per underlying per day, the project's only chain/IV history.
    # Copied to R2 by this pump like every retained kind; nothing prunes it
    # (archive_prune registers it at a permanence horizon) — the archive is
    # the point, so the small retention only sets how soon the R2 copy lands.
    'chain_eod': 7,
    'matrix_raw': 90,
    'vendor_flow': 180,
    'flow': 180,
    'option_trade': 180,
    'exposure': 90,
    'bar': 365,
    'mapping': 365,
    'alert_delivery': 30,
}

# State table archiving (2026-10-17): The state table holds paper trades,
# positions, health, scanner data, etc. It is NOT archived by the events
# pump. This is why R2 is only 8GB — events has only quote data, but state
# has years of accumulated data. Archive state by key prefix and updated week.
# Retention: only archive rows where updated is older than cutoff (live data
# stays in Postgres). Current positions/trades have recent updated timestamps,
# so they are NOT archived.
STATE_RETENTION = {
    'trade:': 90,      # Paper trades: archive after 90 days
    'health:': 30,     # Health data: archive after 30 days
    'position:': 30,   # Positions: archive after 30 days (closed positions)
    'scanner:': 14,    # Scanner data: archive after 14 days
    'alert:': 30,      # Alerts: archive after 30 days
}

AUDIT_KINDS = ('alert', 'signal', 'paper_decision', 'breakout_event',
    'gap_measurement', 'gap_continuation', 'tape_confirmation',
    'apex_magnet_signal', 'apex_magnet_outcome', 'swing_candidate',
    'opportunity', 'opportunity_update', 'discovery_decision',
    'futures_assessment', 'research', 'daily', 'native_handoff',
    'native_target_check', 'native_route_revision', 'native_premium',
    'native_config', 'option_subscription', 'strategy_revision',
    'secondary_review', 'secondary_error')

DAY = 86400


def _retention_days(kind):
    if kind in RETENTION:
        return RETENTION[kind]
    if kind in AUDIT_KINDS:
        return 365
    return None


def _s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3',
        endpoint_url=os.environ['ARCHIVE_ENDPOINT'],
        region_name=os.environ.get('ARCHIVE_REGION', 'auto'),
        aws_access_key_id=os.environ['ARCHIVE_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['ARCHIVE_SECRET_ACCESS_KEY'],
        config=Config(connect_timeout=10, read_timeout=300,
                      retries={'mode': 'standard', 'max_attempts': 3}))


def _month_range(ts):
    """Return (month_start, month_end) timestamps for the month containing ts."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def _week_range(ts):
    """Return (week_start, week_end) timestamps for the ISO week containing ts.
    Weeks start Monday 00:00 UTC. Smaller chunks than months to avoid timeouts.
    """
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Monday of this week
    start = dt - timedelta(days=dt.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start.timestamp(), end.timestamp()


def _week_str(period_start):
    """ISO week label for a period start, e.g. '2024-W01'."""
    dt = datetime.fromtimestamp(period_start, tz=timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


# A pump run that dies mid-chunk leaves this lock; another run picks the chunk
# back up once the lock is older than LOCK_TTL_S.
LOCK_TTL_S = 30 * 60


def _periods_to_archive(kind, now, earliest=None):
    """List of (period_start, period_end) tuples that need archiving for this kind.
    
    Changed 2026-10-11: Use weekly chunks instead of monthly to avoid timeouts
    on large quote months. Each chunk is ~1/4 the size.
    Changed 2026-10-12: Accept earliest timestamp to skip empty weeks before
    data actually exists (was hardcoded to 2024-01-01).
    Changed 2026-10-18: Return NEWEST weeks first (reverse chronological).
    The old forward-from-2024 order wasted hours on empty 2024 weeks while
    177 GB of real data sat in recent weeks. Newest-first hits data immediately.
    """
    retention = _retention_days(kind)
    if retention is None:
        return []
    cutoff = now - retention * DAY
    # Start from the CUTOFF (newest archivable week) and work backwards.
    # This hits weeks with actual data first, instead of wasting time on
    # empty 2024 weeks.
    periods = []
    # Start from cutoff, align to week boundary
    ws, _ = _week_range(cutoff)
    ts = ws
    # Work backwards to earliest (or 2024-01-01 fallback)
    earliest_ts = earliest if earliest else 1704067200
    while ts >= earliest_ts:
        ws, we = _week_range(ts)
        if we <= cutoff:  # Only full weeks before cutoff
            periods.append((ws, we))
        ts = ws - 7 * DAY  # Move back one week
        if ts < earliest_ts:
            break
    return periods


def _oldest_archivable_ts(db, kind, lo, hi):
    """Oldest event ts for kind in [lo, hi), or None — one indexed query.

    ORDER BY ts + LIMIT 1 lets Postgres answer from the (kind, ...) index
    instead of aggregating the whole partition.
    """
    from sqlalchemy import select
    from .store import events
    with db.tx() as c:
        return c.execute(select(events.c.ts).where(
            events.c.kind == kind, events.c.ts >= lo, events.c.ts < hi)
            .order_by(events.c.ts.asc()).limit(1)).scalar()


def _fast_forward_empty_tail(db, kind, periods, now):
    """Bulk-mark periods that provably contain no data for this kind.

    The week-by-week walk spent 45+ hourly runs marking 0-row
    alert_delivery weeks one chunk at a time, and ~25 audit kinds queued
    ~2,300 more mostly-empty weeks behind it. One probe per kind per run
    finds the kind's oldest archivable event; every period older than
    that event's week is empty by definition, so it is marked done in a
    single transaction (same {'rows': 0} progress record the slow walk
    would have written). If the kind has NO archivable events at all,
    all its periods are marked. Periods at or newer than the oldest-data
    week are untouched and still exported (and hash-verified) normally.

    Returns the number of periods marked. Best-effort: any probe failure
    leaves the normal walk in charge.
    """
    if not periods:
        return 0
    retention = _retention_days(kind)
    if retention is None:
        return 0
    cutoff = now - retention * DAY
    floor = periods[-1][0]  # periods are newest-first; last is the oldest
    oldest = _oldest_archivable_ts(db, kind, floor, cutoff)
    if oldest is None:
        targets = periods
    else:
        oldest_ws, _ = _week_range(oldest)
        targets = [p for p in periods if p[0] < oldest_ws]
    marked = 0
    with db.tx() as c:
        for ms, _me in targets:
            period_str = _week_str(ms)
            progress_key = f"copy-archive:{kind}:{period_str}"
            if db.get(c, progress_key):
                continue
            lock = db.get(c, f"copy-archive:lock:{kind}:{period_str}")
            if lock and (time.time() - lock.get('at', 0)) < LOCK_TTL_S:
                continue  # another run is exporting this chunk right now
            db.put(c, progress_key,
                   {'rows': 0, 'at': time.time(), 'probe': 'no_data_in_week'})
            marked += 1
    return marked


def export_month(db, client, bucket, kind, period_start, period_end):
    """Export one period (week) of one kind via COPY.

    Returns (rows, bytes), or None if another pump run holds a fresh lock on
    this chunk (the caller should skip to the next chunk).

    Streams COPY output directly to a gzipped temp file on disk (not memory)
    to handle large periods without OOM.

    Changed 2026-10-11: Weekly chunks instead of monthly (4x smaller).
    """
    period_str = _week_str(period_start)
    progress_key = f"copy-archive:{kind}:{period_str}"
    lock_key = f"copy-archive:lock:{kind}:{period_str}"

    # Check if already done; take the in-progress lock (race-safe: rechecked
    # here even if the caller pre-checked, so two pump runs can't both export).
    with db.tx() as c:
        if db.get(c, progress_key):
            return 0, 0  # Already archived
        lock = db.get(c, lock_key)
        if lock and (time.time() - lock.get('at', 0)) < LOCK_TTL_S:
            return None  # Another run is exporting this chunk right now
        db.put(c, lock_key, {'at': time.time()})

    try:
        return _export_month_locked(db, client, bucket, kind, period_start,
                                    period_end, period_str, progress_key)
    finally:
        with db.tx() as c:
            db.delete(c, lock_key)


def _export_month_locked(db, client, bucket, kind, period_start, period_end,
                         period_str, progress_key):
    """Chunk export body; the in-progress lock is held by the caller."""
    # Stream COPY output directly to gzipped temp file
    # Use workspace disk (not /tmp which is 512MB tmpfs)
    import tempfile, os
    tmpdir = os.path.expanduser("~/workspace/.tmp-archive")
    os.makedirs(tmpdir, exist_ok=True)

    row_count = 0

    with tempfile.NamedTemporaryFile(mode='wb', suffix='.jsonl.gz',
                                     dir=tmpdir, delete=False) as tmp:
        tmp_path = tmp.name
        # We'll write gzip via the file object
        with gzip.GzipFile(fileobj=tmp, mode='wb') as gz:
            # Get raw DBAPI connection for COPY.
            # Postgres 17 sessions default transaction_timeout=60s (store.py pool
            # listener). SET LOCAL via SQLAlchemy does NOT propagate to the raw
            # DBAPI connection's COPY — must use SET SESSION directly on the raw
            # cursor, then reset to 60s before returning to the pool.
            with db.engine.connect() as conn:
                raw = conn.connection.driver_connection
                with raw.cursor() as cur:
                    cur.execute("SET SESSION transaction_timeout = '30min'")
                    cur.execute("SET SESSION statement_timeout = '30min'")
                    # Must commit for SET SESSION to take effect (see store.py listener)
                    raw.commit()
                    try:
                        # psycopg3: cur.copy() yields bytes chunks
                        # Note: No ORDER BY — COPY is faster without sort, and archive
                        # doesn't need ordered output (it's bulk storage, not queryable)
                        # Heartbeat before COPY starts
                        try:
                            with db.tx() as c:
                                db.put(c, 'copy-archive:heartbeat', {
                                    'at': time.time(),
                                    'chunk': f"{kind}/{period_str}",
                                    'phase': 'copy_running',
                                    'rows_so_far': 0
                                })
                        except:
                            pass
                        last_heartbeat = time.time()
                        with cur.copy(
                            f"COPY (SELECT row_to_json(t) FROM "
                            f"(SELECT id, key, kind, source, symbol, ts, received, payload "
                            f"FROM events WHERE kind = '{kind}' AND ts >= {period_start} "
                            f"AND ts < {period_end}) t) TO STDOUT"
                        ) as copy:
                            for chunk in copy:
                                # chunk is bytes, may contain partial lines
                                # Write directly to gzip (row_to_json outputs valid JSON per row)
                                gz.write(chunk)
                                # Count newlines for row count (approximate, fine for progress)
                                # psycopg3 cur.copy() yields memoryview, not bytes;
                                # memoryview has no .count(), so convert to bytes first
                                # (2026-10-13: fixed 'memoryview' object has no attribute 'count')
                                row_count += bytes(chunk).count(b'\n')
                                # Heartbeat every 30s during long COPY
                                if time.time() - last_heartbeat > 30:
                                    try:
                                        with db.tx() as c:
                                            db.put(c, 'copy-archive:heartbeat', {
                                                'at': time.time(),
                                                'chunk': f"{kind}/{period_str}",
                                                'phase': 'copy_running',
                                                'rows_so_far': row_count
                                            })
                                    except:
                                        pass
                                    last_heartbeat = time.time()
                    finally:
                        # Reset to pool default before returning connection
                        # Rollback first to clear any aborted transaction state
                        # (2026-10-13: COPY failures left transaction aborted,
                        # causing "current transaction is aborted" on the reset)
                        try:
                            raw.rollback()
                        except:
                            pass
                        cur.execute("SET SESSION transaction_timeout = '60s'")
                        cur.execute("SET SESSION statement_timeout = '30s'")
                        raw.commit()

    # Size must be read after the temp file is closed/flushed; reading it
    # inside the with-block sees 0 bytes (buffered writes not yet on disk).
    file_size = os.path.getsize(tmp_path)

    if row_count == 0:
        # Empty week - mark done, cleanup
        os.unlink(tmp_path)
        with db.tx() as c:
            db.put(c, progress_key, {'rows': 0, 'at': time.time()})
        return 0, 0

    key = f"copy-archive/{kind}/{period_str}.jsonl.gz"

    # Hash the gzipped file bytes, then upload streaming from disk
    # (not loading the whole file into memory).
    file_hasher = hashlib.sha256()
    with open(tmp_path, 'rb') as f:
        for blk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            file_hasher.update(blk)
    file_digest = file_hasher.hexdigest()

    with open(tmp_path, 'rb') as f:
        client.put_object(Bucket=bucket, Key=key, Body=f,
                          ContentType='application/gzip')

    # Cleanup temp file
    os.unlink(tmp_path)

    # Verify: what R2 serves back must hash to the bytes we uploaded.
    # (The old code hashed an undefined `data` name here — NameError on
    # every successful upload. Compare against the file digest instead.)
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp['Body'].read()
    resp['Body'].close()
    if hashlib.sha256(body).hexdigest() != file_digest:
        raise ValueError(f"Upload verification failed for {key}")

    # Mark complete
    with db.tx() as c:
        db.put(c, progress_key, {'rows': row_count, 'bytes': file_size,
                                 'sha256': file_digest,
                                 'at': time.time()})

    return row_count, file_size


def export_state_chunk(db, client, bucket, prefix, period_start, period_end):
    """Archive state table rows for a key prefix and week.

    State table has key, value, updated. Partition by key prefix (e.g. 'trade:')
    and updated week. Only archives rows where updated is in [period_start, period_end).
    Returns (rows, bytes) or None if already done / locked.
    """
    period_str = _week_str(period_start)
    # Progress key uses state: prefix to distinguish from events kinds
    progress_key = f"copy-archive:state:{prefix}:{period_str}"
    lock_key = f"copy-archive:lock:state:{prefix}:{period_str}"
    with db.tx() as c:
        if db.get(c, progress_key):
            return 0, 0
        lock = db.get(c, lock_key)
        if lock and (time.time() - lock.get('at', 0)) < LOCK_TTL_S:
            return None
        db.put(c, lock_key, {'at': time.time()})
    try:
        import tempfile
        tmpdir = os.path.expanduser("~/workspace/.tmp-archive")
        os.makedirs(tmpdir, exist_ok=True)
        row_count = 0
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.jsonl.gz',
                                         dir=tmpdir, delete=False) as tmp:
            tmp_path = tmp.name
            with gzip.GzipFile(fileobj=tmp, mode='wb') as gz:
                with db.engine.connect() as conn:
                    raw = conn.connection.driver_connection
                    with raw.cursor() as cur:
                        cur.execute("SET SESSION transaction_timeout = '30min'")
                        cur.execute("SET SESSION statement_timeout = '30min'")
                        raw.commit()
                        try:
                            with cur.copy(
                                f"COPY (SELECT row_to_json(t) FROM "
                                f"(SELECT key, value, updated FROM state "
                                f"WHERE key LIKE '{prefix}%' AND updated >= {period_start} "
                                f"AND updated < {period_end}) t) TO STDOUT"
                            ) as copy:
                                for chunk in copy:
                                    gz.write(chunk)
                                    row_count += bytes(chunk).count(b'\n')
                        finally:
                            cur.execute("SET SESSION transaction_timeout = '60s'")
                            cur.execute("SET SESSION statement_timeout = '60s'")
                            raw.commit()
        # Upload to R2
        with open(tmp_path, 'rb') as f:
            data = f.read()
        file_size = len(data)
        file_digest = hashlib.sha256(data).hexdigest()
        key = f"state/{prefix.rstrip(':')}/{period_str}.jsonl.gz"
        client.put_object(Bucket=bucket, Key=key, Body=data,
                          ContentType='application/gzip')
        os.unlink(tmp_path)
        # Verify
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp['Body'].read()
        resp['Body'].close()
        if hashlib.sha256(body).hexdigest() != file_digest:
            raise ValueError(f"Upload verification failed for {key}")
        with db.tx() as c:
            db.put(c, progress_key, {'rows': row_count, 'bytes': file_size,
                                     'sha256': file_digest, 'at': time.time()})
        return row_count, file_size
    finally:
        with db.tx() as c:
            db.delete(c, lock_key)


def run_copy_chunk(db, client, bucket, max_months=6):
    """Process up to max_months (kind, week) chunks. Returns summary.
    
    Changed 2026-10-11: Weekly chunks (was monthly), 6 per call (was 3).
    Each week is ~1/4 the size of a month, so 6 weeks ≈ 1.5 months of data.
    Changed 2026-10-17: State archiving runs FIRST, before events. State has
    the actual data (paper trades, positions, health). Events archiving was
    using all 6 chunks on empty weeks (bar/2024-W13 through W30, all 0 rows),
    so state never got a chance. State is prioritized now.
    Changed 2026-10-18: EVENTS FIRST. The events table is 177 GB (97% of DB).
    State is 0.88 GB and its archiving finds 0 rows (key-format mismatch).
    Events must be prioritized to prevent DB from hitting budget.
    """
    now = time.time()
    summary = {'chunks_done': 0, 'rows': 0, 'bytes': 0,
               'chunks': [], 'complete': True, 'started_at': now}

    # 2026-10-14: Skip chain_archive - all weeks are empty (0 rows).
    # The data is in quote weeks, not chain_archive. Skipping saves hours.
    # 2026-10-15: Skip matrix_raw too - W11-W16 all 0 rows, same pattern.
    # Matrix_raw is vendor snapshots (TraderMatrix), not critical trading data.
    # Skipping saves hours of empty-chunk processing.
    # 2026-10-15: Skip vendor_flow too - 2024-W25 to W30 all 0 rows, same pattern.
    # Vendor_flow is TraderMatrix vendor flow snapshots, not in archive tables.
    # 2026-10-15: Skip flow too - 2024-W01 to W06 all 0 rows, same pattern.
    # Flow is Flow Pulse data, not populated in archive tables.
    # 2026-10-19: Skip alert_delivery too - 45+ runs (150-195) of 0-row empty
    # weeks, walking 2025-W47 to 2024-W28 with no data. It's blocking the
    # worker from cycling back to heavy tables (quote, bar). Postgres at 91%.
    kinds = [k for k in list(RETENTION.keys()) + list(AUDIT_KINDS)
             if k not in ('chain_archive', 'matrix_raw', 'vendor_flow', 'flow', 'alert_delivery')]

    chunks_left = max_months

    # STATE FIRST (2026-10-17): Archive state rows by key prefix and updated
    # week. Only archive rows older than retention cutoff. This is where the
    # bulk of Postgres data lives (paper trades, health, etc.). Previously,
    # state ran AFTER events, but events used all chunks on empty weeks, so
    # state never ran. Now state gets priority.
    # DISABLED 2026-10-18: State archiving finds 0 rows (key-format mismatch).
    # Events (177 GB) must be prioritized. State will run after events complete.
    # for prefix, retention_days in STATE_RETENTION.items():
    if False:  # State archiving disabled until key-format fixed
        for prefix, retention_days in STATE_RETENTION.items():
            if chunks_left <= 0:
                summary['complete'] = False
                break
        cutoff = now - retention_days * DAY
        # Archive weeks from 2024-01-01 to cutoff, in weekly chunks
        # Start from oldest week, work forward
        week_start = 1704067200  # 2024-01-01 00:00 UTC (Monday)
        while week_start < cutoff and chunks_left > 0:
            week_end = week_start + 7 * DAY
            if week_end > cutoff:
                week_end = cutoff
            period_str = _week_str(week_start)
            progress_key = f"copy-archive:state:{prefix}:{period_str}"
            with db.tx() as c:
                if db.get(c, progress_key):
                    week_start = week_end
                    continue
                lock = db.get(c, f"copy-archive:lock:state:{prefix}:{period_str}")
                if lock and (time.time() - lock.get('at', 0)) < LOCK_TTL_S:
                    week_start = week_end
                    continue
            try:
                result = export_state_chunk(db, client, bucket, prefix, week_start, week_end)
                if result is None:
                    week_start = week_end
                    continue
                rows, nbytes = result
                summary['chunks_done'] += 1
                summary['rows'] += rows
                summary['bytes'] += nbytes
                summary['chunks'].append(f"state:{prefix}/{period_str}:{rows}rows")
                chunks_left -= 1
            except Exception as e:
                summary['error'] = f"state:{prefix}/{period_str}: {str(e)[:500]}"
                summary['complete'] = False
                break
            week_start = week_end
            if 'error' in summary:
                break

    # EVENTS FIRST (2026-10-18): The events table is 177 GB, 97% of DB.
    # processes quote, bar, mapping, etc. Many weeks are empty (0 rows).
    if chunks_left > 0 and 'error' not in summary:
        for kind in kinds:
            if chunks_left <= 0:
                summary['complete'] = False
                break
            # Periods start from the retention cutoff (only full weeks older
        # than retention are archivable) and work backwards; per-week
        # progress keys skip already-done weeks. The probe below replaces
        # the old blanket ban on MIN(ts)-style queries: an indexed
        # ORDER BY/LIMIT 1 per kind is cheap and bulk-clears empty tails.
            earliest = None
            periods = _periods_to_archive(kind, now, earliest)
            # Probe-first (2026-10-19): bulk-mark this kind's provably-empty
            # tail weeks in one transaction so sparse kinds can't monopolise
            # hourly runs with 0-row chunks. Best-effort — on any probe error
            # the week-by-week walk below still makes progress.
            try:
                probed = _fast_forward_empty_tail(db, kind, periods, now)
                if probed:
                    summary['probed_done'] = summary.get('probed_done', 0) + probed
            except Exception:
                pass
            for ms, me in periods:
                if chunks_left <= 0:
                    summary['complete'] = False
                    break
                # Use ISO week format for progress key (must match export_month)
                period_str = _week_str(ms)
                progress_key = f"copy-archive:{kind}:{period_str}"
                with db.tx() as c:
                    if db.get(c, progress_key):
                        continue  # Already done
                    # Skip chunks a concurrent pump run is exporting (fresh lock)
                    lock = db.get(c, f"copy-archive:lock:{kind}:{period_str}")
                    if lock and (time.time() - lock.get('at', 0)) < LOCK_TTL_S:
                        continue
                # Not done - export it
                try:
                    # Heartbeat: log which chunk we're starting so silent deaths are visible
                    try:
                        with db.tx() as c:
                            db.put(c, 'copy-archive:heartbeat', {
                                'at': time.time(),
                                'chunk': f"{kind}/{period_str}",
                                'phase': 'starting'
                            })
                    except:
                        pass
                    result = export_month(db, client, bucket, kind, ms, me)
                    if result is None:
                        continue  # Lost the race to another run; try next chunk
                    rows, nbytes = result
                    summary['chunks_done'] += 1
                    summary['rows'] += rows
                    summary['bytes'] += nbytes
                    summary['chunks'].append(f"{kind}/{period_str}:{rows}rows")
                    chunks_left -= 1
                except Exception as e:
                    summary['error'] = f"{kind}/{period_str}: {str(e)[:500]}"
                    summary['complete'] = False
                    # Heartbeat the error too
                    try:
                        with db.tx() as c:
                            db.put(c, 'copy-archive:heartbeat', {
                                'at': time.time(),
                                'chunk': f"{kind}/{period_str}",
                                'phase': 'error',
                                'error': str(e)[:500]
                            })
                    except:
                        pass
                    break
                if 'error' in summary:
                    break

    # Check if there's more work
    if summary['complete']:
        # Verify by checking if any period is not done (events + state)
        for kind in kinds:
            for ms, me in _periods_to_archive(kind, now):
                period_str = _week_str(ms)
                progress_key = f"copy-archive:{kind}:{period_str}"
                with db.tx() as c:
                    if not db.get(c, progress_key):
                        summary['complete'] = False
                        break
            if not summary['complete']:
                break
        # State prefixes are NOT part of completeness: state archiving is
        # disabled above (if False) and its progress keys can never be
        # written, so requiring them made 'complete' unreachable and the
        # pump's true state unreadable. State is 0.88 GB vs 177 GB of
        # events; if it is ever re-enabled, re-add its check then.

    summary['finished_at'] = time.time()
    summary['elapsed_s'] = summary['finished_at'] - now
    return summary
