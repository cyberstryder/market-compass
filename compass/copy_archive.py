"""COPY-based archive: Postgres events -> R2, partitioned by month.

Design:
- For each event kind, export in monthly chunks using COPY TO STDOUT
- COPY streams efficiently; no per-batch query planning or ID pagination
- Each (kind, month) becomes gzipped JSONL files in R2
- Progress tracked by (kind, month) in DB - natural resume points
- Uses existing (kind, symbol, ts) index efficiently
"""
import gzip
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone

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


def _months_to_archive(kind, now):
    """List of (month_start, month_end) tuples that need archiving for this kind."""
    retention = _retention_days(kind)
    if retention is None:
        return []
    cutoff = now - retention * DAY
    # Start from a reasonable epoch (2024-01-01) to now
    # We'll check each month; empty months are skipped quickly
    months = []
    ts = 1704067200  # 2024-01-01 UTC
    while ts < cutoff:
        ms, me = _month_range(ts)
        if me <= cutoff:  # Only full months before cutoff
            months.append((ms, me))
        ts = me
    return months


def export_month(db, client, bucket, kind, month_start, month_end):
    """Export one month of one kind via COPY. Returns (rows, bytes).
    
    Streams COPY output directly to a gzipped temp file on disk (not memory)
    to handle multi-GB months without OOM.
    """
    month_str = datetime.fromtimestamp(month_start, tz=timezone.utc).strftime('%Y-%m')
    progress_key = f"copy-archive:{kind}:{month_str}"

    # Check if already done
    with db.tx() as c:
        if db.get(c, progress_key):
            return 0, 0  # Already archived

    # Stream COPY output directly to gzipped temp file
    # Use workspace disk (not /tmp which is 512MB tmpfs)
    import tempfile, os
    tmpdir = os.path.expanduser("~/workspace/.tmp-archive")
    os.makedirs(tmpdir, exist_ok=True)
    
    hasher = hashlib.sha256()
    row_count = 0
    
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.jsonl.gz', 
                                     dir=tmpdir, delete=False) as tmp:
        tmp_path = tmp.name
        # We'll write gzip via the file object
        with gzip.GzipFile(fileobj=tmp, mode='wb') as gz:
            # Get raw DBAPI connection for COPY
            with db.engine.connect() as conn:
                conn.execute(text("SET statement_timeout = '30min'"))
                raw = conn.connection.driver_connection
                with raw.cursor() as cur:
                    # psycopg3: cur.copy() yields bytes chunks
                    # Note: No ORDER BY — COPY is faster without sort, and archive
                    # doesn't need ordered output (it's bulk storage, not queryable)
                    with cur.copy(
                        f"COPY (SELECT row_to_json(t) FROM "
                        f"(SELECT id, key, kind, source, symbol, ts, received, payload "
                        f"FROM events WHERE kind = '{kind}' AND ts >= {month_start} "
                        f"AND ts < {month_end}) t) TO STDOUT"
                    ) as copy:
                        for chunk in copy:
                            # chunk is bytes, may contain partial lines
                            # Write directly to gzip (row_to_json outputs valid JSON per row)
                            gz.write(chunk)
                            hasher.update(chunk)
                            # Count newlines for row count (approximate, fine for progress)
                            row_count += chunk.count(b'\n')
        
        # Get file size
        file_size = os.path.getsize(tmp_path)
    
    if row_count == 0:
        # Empty month - mark done, cleanup
        os.unlink(tmp_path)
        with db.tx() as c:
            db.put(c, progress_key, {'rows': 0, 'at': time.time()})
        return 0, 0

    key = f"copy-archive/{kind}/{month_str}.jsonl.gz"

    # Upload to R2 from file (streaming, not loading into memory)
    with open(tmp_path, 'rb') as f:
        client.put_object(Bucket=bucket, Key=key, Body=f,
                          ContentType='application/gzip')
    
    # Cleanup temp file
    os.unlink(tmp_path)

    # Verify
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp['Body'].read()
    resp['Body'].close()
    if hashlib.sha256(body).hexdigest() != hashlib.sha256(data).hexdigest():
        raise ValueError(f"Upload verification failed for {key}")

    # Mark complete
    with db.tx() as c:
        db.put(c, progress_key, {'rows': row_count, 'bytes': file_size,
                                 'sha256': hasher.hexdigest(),
                                 'at': time.time()})

    return row_count, file_size


def run_copy_chunk(db, client, bucket, max_months=3):
    """Process up to max_months (kind, month) chunks. Returns summary."""
    now = time.time()
    summary = {'chunks_done': 0, 'rows': 0, 'bytes': 0,
               'chunks': [], 'complete': True, 'started_at': now}

    kinds = list(RETENTION.keys()) + list(AUDIT_KINDS) + ['alert_delivery']

    chunks_left = max_months
    for kind in kinds:
        if chunks_left <= 0:
            summary['complete'] = False
            break
        for ms, me in _months_to_archive(kind, now):
            if chunks_left <= 0:
                summary['complete'] = False
                break
            month_str = datetime.fromtimestamp(ms, tz=timezone.utc).strftime('%Y-%m')
            progress_key = f"copy-archive:{kind}:{month_str}"
            with db.tx() as c:
                if db.get(c, progress_key):
                    continue  # Already done
            # Not done - export it
            try:
                rows, nbytes = export_month(db, client, bucket, kind, ms, me)
                summary['chunks_done'] += 1
                summary['rows'] += rows
                summary['bytes'] += nbytes
                summary['chunks'].append(f"{kind}/{month_str}:{rows}rows")
                chunks_left -= 1
            except Exception as e:
                summary['error'] = f"{kind}/{month_str}: {str(e)[:500]}"
                summary['complete'] = False
                break
        if 'error' in summary:
            break

    # Check if there's more work
    if summary['complete']:
        # Verify by checking if any month is not done
        for kind in kinds:
            for ms, me in _months_to_archive(kind, now):
                month_str = datetime.fromtimestamp(ms, tz=timezone.utc).strftime('%Y-%m')
                progress_key = f"copy-archive:{kind}:{month_str}"
                with db.tx() as c:
                    if not db.get(c, progress_key):
                        summary['complete'] = False
                        break
            if not summary['complete']:
                break

    summary['finished_at'] = time.time()
    summary['elapsed_s'] = summary['finished_at'] - now
    return summary
