"""Bounded database accounting. Database bytes are NOT filesystem free space."""
import asyncio
import json
import logging
import time
import uuid
from sqlalchemy import text

LOG = logging.getLogger('uvicorn.error')
KEY = 'storage:report'


def assess(database_bytes, wal_bytes, budget_gb, history, now):
    measured = database_bytes + (wal_bytes or 0)
    budget = budget_gb * 1_000_000_000 if budget_gb > 0 else None
    ratio = measured / budget if budget else None
    old = next((x for x in history if now - 86400 <= x['at'] <= now - 3600), None)
    growth = max(0, (measured - old['bytes']) / (now - old['at'])) if old else None
    runway = max(0, budget - measured) / growth / 3600 if budget and growth else None
    status = ('capacity_unknown' if ratio is None else 'critical' if ratio >= .95 else
              'high' if ratio >= .85 else 'warning' if ratio >= .70 else 'below_threshold')
    if runway is not None and runway < 24 and status == 'below_threshold': status = 'warning'
    return {'at': now, 'status': status, 'database_bytes': database_bytes,
        'wal_bytes': wal_bytes, 'accounted_bytes': measured, 'budget_gb': budget_gb or None,
        'budget_fraction': ratio, 'growth_bytes_per_hour': growth * 3600 if growth is not None else None,
        'hours_to_budget_at_observed_rate': runway,
        'basis': 'Current database plus accessible WAL; excludes other databases and filesystem overhead. Budget is configured, not measured volume capacity.',
        'filesystem_free_bytes': None, 'automatic_deletion_enabled': False,
        'archive_status': 'export_tool_available_destination_not_verified'}


def capture(db, cfg, owner, now):
    with db.tx() as c:
        if not db.lease(c, 'storage-health', owner, 60): return
        if c.dialect.name != 'postgresql':
            report = {'at': now, 'status': 'unsupported', 'reason': 'PostgreSQL accounting required'}
        else:
            c.execute(text("SET LOCAL statement_timeout = '5s'"))
            database_bytes = c.execute(text('SELECT pg_database_size(current_database())')).scalar_one()
            tables = [dict(r) for r in c.execute(text('''SELECT relname AS name,
                pg_total_relation_size(relid) AS total_bytes,
                pg_indexes_size(relid) AS index_bytes, n_live_tup AS estimated_rows,
                n_dead_tup AS estimated_dead_rows
                FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 15''')).mappings()]
            wal_bytes = None
            try:
                with c.begin_nested():
                    wal_bytes = c.execute(text('SELECT COALESCE(sum(size),0) FROM pg_ls_waldir()')).scalar_one()
            except Exception:
                pass  # Missing permission is explicit; never abort database accounting.
            history = db.get(c, 'storage:samples', [])
            report = assess(int(database_bytes), int(wal_bytes) if wal_bytes is not None else None,
                            cfg.storage_budget_gb, history, now)
            report['tables'] = tables
            samples = [r for r in history if r['at'] >= now - 86400]
            samples.append({'at': now, 'bytes': report['accounted_bytes']})
            db.put(c, 'storage:samples', samples[-289:])
        db.put(c, KEY, report)
    db.health('storage', report['status'], report.get('basis', report.get('reason')), now)
    LOG.info('Storage accounting: %s', json.dumps(report, sort_keys=True))
    return report


def snapshot(db, c, now):
    saved = db.get(c, KEY, {'status': 'awaiting_measurement'})
    return {**saved, 'status': 'stale' if saved.get('at') and now - saved['at'] > 900 else saved['status']}


async def run(db, cfg):
    owner = uuid.uuid4().hex
    while True:
        try: await asyncio.to_thread(capture, db, cfg, owner, time.time())
        except asyncio.CancelledError: raise
        except Exception as exc:
            LOG.error('Storage accounting unavailable: %s; database connectivity or capacity needs attention', type(exc).__name__)
        await asyncio.sleep(300)
