import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from compass.spy_range import valid_bars, verify, apply_verified_range
from compass.spy_brief import bar_context, build
from compass.provider_coverage import collect
from test_spy_brief import db, seed, OPEN, NOW

START = OPEN-330*60


def inventories():
    minutes={START+i*60:dict(o=760,h=760.5,l=759.5,c=760,v=100) for i in range(330) if i not in range(0,170,5)}
    five={START+i*300:dict(o=760,h=760.5,l=759.5,c=760,v=500) for i in range(66)}
    return minutes,five


def install(db):
    seed(db)
    minutes,five=inventories()
    proof=verify(START,OPEN,minutes,five,NOW-1,'sip')
    with db.tx() as c:
        rows=db.get(c,'bar_window:SPY')
        db.put(c,'bar_window:SPY',[r for r in rows if r[0]>=OPEN or r[0] in minutes])
        db.put(c,'spy-provider-range:2026-09-16',proof)
    return proof


def test_sparse_minutes_verified_without_changing_frozen_timeframe_gate(db):
    proof=install(db)
    assert len(proof['minutes'])==296
    with db.tx() as c:
        strict=bar_context(db,c,NOW)
        assert strict['premarket_complete'] is False
        report=build(db,c,NOW,'opening')
        context=report['context']
        assert context['premarket_complete'] is True
        assert context['premarket_clock_complete'] is False
        assert context['premarket_coverage_basis']=='verified_provider_five_minute_range'
        assert report['version']=='spy-morning-brief-v3'


@pytest.mark.parametrize('change', ['stale','future','wrong_day','partial','wrong_feed','changed_range','missing_minute'])
def test_unusable_proof_keeps_gate_closed(db,change):
    proof=install(db)
    with db.tx() as c:
        if change=='stale': proof['fetched_at']=NOW-121
        if change=='future': proof['fetched_at']=NOW+1
        if change=='wrong_day': proof['start']-=86400
        if change=='partial': proof['verified']=False
        if change=='wrong_feed': proof['feed']='iex'
        if change in ('changed_range','missing_minute'):
            rows=db.get(c,'bar_window:SPY')
            if change=='changed_range': rows[0][2]+=1
            else: rows.pop(0)
            db.put(c,'bar_window:SPY',rows)
        db.put(c,'spy-provider-range:2026-09-16',proof)
        assert not build(db,c,NOW,'opening')['context']['premarket_complete']


def test_incomplete_five_minute_or_range_disagreement_fails():
    minutes,five=inventories()
    assert verify(START,OPEN,minutes,five,NOW,'sip')['verified']
    bad=deepcopy(five);bad[START]['h']+=1
    assert not verify(START,OPEN,minutes,bad,NOW,'sip')['verified']
    five.pop(START)
    assert not verify(START,OPEN,minutes,five,NOW,'sip')['verified']


@pytest.mark.parametrize('change',['pagination','null','duplicate','offgrid','future','nan','ohlc'])
def test_invalid_provider_response_rejected(change):
    bar=dict(t=datetime.fromtimestamp(START,timezone.utc).isoformat(),o=760,h=761,l=759,c=760,v=100)
    data={'bars':[bar]}
    if change=='pagination': data['next_page_token']='next'
    if change=='null': data['bars']=None
    if change=='duplicate': data['bars']+=[bar]
    if change=='offgrid': bar['t']=datetime.fromtimestamp(START+1,timezone.utc).isoformat()
    if change=='future': bar['t']=datetime.fromtimestamp(OPEN,timezone.utc).isoformat()
    if change=='nan': bar['h']=float('nan')
    if change=='ohlc': bar['l']=762
    with pytest.raises(ValueError): valid_bars(data,START,OPEN,60)


def test_collector_repairs_real_minutes_and_saves_verification(db,monkeypatch):
    from compass.providers import Collectors
    minutes,five=inventories()
    seed(db)
    with db.tx() as c: db.put(c,'bar_window:SPY',[])
    calls=[]
    async def get(url,headers,params):
        calls.append(params['timeframe'])
        rows=minutes if params['timeframe']=='1Min' else five
        return {'bars':[dict(t=datetime.fromtimestamp(t,timezone.utc).isoformat(),**b) for t,b in rows.items()]}
    collector=SimpleNamespace(db=db,cfg=SimpleNamespace(feed='sip'),alpaca_headers={},get=get)
    collector.bars=lambda source,items: Collectors.bars(collector,source,items)
    monkeypatch.setattr('compass.provider_coverage.time.time',lambda:NOW)
    for _ in range(2): asyncio.run(collect(collector,NOW))
    with db.tx() as c:
        assert len(db.get(c,'bar_window:SPY'))==296
        assert db.get(c,'spy-provider-range:2026-09-16')['verified']
        assert db.get(c,'provider_coverage:SPY:2026-09-16')['collector_missing_minutes']==[]
    assert calls==['1Min','5Min']*2


def test_verified_range_does_not_replace_missing_rth_confirmation(db):
    install(db)
    with db.tx() as c:
        rows=db.get(c,'bar_window:SPY')
        db.put(c,'bar_window:SPY',[r for r in rows if r[0]!=OPEN+60])
        report=build(db,c,NOW,'opening')
        assert report['context']['premarket_complete']
        assert not report['context']['confirmation_candle']['complete']
        assert 'CONFIRMED' not in report['decision']
