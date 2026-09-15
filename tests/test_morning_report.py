import copy
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from compass.app import create_app
from compass.config import Config
from compass.store import Store, events
from compass.morning_history import accept_page, digest, history, save
from compass.morning_report import build_report
from compass.morning_schema.outcomes import stock_coverage, stock_exit, path_outcome

ROOT=Path(__file__).parent/'fixtures'
FIXTURE=json.loads((ROOT/'morning_history.json').read_text())
GOLDEN=json.loads((ROOT/'morning_outcomes_golden.json').read_text())
NOW=GOLDEN['now_ms']/1000

@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'report.db'));db.initialize()
    for stream,keys in [('events',['signal','checkpoint']),('research',['research'])]:
        rows=[{'id':FIXTURE[k]['event_id'],'payload':FIXTURE[k],'sha256':digest(FIXTURE[k]),'received_at_ms':int((NOW-5)*1000)} for k in keys]
        accept_page(db,stream,{'schema_version':1,'project':'morning','mode':'observe_only','stream':stream,
            'since_ms':0,'next':None,'records':sorted(rows,key=lambda r:r['id'])},'',NOW)
    yield db
    db.engine.dispose()

def test_report_matches_original_source_report_on_same_imports(db):
    with db.tx() as c:
        report=build_report(db,c,NOW)
        for section,identifier in [('signals','signal_id'),('candidates','record_id')]:
            actual={r[identifier]:r for r in report[section]}
            for expected in GOLDEN[section]:
                assert {k:actual[expected[identifier]][k] for k in expected}==expected
        assert report['stock_comparisons'][0]['paired_stock_n']==1
        assert all(g['outcomes']['180']['complete']==0 for g in report['candidate_comparisons'])
        assert c.execute(select(func.count()).select_from(events)).scalar_one()==0
        assert report['cutover_ready'] is False

@pytest.mark.parametrize('case',GOLDEN['scenarios'],ids=lambda s:s['name'])
def test_source_golden_complete_missing_and_ambiguous_paths(case):
    s,b=case['signal'],case['bars']
    assert stock_coverage(s,b,True,int(NOW*1000))==case['coverage']
    actual=[stock_exit(s,b,h) for h in (5,15,30,60)]+[stock_exit(s,b,target=t,stop=stop) for t,stop in ((1.,.5),(2.,1.))]
    assert actual==case['exits']
    assert path_outcome(s['price'],s['signal_at_ms'],b,60,int(NOW*1000))==case['path']

def test_filters_apply_before_limit_and_report_partial_cohorts(db):
    with db.tx() as c:
        s=copy.deepcopy(FIXTURE['signal']['signal']);s.update(signal_id='new-other',ticker='NASDAQ:OTHER',signal_at_ms=s['signal_at_ms']+60000)
        save(db,c,'signal',s['signal_id'],s['signal_id'],s,NOW-5,NOW)
        limited=build_report(db,c,NOW,limit=1)
        assert limited['truncated']=={'signals':True,'candidates':True}
        assert limited['signals'][0]['ticker']=='NASDAQ:OTHER'
        filtered=build_report(db,c,NOW,limit=1,ticker='DEMO',start='2026-09-08',end='2026-09-08')
        assert filtered['signals'][0]['ticker']=='NASDAQ:DEMO'
        assert not filtered['truncated']['signals']
        assert build_report(db,c,NOW,stream='unknown')['signals']==[]
        with pytest.raises(ValueError):build_report(db,c,NOW,start='2026-09-09',end='2026-09-08')
        with pytest.raises(ValueError):build_report(db,c,NOW,limit=201)

def test_shared_cohort_excludes_same_candle_ambiguity(db):
    with db.tx() as c:
        row=c.execute(select(history).where(history.c.kind=='signal_bar').order_by(history.c.source_id)).mappings().first()
        # Corrupt fixture prices deliberately to exercise reporting, not the guarded importer.
        c.execute(history.update().where(history.c.key==row['key']).values(payload={**row['payload'],'high':103.,'low':98.}))
        report=build_report(db,c,NOW)
        group=report['stock_comparisons'][0]
        assert group['ambiguous_excluded']==1 and group['paired_stock_n']==0
        assert all(r['avg_return_pct'] is None and r['n']==0 for r in group['stock_rules'])

def test_report_and_download_require_owner_session(tmp_path):
    cfg=Config(local=True,role='web',db='sqlite:///'+str(tmp_path/'app.db'),password='private-test-password',secret='s'*32)
    with TestClient(create_app(cfg)) as client:
        route='/api/projects/morning/report'
        assert client.get(route).status_code==401
        from itsdangerous import URLSafeTimedSerializer
        client.cookies.set('compass_session',URLSafeTimedSerializer(cfg.secret).dumps('owner'))
        assert client.get(route+'?limit=201').status_code==422
        assert client.get(route+'?start=bad-date').status_code==422
        r=client.get(route+'?download=true&ticker=DEMO')
        assert r.status_code==200 and 'attachment' in r.headers['content-disposition']
        assert r.json()['scope']['ticker']=='DEMO' and r.json()['signals']==[]

def test_extended_baseline_requires_matching_identity_and_exposes_candle_conflicts(db):
    batch=FIXTURE['research'];session=batch['session'];baseline=next(r for r in batch['records'] if r['kind']=='BASELINE')
    s={**FIXTURE['signal']['signal'],**{k:session[k] for k in ('ticker','stream_id','script_version','logic_mode','timeframe_min','settings','is_test')},
        'signal_id':baseline['source_signal_id'],'signal_at_ms':baseline['at_ms'],'bar_open_ms':baseline['at_ms']-60000,
        'price':baseline['price'],'features':baseline['features'],'setup':baseline['baseline_setup']}
    with db.tx() as c:
        save(db,c,'signal',s['signal_id'],s['signal_id'],s,NOW-5,NOW)
        for i in range(180):
            b={**batch['bars'][0],'open_at_ms':s['signal_at_ms']+i*60000}
            save(db,c,'research_bar',session['session_id'],str(b['open_at_ms']),b,NOW-5,NOW)
        r=next(r for r in build_report(db,c,NOW)['signals'] if r['signal_id']==s['signal_id'])
        assert r['extended_checkpoints']['180']['status']=='complete'
        assert r['stock_history']['status']=='not_recorded'
        b={**batch['bars'][0],'open_at_ms':s['signal_at_ms'],'high':104.}
        save(db,c,'signal_bar',s['signal_id'],str(b['open_at_ms']),b,NOW-5,NOW)
        r=next(r for r in build_report(db,c,NOW)['signals'] if r['signal_id']==s['signal_id'])
        assert r['extended_checkpoints']['180']['status']=='incomplete'
        assert r['extended_stock_history']['conflicting_bar_open_ms']==[s['signal_at_ms']]
