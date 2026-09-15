import copy
from sqlalchemy import select
from compass import morning_opening as opening
from compass.secondary import assess, technical_context
from test_secondary import db, candidate, context, quote

OPEN=1789479000
NOW=OPEN+11*60


def window():
    return [[OPEN+i*60,99, max(99.3,99+i*.04+.1),98.9,99+i*.04,100,99+i*.04] for i in range(11)]


def inputs():
    return {**context(NOW),'quote':quote(NOW,99.4),'technical':{'status':'warming_up'}}


def source():
    return candidate(NOW,entry=99.4,stop=98,target=102)


def test_eleven_opening_minutes_work_without_premarket_or_relaxing_baseline(db):
    with db.tx() as c:
        db.put(c,'bar_window:SPY',window())
        assert technical_context(db,c,'SPY',NOW)['status']=='warming_up'
    f=opening.context(window(),NOW)
    assert f['ready'] and f['completed_minutes']==11
    assert assess(source(),inputs(),NOW)['verdict']=='insufficient_data'
    assert opening.assess(source(),inputs(),f,NOW)['verdict']=='supported'
    assert 'atr14' not in f and 'ema21' not in f


def test_missing_opening_bar_or_short_window_remains_unready():
    assert not opening.context(window()[1:],NOW)['ready']
    assert not opening.context(window(),OPEN+4*60)['ready']
    assert not opening.context(window(),OPEN+1800)['ready']
    f=opening.context(window()+[[OPEN+660,1,1000,1,1000,1]],NOW)
    assert f['ready'] and f['last_close']==window()[-1][4]


def test_stale_quote_expired_window_and_resolved_source_never_supported():
    f=opening.context(window(),NOW)
    i=inputs();i['quote']=quote(NOW-6,99.4)
    assert opening.assess(source(),i,f,NOW)['verdict']=='insufficient_data'
    assert opening.assess(source(),inputs(),f,NOW+61)['verdict']=='insufficient_data'
    assert opening.assess({**source(),'resolved_at_review':True},inputs(),f,NOW)['verdict']=='rejected'


def test_shadow_is_frozen_and_has_independent_checkpoints_without_alerts(db):
    from compass.store import events
    with db.tx() as c:
        db.put(c,'bar_window:SPY',window())
        opening.consider(db,c,source(),inputs(),assess(source(),inputs(),NOW),NOW)
        row=c.execute(select(opening.rows)).mappings().one(); original=copy.deepcopy(row['payload'])
        opening.consider(db,c,source(),{**inputs(),'quote':quote(NOW+3,110)}, {},NOW+3)
        assert c.execute(select(opening.rows.c.payload)).scalar_one()==original
        assert original['measurements']['horizons']['15']['due']==NOW+900
        assert original['baseline_at_capture']['verdict']=='insufficient_data'
        opening.tick(db,c,NOW+931)
        saved=c.execute(select(opening.rows.c.payload)).scalar_one()
        assert saved['measurements']['horizons']['15']['status']=='missing'
        assert not c.execute(select(events).where(events.c.kind=='alert')).first()


def test_other_projects_and_later_signals_do_not_enter_challenger(db):
    with db.tx() as c:
        for p in ('smoothers','futures','obsidian'):
            opening.consider(db,c,{**source(),'project':p},inputs(),{},NOW)
        opening.consider(db,c,{**source(),'source_ts':OPEN+1800},inputs(),{},OPEN+1800)
        assert not c.execute(select(opening.rows)).first()
