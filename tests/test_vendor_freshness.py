import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from compass.config import Config
from compass.engine import Engine
from compass.flow_recovery import collect, freshness
from compass.readiness import decorate_health
from compass.research import FEEDS, normalize, catalog
from compass.scanner import Scanner
from compass.secondary import capture, build_report, comparisons, VERSION
from compass.vendor import matrix_summary, flow_page
from compass.vendor_freshness import confirmation, progress
from compass.vendor_schedule import matrix_health
from test_scanner import db, fact, quote, NOW
from test_premarket import matrix_fixture, flow_row


@pytest.mark.parametrize('changes,status',[
    ({'cached':True},'current'),
    ({'vendor_stale':True},'vendor_stale'),
    ({'received':NOW-181},'poll_stale'),
    ({'received':None},'poll_stale'),
    ({'source_ts':NOW-181},'stale'),
    ({'source_ts':None},'source_time_unknown'),
    ({'source_ts':NOW+1},'clock_error'),
    ({'received':NOW+1},'clock_error'),
])
def test_cache_flags_do_not_replace_either_clock(changes,status):
    check=confirmation(dict(source_ts=NOW-1,received=NOW,**{})|changes,NOW)
    assert check['status']==status
    assert check['eligible_for_live_confirmation']==(status=='current')


def test_matrix_stale_envelope_survives_parser_and_health(db):
    payload=matrix_fixture()
    payload['data']['snapshotTime']=datetime.fromtimestamp(NOW-1,timezone.utc).isoformat()
    payload['freshness']={'stale':True,'refreshSeconds':60}
    parsed=matrix_summary(payload,NOW)
    with db.tx() as c:
        db.put(c,'matrix:SPY',parsed)
        row=matrix_health(db,c,Config(local=True,stocks=('SPY',)),NOW)[0]
    assert parsed['vendor_stale'] and parsed['cached'] and row['status']=='vendor_stale'
    assert not row['eligible_for_live_confirmation']


def test_repeated_old_source_does_not_become_current_with_new_http_time():
    old=dict(source_ts=NOW-500,received=NOW-10)
    new=dict(source_ts=NOW-500,received=NOW,cached=True)
    new['source_progress']=progress(old,new)
    assert new['source_progress']['repeated_source_polls']==1
    assert new['source_progress']['source_first_seen_at']==NOW-10
    assert confirmation(new,NOW)['status']=='stale'
    assert progress(new,dict(source_ts=NOW-600,received=NOW+1))['source_regressed']


@pytest.mark.parametrize('stopped',[True,False])
def test_stale_flow_cannot_create_scanner_trade_or_secondary_confirmation(db,stopped):
    summary=dict(source_ts=NOW-1,received=NOW-61 if stopped else NOW,vendor_stale=not stopped,
        rows=[dict(symbol='SPY',source_ts=NOW-1,premium=200000,score=90,sentiment='Bullish',vendor_id='one')])
    cfg=Config(local=True,stocks=('SPY',),futures=(),scanner_paper=False)
    with db.tx() as c:
        db.put(c,'matrix:unusual_activity',summary)
        db.put(c,'scanner_features:SPY',fact())
        db.put(c,'quote:SPY',quote())
        Scanner(db,cfg).scan(c,NOW,Engine(db,cfg))
        inputs=capture(db,c,dict(project='morning',symbol='SPY'),cfg,NOW)
        assert not inputs['flow']
        assert not inputs['vendor_checks']['flow']['eligible_for_live_confirmation']
        assert not [r for r in db.recent(c,'alert') if r['payload'].get('status')=='setup_triggered']


def test_flow_health_cannot_overwrite_vendor_stale_with_current_clock():
    h=dict(name='tradermatrix_flow',status='vendor_stale',checked_at=NOW,source_ts=NOW,
        poll_ts=NOW,detail='fixture',freshness={'vendor_stale':True})
    result=decorate_health([h],{'worker:collector':{'at':NOW}},{'equities':True},NOW)[0]
    assert result['status']=='vendor_stale'


@pytest.mark.parametrize('source',[NOW-3600,None,NOW])
def test_closed_session_explains_event_age_without_enabling_confirmation(source):
    h=dict(name='tradermatrix_flow',status='event_stale',checked_at=NOW,source_ts=source,
        poll_ts=NOW,detail='fixture')
    workers={'worker:collector':{'at':NOW}}
    closed=decorate_health([h],workers,{'equities':False},NOW)[0]
    assert closed['status']=='market_closed' and not closed['eligible_for_live_confirmation']
    assert closed['source_ts']==source and h['status']=='event_stale'
    opened=decorate_health([h],workers,{'equities':True},NOW)[0]
    if source!=NOW:
        assert opened['status']==('no_events' if source is None else 'event_stale')
        assert not opened['eligible_for_live_confirmation']
    assert freshness({'source_ts':NOW-3600,'received':NOW},NOW)['status']=='event_stale'


@pytest.mark.parametrize('changes,status',[
    ({'status':'error'},'error'),
    ({'status':'partial'},'partial'),
    ({'status':'blocked'},'blocked'),
    ({'checked_at':NOW-181},'stale'),
    ({'poll_ts':NOW-61},'poll_stale'),
    ({'poll_ts':NOW+1},'clock_error'),
    ({'source_ts':NOW+1},'clock_error'),
    ({'freshness':{'vendor_stale':True}},'vendor_stale'),
])
def test_closed_session_never_hides_flow_failures(changes,status):
    h=dict(name='tradermatrix_flow',status='event_stale',checked_at=NOW,source_ts=NOW-3600,
        poll_ts=NOW,detail='fixture')|changes
    assert decorate_health([h],{'worker:collector':{'at':NOW}},{'equities':False},NOW)[0]['status']==status
    assert decorate_health([h],{'worker:web':{'at':NOW}},{'equities':False},NOW)[0]['status']=='stale'


def test_recovery_page_cannot_refresh_page_one_clock(db):
    calls=[]
    async def request(path,label):
        first='page=1&' in path
        calls.append(path)
        return dict(data=[flow_row(1 if first else 2)],total=101,page=1 if first else 2,pageSize=100,
            cached=True,freshness={'stale':True}), NOW if first else NOW+70
    result=asyncio.run(collect(SimpleNamespace(db=db,matrix_request=request),NOW,page_cap=2))
    assert len(calls)==2 and result['received']==NOW
    assert result['vendor_stale'] and result['cached']
    assert not freshness(result,NOW+70)['eligible_for_live_confirmation']
    result['vendor_stale']=False
    assert freshness(result,NOW+70)['status']=='poll_stale'


def test_research_reports_poll_age_vendor_stale_and_context_only(db):
    cfg=Config(local=True,matrix='fixture')
    with db.tx() as c:
        db.put(c,'health:research',{'status':'receiving'})
        db.put(c,'research:signals',dict(source_ts=NOW,received=NOW-181,items=[]))
        db.put(c,'research:earnings',dict(source_ts=NOW,received=NOW,items=[]))
        feed=next(f for f in FEEDS if f.key=='gamma_market')
        value=normalize(feed,{'data':{'SPY':{'symbol':'SPY','lastUpdated':datetime.fromtimestamp(NOW,timezone.utc).isoformat()}},
            'cached':True,'freshness':{'stale':True}},NOW)
        db.put(c,'research:gamma_market',value)
        rows={r['key']:r for r in catalog(db,c,cfg,NOW)}
    assert rows['signals']['status']=='poll_stale'
    assert rows['gamma_market']['status']=='vendor_stale'
    assert rows['gamma_market']['item_clocks'][0]['status']=='vendor_stale'
    assert not rows['earnings']['eligible_for_live_confirmation']


def test_error_flow_envelope_is_not_a_successful_poll():
    with pytest.raises(ValueError):
        flow_page(dict(success=False,data=[],total=0,page=1,pageSize=100),NOW)


def test_outcome_categories_reconcile_and_missing_does_not_mean_loss():
    rows=[]
    for verdict,value in [('supported',1),('supported',-1),('supported',0),('watch',1),('rejected',-1),('watch',0),('supported',None)]:
        point={'status':'missing'} if value is None else dict(status='observed',return_pct=value,observation_source='retained_quote')
        rows.append(dict(project='morning',candidate=dict(symbol='SPY',side='long',strategy='fixture'),version=VERSION,
            verdict=verdict,decision={'timely':True},measurements={'horizons':{'60':point}},source_outcome={}))
    h=comparisons(rows)[0]['horizons'][2]
    assert h['supported_positive']==h['supported_negative']==h['supported_flat']==1
    assert h['missed_positive']==h['avoided_negative']==h['skipped_flat']==1
    assert h['retained_quote_measurements']==h['measured']==6 and h['missing']==1


@pytest.mark.parametrize('since',[float('nan'),float('inf'),NOW+1,NOW-31*86400])
def test_report_rejects_invalid_period(db,since):
    with db.tx() as c, pytest.raises(ValueError):
        build_report(db,c,NOW,since=since)


def test_report_cutover_excludes_earlier_candidates(db):
    from compass.secondary import Secondary
    from compass.projects import ingest
    from test_secondary import seed, source
    worker=Secondary(db,Config(local=True))
    worker.tick(NOW-1)
    seed(db,NOW)
    with db.tx() as c: ingest(db,c,'morning',source(NOW),NOW)
    worker.tick(NOW)
    with db.tx() as c:
        assert build_report(db,c,NOW+10,since=NOW-1)['window']['reviewed']==1
        assert build_report(db,c,NOW+10,since=NOW+1)['window']['reviewed']==0
        assert build_report(db,c,NOW+10,since=NOW-1)['window']['reviewed']==1


def test_swing_waits_for_current_collection_before_quoted_entry(db):
    from compass.swing_ideas import SwingIdeas
    from test_swing_ideas import seed, signal, flow, records, NOW as SWING_NOW
    cfg=Config(local=True,stocks=('SPY',),futures=(),scanner_paper=False)
    worker=SwingIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c,SWING_NOW)
        worker.queue(c,signal(SWING_NOW),flow(SWING_NOW),SWING_NOW)
        db.put(c,'matrix:unusual_activity',dict(source_ts=SWING_NOW,received=SWING_NOW,vendor_stale=True))
        worker.pending(c,records(c)[0],SWING_NOW)
        row=records(c)[0]
        assert row['status']=='pending' and row['entry'] is None
        assert 'vendor_stale' in row['waiting_reason']


def test_documented_refresh_metadata_is_preserved_without_extending_eligibility():
    from compass.vendor_freshness import metadata, confirmation
    from datetime import datetime
    at=datetime.fromisoformat('2026-09-14T19:00:00+00:00').timestamp()
    data=metadata({'freshness':{'computedAt':'2026-09-14T19:00:00Z',
        'nextRefreshAt':'2026-09-14T19:05:00Z','refreshSeconds':300,'sessionState':'live','stale':False}})
    assert data['vendor_computed_ts']==at and data['vendor_next_refresh_ts']==at+300
    assert data['vendor_session_state']=='live'
    check=confirmation(dict(data,source_ts=at,received=at+301),at+301)
    assert check['vendor_refresh_overdue_seconds']==1
    assert not check['eligible_for_live_confirmation']
    assert metadata({'freshness':{'computedAt':'2026-09-14','nextRefreshAt':'invalid'}})['vendor_computed_ts'] is None


def test_response_timestamp_is_preserved_without_promoting_market_freshness():
    from compass.vendor_freshness import metadata, confirmation
    result=metadata({'timestamp':'2026-09-14T20:00:00Z'})
    at=result['vendor_envelope_ts']
    assert at is not None
    check=confirmation(dict(result,received=at),at)
    assert check['vendor_envelope_ts']==at
    assert check['status']=='source_time_unknown'
    assert not check['eligible_for_live_confirmation']
