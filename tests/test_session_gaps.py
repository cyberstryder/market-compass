from datetime import datetime, timezone
import base64
import json
import pytest
from sqlalchemy import insert
from compass.store import Store
from compass.setup_study import trials
from compass.option_ideas import ideas
from compass.session_gaps import window, totals, gap_page
from compass.session_trace import futures_session_audit

NOW=datetime(2026,9,18,21,10,tzinfo=timezone.utc).timestamp()

@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'gaps.db'));db.initialize()
    yield db
    db.engine.dispose()

def trial(id,started,asset='future',status='unresolved',finished=None):
    return dict(id=id,source_id=id,symbol='MNQ' if asset=='future' else 'SPY',strategy='test',side='long',version='test',status=status,started=started,finished=finished or started+1,payload=dict(id=id,asset=asset,status=status,symbol='MNQ',strategy='test',gap_detail={'reason':'missing_recorded_path'}))

def test_full_counts_survive_more_than_2000_newer_stock_rows(db):
    w=window(NOW,'2026-09-18');start=w['since']
    with db.tx() as c:
        c.execute(insert(trials),[trial('future-'+str(i),start+i,status='closed' if i<7 else 'unresolved') for i in range(20)])
        c.execute(insert(trials),[trial('stock-'+str(i),start+100+i,asset='stock') for i in range(2100)])
        c.execute(insert(trials),[trial('before',start-1),trial('end',w['until']),trial('after',w['until']+1)])
        report=futures_session_audit(db,c,NOW)
        assert report['total']==20 and report['states']=={'closed':7,'unresolved':13}
        assert report['counts_complete'] and not report['truncated']
        assert len(report['gaps'])==12 and report['gap_details_truncated']
        assert report['gap_total']==13

def test_keyset_pages_tied_timestamps_and_pins_new_failures(db):
    w=window(NOW,'2026-09-18');start=w['since']
    with db.tx() as c:
        c.execute(insert(trials),[trial(f'{i:064x}',start) for i in range(205)])
        page=gap_page(c,NOW,session_day=w['day']);ids=[r['id'] for r in page['records']]
        assert page['total']==205 and len(ids)==100 and page['has_more']
        c.execute(insert(trials),[trial('late',start,finished=NOW+1)])
        while page['has_more']:
            page=gap_page(c,NOW+10,session_day=w['day'],cursor=page['next_cursor'])
            assert page['total']==205
            ids.extend(r['id'] for r in page['records'])
        assert len(ids)==len(set(ids))==205 and page['page_complete']
        assert gap_page(c,NOW+10,session_day=w['day'])['total']==206

def test_options_are_separate_and_all_details_reachable(db):
    w=window(NOW,'2026-09-18',asset='option')
    with db.tx() as c:
        c.execute(insert(ideas),[dict(id=f'{i:064x}',underlying='AAPL',status='unresolved',created=w['since']+i,updated=w['since']+i+1,payload={'underlying':'AAPL','contract':{'symbol':'O:AAPL'},'gap_detail':{'option_last':1}}) for i in range(53)])
        page=gap_page(c,NOW,asset='option',session_day=w['day'],limit=12)
        ids=[]
        while True:
            ids.extend(r['id'] for r in page['records'])
            if not page['has_more']:break
            page=gap_page(c,NOW,asset='option',session_day=w['day'],limit=12,cursor=page['next_cursor'])
        assert len(set(ids))==53 and totals(c,w)['total']==53
        assert totals(c,window(NOW,w['day']))['total']==0

@pytest.mark.parametrize('cursor',['garbage','e30='])
def test_bad_cursor_rejected(db,cursor):
    with db.tx() as c:
        with pytest.raises(ValueError):gap_page(c,NOW,cursor=cursor)

def test_cursor_scope_and_clock_validation(db):
    w=window(NOW,'2026-09-18')
    with db.tx() as c:
        c.execute(insert(trials),[trial(str(i),w['since']) for i in range(2)])
        cursor=gap_page(c,NOW,limit=1)['next_cursor']
        with pytest.raises(ValueError):gap_page(c,NOW,asset='option',cursor=cursor)
        with pytest.raises(ValueError):gap_page(c,NOW,session_day='2026-09-17',cursor=cursor)
        token=json.loads(base64.urlsafe_b64decode(cursor));token['asof']=float('nan')
        with pytest.raises(ValueError):gap_page(c,NOW,cursor=base64.urlsafe_b64encode(json.dumps(token).encode()).decode())

def test_weekend_defaults_to_last_session_and_empty_is_complete(db):
    saturday=NOW+86400
    with db.tx() as c:
        page=gap_page(c,saturday)
        assert page['day']=='2026-09-18' and page['records']==[]
        assert page['page_complete'] and page['total']==0
        with pytest.raises(ValueError):gap_page(c,saturday,session_day='2026-09-19')

def test_api_auth_validation_and_complete_counts(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    from itsdangerous import URLSafeTimedSerializer
    cfg=Config(local=True,db='sqlite:///'+str(tmp_path/'api.db'),password='test-only',secret='test-session-secret',role='web')
    app=create_app(cfg);app.state.db.initialize()
    monkeypatch.setattr('compass.app.time.time',lambda:NOW)
    client=TestClient(app)
    assert client.get('/api/session-gaps').status_code==401
    client.cookies.set('compass_session',URLSafeTimedSerializer(cfg.secret).dumps('owner'))
    assert client.get('/api/session-gaps?asset=stock').status_code==422
    assert client.get('/api/session-gaps?limit=101').status_code==422
    assert client.get('/api/session-gaps?session_day=2026-09-19').status_code==400
    assert client.get('/api/session-gaps?cursor=garbage').status_code==400
    response=client.get('/api/session-gaps?session_day=2026-09-18')
    assert response.status_code==200
    assert response.json()['summary']['counts_complete']
    assert response.json()['summary']['total']==0
    app.state.db.engine.dispose()
