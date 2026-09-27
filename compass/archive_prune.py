"""Bounded retention pruning for the Compass event archive. Dry-run by default.

The live database is the working archive: every market-data sample the
collectors persist (quotes at up to 4 Hz per symbol, full option chains every
45 s, vendor API snapshots, flow prints, bars) lands in the ``events`` table,
plus the audit trail and derived study tables. ``compass/archive.py`` and
``compass/archive_schedule.py`` export ``events`` to S3 but never delete
database rows, and nothing else prunes, so the production volume grows until
it fills.

This module plans and executes bounded deletions under explicit guards:

* dry-run is the default; rows are deleted only with ``confirm=True``
  (``python -m compass.archive_prune --confirm``);
* a global recency floor (MIN_KEEP_DAYS) applies to every category, so the
  newest data is never a candidate;
* raw evidence is deleted only below the S3 archive frontier, i.e. only rows
  that already have a verified durable second copy. With no archive state,
  raw evidence is reported but refused;
* derived study rows are deleted only when finished (never ``pending``) and
  older than their retention;
* event kinds not listed in EVENT_POLICY are never touched;
* deletions run in bounded batches with a per-run row cap, and every batch
  is logged and returned in the result.

It is deliberately not wired into any scheduler: retention is a human
decision, run by hand after reviewing the dry-run report. After a large
confirmed run on PostgreSQL, schedule a VACUUM ANALYZE on the touched tables
outside traffic hours; autovacuum will otherwise reclaim space lazily.
"""
import argparse
import json
import logging
import os
import time
from sqlalchemy import select, func, delete, and_, or_
from .store import (Store, events, tm_studies, tm_checkpoints, swing_trials,
    swing_checkpoints, spy_checks, spy_options, spy_marks, discovery_trials,
    discovery_checks, smoothers_daily, flow_records)

LOG = logging.getLogger('uvicorn.error')

ARCHIVE_KEY = 'archive:schedule-v1'
MIN_KEEP_DAYS = 7
BATCH_ROWS = 5000
MAX_ROWS_PER_RUN = 1_000_000
DAY = 86400

# Event kinds are the volume: quotes dominate, chains are the next chunk.
# retention_days is how long a row stays in the live database; the S3 archive
# keeps the full history either way.
EVENT_POLICY = {
    'market_quotes': dict(kinds=('quote',), retention_days=14, requires_archive=True,
        basis='Raw quote samples. Studies consume bars and flow; quote-level forensics rarely needs more than two weeks live.'),
    'chain_snapshots': dict(kinds=('chain_archive',), retention_days=30, requires_archive=True,
        basis='Full option chains every 45 s per focus symbol. Highly redundant; a month covers research cycles.'),
    'vendor_snapshots': dict(kinds=('matrix_raw',), retention_days=90, requires_archive=True,
        basis='Raw TraderMatrix API responses, content-deduped per day. Vendor evidence for audits.'),
    'flow_prints': dict(kinds=('vendor_flow', 'flow', 'option_trade'), retention_days=180, requires_archive=True,
        basis='Option flow prints. Core research input; half a year live, full history in S3.'),
    'exposure_snapshots': dict(kinds=('exposure',), retention_days=90, requires_archive=True,
        basis='Per-refresh GEX exposure aggregates. Recomputable from chains while those live.'),
    'bars': dict(kinds=('bar',), retention_days=365, requires_archive=True,
        basis='OHLCV bars. Small and needed for backtests; a year live.'),
    'mappings': dict(kinds=('mapping',), retention_days=365, requires_archive=True,
        basis='Futures instrument mappings. Tiny; kept a year for lineage.'),
    'audit_trail': dict(kinds=('alert', 'signal', 'paper_decision', 'breakout_event',
        'gap_measurement', 'gap_continuation', 'tape_confirmation', 'apex_magnet_signal',
        'apex_magnet_outcome', 'swing_candidate', 'opportunity', 'opportunity_update',
        'discovery_decision', 'futures_assessment', 'research', 'daily', 'native_handoff',
        'native_target_check', 'native_route_revision', 'native_premium', 'native_config',
        'option_subscription', 'strategy_revision', 'secondary_review', 'secondary_error'),
        retention_days=365, requires_archive=True,
        basis='What the system decided and why. A year live; full history in S3.'),
    'delivery_receipts': dict(kinds=('alert_delivery',), retention_days=30, requires_archive=False,
        basis='Discord delivery confirmations. Derived from alerts; low value after a month.'),
}

# Derived aggregates: small, recomputable from raw evidence while it lives.
# Only finished rows are ever candidates; pending work is never pruned.
STUDY_POLICY = [
    dict(name='tm_flow_studies', table=tm_studies, time_col='assessed_at', status_col='status', retention_days=365),
    dict(name='tm_flow_checkpoints', table=tm_checkpoints, time_col='checked_at', status_col='status', retention_days=365),
    dict(name='swing_trials', table=swing_trials, time_col='assessed_at', status_col='status', retention_days=365),
    dict(name='swing_checkpoints', table=swing_checkpoints, time_col='checked_at', status_col='status', retention_days=365),
    dict(name='spy_checks', table=spy_checks, time_col='created', status_col=None, retention_days=365),
    dict(name='spy_options', table=spy_options, time_col='updated', status_col='status', retention_days=365),
    dict(name='spy_marks', table=spy_marks, time_col='processed_at', status_col=None, retention_days=365),
    dict(name='discovery_trials', table=discovery_trials, time_col='created', status_col='status', retention_days=90),
    dict(name='discovery_checks', table=discovery_checks, time_col='target_at', status_col='status', retention_days=90),
    dict(name='smoothers_daily', table=smoothers_daily, time_col='created', status_col='status', retention_days=365),
    dict(name='flow_records', table=flow_records, time_col='source_ts', status_col=None, retention_days=730),
]


def archive_coverage(db, c):
    """Return (frontier_id, frontier_ts): event ids at/below frontier_id have a
    verified S3 copy. (0, None) when the archive scheduler never ran."""
    progress = db.get(c, ARCHIVE_KEY, {}) or {}
    frontier = progress.get('frontier') or 0
    if not frontier:
        return 0, None
    ts = c.execute(select(events.c.ts).where(events.c.id == frontier)).scalar()
    return frontier, ts


def plan(db, now=None, event_policy=None, study_policy=None):
    """Dry-run plan: what would be deleted, what is held, and why."""
    now = time.time() if now is None else now
    event_policy = EVENT_POLICY if event_policy is None else event_policy
    study_policy = STUDY_POLICY if study_policy is None else study_policy
    categories = []
    with db.tx() as c:
        frontier, frontier_ts = archive_coverage(db, c)
        for name, spec in event_policy.items():
            days = max(spec['retention_days'], MIN_KEEP_DAYS)
            cutoff = now - days * DAY
            conds = [events.c.kind.in_(spec['kinds']), events.c.ts < cutoff]
            total = c.execute(select(func.count()).select_from(events).where(*conds)).scalar()
            if spec['requires_archive']:
                if frontier:
                    eligible = c.execute(select(func.count()).select_from(events)
                        .where(*conds, events.c.id <= frontier)).scalar()
                    guard = 'below_s3_frontier'
                else:
                    eligible, guard = 0, 'refused_no_archive'
            else:
                eligible, guard = total, 'derived_low_value'
            categories.append(dict(category=name, scope='events', kinds=list(spec['kinds']),
                retention_days=spec['retention_days'], cutoff_ts=cutoff,
                eligible_rows=eligible, held_rows=total - eligible, guard=guard,
                basis=spec['basis']))
        for spec in study_policy:
            table = spec['table']
            time_col = getattr(table.c, spec['time_col'])
            days = max(spec['retention_days'], MIN_KEEP_DAYS)
            cutoff = now - days * DAY
            conds = [time_col.isnot(None), time_col < cutoff]
            if spec['status_col'] is not None:
                conds.append(getattr(table.c, spec['status_col']) != 'pending')
            total = c.execute(select(func.count()).select_from(table).where(*conds)).scalar()
            if frontier_ts:
                eligible = c.execute(select(func.count()).select_from(table)
                    .where(*conds, time_col < frontier_ts)).scalar()
                guard = 'below_s3_frontier_ts'
            else:
                eligible, guard = 0, 'refused_no_archive'
            categories.append(dict(category=spec['name'], scope='study_table',
                table=table.name, retention_days=spec['retention_days'], cutoff_ts=cutoff,
                eligible_rows=eligible, held_rows=total - eligible, guard=guard,
                basis='Derived aggregate; finished rows only.'))
    return {'at': now, 'archive_frontier_id': frontier,
        'archive_frontier_ts': frontier_ts, 'categories': categories,
        'policies': {
            'events': {name: dict(kinds=list(spec['kinds']),
                requires_archive=spec['requires_archive']) for name, spec in event_policy.items()},
            'studies': {spec['name']: dict(table_name=spec['table'].name,
                time_col=spec['time_col'], status_col=spec['status_col'])
                for spec in study_policy},
        }}


_STUDY_TABLES = {t.name: t for t in (tm_studies, tm_checkpoints, swing_trials,
    swing_checkpoints, spy_checks, spy_options, spy_marks, discovery_trials,
    discovery_checks, smoothers_daily, flow_records)}


def _delete_batches(db, c, table, pk_cols, conds, batch, entry, log, budget):
    deleted = 0
    while budget > 0:
        requested = min(batch, budget)
        keys = c.execute(select(*pk_cols).where(*conds).limit(requested)).all()
        if not keys:
            break
        if len(pk_cols) == 1:
            stmt = delete(table).where(pk_cols[0].in_([k[0] for k in keys]))
        else:
            stmt = delete(table).where(or_(*[and_(*[col == k[i] for i, col in enumerate(pk_cols)])
                for k in keys]))
        c.execute(stmt)
        # len(keys) is the authoritative count; rowcount is not portable.
        deleted += len(keys)
        budget -= len(keys)
        record = {**entry, 'rows_deleted': len(keys), 'at': time.time()}
        log.append(record)
        LOG.info('Archive prune deleted: %s', json.dumps(record, sort_keys=True))
        if len(keys) < requested:
            break
    return deleted, budget


def execute(db, work, confirm=False, batch=BATCH_ROWS, max_rows=MAX_ROWS_PER_RUN):
    """Run a plan. Without confirm=True this is a dry-run: nothing is deleted
    and the result reports what would be deleted."""
    log = []
    if not confirm:
        return {'dry_run': True, 'at': work['at'], 'deleted_rows': 0,
            'would_delete_rows': sum(c['eligible_rows'] for c in work['categories']),
            'categories': [{**c, 'deleted_rows': 0, 'status': 'planned'} for c in work['categories']],
            'log': log}
    budget = max_rows
    results = []
    with db.tx() as c:
        frontier, frontier_ts = archive_coverage(db, c)
        for item in work['categories']:
            entry = dict(category=item['category'], scope=item['scope'], cutoff_ts=item['cutoff_ts'])
            if item['scope'] == 'events':
                spec = work['policies']['events'][item['category']]
                conds = [events.c.kind.in_(spec['kinds']), events.c.ts < item['cutoff_ts']]
                if spec['requires_archive']:
                    if not frontier:
                        results.append({**item, 'deleted_rows': 0, 'status': 'refused_no_archive'})
                        continue
                    conds.append(events.c.id <= frontier)
                deleted, budget = _delete_batches(db, c, events, [events.c.id], conds,
                    batch, entry, log, budget)
            else:
                spec = work['policies']['studies'][item['category']]
                table = _STUDY_TABLES[spec['table_name']]
                time_col = getattr(table.c, spec['time_col'])
                conds = [time_col.isnot(None), time_col < item['cutoff_ts']]
                if spec['status_col'] is not None:
                    conds.append(getattr(table.c, spec['status_col']) != 'pending')
                if not frontier_ts:
                    results.append({**item, 'deleted_rows': 0, 'status': 'refused_no_archive'})
                    continue
                conds.append(time_col < frontier_ts)
                pk_cols = list(table.primary_key.columns)
                deleted, budget = _delete_batches(db, c, table, pk_cols, conds,
                    batch, entry, log, budget)
            status = 'deleted' if deleted else 'nothing_eligible'
            if budget <= 0 and deleted:
                status = 'capped'
            results.append({**item, 'deleted_rows': deleted, 'status': status})
            if budget <= 0:
                break
    return {'dry_run': False, 'at': time.time(), 'deleted_rows': sum(r['deleted_rows'] for r in results),
        'categories': results, 'log': log}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm', action='store_true',
        help='Actually delete. Without this flag only a dry-run report is printed.')
    parser.add_argument('--batch', type=int, default=BATCH_ROWS)
    parser.add_argument('--max-rows', type=int, default=MAX_ROWS_PER_RUN)
    args = parser.parse_args()
    if not os.environ.get('DATABASE_URL'):
        raise ValueError('DATABASE_URL is required')
    db = Store(os.environ['DATABASE_URL'])
    try:
        work = plan(db)
        result = execute(db, work, confirm=args.confirm, batch=args.batch, max_rows=args.max_rows)
    finally:
        db.engine.dispose()
    print(json.dumps(result, sort_keys=True, default=str))


if __name__ == '__main__':
    main()
