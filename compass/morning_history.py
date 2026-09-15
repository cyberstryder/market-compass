"""Read-only source history mirror. Imported candles are never live quotes."""
import asyncio
import hashlib
import json
import logging
import time
import uuid
import httpx
from sqlalchemy import Column, Float, Index, JSON, String, Table, select, func
from .store import meta, identity
from .morning_schema.models import Event
from .morning_schema.candidate_models import ResearchBatch

LOG = logging.getLogger('uvicorn.error')
history = Table('morning_history_v1', meta,
    Column('key', String(64), primary_key=True), Column('kind', String(32), nullable=False),
    Column('parent', String(550), nullable=False), Column('source_id', String(550), nullable=False),
    Column('source_received', Float, nullable=False), Column('imported_at', Float, nullable=False),
    Column('sha256', String(64), nullable=False), Column('payload', JSON, nullable=False))
Index('morning_history_parent', history.c.kind, history.c.parent)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def save(db, c, kind, parent, source_id, payload, received, now):
    key, checksum = identity(kind, parent, source_id), digest(payload)
    inserted = c.execute(db.insert(history).values(key=key, kind=kind, parent=parent,
        source_id=source_id, source_received=received, imported_at=now, sha256=checksum, payload=payload)
        .on_conflict_do_nothing(index_elements=['key']).returning(history.c.key)).first() is not None
    existing = c.execute(select(history.c.sha256).where(history.c.key == key)).scalar_one()
    if existing != checksum:
        raise ValueError('Source history identity conflict: ' + kind)
    # A later imported batch can contain an earlier source receipt of the same candle.
    c.execute(history.update().where(history.c.key == key, history.c.source_received > received)
        .values(source_received=received))
    return inserted


def ingest(db, c, stream, row, now):
    if not isinstance(row, dict) or set(row) != {'id', 'received_at_ms', 'sha256', 'payload'}:
        raise ValueError('Invalid history envelope')
    payload, received = row['payload'], row['received_at_ms']
    if type(received) is not int or not 0 < received <= (now + 60) * 1000:
        raise ValueError('Invalid source receipt clock')
    if digest(payload) != row['sha256']:
        raise ValueError('Source checksum mismatch')
    if stream not in {'events', 'research'}: raise ValueError('Invalid history stream')
    parsed = (Event if stream == 'events' else ResearchBatch).model_validate(payload)
    if parsed.event_id != row['id']: raise ValueError('Envelope identity mismatch')
    research = stream == 'research'
    parent = payload['session' if research else 'signal']
    parent_id = parent['session_id' if research else 'signal_id']
    stamp = payload['observed_at_ms'] if research else (payload.get('observation') or {}).get('observed_at_ms', parent['signal_at_ms'])
    if parent.get('is_test') or stamp > received + 60000:
        raise ValueError('Test or future source history')
    changed = save(db, c, stream, parent_id, row['id'], row, received / 1000, now)
    if not changed: return False  # Children were committed atomically with this envelope.
    save(db, c, 'session' if research else 'signal', parent_id, parent_id, parent, received / 1000, now)
    for bar in payload.get('bars' if research else 'stock_bars') or []:
        save(db, c, 'research_bar' if research else 'signal_bar', parent_id, str(bar['open_at_ms']), bar, received / 1000, now)
    for candidate in payload.get('records', []):
        save(db, c, 'candidate', parent_id, candidate['record_id'], candidate, received / 1000, now)
    return changed


def accept_page(db, stream, payload, after, now):
    if (not isinstance(payload, dict) or payload.get('schema_version') != 1 or
            payload.get('project') != 'morning' or payload.get('mode') != 'observe_only' or
            payload.get('stream') != stream or payload.get('since_ms') != 0):
        raise ValueError('Unexpected history schema or scope')
    rows = payload.get('records')
    if not isinstance(rows, list) or len(rows) > 20: raise ValueError('Invalid history page')
    ids = [r.get('id') for r in rows if isinstance(r, dict)]
    if len(ids) != len(rows) or any(not isinstance(i, str) or not after < i for i in ids) or ids != sorted(set(ids)):
        raise ValueError('Invalid history ordering')
    next_page = payload.get('next')
    if next_page is not None and (not ids or next_page != ids[-1]):
        raise ValueError('Invalid history continuation')
    with db.tx() as c:
        changed = sum(ingest(db, c, stream, row, now) for row in rows)
        previous = db.get(c, 'morning_history:' + stream, {})
        status = {'at': now, 'next': next_page or '', 'status': 'scanning' if next_page else 'scan_complete',
            'last_complete_at': previous.get('last_complete_at') if next_page else now,
            'pages': previous.get('pages', 0) + 1, 'last_page_records': len(rows), 'last_page_new': changed,
            'scope': 'All available non-test source history; repeated scans recover late arrivals'}
        if next_page is None:
            status['import_counts'] = dict(c.execute(select(history.c.kind, func.count()).group_by(history.c.kind)).all())
        db.put(c, 'morning_history:' + stream, status)
    LOG.info('Morning history: stream=%s status=%s records=%s new=%s', stream, status['status'], len(rows), changed)
    if next_page is None: LOG.info('Morning history totals: %s', json.dumps(status['import_counts'], sort_keys=True))
    return status


async def poll_page(db, cfg, client, stream):
    with db.tx() as c: saved = db.get(c, 'morning_history:' + stream, {})
    after = saved.get('next', '')
    async with client.stream('GET', cfg.morning_url.rstrip('/') + '/api/compass/history',
            params={'stream': stream, 'since_ms': 0, 'after': after, 'limit': 20},
            headers={'Authorization': 'Bearer ' + cfg.morning_token}) as response:
        if response.status_code != 200: raise ValueError('Source history HTTP ' + str(response.status_code))
        chunks, size = [], 0
        async for part in response.aiter_bytes():
            size += len(part)
            if size > 8 * 1024 * 1024: raise ValueError('History page exceeds limit')
            chunks.append(part)
    return await asyncio.to_thread(accept_page, db, stream, json.loads(b''.join(chunks)), after, time.time())


async def run(db, cfg):
    if not cfg.morning_url or not cfg.morning_token: return
    owner = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4), follow_redirects=False) as client:
        while True:
            for stream in ('events', 'research'):
                try:
                    with db.tx() as c:
                        active = db.lease(c, 'morning-history-' + stream, owner, 120)
                        saved = db.get(c, 'morning_history:' + stream, {})
                    if not active or (not saved.get('next') and time.time() - saved.get('last_complete_at', 0) < 60): continue
                    for _ in range(5):
                        status = await poll_page(db, cfg, client, stream)
                        if status['status'] == 'scan_complete': break
                except asyncio.CancelledError: raise
                except Exception as exc:
                    LOG.error('Morning history import failed: stream=%s type=%s detail=%s', stream, type(exc).__name__,
                        str(exc)[:150] if isinstance(exc, ValueError) and not hasattr(exc, 'errors') else 'See source protocol or connectivity')
                    try:
                        with db.tx() as c:
                            saved = db.get(c, 'morning_history:' + stream, {})
                            db.put(c, 'morning_history:' + stream, {**saved, 'at': time.time(), 'status': 'error',
                                'error': type(exc).__name__, 'note': 'Page rolled back; cursor retained; retry scheduled'})
                    except Exception:
                        LOG.error('Morning history status unavailable; retry scheduled')
            await asyncio.sleep(10)


def snapshot(db, c, now):
    streams = {s: db.get(c, 'morning_history:' + s, {'status': 'awaiting_import'}) for s in ('events', 'research')}
    for saved in streams.values():
        if saved.get('at') and now - saved['at'] > 180: saved['status'] = 'stale'
    counts = dict(c.execute(select(history.c.kind, func.count()).group_by(history.c.kind)).all())
    return {'streams': streams, 'counts': counts, 'cutover_ready': False,
        'basis': 'Original research batches and native candles. Import completeness is not candle coverage or live forward evidence.'}
