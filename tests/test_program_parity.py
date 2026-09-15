from copy import deepcopy
from datetime import date
from compass.program_parity import morning_options, smoother_quality, week_schedule, tick, VERSION
from compass.smoothers_quality import score_signal, QUALITY_VERSION
from test_projects import db,source,ingest


def test_option_arithmetic_is_not_missing_as_zero():
    ref=dict(status='available',ask=2,midpoint=1.9,quote_at_ms=1000,contract={'symbol':'SPY260918C00500000'})
    q=dict(status='available',bid=3,midpoint=3.1,quote_at_ms=2000,contract=ref['contract'],ask_to_bid_return_pct=50,midpoint_return_pct=(3.1/1.9-1)*100)
    p=source(1,option=ref,option_samples=[dict(minute=5,status='complete',quote=q)])
    assert morning_options(p)['samples'][0]['status']=='matched'
    bad=deepcopy(p);bad['option_samples'][0]['quote']['ask_to_bid_return_pct']=0
    assert morning_options(bad)['samples'][0]['status']=='different'
    bad['option_samples'][0]['quote']['contract']={'symbol':'OTHER'}
    assert morning_options(bad)['samples'][0]['status']=='unavailable'
    assert morning_options(source(1))['delivery_status']=='not_exported'


def test_quality_uses_frozen_entry_inputs():
    original=dict(quality_version=QUALITY_VERSION,target_atr_mult=.3,alltime_wins_at_entry=5,
                  alltime_losses_at_entry=2,backtest_wr_at_entry=.7,entry_premium=2,est_return_pct_at_entry=20)
    original.update(score_signal(target_atr_mult=.3,alltime_wins=5,alltime_losses=2,backtest_wr=.7,entry_premium=2,est_return_pct=20))
    p=source(1,original=original)
    assert smoother_quality(p)['status']=='matched'
    original['quality_score']-=1
    assert smoother_quality(p)['status']=='different'
    original['alltime_wins_at_entry']=None
    assert smoother_quality(p)['status']=='unavailable'


def test_week_holidays_and_early_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    labor=week_schedule(date(2026,9,7))
    assert labor['first_session']=='2026-09-08'
    assert labor['last_session']=='2026-09-11'
    thanks=week_schedule(date(2026,11,23))
    assert datetime.fromtimestamp(thanks['close_at'],ZoneInfo('America/New_York')).strftime('%H:%M')=='13:05'


def test_worker_has_no_notification_side_effects(db):
    with db.tx() as c: ingest(db,c,'morning',source(100),100)
    tick(db,'test',1789502800)
    with db.tx() as c:
        r=db.get(c,VERSION+':report')
        assert r['projects']['morning']['checked']==1 and not r['cutover_ready']
        assert not db.recent(c,'alert')


def test_target_monitor_preserves_gaps_and_completed_bars(db):
    from compass.program_parity import target_observation
    from datetime import datetime,timezone
    at=datetime(2026,9,15,14,30,tzinfo=timezone.utc).timestamp()
    p=source(at,side='long',target=101)
    with db.tx() as c:
        db.append(c,'bar','test','SPY',at+60,dict(o=100,h=102,l=99,c=100,v=10))
        result=target_observation(db,c,p,at+120)
        assert result['status']=='observed_touch'
        assert result['missing_minutes']==1 and not result['first_touch_verified']
        assert target_observation(db,c,p,at+90)['status']=='incomplete_observation'
        db.append(c,'bar','test','SPY',at,dict(o=100,h=100,l=99,c=100,v=10))
        assert target_observation(db,c,p,at+120)['first_touch_verified']
