from copy import deepcopy
from datetime import datetime

import pytest

from compass import futures_feed_study as feeds
from compass.engine import spec
from compass.market import CT
from compass.setup_study import SetupStudy
from test_setup_study import NOW, cfg, db, quote, rows, signal


def context(db, c, now=NOW, sentiment='bullish'):
    for symbol in ('SPY', 'QQQ'):
        db.put(c, 'quote:'+symbol, quote(now))
        db.put(c, 'scanner_features:'+symbol, dict(status='ready', asof=now-30, vwap=99, htf15_bias=1))
        db.put(c, 'matrix:'+symbol, dict(source_ts=now-10, received=now-1,
            strikes=[dict(strike=100, gex=5, vex=-3), dict(strike=101, gex=-2, vex=1)]))
    flow = dict(vendor_id='1', symbol='SPY', source_ts=now-10, received=now-1,
        score=90, premium=200000, sentiment=sentiment)
    db.put(c, 'matrix:unusual_activity', dict(source_ts=now-10, received=now-1, rows=[flow]))
    return flow


def freeze(db, c, symbol='MESZ6@1', now=NOW, eligible=True):
    return feeds.freeze(db, c, signal(symbol=symbol), now, eligible,
        dict(state='trend', version='futures-market-assessment-v1'))


def test_activation_waits_for_full_session_and_survives_restart(db):
    friday = datetime(2026, 9, 18, 9, 0, tzinfo=CT).timestamp()
    with db.tx() as c:
        a = feeds.activate(db, c, friday)
        assert a['first_full_session'] == '2026-09-21'
        assert len(a['sessions']) == 10 and len({s['day'] for s in a['sessions']}) == 10
        assert all(s['open'] >= friday and s['close'] > s['open'] for s in a['sessions'])
        assert a['evaluation_end'] == a['sessions'][-1]['close']
        assert feeds.activate(db, c, friday+100000) == a
        assert freeze(db, c, now=friday)['phase'] == 'warmup'
        assert freeze(db, c, now=datetime(2026,9,19,9,tzinfo=CT).timestamp())['phase'] == 'warmup'
        assert freeze(db, c, now=a['sessions'][0]['open'])['phase'] == 'evaluation'
        assert freeze(db, c, now=a['evaluation_end']) is None


def test_activation_exact_open_can_use_that_complete_session(db):
    opening = datetime(2026, 9, 20, 17, tzinfo=CT).timestamp()
    with db.tx() as c:
        assert feeds.activate(db, c, opening)['first_full_session'] == '2026-09-21'


@pytest.mark.parametrize('hour,minute,expected', [(17,0,'overnight'),(6,59,'overnight'),
    (7,0,'preopen'),(8,29,'preopen'),(8,30,'opening'),(10,0,'midday'),(13,0,'afternoon'),(15,0,'late')])
@pytest.mark.parametrize('month', [1,7])
def test_dayparts_follow_chicago_dst(month,hour,minute,expected):
    now=datetime(2026,month,12,hour,minute,tzinfo=CT).timestamp()
    assert feeds.daypart(now)==expected


def test_all_twelve_keep_price_baseline_without_inventing_feed_proxies(db):
    with db.tx() as c:
        context(db,c)
        for root in ('ES','MES','NQ','MNQ','YM','MYM','GC','MGC','SI','SIL','CL','MCL'):
            f=freeze(db,c,symbol=root+'Z6@1')
            assert f['arms']['price_only']=='selected'
            assert f['regime']=='trend' and f['daypart']=='opening'
            assert f['arms']['cross_market']==('selected' if root in feeds.INDEX_FUTURES else 'not_applicable')
            assert f['arms']['flow']==('selected' if root in ('ES','MES') else 'skipped' if root in ('NQ','MNQ') else 'not_applicable')
            if root not in feeds.PROXIES:
                assert f['arms']['combined']=='not_applicable'
                assert f['exposure']['gex_state']=='not_applicable'


def test_frozen_trial_is_unchanged_by_feed_updates_duplicate_signals_and_exits(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        context(db,c)
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'),alerted=False)
        original=deepcopy(rows(c)[0])
        assert set(original['feed_comparison']['arms'].values())=={'selected'}
        context(db,c,sentiment='bearish')
        study.start(c,signal(),NOW+1,spec('MESZ6@1'))
        assert rows(c)==[original]
        db.put(c,'quote:MESZ6@1',quote(NOW+2,original['target'],original['target']+.25))
        study.tick(c,NOW+2)
        p=rows(c)[0]
        assert p['feed_comparison']==original['feed_comparison']
        assert p['exit_reason']=='target' and p['pnl']==17
        assert db.get(c,'setup_study:report') is None
        study.refresh_report(c,NOW+2)
        assert db.get(c,'setup_study:report')['feed_comparison']['groups']


def test_existing_trial_does_not_get_reconstructed_evidence(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        p=rows(c)[0]
        p.pop('feed_comparison')
        study.save(c,p)
        context(db,c)
        study.start(c,signal(),NOW+1,spec('MESZ6@1'))
        assert 'feed_comparison' not in rows(c)[0]


@pytest.mark.parametrize('case',['stale_quote','future_quote','stale_bar','missing_bias','missing_vwap'])
def test_incomplete_cross_market_context_is_unknown_not_skip(db,case):
    with db.tx() as c:
        context(db,c)
        q=db.get(c,'quote:QQQ');f=db.get(c,'scanner_features:QQQ')
        if case=='stale_quote':q['ts']=NOW-6
        if case=='future_quote':q['ts']=NOW+.5
        if case=='stale_bar':f['asof']=NOW-91
        if case=='missing_bias':f.pop('htf15_bias')
        if case=='missing_vwap':f.pop('vwap')
        db.put(c,'quote:QQQ',q);db.put(c,'scanner_features:QQQ',f)
        result=freeze(db,c)
        assert result['arms']['cross_market']==result['arms']['combined']=='unknown'
        assert result['arms']['price_only']=='selected'


def test_known_peer_disagreement_and_contradictory_flow_skip(db):
    with db.tx() as c:
        first=context(db,c)
        f=db.get(c,'scanner_features:QQQ');f['htf15_bias']=0
        db.put(c,'scanner_features:QQQ',f)
        assert freeze(db,c)['arms']['cross_market']=='skipped'
        summary=db.get(c,'matrix:unusual_activity')
        summary['rows']=[first]*10+[dict(first,vendor_id='2',sentiment='bearish')]
        db.put(c,'matrix:unusual_activity',summary)
        result=freeze(db,c)
        assert result['arms']['flow']==result['arms']['combined']=='skipped'
        assert result['flow']['qualifying_rows']==2


def test_flow_decision_uses_rows_beyond_preview_and_short_direction(db):
    with db.tx() as c:
        first=context(db,c)
        summary=db.get(c,'matrix:unusual_activity')
        summary['rows']=[dict(first,vendor_id=str(i)) for i in range(9)]+[dict(first,vendor_id='10',sentiment='bearish')]
        db.put(c,'matrix:unusual_activity',summary)
        result=freeze(db,c)
        assert result['arms']['flow']=='skipped' and result['flow']['qualifying_rows']==10
        assert len(result['flow']['rows'])==8
        summary['rows']=[dict(first,sentiment='bearish')]
        db.put(c,'matrix:unusual_activity',summary)
        assert feeds.flow_context(db,c,'SPY',-1,NOW,True)['selection']=='selected'


@pytest.mark.parametrize('case',['missing','stale','poll_stale','future','source_after_receipt','vendor_stale','row_future_receipt','bad_sentiment'])
def test_invalid_flow_is_unknown(db,case):
    with db.tx() as c:
        context(db,c)
        summary=db.get(c,'matrix:unusual_activity')
        if case=='missing':summary={}
        if case=='stale':summary['source_ts']=NOW-121
        if case=='poll_stale':summary['received']=NOW-61
        if case=='future':summary['source_ts']=NOW+1
        if case=='source_after_receipt':summary['received']=NOW-11
        if case=='vendor_stale':summary['vendor_stale']=True
        if case=='row_future_receipt':summary['rows'][0]['received']=NOW+1
        if case=='bad_sentiment':summary['rows'][0]['sentiment']='unclassified'
        db.put(c,'matrix:unusual_activity',summary)
        assert freeze(db,c)['arms']['flow']=='unknown'


def test_no_qualifying_flow_is_known_skip_only_with_current_snapshot(db):
    with db.tx() as c:
        context(db,c)
        summary=db.get(c,'matrix:unusual_activity');summary['rows'][0]['score']=84
        db.put(c,'matrix:unusual_activity',summary)
        assert freeze(db,c)['arms']['flow']=='skipped'
        summary['source_ts']=None
        db.put(c,'matrix:unusual_activity',summary)
        assert freeze(db,c)['arms']['flow']=='unknown'


def test_cash_closed_and_excluded_entries_stay_explicit(db):
    night=datetime(2026,9,14,20,tzinfo=CT).timestamp()
    with db.tx() as c:
        context(db,c,night)
        result=freeze(db,c,now=night)
        assert result['arms']['price_only']=='selected'
        assert result['arms']['combined']=='unknown'
        assert result['exposure']['status']=='cash_market_closed'
        assert set(freeze(db,c,now=night,eligible=False)['arms'].values())=={'excluded'}


def test_exposure_is_nondirectional_and_incomplete_metrics_remain_unknown(db):
    with db.tx() as c:
        context(db,c)
        exposure=freeze(db,c)['exposure']
        assert exposure['gex_sum']==3 and exposure['vex_sum']==-2
        assert exposure['gex_state']=='positive' and exposure['vex_state']=='negative'
        obj=db.get(c,'matrix:SPY');obj['strikes'][1].pop('gex')
        db.put(c,'matrix:SPY',obj)
        result=freeze(db,c)
        assert result['exposure']['gex_state']=='unknown'
        assert result['exposure']['vex_state']=='negative'
        assert result['arms']['combined']=='selected'
        obj['source_ts']=NOW-181;db.put(c,'matrix:SPY',obj)
        assert freeze(db,c)['exposure']['vex_state']=='unknown'
