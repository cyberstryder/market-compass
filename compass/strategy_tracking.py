"""Versioned source ledger and independent Morning checkpoints; no alert/order writer."""
import asyncio
import logging
import time
import uuid
from sqlalchemy import Column, Float, Index, JSON, String, Table, select, func
from .store import meta, identity, events
from .market import fresh, number

VERSION = 'morning-shadow-v1'
HORIZONS = (5, 15, 30, 60)
ledger = Table('strategy_tracking_v1', meta,
    Column('key', String(64), primary_key=True),
    Column('project', String(24), nullable=False),
    Column('source_id', String(1000), nullable=False),
    Column('source_ts', Float, nullable=False),
    Column('registered_at', Float, nullable=False),
    Column('frozen', JSON, nullable=False),
    Column('latest', JSON, nullable=False),
    Column('measurements', JSON, nullable=False))
Index('strategy_tracking_project_time', ledger.c.project, ledger.c.source_ts)


def register(db, c, project, row, now, refresh=True):
    key = identity(project, row['id'])
    q = db.insert(ledger).values(key=key, project=project, source_id=row['id'],
        source_ts=row['source_ts'], registered_at=now, frozen=row, latest=row, measurements={})
    if refresh:
        c.execute(q.on_conflict_do_update(index_elements=['key'], set_={'latest': row}))
    elif c.execute(q.on_conflict_do_nothing(index_elements=['key']).returning(ledger.c.key)).first() is None:
        return
    db.append(c, 'strategy_revision', project, row['symbol'], row['source_ts'],
        {'source_id': row['id'], 'revision': identity(row), 'observed_at': now, 'payload': row},
        key='strategy-revision:' + identity(project, row))


def measure(c, record, horizon, now):
    due = record['source_ts'] + horizon * 60
    if now < due + 10:
        return None
    base = {'horizon_min': horizon, 'due_at': due, 'version': VERSION,
            'basis': 'Source entry to retained underlying midpoint; not a candle close or fill'}
    if record['registered_at'] > due:
        return {**base, 'status': 'not_observed', 'reason': 'registered_after_checkpoint'}
    original = record['frozen']
    entry = number(original.get('entry'))
    if not entry or entry <= 0 or original.get('side') != 'long':
        return {**base, 'status': 'not_observed', 'reason': 'unsupported_entry_or_direction'}
    # Fixed window and recorded-at deadline make restarts deterministic. Later
    # backfills cannot manufacture a live checkpoint. At most five seconds late.
    candidates = c.execute(select(events.c.ts, events.c.received, events.c.payload)
        .where(events.c.kind == 'quote', events.c.symbol == original['symbol'],
               events.c.ts >= due, events.c.ts <= due + 5,
               events.c.received <= due + 10)
        .order_by(events.c.ts, events.c.id)).all()
    for q in candidates:
        if q.payload.get('ts') != q.ts or q.received < q.ts or not fresh(q.payload, q.received):
            continue
        mid = (q.payload['bid'] + q.payload['ask']) / 2
        return {**base, 'status': 'observed', 'quote_ts': q.ts, 'recorded_at': q.received,
                'price': mid, 'return_pct': (mid / entry - 1) * 100}
    return {**base, 'status': 'not_observed', 'reason': 'no_retained_quote_in_checkpoint_window'}


def comparison(record, horizon):
    measurement = record['measurements'].get(str(horizon))
    sources = [q for q in record['latest'].get('checkpoints', []) if q.get('horizon_min') == horizon]
    source = max(sources, key=lambda q: q.get('observed_at_ms', 0), default=None)
    frozen, latest = record['frozen'], record['latest']
    changed = [k for k in ('entry', 'side', 'version', 'stream') if frozen.get(k) != latest.get(k)]
    if (frozen.get('original') or {}).get('settings') != (latest.get('original') or {}).get('settings'):
        changed.append('settings')
    result = {'horizon_min': horizon, 'compass': measurement, 'source': source,
              'changed_entry_fields': changed, 'return_difference_pp': None}
    if changed:
        return {**result, 'status': 'source_entry_changed'}
    if not measurement:
        return {**result, 'status': 'pending'}
    if measurement['status'] != 'observed':
        return {**result, 'status': 'compass_observation_missing'}
    if source is None:
        return {**result, 'status': 'source_checkpoint_missing'}
    stamp = number(source.get('observed_at_ms'))
    price, entry = number(source.get('price')), number(frozen.get('entry'))
    if not stamp or abs(stamp / 1000 - measurement['due_at']) > 5 or source.get('continuous') is not True:
        return {**result, 'status': 'source_timing_or_coverage_mismatch'}
    if not price or price <= 0 or not entry or entry <= 0:
        return {**result, 'status': 'invalid_source_price'}
    return {**result, 'status': 'paired_different_price_basis',
            'source_return_pct': (price / entry - 1) * 100,
            'return_difference_pp': measurement['return_pct'] - (price / entry - 1) * 100}


def tick(db, owner, now):
    from .projects import records
    with db.tx() as c:
        if not db.lease(c, 'strategy-tracking', owner, 30):
            return
        activation = db.get(c, 'strategy_tracking:activation')
        if not activation:
            activation = {'at': now, 'version': VERSION}
            db.put(c, 'strategy_tracking:activation', activation)
        # One pass through pre-existing records; historical registration is
        # explicit, not a reconstructed forward test. Live ingest handles revisions.
        cursor = db.get(c, 'strategy_tracking:bootstrap_cursor', '')
        if cursor != 'complete':
            rows = c.execute(select(records).where(records.c.key > cursor)
                .order_by(records.c.key).limit(200)).mappings().all()
            for r in rows:
                register(db, c, r['project'], r['payload'], now, refresh=False)
            db.put(c, 'strategy_tracking:bootstrap_cursor', rows[-1]['key'] if len(rows) == 200 else 'complete')
        # JSON completion filter avoids repeatedly processing old, finalized history.
        rows = c.execute(select(ledger).where(ledger.c.project == 'morning',
            ledger.c.measurements['complete'].as_boolean().is_(None))
            .order_by(ledger.c.source_ts).limit(200)).mappings().all()
        for r in rows:
            measurements = dict(r['measurements'])
            for h in HORIZONS:
                if str(h) not in measurements:
                    point = measure(c, r, h, now)
                    if point: measurements[str(h)] = point
            if all(str(h) in measurements for h in HORIZONS): measurements['complete'] = True
            c.execute(ledger.update().where(ledger.c.key == r['key']).values(measurements=measurements))
        previous = db.get(c, 'strategy_tracking:status', {})
        if int(previous.get('at', 0) // 60) != int(now // 60):
            logging.getLogger('uvicorn.error').info('Strategy tracking shadow: version=%s processed=%s orders=false notifications=false', VERSION, len(rows))
        db.put(c, 'strategy_tracking:status', {'at': now, 'version': VERSION,
            'mode': 'shadow', 'notifications_enabled': False, 'orders_enabled': False,
            'bootstrap_complete': db.get(c, 'strategy_tracking:bootstrap_cursor') == 'complete'})


def snapshot(db, c):
    rows = c.execute(select(ledger).where(ledger.c.project == 'morning')
        .order_by(ledger.c.source_ts.desc()).limit(100)).mappings().all()
    counts = {p: n for p, n in c.execute(select(ledger.c.project, func.count()).group_by(ledger.c.project))}
    return {'status': db.get(c, 'strategy_tracking:status', {'mode': 'awaiting_worker'}),
            'activation': db.get(c, 'strategy_tracking:activation'), 'counts': counts,
            'cutover_ready': False,
            'scope': 'Shared source ledger and Morning checkpoint shadow. Signal generation, options and notifications remain with originals.',
            'rows': [{'source_id': r['source_id'], 'symbol': r['frozen']['symbol'],
                'source_ts': r['source_ts'], 'version': r['frozen'].get('version'),
                'stream': r['frozen'].get('stream'), 'registered_at': r['registered_at'],
                'checkpoints': [comparison(r, h) for h in HORIZONS]} for r in rows]}


async def run(db):
    owner = uuid.uuid4().hex
    while True:
        try:
            await asyncio.to_thread(tick, db, owner, time.time())
        except Exception as exc:
            logging.exception('Strategy tracking worker failed')
            db.health('strategy_tracking', 'error', type(exc).__name__)
        await asyncio.sleep(5)
