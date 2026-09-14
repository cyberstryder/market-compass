from datetime import datetime
import pytest
from compass.alert_format import alert_identity, message_for
from compass.config import Config
from compass.engine import Engine, spec
from compass.futures import future_levels, futures_session, selection
from compass.instruments import configured, future_root
from compass.market import CT
from compass.scanner import Scanner, context_evidence
from compass.secondary import capture, contract_code, resolve_symbol
from test_setup_study import NOW, db, cfg, quote, rows, signal


@pytest.mark.parametrize('root,multiplier',[('YM',5),('MYM',.5)])
def test_dow_symbols_specs_and_source_alert_identity(root,multiplier):
    for symbol in (root+'Z6',root+'Z2026@123','CBOT_MINI:'+root+'1!'):
        assert future_root(symbol)==root
        assert spec(symbol)['asset']=='future'
        assert spec(symbol)['tick']==1 and spec(symbol)['multiplier']==multiplier
        row=dict(symbol=symbol,source='scanner',payload=dict(status='setup_triggered',expires_at=NOW+120))
        assert alert_identity(row,NOW)['category']=='futures'
    assert contract_code(root+'Z26',NOW)==(root,'Z',2026)
    assert spec(root)['asset']=='stock'


@pytest.mark.parametrize('root',['YM','MYM'])
def test_dow_mapping_secondary_context_and_session_clock(db,cfg,root):
    alias,symbol=root+'.v.0',root+'Z6@123'
    cfg.extra_futures=(alias,)
    assert not selection((alias,),NOW)[0]['resolved']
    with db.tx() as c:
        db.put(c,'contract:'+root,dict(configured_symbol=alias,raw_symbol=root+'Z6',mapping_start=NOW-1,mapping_end=NOW+30))
        db.put(c,'quote:'+symbol,quote(NOW,40000,40001))
        candidate=dict(symbol='CBOT_MINI:'+root+'1!',project='compass_futures')
        assert resolve_symbol(db,c,candidate,cfg,NOW)==(symbol,'linked_contract_context')
        assert resolve_symbol(db,c,dict(symbol=root+'Z2026'),cfg,NOW)==(symbol,'same_contract')
        for peer in ('SPY','QQQ'):
            db.put(c,'quote:'+peer,quote())
            db.put(c,'scanner_features:'+peer,dict(status='ready',asof=NOW,htf15_bias=1))
        inputs=capture(db,c,candidate,cfg,NOW)
        assert {p['symbol'] for p in inputs['peers']}=={'SPY','QQQ'}
        assert inputs['exposure']['symbol'] is None and inputs['flow']==[]
        assert resolve_symbol(db,c,candidate,cfg,NOW+31)==(None,'missing_contract')
    assert future_levels([],NOW,symbol=symbol)['range_name']=='RTH'
    for hour,minute in ((2,0),(9,46),(15,20),(15,45),(17,1)):
        now=datetime(2026,9,14,hour,minute,tzinfo=CT).timestamp()
        assert futures_session(now,symbol)==futures_session(now,'MESZ6')
    assert datetime.fromtimestamp(futures_session(NOW,symbol)['flatten_at'],CT).strftime('%H:%M')=='15:45'


@pytest.mark.parametrize('side',['long','short'])
def test_dow_trials_keep_full_and_micro_results_separate_after_costs(db,cfg,side):
    engine=Engine(db,cfg)
    with db.tx() as c:
        # All setups still get independent trials when the simulated account is blocked.
        db.put(c,'risk:2026-09-14',dict(entries=500,realized=-10000))
        for root in ('YM','MYM'):
            symbol=root+'Z6@123'
            db.put(c,'quote:'+symbol,quote(NOW,40000,40001))
            candidate=signal(root,side,symbol)|dict(signal_price=40000,stop_distance=20)
            assert not engine.enter(c,candidate,NOW)
        opened=rows(c)
        assert len(opened)==2 and all(p['status']=='open' for p in opened)
        for p in opened:
            bid=p['target']+10 if side=='long' else p['target']-10
            db.put(c,'quote:'+p['symbol'],quote(NOW+2,bid,bid+1))
        engine.study.tick(c,NOW+2)
        result={p['root']:p for p in rows(c)}
        assert result['YM']['pnl']==195 and result['MYM']['pnl']==17
        assert result['YM']['r_multiple']==pytest.approx(195/105)
        assert result['MYM']['r_multiple']==pytest.approx(17/13)
        assert db.prefix(c,'position:')=={}
        for row in db.recent(c,'alert'):
            if row['payload']['status']=='setup_result':
                assert 'FUTURES | SETUP RESULT · WIN' in message_for(row,NOW+2)


@pytest.mark.parametrize('root',['YM','MYM'])
def test_dow_scanner_requires_current_mapping_and_tracks_breakout(db,cfg,root):
    symbol=root+'Z6@123'
    cfg.extra_futures=(root+'.v.0',)
    cfg.scanner_paper=False
    engine=Engine(db,cfg)
    with db.tx() as c:
        # Include the 21 completed 15-minute bars needed for directional bias.
        for i in range(360):
            price=39700+i
            bar=dict(o=price-1,h=price+1,l=price-2,c=price,v=100)
            if i==359: bar.update(h=40082,c=40080,v=500)
            db.append(c,'bar','fixture',symbol,NOW-60*(360-i),bar,str(i))
        db.put(c,'latestbar:'+symbol,NOW-60)
        db.put(c,'quote:'+symbol,quote(NOW,40080,40081))
        Scanner(db,cfg).scan(c,NOW,engine)
        assert db.get(c,'scanner_features:'+symbol) is None
        db.put(c,'contract:'+root,dict(configured_symbol=root+'.v.0',raw_symbol=root+'Z6',mapping_start=NOW-1,mapping_end=NOW+30))
        Scanner(db,cfg).scan(c,NOW,engine)
        assert db.get(c,'scanner_features:'+symbol)['status']=='ready'
        alerts=[r for r in db.recent(c,'alert') if r['payload']['status']=='setup_triggered']
        assert len(alerts)==1 and alert_identity(alerts[0],NOW)['category']=='futures'
        assert any(p['rule']=='volume_breakout' and p['status']=='open' for p in rows(c))
        assert db.prefix(c,'position:')=={}


def test_dow_defaults_and_paired_context(cfg):
    defaults=Config(local=True)
    assert {'YM.v.0','MYM.v.0'}<=set(configured(defaults))
    defaults.validate()
    cfg.extra_futures=('YM.c.0',)
    with pytest.raises(ValueError): cfg.validate()
    facts={s:dict(symbol=s,status='ready',asof=NOW,htf15_bias=1) for s in ('MYMZ6@1','YMZ6@2','SPY','QQQ')}
    evidence=context_evidence(facts['MYMZ6@1'],[],None,{s:quote() for s in facts},facts,NOW)
    assert {e['symbol'] for e in evidence if e['kind']=='cross_market'}=={'YMZ6@2','SPY','QQQ'}
