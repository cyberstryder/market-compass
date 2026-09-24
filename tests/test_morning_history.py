import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select, func
from compass.store import Store, events
from compass.morning_history import accept_page, digest, history, poll_page, snapshot

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/morning_history.json').read_text())
NOW = 1789000000.

def envelope(payload):
    return {'id': payload['event_id'], 'payload': copy.deepcopy(payload), 'sha256': digest(payload),
        'received_at_ms': int((NOW - 5) * 1000)}

def page(stream, payloads, next_page=None):
    return {'schema_version': 1, 'project': 'morning', 'mode': 'observe_only', 'stream': stream,
        'since_ms': 0, 'records': sorted([envelope(x) for x in payloads], key=lambda r:r['id']), 'next': next_page}

@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'mirror.db')); db.initialize()
    yield db
    db.engine.dispose()

def test_lossless_roundtrip_dedup_native_candles_and_all_research_kinds(db):
    p = page('events', [FIXTURE['signal'], FIXTURE['checkpoint']])
    accept_page(db, 'events', p, '', NOW)
    accept_page(db, 'events', p, '', NOW + 1)
    accept_page(db, 'research', page('research', [FIXTURE['research']]), '', NOW)
    with db.tx() as c:
        report = snapshot(db, c, NOW)
        assert report['counts'] == {'events': 2, 'signal': 1, 'signal_bar': 60, 'research': 1,
            'session': 1, 'research_bar': 6, 'candidate': 5}
        row = c.execute(select(history).where(history.c.kind=='events', history.c.source_id==FIXTURE['checkpoint']['event_id'])).mappings().one()
        assert row['payload'] == envelope(FIXTURE['checkpoint'])
        assert row['imported_at'] == NOW and row['source_received'] == NOW - 5
        assert c.execute(select(func.count()).select_from(events)).scalar_one() == 0
        assert report['cutover_ready'] is False
        assert snapshot(db,c,NOW+182)['streams']['events']['status'] == 'stale'

def test_bad_checksum_or_candle_rolls_back_whole_page_and_cursor(db):
    valid = page('events', [FIXTURE['signal']])
    valid['next'] = valid['records'][-1]['id']
    accept_page(db, 'events', valid, '', NOW)
    bad = page('events', [FIXTURE['checkpoint']]); bad['records'][0]['payload']['stock_bars'][0]['close'] = 999.
    with pytest.raises(ValueError): accept_page(db, 'events', bad, '', NOW)
    bad['records'][0]['sha256'] = digest(bad['records'][0]['payload'])
    with pytest.raises(ValueError): accept_page(db, 'events', bad, '', NOW)
    with db.tx() as c:
        assert snapshot(db,c,NOW)['counts'] == {'events':1, 'signal':1}
        assert db.get(c,'morning_history:events')['next'] == FIXTURE['signal']['event_id']

def test_cross_batch_candle_conflict_is_rejected_without_partial_import(db):
    batch = copy.deepcopy(FIXTURE['research'])
    accept_page(db,'research',page('research',[batch]),'',NOW)
    batch['part']=2; batch['event_id']=batch['event_id'][:-1]+'2'; batch['bars'][0]['high']+=1.
    with pytest.raises(ValueError, match='identity conflict'):
        accept_page(db,'research',page('research',[batch]),'',NOW+1)
    with db.tx() as c: assert snapshot(db,c,NOW)['counts']['research']==1

def test_poller_auth_restart_cursor_and_late_lower_identity(db):
    cfg=SimpleNamespace(morning_url='https://source.example',morning_token='scoped-token')
    first=page('events',[FIXTURE['checkpoint']]); first['next']=first['records'][-1]['id']
    tail=page('events',[FIXTURE['signal']])
    calls=[]
    def handler(request):
        assert request.headers['authorization']=='Bearer scoped-token'
        calls.append(request.url.params['after'])
        return httpx.Response(200,json=tail if request.url.params['after'] else first)
    async def scan():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await poll_page(db,cfg,client,'events')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await poll_page(db,cfg,client,'events')
    asyncio.run(scan())
    assert calls==['',first['next']]
    late=copy.deepcopy(FIXTURE['signal']); late['signal']['signal_id']='a-late';late['event_id']='a-late-signal'
    accept_page(db,'events',page('events',[late,FIXTURE['checkpoint'],FIXTURE['signal']]),'',NOW+2)
    with db.tx() as c: assert snapshot(db,c,NOW+2)['counts']['events']==3

@pytest.mark.parametrize('mutation',[lambda p:p.update(next='invalid'),lambda p:p.update(since_ms=1),
    lambda p:p['records'].append(p['records'][0]),lambda p:p['records'][0].update(received_at_ms=True)])
def test_malformed_pages_do_not_advance(db, mutation):
    p=page('events',[FIXTURE['signal']]);mutation(p)
    with pytest.raises(ValueError):accept_page(db,'events',p,'',NOW)
    with db.tx() as c: assert db.get(c,'morning_history:events') is None


def test_import_commits_saved_cursor_while_collector_loop_is_blocked(db,monkeypatch):
    from compass import morning_history as importer
    cfg=SimpleNamespace(morning_url='https://source.example',morning_token='scoped-token')
    with db.tx() as c:db.put(c,'morning_history:stock',{'next':'a-saved-position'})
    # Use the stock stream's existing complete-inventory fixture.
    stock=json.loads((Path(__file__).parent/'fixtures/morning_frame_reconciliation.json').read_text())['stock']
    stock['next']=stock['records'][-1]['id']
    applied=threading.Event();loops={};requests=[]
    original_client=httpx.AsyncClient;original_accept=importer.accept_page
    async def handler(request):
        await asyncio.sleep(.05)
        requests.append(request)
        loops['request']=asyncio.get_running_loop()
        assert request.url.params['after']=='a-saved-position'
        assert request.url.params['limit']=='20' and request.url.params['since_ms']=='0'
        assert request.headers['authorization']=='Bearer scoped-token'
        return httpx.Response(200,json=stock)
    class Client(original_client):
        def __init__(self,**kwargs):
            loops['created']=asyncio.get_running_loop()
            assert kwargs['timeout'].connect==4 and kwargs['timeout'].read==10
            super().__init__(transport=httpx.MockTransport(handler),**kwargs)
        async def __aexit__(self,*args):
            loops['closed']=asyncio.get_running_loop()
            return await super().__aexit__(*args)
    def accept(*args):
        result=original_accept(*args)
        applied.set()
        return result
    monkeypatch.setattr(importer.httpx,'AsyncClient',Client)
    monkeypatch.setattr(importer,'accept_page',accept)
    # Stop after one accepted page so the test cannot start another stream.
    original_poll=importer.poll_page
    async def one_page(*args):
        result=await original_poll(*args)
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(importer,'poll_page',one_page)
    async def run():
        loops['main']=asyncio.get_running_loop()
        task=asyncio.create_task(importer.run(db,cfg))
        try:
            await asyncio.sleep(.01)
            # Deliberately block the collector loop as synchronous SQL work does.
            assert applied.wait(3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert len(requests)==1
    assert loops['created'] is loops['request'] is loops['closed']
    assert loops['created'] is not loops['main']
    with db.tx() as c:
        saved=db.get(c,'morning_history:stock')
        assert saved['status']=='scanning' and saved['next']==stock['next']
        assert saved['last_page_new']==len(stock['records'])
        assert c.execute(select(func.count()).select_from(events)).scalar_one()==0
