"""Simple one-time archive: Postgres events -> R2. No safety checks, no locks.

Usage via API endpoints in app.py. Exports in batches, tracks last_id in DB.
"""
import gzip
import hashlib
import io
import json
import os
import time

from sqlalchemy import select, func

from .store import Store, events

# Simple retention: keep this many days live, archive older
RETENTION = {
    'quote': 14,           # market quotes: biggest table
    'chain_archive': 30,   # option chains
    'matrix_raw': 90,      # vendor snapshots
    'vendor_flow': 180,    # flow prints
    'flow': 180,
    'option_trade': 180,
    'exposure': 90,
    'bar': 365,
    'mapping': 365,
    # audit trail kinds: 365 days
}

AUDIT_KINDS = ('alert', 'signal', 'paper_decision', 'breakout_event',
    'gap_measurement', 'gap_continuation', 'tape_confirmation',
    'apex_magnet_signal', 'apex_magnet_outcome', 'swing_candidate',
    'opportunity', 'opportunity_update', 'discovery_decision',
    'futures_assessment', 'research', 'daily', 'native_handoff',
    'native_target_check', 'native_route_revision', 'native_premium',
    'native_config', 'option_subscription', 'strategy_revision',
    'secondary_review', 'secondary_error')

BATCH_SIZE = 50000
DAY = 86400


def _s3_client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3',
        endpoint_url=os.environ['ARCHIVE_ENDPOINT'],
        region_name=os.environ.get('ARCHIVE_REGION', 'auto'),
        aws_access_key_id=os.environ['ARCHIVE_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['ARCHIVE_SECRET_ACCESS_KEY'],
        config=Config(connect_timeout=10, read_timeout=60,
                      retries={'mode': 'standard', 'max_attempts': 3}))


def _retention_days(kind):
    if kind in RETENTION:
        return RETENTION[kind]
    if kind in AUDIT_KINDS:
        return 365
    return None  # unknown kind: don't archive


def export_batch(db, client, bucket, kind, cutoff_ts, after_id, limit=BATCH_SIZE):
    """Export one batch of events to R2. Returns (rows_exported, last_id)."""
    with db.tx() as c:
        rows = c.execute(
            select(events.c.id, events.c.key, events.c.kind, events.c.source,
                   events.c.symbol, events.c.ts, events.c.received,
                   events.c.payload)
            .where(events.c.kind == kind,
                   events.c.ts < cutoff_ts,
                   events.c.id > after_id)
            .order_by(events.c.id)
            .limit(limit)
        ).all()

    if not rows:
        return 0, after_id

    # Build gzipped JSONL
    buf = io.BytesIO()
    digest = hashlib.sha256()
    with gzip.GzipFile(fileobj=buf, mode='wb') as gz:
        for r in rows:
            obj = {'id': r[0], 'key': r[1], 'kind': r[2], 'source': r[3],
                   'symbol': r[4], 'ts': r[5], 'received': r[6],
                   'payload': r[7]}
            line = (json.dumps(obj, sort_keys=True, separators=(',', ':'),
                               allow_nan=False, default=str) + '\n').encode()
            gz.write(line)
            digest.update(line)

    data = buf.getvalue()
    last_id = rows[-1][0]
    key = f"simple-archive/{kind}/{after_id + 1}-{last_id}.jsonl.gz"

    # Upload to R2
    client.put_object(Bucket=bucket, Key=key, Body=data,
                      ContentType='application/gzip')

    # Verify by reading back the hash
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp['Body'].read()
    resp['Body'].close()
    if hashlib.sha256(body).hexdigest() != hashlib.sha256(data).hexdigest():
        raise ValueError(f"Upload verification failed for {key}")

    return len(rows), last_id


def run_simple_archive(db, client, bucket, max_batches_per_kind=1000):
    """Run full simple archive. Returns summary dict."""
    now = time.time()
    summary = {'kinds': {}, 'total_rows': 0, 'started_at': now}

    # Get distinct kinds
    with db.tx() as c:
        kinds = [r[0] for r in c.execute(
            select(events.c.kind).distinct()).all()]

    for kind in kinds:
        retention = _retention_days(kind)
        if retention is None:
            summary['kinds'][kind] = {'skipped': 'unknown kind'}
            continue

        cutoff = now - retention * DAY
        # Resume from stored progress
        progress_key = f"simple-archive-progress:{kind}"
        with db.tx() as c:
            after_id = db.get(c, progress_key, 0) or 0

        kind_rows = 0
        batches = 0
        while batches < max_batches_per_kind:
            n, last_id = export_batch(db, client, bucket, kind, cutoff,
                                      after_id)
            if n == 0:
                break
            kind_rows += n
            after_id = last_id
            batches += 1
            with db.tx() as c:
                db.put(c, progress_key, after_id)

        summary['kinds'][kind] = {'rows': kind_rows, 'batches': batches,
                                  'retention_days': retention}
        summary['total_rows'] += kind_rows

    summary['finished_at'] = time.time()
    summary['elapsed_s'] = summary['finished_at'] - now
    return summary
