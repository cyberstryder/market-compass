from datetime import datetime,timezone
from copy import deepcopy
import pytest
from sqlalchemy import select,func,update
from fastapi.testclient import TestClient
from compass.app import create_app
from compass.config import Config
from compass.store import Store,flow_records,tm_studies as studies,tm_checkpoints as checkpoints,events,identity,state
from compass.flow_recovery import ingest
from compass.tm_scoring import score,checkpoint_times,VERSION,HORIZONS
from compass.tm_study import register,inventory,observe,Study
from compass.tm_reporting import report,record_page,detail
from compass.universe import data_symbols
from compass.market import NY,session
from test_smoothers_postgres_handoff import pg

NOW=datetime(2026,9,17,10,30,tzinfo=NY).timestamp()
DAY='2026-09-17'


@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'tm.db'));db.initialize()
    yield db
    db.engine.dispose()


@pytest.fixture(params=['sqlite','postgres'])
def storage(request):return request.getfixturevalue('db' if request.param=='sqlite' else 'pg')


def row(id='1',**changes):
    return dict(dict(vendor_id=id,symbol='SPY',source_ts=NOW-120,received=NOW,score=90,
        premium=200000,volume=300,open_interest=100,sentiment='Bullish',option_type='Call',
        classification='Sweep',expiry='2026-09-18',strike=101),**changes)


def quote(at=NOW,price=101.1):
    return dict(ts=at,bid=price-.01,ask=price+.01,bid_size=10,ask_size=10,received=at,source='fixture')


def facts(at=NOW):
    return dict(status='ready',asof=at-30,ema9=101,ema21=100,vwap=100,htf15_bias=1,
        high5=100,low5=99,rvol20=1.5)


def seed(db,c):
    db.put(c,'quote:SPY',quote());db.put(c,'scanner_features:SPY',facts())


def part(rows):
    return dict(page=1,page_size=100,total=len(rows),raw_count=len(rows),rejected=0,rows=rows,vendor_stale=False)


def test_compass_score_is_separate_from_tm_and_missing_is_not_zero():
    a=score(row(score=99),quote(),facts(),NOW)
    b=score(row(score=1),quote(),facts(),NOW)
    assert a['compass_score']==b['compass_score']==100
    assert a['tm_selected'] and not b['tm_selected'] and b['compass_selected']
    missing=score(row(),quote(),{**facts(),'htf15_bias':None},NOW)
    assert missing['compass_score'] is None and missing['score_range']==[80,100]
    assert missing['compass_selected'] is None
    thin=score(row(open_interest=5,volume=100000),quote(),facts(),NOW)
    assert thin['compass_score']==90
    assert score(row(sentiment='Neutral'),quote(),facts(),NOW)['compass_score'] is None


def test_receipt_freeze_survives_vendor_correction_and_repeated_polls(storage,monkeypatch):
    clock=[NOW];monkeypatch.setattr('compass.store.time.time',lambda:clock[0])
    with storage.tx() as c:seed(storage,c)
    ingest(storage,DAY,1,part([row()]),NOW)
    with storage.tx() as c:
        first=c.execute(select(studies)).mappings().one()
        assert first['tm_score']==90 and first['compass_score']==100 and first['origin']=='prospective'
        assert c.execute(select(func.count()).select_from(checkpoints)).scalar_one()==6
    clock[0]=NOW+10
    changed=row(score=30,premium=300000,sentiment='Bearish',received=NOW+10)
    for _ in range(2):ingest(storage,DAY,1,part([changed]),NOW+10)
    with storage.tx() as c:
        frozen=c.execute(select(studies)).mappings().one()
        assert dict(frozen)==dict(first)
        record=detail(c,first['id'])
        assert record['first_snapshot']['score']==90 and record['latest_vendor_snapshot']['score']==30
        assert record['vendor_changed_since_assessment'] is True
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='vendor_flow')).scalar_one()==2
        assert not storage.recent(c,'alert')


def test_delayed_low_score_records_still_receive_price_checkpoints(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        seed(db,c)
        register(db,c,DAY,[row(score=10,source_ts=NOW-1800)],NOW,at=NOW)
        saved=c.execute(select(studies)).mappings().one()
        assert saved['age_bucket']=='15–60m' and saved['tm_score']==10
        assert saved['status']=='pending' and saved['payload']['assessment']['tm_selected'] is False
        assert c.execute(select(func.count()).select_from(checkpoints).where(checkpoints.c.status=='pending')).scalar_one()==6


@pytest.mark.parametrize('case',['future_quote','future_context','late_processing','future_vendor','excluded_symbol'])
def test_future_or_late_inputs_cannot_become_receipt_time_evidence(db,monkeypatch,case):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        seed(db,c);r=row();at=NOW
        if case=='future_quote':db.put(c,'quote:SPY',quote(NOW+.5))
        if case=='future_context':c.execute(update(state).where(state.c.key=='scanner_features:SPY').values(updated=NOW+1))
        if case=='late_processing':at=NOW+31
        if case=='future_vendor':r['source_ts']=NOW+1
        if case=='excluded_symbol':r['symbol']='DJT'
        register(db,c,DAY,[r],NOW,at=at)
        p=c.execute(select(studies)).mappings().one()
        assert p['compass_score'] is None
        if case!='future_context':assert p['status']=='unavailable'


def test_old_inventory_registers_in_batches_without_inventing_original_scores(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        for i in range(5):
            r=row(str(i),score=20)
            c.execute(flow_records.insert().values(day=DAY,vendor_id=r['vendor_id'],source_ts=r['source_ts'],
                payload=r,first_seen=NOW-3600,last_seen=NOW))
        seed(db,c)
        assert inventory(db,c,NOW,2)==2
        assert inventory(db,c,NOW,2)==2
        assert inventory(db,c,NOW,2)==1
        assert inventory(db,c,NOW,2)==0
        rows=c.execute(select(studies)).mappings().all()
        assert len(rows)==5 and all(r['origin']=='historical_inventory' and r['tm_score'] is None and
            r['compass_score'] is None and r['payload']['entry_mid'] is None for r in rows)
        assert report(db,c,NOW)['inventory']==dict(received_records=5,registered_records=5,unregistered_records=0)


def test_calendar_targets_use_trading_minutes_holidays_and_early_closes():
    friday=datetime(2026,9,4,15,55,tzinfo=NY).timestamp()
    p=checkpoint_times(friday)
    assert p['15m']==datetime(2026,9,8,9,40,tzinfo=NY).timestamp()  # Labor Day skipped.
    assert p['1session']==session('2026-09-08')[1]-.001
    short=datetime(2026,11,27,12,55,tzinfo=NY).timestamp()
    p=checkpoint_times(short)
    assert p['close']==session('2026-11-27')[1]-.001
    assert p['15m']==datetime(2026,11,30,9,40,tzinfo=NY).timestamp()


def test_timely_archive_can_be_processed_late_but_late_backfill_cannot_repair_missing(db,monkeypatch):
    clock=[NOW];monkeypatch.setattr('compass.store.time.time',lambda:clock[0])
    with db.tx() as c:
        seed(db,c);register(db,c,DAY,[row()],NOW,at=NOW)
        id=c.execute(select(studies.c.id)).scalar_one()
        targets=checkpoint_times(NOW)
        # Complete 15m from an original timely sample; process much later.
        clock[0]=targets['15m']
        db.append(c,'quote','fixture','SPY',clock[0],quote(clock[0],102))
        # Fresh-at-socket is insufficient when recording missed the deadline.
        clock[0]=targets['60m']+20
        late=quote(targets['60m'],103)
        db.append(c,'quote','fixture','SPY',late['ts'],late)
        observe(db,c,targets['60m']+100)
        rows={r.horizon:dict(r._mapping) for r in c.execute(select(checkpoints).where(checkpoints.c.study_id==id))}
        assert rows['15m']['status']=='completed' and rows['15m']['directional_return_pct']==pytest.approx((102/101.1-1)*100)
        assert rows['60m']['status']=='unavailable' and rows['60m']['directional_return_pct'] is None
        # A later replay of the same source timestamp cannot rewrite the outcome.
        observe(db,c,targets['60m']+200)
        assert c.execute(select(checkpoints.c.status).where(checkpoints.c.horizon=='60m')).scalar_one()=='unavailable'
        assert c.execute(select(studies.c.status)).scalar_one()=='pending'  # Multi-session windows remain open.


def test_new_symbols_expand_collection_without_changing_scanner_or_excluded_universe(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    cfg=Config(local=True,stocks=('SPY',),futures=())
    with db.tx() as c:
        register(db,c,DAY,[row('one',symbol='ALAB'),row('two',symbol='DJT'),row('index',symbol='SPX')],NOW,at=NOW)
        assert data_symbols(db,c,cfg,NOW)==('SPY','ALAB')
        assert cfg.watch_symbols==('SPY',)
        coverage=report(db,c,NOW,cfg=cfg)['collection']
        assert coverage['requested_symbols']==3 and coverage['in_collection']==1 and coverage['not_in_collection']==2


def test_rejected_bearish_record_is_still_measured_and_unknown_direction_is_unsigned(db,monkeypatch):
    clock=[NOW];monkeypatch.setattr('compass.store.time.time',lambda:clock[0])
    with db.tx() as c:
        seed(db,c)
        register(db,c,DAY,[row('short',sentiment='Bearish'),row('neutral',sentiment='Neutral')],NOW,at=NOW)
        target=checkpoint_times(NOW)['15m'];clock[0]=target
        db.append(c,'quote','fixture','SPY',target,quote(target,100))
        observe(db,c,target+20)
        results=c.execute(select(studies.c.vendor_id,checkpoints.c.directional_return_pct,checkpoints.c.raw_return_pct)
            .join(checkpoints,checkpoints.c.study_id==studies.c.id).where(checkpoints.c.horizon=='15m')).all()
        by_id={r.vendor_id:r for r in results}
        assert by_id['short'].directional_return_pct>0 and by_id['short'].raw_return_pct<0
        assert by_id['neutral'].directional_return_pct is None and by_id['neutral'].raw_return_pct<0
        groups=report(db,c,target+20)['comparisons']
        rejected=next(r for r in groups if r['horizon']=='15m' and r['arm']=='compass_rejected')
        assert rejected['measured']==1 and rejected['positive']==1


def test_worker_registers_history_and_publishes_reconciled_coverage(db,monkeypatch):
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    with db.tx() as c:
        r=row()
        c.execute(flow_records.insert().values(day=DAY,vendor_id=r['vendor_id'],source_ts=r['source_ts'],
            payload=r,first_seen=NOW-600,last_seen=NOW))
    Study(db,Config(local=True,stocks=('SPY',),futures=())).tick(NOW)
    with db.tx() as c:
        saved=db.get(c,'tm-study-v1:report')
        assert saved['inventory']==dict(received_records=1,registered_records=1,unregistered_records=0)
        assert saved['counts']['historical_inventory']==1 and saved['counts']['scored']==0
        assert db.get(c,'health:tm_scoring_study')['status']=='running'


def bulk_rows(db,c,n=10005):
    values=[];raw=[];out=[]
    for i in range(n):
        id=f'{i:064x}';tm=90 if i%2 else 20;compass=80 if i%3 else 40
        p=dict(id=id,version=VERSION,day=DAY,vendor_id=str(i),symbol='SPY',first_seen=NOW,
            assessed_at=NOW,origin='prospective',receipt_day=DAY,assessment=dict(receipt_session='regular'),
            first_snapshot=row(str(i),score=tm),entry_mid=100)
        values.append(dict(id=id,day=DAY,vendor_id=str(i),version=VERSION,symbol='SPY',first_seen=NOW,assessed_at=NOW,
            origin='prospective',status='pending',score_status='scored',direction='long',age_bucket='0–2m',
            dte_bucket='1–7DTE',tm_score=tm,compass_score=compass,payload=p))
        raw.append(dict(day=DAY,vendor_id=str(i),source_ts=NOW-120,payload=p['first_snapshot'],first_seen=NOW,last_seen=NOW))
        out.append(dict(study_id=id,horizon='60m',target_at=NOW+3600,due_at=NOW+3610,status='completed',
            price=101,source_ts=NOW+3600,received_at=NOW+3600,raw_return_pct=1,directional_return_pct=1,checked_at=NOW+3610))
    for i in range(0,n,500):
        c.execute(studies.insert(),values[i:i+500]);c.execute(flow_records.insert(),raw[i:i+500]);c.execute(checkpoints.insert(),out[i:i+500])
    return values


def test_full_sql_denominators_and_matched_selection_past_display_limits(storage):
    with storage.tx() as c:
        rows=bulk_rows(storage,c)
        r=report(storage,c,NOW+4000)
        assert r['inventory']['received_records']==r['counts']['registered']==10005
        assert not r['truncated'] and r['coverage']=='full_window'
        arms={x['arm']:x for x in r['comparisons'] if x['horizon']=='60m'}
        assert arms['all_matched']['measured']==10005
        assert arms['both']['records']==sum(x['tm_score']>=85 and x['compass_score']>=70 for x in rows)
        assert arms['both']['mean_directional_pct']==1
        assert arms['tm_only']['records']+arms['tm_rejected']['records']==10005
        assert sum(x['records'] for x in r['buckets'] if x['dimension']=='delay' and x['horizon']=='60m')==10005


def test_pages_reach_all_equal_time_records_and_exclude_later_inventory(storage):
    with storage.tx() as c:
        rows=bulk_rows(storage,c,350)
        first=record_page(c,NOW,cohort='tm_selected')
        page=first;ids=[]
        while True:
            ids.extend(p['id'] for p in page['records'])
            if not page['has_more']:break
            page=record_page(c,NOW+10,cohort='tm_selected',cursor=page['next_cursor'])
        assert len(ids)==len(set(ids))==175
        c.execute(update(studies).where(studies.c.id==ids[-1]).values(assessed_at=NOW+5))
        # Anchor also bounds registration/assessment time, not just old source time.
        assert record_page(c,NOW+10,cohort='tm_selected',asof=NOW)['total']==174
        with pytest.raises(ValueError):record_page(c,NOW+10,cohort='all',cursor=first['next_cursor'])


def test_api_requires_owner_and_validates_filters(tmp_path):
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'web.db'),
        password='fixture-password-16-chars',secret='fixture-secret')
    app=create_app(cfg)
    with TestClient(app) as client:
        for path in ('/api/tm-study','/api/tm-study/records','/api/tm-study/record?id='+'a'*64):
            assert client.get(path).status_code==401
        client.post('/login',json={'password':cfg.password})
        assert client.get('/api/tm-study/records?limit=101').status_code==422
        for query in ('cohort=bad','cursor=bad','asof=9999999999999'):
            assert client.get('/api/tm-study/records?'+query).status_code==400
        r=client.get('/api/tm-study/records')
        assert r.status_code==200 and r.json()['total']==0 and 'no-store' in r.headers['cache-control']
