import asyncio
import sys
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass.config import Config
from compass.providers import Collectors
from compass.extra_futures import collect_group
from compass.futures import selection, risk_day
from compass.readiness import decorate_health
from compass.secondary import capture
from test_setup_study import NOW


@pytest.mark.parametrize('denied',[False,True])
def test_optional_live_access_mapping_quotes_and_rejection_isolation(tmp_path,monkeypatch,denied):
    db=Store('sqlite:///'+str(tmp_path/'optional.db'));db.initialize()
    class Mapping:
        instrument_id=123
        stype_in_symbol='MGC.v.0';stype_out_symbol='MGCZ6'
        start_ts=int((NOW-86400)*1e9);end_ts=int((NOW+86400)*1e9)
    class Error:
        err='exchange entitlement missing'
    class Bar: pass
    class Quote:
        instrument_id=123;ts_event=int(NOW*1e9)
        levels=[SimpleNamespace(bid_px=100000000000,ask_px=100100000000,bid_sz=4,ask_sz=5)]
    subscriptions=[]
    class Live:
        symbology_map={123:'MGCZ6'}
        def __init__(self,**kwargs): pass
        def add_callback(self,fn,exception_callback): self.callback=fn
        def subscribe(self,**kwargs): subscriptions.append(kwargs)
        def start(self):
            if denied: self.callback(Error())
            else:
                self.callback(Mapping());self.callback(Quote())
        def stop(self): pass
        def terminate(self): pass
        def block_for_close(self): pass
    monkeypatch.setitem(sys.modules,'databento',SimpleNamespace(Live=Live,ErrorMsg=Error,
        SymbolMappingMsg=Mapping,OHLCVMsg=Bar,MBP1Msg=Quote))
    monkeypatch.setattr('compass.extra_futures.time.time',lambda:NOW)
    async def end(_): raise asyncio.CancelledError
    monkeypatch.setattr('compass.extra_futures.asyncio.sleep',end)
    async def run():
        collector=Collectors(db,Config(local=True,databento='test-key'))
        with db.tx() as c: db.put(c,'health:databento_futures',{'status':'receiving','marker':'original-index-stream'})
        with pytest.raises(asyncio.CancelledError): await collect_group(collector,'COMEX',['MGC.v.0'])
        assert collector.extra_live=={}
        await collector.close()
    asyncio.run(run())
    with db.tx() as c:
        assert db.get(c,'health:databento_futures')['marker']=='original-index-stream'
        if denied:
            assert db.get(c,'health:databento_comex')['status']=='error'
            assert db.get(c,'quote:MGCZ6@123') is None
        else:
            q=db.get(c,'quote:MGCZ6@123')
            assert q['bid']==100 and q['ask']==100.1
            mapping=db.prefix(c,'contract:')
            assert selection(('MGC.v.0',),NOW,mapping)[0]['raw_symbol']=='MGCZ6'
    assert len(subscriptions)==2
    assert all(s['stype_in']=='continuous' for s in subscriptions)
    db.engine.dispose()


def test_optional_portfolio_positions_do_not_join_primary_stream(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'isolation.db'));db.initialize()
    async def run():
        collector=Collectors(db,Config(local=True,futures=('MES.c.0',)))
        with db.tx() as c:
            db.put(c,'position:MGCZ6@1',dict(status='open',asset='future',symbol='MGCZ6@1'))
        assert 'MGCZ6' not in collector.future_targets()[1]
        await collector.close()
    asyncio.run(run());db.engine.dispose()


def test_optional_health_uses_collector_heartbeat_and_actual_quote_age():
    health=[dict(name='databento_comex_MGC',status='receiving',detail='quote',source_ts=NOW-50,checked_at=NOW)]
    result=decorate_health(health,{'worker:collector':{'at':NOW}},dict(futures=True,equities=True),NOW)
    assert result[0]['status']=='stale' and result[0]['heartbeat_age']==0


def test_mapping_remains_valid_across_session_boundary_until_provider_roll():
    mapping={'contract:MGC':dict(configured_symbol='MGC.v.0',raw_symbol='MGCZ6',trading_day='old-session',
        mapping_start=NOW-86400,mapping_end=NOW+86400)}
    assert selection(('MGC.v.0',),NOW,mapping)[0]['resolved']


def test_metals_secondary_does_not_assume_spy_qqq_directional_confirmation(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'context.db'));db.initialize()
    with db.tx() as c:
        db.put(c,'quote:MGCZ6@1',dict(ts=NOW,bid=100,ask=100.1,bid_size=4,ask_size=4))
        for symbol in ('SPY','QQQ'):
            db.put(c,'quote:'+symbol,dict(ts=NOW,bid=100,ask=100.1,bid_size=4,ask_size=4))
            db.put(c,'scanner_features:'+symbol,dict(status='ready',asof=NOW,htf15_bias=-1))
        inputs=capture(db,c,dict(symbol='MGCZ6@1',project='compass_futures'),Config(local=True),NOW)
        assert inputs['peers']==[] and inputs['flow']==[] and inputs['exposure']['symbol'] is None
    db.engine.dispose()
