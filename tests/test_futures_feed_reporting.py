import pytest

from compass import futures_feed_reporting as reporting, futures_feed_study as feeds
from compass.setup_study import trials
from test_setup_reporting import insert, trial, report_db, db, pg
from test_setup_study import NOW


def activation(end=NOW+100):
    return dict(at=NOW-1000000,evaluation_end=end)


def row(id, choice='selected', r=1, status='closed', phase='evaluation', session='2026-09-14', **kw):
    return trial(id,asset='future',symbol='MESZ6@1',status=status,r_multiple=r,
        feed_comparison=dict(version=feeds.VERSION,phase=phase,session=session,daypart='opening',regime='trend',
            exposure=dict(gex_state='positive',vex_state='negative'),
            arms=dict(price_only='excluded' if choice=='excluded' else 'selected',
                cross_market=choice,flow=choice,combined=choice)),**kw)


def group(report,arm='combined',view='groups',**where):
    return next(g for g in report[view] if g['arm']==arm and all(g[k]==v for k,v in where.items()))


def test_common_opportunity_denominator_and_missing_paths(report_db):
    examples=[row(1,r=2),row(2,'skipped',r=-1),row(3,'unknown',r=100),
        row(4,'selected',None,'unresolved'),row(5,'skipped',None,'unresolved'),
        row(6,'selected',None,'open'),row(7,'skipped',None,'open'),
        row(8,'not_applicable',r=-100),row(9,'excluded',None,'excluded'),
        row(10,'selected',None,'closed')]
    with report_db.tx() as c:
        insert(c,examples)
        result=reporting.report(c,trials,NOW,activation(NOW))
        for view in ('groups','conditions','exposure_groups'):
            g=group(result,view=view)
            assert g['total']==10
            assert (g['selected'],g['skipped'],g['unknown'],g['not_applicable'],g['excluded'])==(4,3,1,1,1)
            assert g['paired_closed']==2 and g['selected_closed']==1
            assert g['open']==2 and g['unresolved']==3
            assert g['baseline_r_per_opportunity']==.5
            assert g['filtered_r_per_opportunity']==1
            assert g['difference_r_per_opportunity']==.5
            assert g['selected_mean_r']==2
            assert g['session_cluster_95'] is None


def test_full_cohort_no_trial_cap_and_old_evidence_not_backfilled(report_db):
    examples=[row(i,'skipped',r=-1) for i in range(10001)]
    old=row(20000,r=100);old.pop('feed_comparison')
    wrong=row(20001,r=100);wrong['feed_comparison']['version']='other-version'
    with report_db.tx() as c:
        insert(c,examples+[old,wrong,row(20002,r=100,started=NOW+1),row(20003,r=100,started=NOW-1000001)])
        result=reporting.report(c,trials,NOW,activation(NOW))
        g=group(result)
        assert g['total']==g['paired_closed']==10001
        assert g['filtered_r_per_opportunity']==0
        assert g['difference_r_per_opportunity']==1
        assert g['selected_mean_r'] is None


def test_evaluation_returns_locked_but_warmup_and_coverage_visible(report_db):
    with report_db.tx() as c:
        insert(c,[row(1,'skipped',r=-100),row(2,'selected',r=1,phase='warmup')])
        result=reporting.report(c,trials,NOW,activation())
        g=group(result,phase='evaluation')
        assert g['outcomes_locked'] and g['paired_closed']==1
        for field in ('baseline_r_per_opportunity','filtered_r_per_opportunity',
            'difference_r_per_opportunity','selected_mean_r','session_cluster_95'):
            assert g[field] is None
        assert group(result,phase='warmup')['baseline_r_per_opportunity']==1


def test_contract_model_cohort_and_conditions_never_silently_pool(report_db):
    a=row(1);b=row(2,alerted=False);d=row(3,fill_version='other')
    e=row(4);e['feed_comparison']['daypart']='midday'
    f=row(5);f['feed_comparison']['regime']='mixed'
    h=row(6);h['feed_comparison']['exposure']['gex_state']='unknown'
    with report_db.tx() as c:
        insert(c,[a,b,d,e,f,h])
        result=reporting.report(c,trials,NOW,activation(NOW))
        assert len(result['groups'])==3*4
        assert len(result['conditions'])==5*4
        assert len(result['exposure_groups'])==4*4


def test_session_cluster_inference_needs_ten_sessions_end_and_no_open(report_db):
    examples=[row(i,'skipped',r=-1,session=f'2026-09-{i+1:02}') for i in range(10)]
    with report_db.tx() as c:
        insert(c,examples)
        locked=group(reporting.report(c,trials,NOW,activation()))
        assert locked['session_cluster_95'] is None
        unlocked=reporting.report(c,trials,NOW,activation(NOW))
        assert group(unlocked)['session_cluster_95']==[1,1]
        assert group(unlocked)['usable_sessions']==10
        assert group(unlocked,'flow')['session_cluster_95'] is None
        assert group(unlocked,view='conditions')['session_cluster_95'] is None
        insert(c,[row(100,'skipped',None,'open')])
        assert group(reporting.report(c,trials,NOW,activation(NOW)))['session_cluster_95'] is None


def test_bootstrap_is_reproducible_and_uses_session_clusters():
    daily={f'day{i}':[i+1,float(i-4)] for i in range(10)}
    result=reporting.cluster_interval(daily)
    assert result==reporting.cluster_interval(dict(reversed(list(daily.items()))))
    assert result[0]<result[1]
    assert reporting.cluster_interval(dict(list(daily.items())[:9])) is None
