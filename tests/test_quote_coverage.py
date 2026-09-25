from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from compass.providers import Collectors
from compass.quote_coverage import stock_archive_health
from compass.secondary import advance_recorded_measurement, start_measurement
from compass.stock_stream import StockBuffer
from compass.store import Store, events
from tests.test_smoothers_postgres_handoff import pg

NOW = datetime(2026, 9, 14, 15, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///'+str(tmp_path/'coverage.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(stamp, price=100):
    return dict(ts=stamp, bid=price-.01, ask=price+.01, bid_size=10, ask_size=10)


def measurement(symbol='TGT'):
    return start_measurement({'symbol':symbol},
        {'quote':quote(NOW), 'price_basis':'same_underlying', 'market_symbol':symbol},
        {'timely':True}, NOW)


def archive(db, c, stamp, price=101, received=None, symbol='TGT', **changes):
    payload = {**quote(stamp, price), **changes}
    c.execute(db.insert(events).values(key=str((symbol,stamp,price)), kind='quote', source='alpaca',
        symbol=symbol, ts=stamp, received=stamp+.1 if received is None else received, payload=payload))


def test_non_core_quotes_reach_journal_and_keep_one_hz_sampling(db, monkeypatch):
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW+2)
    buffer = StockBuffer(['SPY','TGT','SBUX'], ['SPY'])
    for symbol in buffer.watch:
        buffer.offer(dict(T='q',S=symbol,t=NOW+1,bp=100,ap=100.02,bs=10,**{'as':10}))
    batch, _ = buffer.take(0)
    collector = SimpleNamespace(db=db)
    Collectors.quote_batch(collector,'alpaca',batch)
    Collectors.quote_batch(collector,'alpaca',batch)  # duplicate write is idempotent
    with db.tx() as c:
        assert sorted(c.execute(select(events.c.symbol)).scalars()) == ['SBUX','SPY','TGT']
        assert db.get(c,'quote:TGT')['ts'] == NOW+1
    buffer.offer(dict(T='q',S='TGT',t=NOW+2,bp=101,ap=101.02))
    assert buffer.take(.25)[0] == []
    assert buffer.take(1)[0][0][2] is True


def test_archive_health_distinguishes_missing_behind_and_no_quote(db):
    quotes = {'quote:'+s:quote(NOW) for s in ('SPY','TGT','SBUX')}
    with db.tx() as c:
        for key,value in quotes.items(): db.put(c,key,value)
        archive(db,c,NOW-1,symbol='SPY')
        archive(db,c,NOW-30,symbol='SBUX')
        health = stock_archive_health(c,['SPY','TGT','SBUX','NEW'],quotes,NOW,True)
    assert health['status'] == 'partial'
    assert {r['symbol']:r['status'] for r in health['rows']} == {
        'SPY':'retained','TGT':'missing_archive','SBUX':'archive_behind','NEW':'no_quote'}


def test_index_roots_never_enter_equity_collection_but_research_is_retained(db,monkeypatch):
    from compass.config import Config
    from compass.universe import data_symbols
    from compass.active_observations import inventory
    monkeypatch.setattr('compass.discovery.requested',lambda *a:['VIX','XND','QQQ'])
    contract='O:VIX261021C00020000'
    with db.tx() as c:
        db.put(c,'position:vix',dict(status='open',asset='option',symbol=contract,underlying='VIX'))
        db.put(c,'research:vix',{'value':20})
        active=inventory(db,c,NOW)
        assert contract in active['options'] and 'VIX' not in active['stocks']
        cfg=Config(local=True,stocks=('VIX','SPX','SPY'))
        assert data_symbols(db,c,cfg,NOW)==('SPY','QQQ')
        assert db.get(c,'research:vix')=={'value':20}
        db.put(c,'quote:SPY',quote(NOW));archive(db,c,NOW-1,symbol='SPY')
        health=stock_archive_health(c,['SPY','VIX','SPX','NEW'],{'quote:SPY':quote(NOW)},NOW,False)
        assert health['status']=='waiting' and 'No quote yet: NEW.' in health['detail']
        assert health['excluded_index_symbols']==['SPX','VIX']
        assert {r['symbol'] for r in health['rows']}=={'SPY','NEW'}


def test_late_cycle_recovers_all_horizons_from_first_timely_archived_quote(db):
    m = measurement()
    original = deepcopy(m)
    with db.tx() as c:
        for minutes in (15,30,60):
            archive(db,c,NOW+minutes*60,price=101)
            archive(db,c,NOW+minutes*60+1,price=105)
        result = advance_recorded_measurement(db,c,m,'short',quote(NOW+4000,50),NOW+4000)
    assert result['state'] == 'complete'
    assert m == original
    for point in result['horizons'].values():
        assert point['status'] == 'observed' and point['price'] == 101
        assert point['return_pct'] == pytest.approx(-1)
        assert point['captured_at'] == point['due']+.1
        assert point['recovered_at'] == NOW+4000
        assert point['observation_source'] == 'retained_quote'
    assert result['samples'] == 1  # recovered checkpoints do not invent a full extrema path


@pytest.mark.parametrize('offset,received,changes',[
    (0,10,{}),                  # stale at receipt
    (0,-1,{}),                  # future timestamp when recorded
    (29,31,{}),                 # received outside 30-second window
    (31,31.1,{}),               # source outside window
    (0,.1,{'bid_size':0}),       # unusable quote
    (0,.1,{'ts':NOW+901}),       # payload/event mismatch
])
def test_invalid_or_late_archives_never_repair_checkpoint(db,offset,received,changes):
    with db.tx() as c:
        archive(db,c,NOW+900+offset,received=NOW+900+received,**changes)
        result = advance_recorded_measurement(db,c,measurement(),'long',None,NOW+1000)
    assert result['horizons']['15']['status'] == 'missing'
    assert result['horizons']['15']['archive_check']['usable_rows'] == 0


def test_no_archive_uses_fresh_live_quote_only_inside_window(db):
    with db.tx() as c:
        result = advance_recorded_measurement(db,c,measurement(),'long',quote(NOW+900,102),NOW+901)
        missing = advance_recorded_measurement(db,c,measurement(),'long',quote(NOW+1000,102),NOW+1000)
    assert result['horizons']['15']['status'] == 'observed'
    assert missing['horizons']['15']['status'] == 'missing'


def test_old_completed_missing_observations_are_not_rewritten(db):
    m = measurement()
    m['state'] = 'complete'
    for point in m['horizons'].values(): point['status']='missing'
    with db.tx() as c:
        archive(db,c,NOW+900)
        assert advance_recorded_measurement(db,c,m,'long',None,NOW+4000) == m



def test_futures_burst_updates_latest_once_and_keeps_ordered_samples(tmp_path):
    from sqlalchemy import event
    from compass.store import Store
    db=Store('sqlite:///'+str(tmp_path/'burst.db'));db.initialize()
    collector=SimpleNamespace(db=db)
    collector.future_quote_batch=lambda items:Collectors.future_quote_batch(collector,items)
    statements=[]
    event.listen(db.engine,'before_cursor_execute',lambda c,cu,s,p,ctx,m:statements.append(s))
    rows=[('SIZ6@701',dict(ts=100+i,bid=1,ask=2),True) for i in range(64)]
    Collectors.quote_batch(collector,'databento',rows+[('SIZ6@701',dict(ts=99,bid=1,ask=2),True)])
    assert len(statements)==4  # insert/lock/latest/archive, independent of burst length
    with db.tx() as c:
        assert db.get(c,'quote:SIZ6@701')['ts']==163
        assert len(db.recent(c,'quote','SIZ6@701',limit=100))==64
    db.engine.dispose()


@pytest.mark.parametrize('database',('db','pg'))
def test_bulk_stream_writes_keep_every_sample_without_rewinding_latest(request,database):
    db=request.getfixturevalue(database)
    collector=SimpleNamespace(db=db)
    Collectors.quote_batch(collector,'alpaca_stock_recovery',[('TGT',quote(NOW+5),True)])
    batch=[('TGT',quote(NOW+offset),True) for offset in (1,3,2,4,3)]
    batch+=[('SPY',quote(NOW+offset),True) for offset in (3,2,1)]
    receipts=Collectors.quote_batch(collector,'alpaca',batch)
    assert len(receipts)==len(batch)
    with db.tx() as c:
        assert db.get(c,'quote:TGT')['ts']==NOW+5
        assert db.get(c,'quote:SPY')['ts']==NOW+3
        # Each distinct original source timestamp survives once, even if a
        # parallel recovery already wrote a newer latest quote.
        assert len(db.recent(c,'quote',limit=20))==8


def test_full_watchlist_uses_bounded_bulk_statements_and_real_chunk_commit_clocks(db,monkeypatch):
    from contextlib import contextmanager
    from sqlalchemy import event
    clock=[NOW];commits=[];statements=[];original=db.tx
    monkeypatch.setattr('compass.providers.time.time',lambda:clock[0])
    @contextmanager
    def tx():
        with original() as c:yield c
        clock[0]+=.1;commits.append(clock[0])
    monkeypatch.setattr(db,'tx',tx)
    event.listen(db.engine,'before_cursor_execute',lambda c,cu,s,p,ctx,m:statements.append(s))
    batch=[(f'S{i:03}',{**quote(NOW),'socket_read_at':NOW},True) for i in range(243)]
    receipts=Collectors.quote_batch(SimpleNamespace(db=db),'alpaca',batch)
    assert len(statements)==16  # latest + archive for each of eight bounded commits
    assert len(commits)==8 and len(receipts)==243
    assert receipts[0][2]==commits[0] and receipts[-1][2]==commits[-1]
    assert receipts[0][2]<receipts[-1][2]


@pytest.mark.parametrize('database',('db','pg'))
def test_bulk_latest_and_archive_rollback_together(request,database,monkeypatch):
    db=request.getfixturevalue(database)
    def fail(*args):raise RuntimeError('archive unavailable')
    monkeypatch.setattr(db,'append_quotes',fail)
    with pytest.raises(RuntimeError,match='archive unavailable'):
        Collectors.quote_batch(SimpleNamespace(db=db),'alpaca',[('TGT',quote(NOW),True)])
    with db.tx() as c:
        assert db.get(c,'quote:TGT') is None and not db.recent(c,'quote')
