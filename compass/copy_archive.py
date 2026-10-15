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
    'quote': 14,
    'chain_archive': 30,
    'matrix_raw': 90,
    'vendor_flow': 180,
    'flow': 180,
    'option_trade': 180,
    'exposure': 90,
    'bar': 365,
    'mapping': 365,
    'alert_delivery': 30,
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
    """
    retention = _retention_days(kind)
    if retention is None:
        return []
    cutoff = now - retention * DAY
    # Start from earliest data (or 2024-01-01 fallback) to avoid scanning
    # hundreds of empty weeks before data collection began
    periods = []
    ts = earliest if earliest else 1704067200  # 2024-01-01 UTC fallback
    # Align to week boundary
    ws, _ = _week_range(ts)
    ts = ws
    while ts < cutoff:
        ws, we = _week_range(ts)
        if we <= cutoff:  # Only full weeks before cutoff
            periods.append((ws, we))
        ts = we
    return periods


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


def run_copy_chunk(db, client, bucket, max_months=6):
    """Process up to max_months (kind, week) chunks. Returns summary.
    
    Changed 2026-10-11: Weekly chunks (was monthly), 6 per call (was 3).
    Each week is ~1/4 the size of a month, so 6 weeks ≈ 1.5 months of data.
    """
    now = time.time()
    summary = {'chunks_done': 0, 'rows': 0, 'bytes': 0,
               'chunks': [], 'complete': True, 'started_at': now}

    # 2026-10-14: Skip chain_archive - all weeks are empty (0 rows).
    # The data is in quote weeks, not chain_archive. Skipping saves hours.
    # 2026-10-15: Skip matrix_raw too - W11-W16 all 0 rows, same pattern.
    # Matrix_raw is vendor snapshots (TraderMatrix), not critical trading data.
    # Skipping saves hours of empty-chunk processing.
    kinds = [k for k in list(RETENTION.keys()) + list(AUDIT_KINDS) + ['alert_delivery']
             if k not in ('chain_archive', 'matrix_raw')]

    chunks_left = max_months
    for kind in kinds:
        if chunks_left <= 0:
            summary['complete'] = False
            break
        # Skip the MIN(ts) query (times out on 135M rows without index).
        # Instead, start from retention window: we only archive data older
        # than the retention cutoff, so start there. The per-week progress
        # keys skip already-done weeks.
        earliest = None
        for ms, me in _periods_to_archive(kind, now, earliest):
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
        # Verify by checking if any period is not done
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

    summary['finished_at'] = time.time()
    summary['elapsed_s'] = summary['finished_at'] - now
    return summary
