from copy import deepcopy
import pytest
from compass import futures_variants as variants
from compass.setup_study import SetupStudy, report_for
from compass.engine import spec
from test_setup_study import db, cfg, NOW, quote, signal, rows

SYMBOL = 'MESZ6@1'


def bar(at=NOW, bias=1, low=101, high=104, close=103):
    return dict(status='ready', asof=at, htf15_bias=bias, ema9=102, ema21=100,
                bar=dict(l=low, h=high, c=close))


def classify(db,c,id='a',at=NOW,side='long',strategy='study-fixture',symbol=SYMBOL):
    return variants.classify(db,c,dict(signal(id,side,symbol),signal_time=at,strategy=strategy),at,True)


def test_repeated_first_and_completed_pullback_are_distinct(db):
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        first=classify(db,c)
        assert all(first[name]=='selected' for name in variants.NAMES)
        again=classify(db,c,'b')
        assert again['repeated']=='selected'
        assert again['first_per_trend']==again['pullback_reset']=='skipped'
        # Entry bar's touch cannot be reused. A later touch+reclaim on one
        # candle cannot establish intrabar ordering either.
        variants.observe_bar(db,c,SYMBOL,bar(NOW+60,low=99),NOW+60)
        assert classify(db,c,'c',NOW+60)['pullback_reset']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+120),NOW+120)
        reset=classify(db,c,'d',NOW+120)
        assert reset['pullback_reset']=='selected' and reset['first_per_trend']=='skipped'
        assert classify(db,c,'e',NOW+120)['pullback_reset']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+180),NOW+180)
        assert classify(db,c,'f',NOW+180)['pullback_reset']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+240,low=99),NOW+240)
        variants.observe_bar(db,c,SYMBOL,bar(NOW+300),NOW+300)
        assert classify(db,c,'g',NOW+300)['pullback_reset']=='selected'


def test_restart_same_bar_and_separate_rules_contracts(db):
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        assert classify(db,c)['first_per_trend']=='selected'
    with db.tx() as c:
        before=db.get(c,variants.PREFIX+SYMBOL)
        variants.observe_bar(db,c,SYMBOL,bar(bias=-1),NOW+1)
        assert db.get(c,variants.PREFIX+SYMBOL)==before
        assert classify(db,c,'b')['first_per_trend']=='skipped'
        assert classify(db,c,'c',strategy='another-rule')['first_per_trend']=='selected'
        variants.observe_bar(db,c,'SIZ6@2',bar(),NOW)
        assert classify(db,c,'d',symbol='SIZ6@2')['first_per_trend']=='selected'
        assert classify(db,c,'e',side='short')['first_per_trend']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+60,bias=-1),NOW+60)
        assert classify(db,c,'f',NOW+60,side='short')['first_per_trend']=='selected'


def test_gap_never_mints_first_entry_until_observed_transition(db):
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        classify(db,c)
        variants.observe_bar(db,c,SYMBOL,bar(NOW+120,bias=-1),NOW+120)
        assert classify(db,c,'b',NOW+120,side='short')['first_per_trend']=='unknown'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+180,bias=-1),NOW+180)
        assert classify(db,c,'c',NOW+180,side='short')['first_per_trend']=='unknown'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+240,bias=0),NOW+240)
        assert classify(db,c,'d',NOW+240)['first_per_trend']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+300),NOW+300)
        assert classify(db,c,'e',NOW+300)['first_per_trend']=='selected'


@pytest.mark.parametrize('case',['stale','future','no_bias','warmup','wrong_signal_bar'])
def test_invalid_context_keeps_baseline_and_explicit_unknown(db,case):
    with db.tx() as c:
        f=bar(NOW-120 if case=='stale' else NOW+60 if case=='future' else NOW)
        if case=='no_bias': f['htf15_bias']=None
        if case=='warmup': f['status']='warming_up'
        variants.observe_bar(db,c,SYMBOL,f,NOW)
        result=classify(db,c,at=NOW+1 if case=='wrong_signal_bar' else NOW)
        assert result['repeated']=='selected'
        assert result['first_per_trend']==result['pullback_reset']=='unknown'


def test_excluded_trial_does_not_consume_first_and_duplicates_stay_frozen(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        study.start(c,signal('a'),NOW,spec(SYMBOL))  # missing quote
        assert rows(c)[0]['entry_variants']['first_per_trend']=='excluded'
        db.put(c,'quote:'+SYMBOL,quote())
        study.start(c,signal('b'),NOW,spec(SYMBOL))
        original=deepcopy(rows(c)[1])
        assert original['entry_variants']['first_per_trend']=='selected'
        study.start(c,signal('b'),NOW+1,spec(SYMBOL))
        assert rows(c)[1]==original
        study.start(c,signal('c'),NOW,spec(SYMBOL))
        assert rows(c)[2]['entry_variants']['first_per_trend']=='skipped'
        assert len(rows(c))==3


def test_future_bar_cannot_poison_the_durable_cursor(db):
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(NOW+3600),NOW)
        assert db.get(c,variants.PREFIX+SYMBOL) is None
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        assert classify(db,c)['first_per_trend']=='selected'


def test_report_shares_original_outcomes_and_preserves_missing_observations(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(),NOW)
        db.put(c,'quote:'+SYMBOL,quote())
        for id in ('a','b','c'):
            study.start(c,signal(id),NOW,spec(SYMBOL))
        a,b,c_trial=rows(c)
        study.finish(c,a,NOW+1,'target',a['target'])
        study.finish(c,b,NOW+1,'stop',b['stop'])
        study.finish(c,c_trial,NOW+20,'observation_gap')
        report=report_for(c,NOW+20)
        groups={g['variant']:g for g in report['entry_variants']['groups']}
        assert groups['repeated']['wins']==groups['repeated']['losses']==groups['repeated']['unresolved']==1
        assert groups['first_per_trend']['selected']==groups['first_per_trend']['wins']==1
        assert groups['pullback_reset']['selected']==1
        assert report['groups'][0]['total']==3
        # Legacy futures are explicitly outside the prospective cohort.
        a.pop('entry_variants')
        assert variants.report([a])['pre_activation_trials']==1
        assert variants.report([dict(a,asset='stock')])['pre_activation_trials']==0


def test_short_pullback_and_new_risk_day(db):
    with db.tx() as c:
        variants.observe_bar(db,c,SYMBOL,bar(bias=-1,low=96,high=99,close=98),NOW)
        assert classify(db,c,side='short')['first_per_trend']=='selected'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+60,bias=-1,low=99,high=103,close=101),NOW+60)
        assert classify(db,c,'b',NOW+60,side='short')['pullback_reset']=='skipped'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+120,bias=-1,low=96,high=99,close=98),NOW+120)
        assert classify(db,c,'c',NOW+120,side='short')['pullback_reset']=='selected'
        variants.observe_bar(db,c,SYMBOL,bar(NOW+86400,bias=-1),NOW+86400)
        assert classify(db,c,'d',NOW+86400,side='short')['first_per_trend']=='selected'
