import copy
import json
from pathlib import Path
import pytest
from sqlalchemy import select
from compass.store import Store
from compass.morning_history import accept_page, digest, history
from compass.morning_report import build_report

FIXTURE=json.loads((Path(__file__).parent/'fixtures/morning_frame_reconciliation.json').read_text())
NOW=FIXTURE['now_ms']/1000

@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'reconcile.db'));db.initialize()
    accept_page(db,'events',FIXTURE['events'],'',NOW)
    yield db
    db.engine.dispose()

def report(db):
    with db.tx() as c:return build_report(db,c,NOW)['signals'][0]

def rehash(page):
    for r in page['records']:r['sha256']=digest(r['payload'])
    return page

def test_v13_event_only_gap_is_reproduced_then_reconciled_to_source(db):
    before=report(db)
    assert before['stock_history']['status']=='incomplete' and before['stock_history']['recorded']==0
    assert before['native_reconciliation']['status']=='awaiting_source_inventory'
    accept_page(db,'stock',FIXTURE['stock'],'',NOW)
    after=report(db)
    for k in ('stock_history','modeled_exits','extended_checkpoints'):
        assert after[k]==FIXTURE['expected'][k]
    assert after['extended_stock_history']['status']=='complete'
    proof=after['native_reconciliation']
    assert proof['status']=='matched_source_snapshot'
    assert proof['source_candles']==proof['compass_candles']==180
    assert proof['source_stock_coverage']['status']=='complete'
    again=accept_page(db,'stock',FIXTURE['stock'],'',NOW+1)
    assert again['last_page_new']==0
    with db.tx() as c:
        bar=c.execute(select(history).where(history.c.kind=='signal_bar').order_by(history.c.source_id)).mappings().first()
        source=FIXTURE['stock']['records'][0]['payload']['bars'][0]
        assert bar['source_received']==source['received_at_ms']/1000
        assert bar['imported_at']==NOW

def test_real_source_gap_remains_incomplete_and_is_not_replaced_with_research(db):
    page=copy.deepcopy(FIXTURE['stock']);del page['records'][0]['payload']['bars'][10]
    accept_page(db,'stock',rehash(page),'',NOW)
    r=report(db)
    assert r['stock_history']['status']=='incomplete'
    assert r['stock_history']['unreceived_elapsed_minutes']==[11]
    assert r['native_reconciliation']['status']=='matched_source_snapshot'
    assert r['native_reconciliation']['source_stock_coverage']['unreceived_elapsed_minutes']==[11]

def test_growing_inventory_recovers_later_candles_and_preserves_earlier_import_time(db):
    page=copy.deepcopy(FIXTURE['stock']);page['records'][0]['payload']['bars']=page['records'][0]['payload']['bars'][:30]
    accept_page(db,'stock',rehash(page),'',NOW)
    assert report(db)['stock_history']['recorded']==30
    accept_page(db,'stock',FIXTURE['stock'],'',NOW+1)
    assert report(db)['stock_history']['recorded']==60
    with db.tx() as c:
        first=c.execute(select(history).where(history.c.kind=='signal_bar').order_by(history.c.source_id)).mappings().first()
        assert first['imported_at']==NOW

def test_changed_source_candle_rolls_back_inventory_and_cursor(db):
    accept_page(db,'stock',FIXTURE['stock'],'',NOW)
    page=copy.deepcopy(FIXTURE['stock']);bar=page['records'][0]['payload']['bars'][0]
    bar['payload']['high']+=1.;bar['sha256']=digest(bar['payload'])
    with pytest.raises(ValueError,match='identity conflict'):
        accept_page(db,'stock',rehash(page),'',NOW+1)
    with db.tx() as c:assert db.get(c,'morning_history:stock')['at']==NOW
    assert report(db)['stock_history']['status']=='complete'

def test_source_inventory_shrink_is_visible_without_deleting_saved_candles(db):
    accept_page(db,'stock',FIXTURE['stock'],'',NOW)
    page=copy.deepcopy(FIXTURE['stock']);removed=page['records'][0]['payload']['bars'].pop(0)
    accept_page(db,'stock',rehash(page),'',NOW+1)
    r=report(db)['native_reconciliation']
    assert r['status']=='inventory_differs'
    assert r['extra_in_compass']==[removed['payload']['open_at_ms']]
    assert r['source_candles']==179 and r['compass_candles']==180
