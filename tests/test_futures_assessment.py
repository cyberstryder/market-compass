from compass import futures_assessment as a
from compass.setup_study import SetupStudy
from compass.engine import spec
from test_setup_study import db, cfg, NOW, quote, signal, rows


def features(**overrides):
    return dict(dict(status='ready', asof=NOW, ema9=102, ema21=101,
        ema21_previous=100.9, atr14=2, vwap=100, rvol20=1,
        htf15_bias=1, bar=dict(c=103,h=104,l=102)), **overrides)


def test_states_and_missing_context():
    assert a.assess(features(),NOW)['state']=='trend'
    assert a.assess(features(ema9=101,ema21_previous=101),NOW)['state']=='range_candidate'
    assert a.assess(features(rvol20=2,bar=dict(c=103,h=106,l=100)),NOW)['state']=='expansion'
    assert a.assess(features(htf15_bias=-1),NOW)['state']=='mixed'
    for f in (features(htf15_bias=None),features(atr14=0),features(asof=NOW+1)):
        assert a.assess(f,NOW)['state']=='unknown'
    assert a.assess(features(),NOW+91)['state']=='unknown'


def test_freeze_restart_and_no_retroactive_reclassification(db,cfg):
    s=dict(signal(),rule='trend_pullback')
    with db.tx() as c:
        a.observe(db,c,s['symbol'],features(),NOW)
        db.put(c,'quote:'+s['symbol'],quote())
        SetupStudy(db,cfg).start(c,s,NOW,spec(s['symbol']))
        frozen=rows(c)[0]['market_assessment']
        assert frozen['proposed_selection']=='candidate'
        a.observe(db,c,s['symbol'],features(htf15_bias=-1),NOW+1)
        assert db.get(c,a.PREFIX+s['symbol'])['state']=='trend'
    with db.tx() as c:
        a.observe(db,c,s['symbol'],features(asof=NOW+60,htf15_bias=-1),NOW+60)
        SetupStudy(db,cfg).start(c,s,NOW+60,spec(s['symbol']))
        assert rows(c)[0]['market_assessment']==frozen
        assert a.freeze(db,c,s,NOW+60,quote(NOW+60),spec(s['symbol']))['state']=='unknown'
        report=a.report(rows(c))
        assert report['groups'][0]['open']==1
        assert report['groups'][0]['mean_r'] is None


def test_stale_quotes_abstain_without_erasing_state(db):
    s=dict(signal(),rule='trend_pullback')
    with db.tx() as c:
        a.observe(db,c,s['symbol'],features(),NOW)
        result=a.freeze(db,c,s,NOW,quote(NOW-30),spec(s['symbol']))
        assert result['state']=='trend' and result['proposed_selection']=='abstain'
        assert not result['execution_eligible']
