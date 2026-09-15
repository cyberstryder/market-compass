from datetime import datetime
from compass.market import CT
from compass.futures import research_session, futures_session
from compass.futures_catalog import candidates
from compass.futures_research import observe
from compass.setup_study import SetupStudy
from compass.engine import spec
from test_setup_study import db,cfg,quote,rows
from test_futures_assessment import features


def clock(h,m=0):
    return datetime(2026,9,15,h,m,tzinfo=CT).timestamp()


def test_full_session_is_separate_from_account():
    for h,m in ((1,0),(15,20),(15,50)):
        assert research_session(clock(h,m),'MESZ6@1')['entry_open']
    assert not futures_session(clock(15,50),'MESZ6@1')['entry_open']
    assert not research_session(clock(16,15),'MESZ6@1')['is_open']
    assert research_session(clock(17,1),'MESZ6@1')['entry_open']
    assert research_session(clock(15,50))['flatten_at']==clock(15,59)


def fact(at):
    return dict(features(asof=at),symbol='MESZ6@1',previous_bar=dict(o=100,c=102,h=103,l=99),
                high20=110,low20=95,prior5_high=104,prior5_low=98,prior5_range_atr=3)


def test_additional_candidates_are_causal_and_directional():
    f=fact(clock(1))
    got=candidates(f,clock(1),.25)
    assert any(s['rule']=='momentum_continuation' and s['side']=='long' for s in got)
    assert not candidates(f,clock(1)+91,.25)
    f.update(bar=dict(c=106,h=107,l=104),prior5_high=105,prior5_low=103,prior5_range_atr=1,rvol20=2)
    assert any(s['rule']=='compression_breakout' for s in candidates(f,clock(1),.25))
    f.update(bar=dict(c=109,h=112,l=108))
    assert any(s['rule']=='failed_breakout' and s['side']=='short' for s in candidates(f,clock(1),.25))


def test_census_and_trials_after_account_cutoff_are_idempotent(db,cfg):
    at=clock(15,50); f=fact(at)
    with db.tx() as c:
        db.put(c,'quote:'+f['symbol'],quote(at,103,103.25))
        study=SetupStudy(db,cfg)
        observe(db,c,f['symbol'],f,at,study,spec(f['symbol']))
        first=rows(c)
        assert first and all(p['status']=='open' for p in first)
        assert all(p['flatten_at']==clock(15,59) and not p['alerted'] for p in first)
        observe(db,c,f['symbol'],f,at+1,study,spec(f['symbol']))
        assert rows(c)==first
        assert not db.prefix(c,'position:')
        assert len(db.recent(c,'futures_assessment',f['symbol']))==1
        f.update(asof=at+60,htf15_bias=None,bar=dict(c=102,h=103,l=101))
        observe(db,c,f['symbol'],f,at+60,study,spec(f['symbol']))
        assert len(db.recent(c,'futures_assessment',f['symbol']))==2
