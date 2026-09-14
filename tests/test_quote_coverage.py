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
