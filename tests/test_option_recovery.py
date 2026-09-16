import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from compass.option_recovery import parse_quotes,recover
from compass.option_stream import OptionBuffer
from compass.providers import Collectors
from compass.store import Store,events

NOW=1789490000
SYMBOL='O:SPY260918C00600000'


def test_final_quote_in_a_burst_is_flushed_without_another_message():
    b=OptionBuffer()
    for offset in (0,.4,.8):
        b.offer(dict(ev='Q',sym=SYMBOL,t=(NOW+offset)*1000,bp=1,ap=1.1,bs=1,**{'as':1}),{SYMBOL},NOW+offset,offset)
    assert len(b.quotes)==1
    b.flush(.9);assert len(b.quotes)==1
    b.flush(1)
    assert [q['ts'] for _,q,_ in b.quotes]==[NOW,NOW+.8]
    assert b.quotes[-1][1]['socket_read_at']==NOW+.8
    b.flush(2);assert len(b.quotes)==2


def test_recovery_does_not_refresh_old_future_or_invalid_quotes():
    def q(t,**changes):
        return dict(sip_timestamp=int(t*1e9),bid_price=1,ask_price=1.1,bid_size=1,ask_size=1,**changes)
    rows=parse_quotes({'results':[q(NOW-6),q(NOW+1),q(NOW-1),q(NOW-2),q(NOW-1)]},NOW)
    assert [r['ts'] for r in rows]==[NOW-2,NOW-1]
    assert all(r['recovery_fetched_at']==NOW for r in rows)
    assert not parse_quotes({'results':[dict(q(NOW),bid_size=0)]},NOW)


@pytest.mark.parametrize('source',['massive_rest','alpaca'])
def test_late_history_is_archived_without_regressing_latest_state(tmp_path,monkeypatch,source):
    db=Store('sqlite:///'+str(tmp_path/'test.db'));db.initialize()
    collector=SimpleNamespace(db=db)
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    def q(t):return dict(ts=t,bid=1,ask=1.1,bid_size=1,ask_size=1)
    Collectors.quote_batch(collector,'massive',[(SYMBOL,q(NOW),True)])
    for _ in range(2):Collectors.quote_batch(collector,source,[(SYMBOL,q(NOW-1),True)])
    with db.tx() as c:
        assert db.get(c,'quote:'+SYMBOL)['ts']==NOW
        assert len(c.execute(select(events)).all())==2
    db.engine.dispose()


def test_recovery_is_bounded_and_rate_limits_back_off(monkeypatch):
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    monkeypatch.setattr('compass.option_recovery.targets',lambda *args:([SYMBOL],1,False))
    calls=[]
    async def get(url,params):
        calls.append(params)
        from compass.providers import FeedError
        raise FeedError('quota',429)
    c=SimpleNamespace(get=get,cfg=SimpleNamespace(massive='test'),db=SimpleNamespace(health=lambda *a,**k:None))
    async def run():
        await recover(c)
        await recover(c)
    asyncio.run(run())
    assert len(calls)==1 and calls[0]['limit']==32
    assert c.option_recovery_backoff==NOW+60


def test_live_recovery_retains_original_clock_and_source(monkeypatch):
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    monkeypatch.setattr('compass.option_recovery.targets',lambda *args:([SYMBOL],1,False))
    saved=[]
    async def get(url,params):
        assert params['timestamp.gte']==str(int((NOW-5)*1e9))
        return {'results':[dict(sip_timestamp=int((NOW-1)*1e9),bid_price=1,ask_price=1.1,bid_size=1,ask_size=1)]}
    c=SimpleNamespace(get=get,cfg=SimpleNamespace(massive='test'),
        quote_batch=lambda source,rows:saved.append((source,rows)),db=SimpleNamespace(health=lambda *a,**k:None))
    asyncio.run(recover(c))
    assert saved[0][0]=='massive_rest'
    q=saved[0][1][0][1]
    assert q['ts']==NOW-1 and q['recovery_fetched_at']==NOW
    assert q['collection_version']=='option-reliability-v4'


def test_recovery_owns_http_client_on_its_isolated_loop(monkeypatch):
    loops={}
    class Client:
        def __init__(self,**kwargs):loops['created']=asyncio.get_running_loop()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):loops['closed']=asyncio.get_running_loop()
        async def get(self,*args,**kwargs):
            loops['request']=asyncio.get_running_loop()
            return SimpleNamespace(status_code=200,json=lambda:{'results':[]})
    async def cycle(worker):
        assert await worker.get('https://example.invalid')=={'results':[]}
        raise asyncio.CancelledError
    monkeypatch.setattr('compass.providers.httpx.AsyncClient',Client)
    monkeypatch.setattr('compass.option_recovery.recover',cycle)
    class Collector(Collectors):
        def __init__(self):
            self.cfg=SimpleNamespace(alpaca_key='test',alpaca_secret='test')
            self.db=SimpleNamespace()
            self.client=object()  # Cannot be used as an HTTP client.
    async def run():
        loops['main']=asyncio.get_running_loop()
        with pytest.raises(asyncio.CancelledError):await Collector().option_recovery()
    asyncio.run(run())
    assert loops['created'] is loops['request'] is loops['closed']
    assert loops['created'] is not loops['main']


def test_late_storage_is_not_made_fresh_by_socket_receipt(tmp_path,monkeypatch):
    from compass.quote_path import recorded_path
    db=Store('sqlite:///'+str(tmp_path/'late.db'));db.initialize()
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW+10)
    q=dict(ts=NOW,bid=1,ask=1.1,bid_size=1,ask_size=1,socket_read_at=NOW+.1)
    Collectors.quote_batch(SimpleNamespace(db=db),'alpaca',[('SPY',q,True)])
    with db.tx() as c:
        path,truncated,check=recorded_path(db,c,'SPY',NOW-1,NOW+11)
    assert not path and not truncated and check['stale_or_invalid_when_recorded']==1
    db.engine.dispose()


def test_alpaca_recovery_explicit_opra_preserves_clock(monkeypatch):
    from compass.option_recovery import recover_alpaca
    from datetime import datetime,timezone
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    saved=[]
    async def get(url,headers,params):
        assert params['feed']=='opra' and params['symbols']==SYMBOL[2:]
        return {'quotes':{SYMBOL[2:]:dict(t=datetime.fromtimestamp(NOW-1,timezone.utc).isoformat(),bp=1,ap=1.1,bs=1,**{'as':1})}}
    c=SimpleNamespace(cfg=SimpleNamespace(alpaca_key='test',alpaca_secret='test'),alpaca_headers={},get=get,
        quote_batch=lambda source,rows:saved.append((source,rows)))
    result=asyncio.run(recover_alpaca(c,[SYMBOL]))
    assert result[0]['fresh_rows']==1 and saved[0][0]=='alpaca_opra_recovery'
    assert saved[0][1][0][1]['ts']==NOW-1


def test_opra_access_failure_backs_off_without_indicative_fallback(monkeypatch):
    from compass.option_recovery import recover_alpaca
    from compass.providers import FeedError
    monkeypatch.setattr('compass.option_recovery.time.time',lambda:NOW)
    calls=[]
    async def get(*args,**kwargs):
        calls.append(kwargs)
        raise FeedError('forbidden',403)
    c=SimpleNamespace(cfg=SimpleNamespace(alpaca_key='test',alpaca_secret='test'),alpaca_headers={},get=get)
    async def run():
        first=await recover_alpaca(c,[SYMBOL]);second=await recover_alpaca(c,[SYMBOL])
        assert first[0]['http_status']==403 and second==[]
    asyncio.run(run())
    assert len(calls)==1 and c.option_opra_backoff==NOW+300
