import json
from datetime import datetime
import pytest
from sqlalchemy import select
from compass.daily_results import build, cohort, paper, morning_daily
from compass.market import CT, session
from compass.option_ideas import ideas
from compass.config import Config
from compass.native_morning_worker import initialize
from compass.native_morning import store as m
from compass.store import events
from compass.engine import Engine
from compass import paper_risk
from compass.setup_study import trials
from test_setup_reporting import report_db, pg
from test_setup_study import db, signal, quote

NOW=datetime(2026,9,21,16,25,tzinfo=CT).timestamp()
OPEN=session('2026-09-21')[0]


def test_complete_options_counts_no_recent_record_cap(report_db):
    with report_db.tx() as c:
        c.execute(ideas.insert(),[dict(id=str(i),underlying='QQQ',status='closed' if i<80 else 'unresolved' if i<120 else 'excluded',created=OPEN+1,updated=NOW,payload=dict(underlying='QQQ',strategy='test',version='v1',pnl=10 if i<30 else -2 if i<80 else None,exit_reason='missing_quote')) for i in range(1801)])
        r=cohort(c,ideas,ideas.c.created,OPEN,NOW)
        assert r['totals']['total']==1801 and r['totals']['closed']==80
        assert r['totals']['wins']==30 and r['totals']['losses']==50
        assert r['totals']['unresolved']==40 and r['totals']['excluded']==1681
        assert r['totals']['net_pnl']==200 and r['counts_complete']


def test_paper_exit_day_is_not_entry_cohort_and_missing_is_not_loss(report_db):
    start=OPEN-15.5*3600
    with report_db.tx() as c:
        for id,entered,exited,status,pnl in [('old',start-10,start+10,'closed',40),('new',start+1,None,'open',None),('missing',start+1,start+20,'closed',None),('end',NOW,NOW,'closed',100)]:
            report_db.put(c,'trade:'+id,dict(asset='future',symbol='MESZ6@1',strategy='test',status=status,pnl=pnl,entered_at=entered,exited_at=exited))
        r=paper(c,start,NOW)
        assert r['entries'][0]['total']==2 and r['entries'][0]['open']==1
        assert r['realized'][0]['closed']==2 and r['realized'][0]['wins']==1
        assert r['realized'][0]['missing_pnl']==1 and r['realized'][0]['losses']==0
        assert r['realized'][0]['net_pnl']==40


def test_loss_lock_keeps_overnight_and_cash_research_open_and_reported(report_db,monkeypatch):
    initialize(report_db)
    cfg=Config(local=True,paper_trading=True);engine=Engine(report_db,cfg)
    overnight=datetime(2026,9,20,18,tzinfo=CT).timestamp()
    with report_db.tx() as c:
        risk=paper_risk.ledgers(report_db,c,overnight,persist=True)['future']
        risk.update(realized=-300,entries=30);report_db.put(c,paper_risk.key(overnight,'future'),risk)
        for i,now in enumerate((overnight,OPEN+300)):
            monkeypatch.setattr('compass.engine.time.time',lambda:now)
            report_db.put(c,'quote:MESZ6@1',quote(now))
            assert not engine.enter(c,{**signal(str(i)), 'signal_time':now},now,quiet=i==1)
        observations=list(c.execute(select(trials.c.payload)).scalars())
        assert len(observations)==2 and all(r['status']=='open' for r in observations)
        assert not report_db.prefix(c,'position:')
        r=build(report_db,c,cfg,NOW,'2026-09-21')
        assert r['futures']['totals']['open']==2
        assert r['post_lock']['first_recorded_block_at']==overnight
        assert r['post_lock']['cohort']['totals']['open']==2
        assert paper_risk.account(report_db,c,overnight,'future')['realized']==-300
        assert sum(x['count'] for x in r['skips'])==2
        assert r['options']['totals']['total']==0


def test_native_census_exceeds_100_and_reports_original_gap(report_db):
    initialize(report_db)
    with report_db.tx() as c:
        c.execute(m.signals.insert(),[dict(signal_id=str(i),ticker='QQQ',signal_at_ms=int((OPEN+300)*1000),received_at_ms=int(OPEN*1000),signal_json=json.dumps(dict(signal_id=str(i),price=100,signal_at_ms=int((OPEN+300)*1000),setup='FAST_OPEN',settings={'checkpoints':True})),signal_hash=str(i),is_test=False) for i in range(205)])
        r=morning_daily(c,OPEN,NOW,NOW)
        assert r['signals']==205 and r['native_without_original']==205
        assert r['original_signals']==0 and r['source_status']=='original_source_gap'
        assert all(x['measured']==0 and x['unmeasured']==205 for x in r['options'])


def test_empty_day_weekend_default_future_date_and_verification(report_db):
    initialize(report_db)
    with report_db.tx() as c:
        r=build(report_db,c,Config(local=True),NOW,'2026-09-21')
        assert r['day']=='2026-09-21' and r['post_lock']['status']=='no_loss_block_recorded'
        assert r['verification']['spy_daily']['status']=='awaiting_session_evidence'
        assert r['verification']['ask_options']==[]
        assert r['options']['totals']['net_pnl'] is None
        with pytest.raises(ValueError):build(report_db,c,Config(local=True),NOW,'2026-09-22')
        with pytest.raises(ValueError):build(report_db,c,Config(local=True),NOW,'2026-09-20')


def test_ask_evidence_preserves_horizon_and_reports_stale_quotes(db):
    from compass.assistant_options import record_evidence, request
    result=dict(request=request('QQQ LEAPS calls',NOW),symbols={'QQQ':dict(status='available',source='massive',candidates=[dict(expiry='2028-01-21',quote_status='stale')])})
    record_evidence(db,result,NOW)
    with db.tx() as c:
        saved=db.prefix(c,'ask-evidence:2026-09-21:')
        row=next(iter(saved.values()))
        assert row['horizon']=='leaps' and row['fresh_quotes']==0 and row['candidate_count']==1
        assert row['returned_expirations']==['2028-01-21']


def test_daily_endpoint_authentication_and_validation(tmp_path,monkeypatch):
    from compass.app import create_app
    from compass.store import Store
    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeTimedSerializer
    db=Store('sqlite:///'+str(tmp_path/'api.db'));db.initialize();initialize(db)
    cfg=Config(local=True,db='sqlite:///'+str(tmp_path/'api.db'),password='test-only',secret='daily-test',role='web')
    client=TestClient(create_app(cfg))
    assert client.get('/api/daily-results').status_code==401
    monkeypatch.setattr('compass.app.time.time',lambda:NOW)
    client.cookies.set('compass_session',URLSafeTimedSerializer(cfg.secret).dumps('owner'))
    assert client.get('/api/daily-results?session_day=bad').status_code==422
    response=client.get('/api/daily-results?session_day=2026-09-21&download=true')
    assert response.status_code==200
    assert response.json()['counts_complete']
    assert 'daily-results.json' in response.headers['content-disposition']


def test_native_fixed_horizons_count_quotes_not_missing_as_losses(report_db):
    initialize(report_db)
    at=int((OPEN+300)*1000)
    ref=dict(status='available',ask=1,bid=.9,quote_at_ms=at,contract={'symbol':'QQQ260921C00729000'})
    good=dict(status='available',bid=2,ask=2.1,quote_at_ms=at+300000,sampled_at_ms=at+300000,contract=ref['contract'])
    bad={**good,'contract':{'symbol':'different'}}
    with report_db.tx() as c:
        c.execute(m.signals.insert(),dict(signal_id='q',ticker='QQQ',signal_at_ms=at,received_at_ms=at,signal_json=json.dumps(dict(price=100,signal_at_ms=at,settings={'checkpoints':True})),signal_hash='q',is_test=False))
        c.execute(m.option_jobs.insert(),dict(event_id='q-option',signal_id='q',policy_json='{}',status='complete',quote_json=json.dumps(ref)))
        c.execute(m.option_samples.insert(),[dict(signal_id='q',minute=h,due_at_ms=at+h*60000,status='complete',quote_json=json.dumps(q)) for h,q in ((5,good),(15,bad))])
        r=morning_daily(c,OPEN,NOW,NOW)
        five=next(x for x in r['options'] if x['variant']=='current_contract' and x['minutes']==5)
        fifteen=next(x for x in r['options'] if x['variant']=='current_contract' and x['minutes']==15)
        assert five['wins']==1 and five['net_pnl']==pytest.approx(98.7)
        assert fifteen['measured']==0 and fifteen['losses']==0 and fifteen['unmeasured']==1


def test_direct_receipts_do_not_fabricate_original_parity(report_db):
    from compass.native_routing import receipts
    initialize(report_db)
    with report_db.tx() as c:
        c.execute(m.signals.insert(),dict(signal_id='direct',ticker='QQQ',signal_at_ms=int((OPEN+300)*1000),received_at_ms=int(OPEN*1000),signal_json=json.dumps(dict(signal_id='direct',price=100,signal_at_ms=int((OPEN+300)*1000),setup='FAST_OPEN',settings={'checkpoints':True})),signal_hash='direct',is_test=False))
        c.execute(receipts.insert(),[dict(id=str(i),signal_id='direct',event_id='evt'+str(i),origin='direct',received=OPEN+300,payload={'entry':True,'status':'accepted' if i==0 else 'duplicate'}) for i in range(2)])
        r=morning_daily(c,OPEN,NOW,NOW)
        assert r['direct_received_signals']==1
        assert r['native_without_original']==1
        assert r['original_comparison_available'] is False
