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
from .feed_loop import FeedLoop
from .morning_schema.models import Event, Signal, StockBar
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
    if stream == 'stock': return ingest_stock(db, c, row, now)
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


def ingest_stock(db, c, row, now):
    payload = row['payload']
    if not isinstance(payload, dict) or set(payload) != {'schema_version','signal','initial_received_at_ms','bars'} or payload['schema_version'] != 1:
        raise ValueError('Invalid stock inventory schema')
    signal = Signal.model_validate(payload['signal'])
    if signal.is_test or signal.signal_id != row['id'] or signal.signal_at_ms > row['received_at_ms'] + 60000:
        raise ValueError('Invalid stock inventory identity')
    initial = payload['initial_received_at_ms']
    if initial is not None and (type(initial) is not int or not 0 < initial <= (now+60)*1000):
        raise ValueError('Invalid initial source receipt')
    bars = payload['bars']
    if not isinstance(bars, list) or len(bars)>180: raise ValueError('Invalid stock candle inventory')
    values, previous = [], 0
    sid = signal.signal_id
    for item in bars:
        if not isinstance(item,dict) or set(item) != {'payload','sha256','received_at_ms'}:
            raise ValueError('Invalid stock candle envelope')
        b = StockBar.model_validate(item['payload']); received=item['received_at_ms']
        if type(received) is not int or not 0 < received <= (now+60)*1000 or b.open_at_ms+60000 > received+60000:
            raise ValueError('Invalid stock candle receipt')
        offset=b.open_at_ms-signal.signal_at_ms
        if not 0 <= offset < 180*60000 or offset%60000 or b.open_at_ms<=previous:
            raise ValueError('Invalid stock candle order or bounds')
        if digest(item['payload']) != item['sha256']: raise ValueError('Stock candle checksum mismatch')
        previous=b.open_at_ms
        values.append(dict(key=identity('signal_bar',sid,str(b.open_at_ms)),kind='signal_bar',parent=sid,
            source_id=str(b.open_at_ms),source_received=received/1000,imported_at=now,
            sha256=item['sha256'],payload=item['payload']))
    save(db,c,'signal',sid,sid,payload['signal'],row['received_at_ms']/1000,now)
    if values:
        # One bounded batch; conflicts never replace existing candle prices.
        q=db.insert(history).values(values)
        c.execute(q.on_conflict_do_nothing(index_elements=['key']))
        stored={r.key:r for r in c.execute(select(history.c.key,history.c.sha256,history.c.source_received)
            .where(history.c.key.in_([v['key'] for v in values])))}
        for value in values:
            saved=stored[value['key']]
            if saved.sha256!=value['sha256']:raise ValueError('Source history identity conflict: signal_bar')
            if saved.source_received>value['source_received']:
                c.execute(history.update().where(history.c.key==value['key'],history.c.source_received>value['source_received'])
                    .values(source_received=value['source_received']))
    key=identity('stock_inventory',sid,sid)
    old=c.execute(select(history.c.sha256).where(history.c.key==key)).scalar_one_or_none()
    q=db.insert(history).values(key=key,kind='stock_inventory',parent=sid,source_id=sid,
        source_received=row['received_at_ms']/1000,imported_at=now,sha256=row['sha256'],payload=row)
    # The latest complete source inventory may grow; individual candle rows stay immutable.
    updates={'imported_at':now}
    if old!=row['sha256']:updates.update(payload=row,sha256=row['sha256'])
    c.execute(q.on_conflict_do_update(index_elements=['key'],set_=updates))
    return old != row['sha256']


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
        status = {k:previous[k] for k in ('failures','last_failure','recovered_at') if k in previous}
        if previous.get('consecutive_failures'):status['recovered_at']=now
        status.update({'at': now, 'next': next_page or '', 'status': 'scanning' if next_page else 'scan_complete',
            'last_complete_at': previous.get('last_complete_at') if next_page else now,
            'pages': previous.get('pages', 0) + 1, 'last_page_records': len(rows), 'last_page_new': changed,
            'last_success_at':now,'consecutive_failures':0,'retry_after':0,
            'scope': 'All available non-test source history; repeated scans recover late arrivals'})
        if next_page is None:
            status['import_counts'] = dict(c.execute(select(history.c.kind, func.count()).group_by(history.c.kind)).all())
        db.put(c, 'morning_history:' + stream, status)
    return status


async def poll_page(db, cfg, client, stream):
    def read_cursor():
        with db.tx() as c:return db.get(c, 'morning_history:' + stream, {})
    saved = await asyncio.to_thread(read_cursor)
    after = saved.get('next', '')
    requested = time.monotonic()
    async with client.stream('GET', cfg.morning_url.rstrip('/') + '/api/compass/history',
            params={'stream': stream, 'since_ms': 0, 'after': after, 'limit': 20},
            headers={'Authorization': 'Bearer ' + cfg.morning_token}) as response:
        if response.status_code != 200: raise ValueError('Source history HTTP ' + str(response.status_code))
        chunks, size = [], 0
        async for part in response.aiter_bytes():
            size += len(part)
            if size > 8 * 1024 * 1024: raise ValueError('History page exceeds limit')
            chunks.append(part)
    fetched = time.monotonic()
    def apply():
        return accept_page(db, stream, json.loads(b''.join(chunks)), after, time.time())
    status = await asyncio.to_thread(apply)
    LOG.info('Morning history: stream=%s status=%s records=%s new=%s request_seconds=%.3f apply_seconds=%.3f',
        stream, status['status'], status['last_page_records'], status['last_page_new'],
        fetched-requested, time.monotonic()-fetched)
    if status['status'] == 'scan_complete':
        LOG.info('Morning history totals: %s', json.dumps(status['import_counts'], sort_keys=True))
    if stream == 'stock' and status['status'] == 'scan_complete':
        def audit():
            from .morning_report import build_report
            with db.tx() as c:
                report=build_report(db,c,time.time())
                proof={**report['summary'],'at':report['as_of_ms']/1000,'truncated':report['truncated']}
                db.put(c,'morning_native_reconciliation:status',proof)
            LOG.info('Morning native reconciliation: %s',json.dumps(proof,sort_keys=True))
        try: await asyncio.to_thread(audit)
        except Exception as exc: LOG.error('Morning native reconciliation audit failed: %s',type(exc).__name__)
    return status


def record_failure(db,stream,error,now):
    """Keep the cursor and last success; never turn a transport failure into an empty page."""
    with db.tx() as c:
        saved=db.locked_get(c,'morning_history:'+stream,{})
        consecutive=saved.get('consecutive_failures',0)+1
        failure={'at':now,'type':type(error).__name__,
            'category':'transport' if isinstance(error,httpx.TransportError) else 'protocol_or_storage',
            'cursor':saved.get('next','')}
        db.put(c,'morning_history:'+stream,{**saved,'at':now,'status':'error',
            'error':type(error).__name__,'last_failure':failure,
            'failures':saved.get('failures',0)+1,'consecutive_failures':consecutive,
            'retry_after':now+min(60,10*2**min(consecutive-1,3)),
            'note':'Page not accepted; cursor retained; bounded retry scheduled'})
    LOG.warning('Morning history retry: stream=%s type=%s category=%s cursor_retained=true',stream,failure['type'],failure['category'])


async def poll_with_retry(db,cfg,client,stream):
    try:
        return await poll_page(db,cfg,client,stream)
    except httpx.TransportError as error:
        await asyncio.to_thread(record_failure,db,stream,error,time.time())
        # A single reconnect/read retry uses the same committed cursor. Schema,
        # identity and storage failures are never treated as transient HTTP errors.
        await asyncio.sleep(1)
        return await poll_page(db,cfg,client,stream)


async def run(db, cfg):
    if not cfg.morning_url or not cfg.morning_token: return
    # HTTP connections and their deadlines must progress independently of
    # synchronous work elsewhere in the collector's event loop.
    feed = FeedLoop('morning-history')
    try:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(run_import(db,cfg),feed.loop))
    finally:
        await asyncio.to_thread(feed.close)


async def run_import(db, cfg):
    owner = uuid.uuid4().hex
    def claim(stream):
        with db.tx() as c:
            active = db.lease(c, 'morning-history-' + stream, owner, 120)
            saved = db.get(c, 'morning_history:' + stream, {})
        return active,saved
    LOG.info('Morning history worker: isolated_loop=true page_limit=20 batch_pages=5')
    # Construct and close this client on the import loop; never borrow the
    # collector's client. Preserve the existing timeouts and bounded retries.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4), follow_redirects=False) as client:
        while True:
            for stream in ('stock', 'events', 'research'):
                try:
                    active,saved = await asyncio.to_thread(claim,stream)
                    if not active or time.time()<saved.get('retry_after',0) or (not saved.get('next') and time.time() - saved.get('last_complete_at', 0) < 60): continue
                    for _ in range(5):
                        status = await poll_with_retry(db, cfg, client, stream)
                        if status['status'] == 'scan_complete': break
                except asyncio.CancelledError: raise
                except Exception as exc:
                    LOG.error('Morning history import failed: stream=%s type=%s detail=%s', stream, type(exc).__name__,
                        str(exc)[:150] if isinstance(exc, ValueError) and not hasattr(exc, 'errors') else 'See source protocol or connectivity')
                    try:
                        await asyncio.to_thread(record_failure,db,stream,exc,time.time())
                    except Exception:
                        LOG.error('Morning history status unavailable; retry scheduled')
            await asyncio.sleep(10)


def snapshot(db, c, now):
    streams = {s: db.get(c, 'morning_history:' + s, {'status': 'awaiting_import'}) for s in ('stock', 'events', 'research')}
    for saved in streams.values():
        if saved.get('at') and now - saved['at'] > 180: saved['status'] = 'stale'
    counts = dict(c.execute(select(history.c.kind, func.count()).group_by(history.c.kind)).all())
    return {'streams': streams, 'counts': counts, 'cutover_ready': False,
        'basis': 'Original research batches and native candles. Import completeness is not candle coverage or live forward evidence.'}
