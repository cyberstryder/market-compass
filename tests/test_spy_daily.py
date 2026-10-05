from datetime import datetime

from test_spy_brief import db, seed, at
from compass.spy_brief import bar_context
from compass.market import session


def test_daily_revisions_do_not_evict_required_sessions(db):
    now = at('2026-09-21T10:00:10')
    seed(db, now)
    # The archive retains revisions by payload; a row limit is not a day limit.
    with db.tx() as c:
        for revision in range(100):
            db.append(c, 'daily', 'alpaca', 'SPY', session('2026-09-18')[0],
                      dict(o=760, h=762, l=758, c=760, v=1000+revision))
        context = bar_context(db, c, now, 'followup_30')
    assert context['atr14_daily'] == 4


import asyncio
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import delete
from compass.store import events
from compass.spy_daily import collect, evidence, required_days
from compass.providers import Collectors
from compass.spy_brief import BriefWorker, delivery_payload
from compass.config import Config

MONDAY = at('2026-09-21T09:45:10')


def collector(db, response, calls):
    async def get(url, headers, params):
        calls.append(params)
        return response
    result = SimpleNamespace(db=db, cfg=SimpleNamespace(feed='sip'), alpaca_headers={}, get=get)
    result.bars=lambda source, items, kind: Collectors.bars(result, source, items, kind)
    return result


def bars(now):
    return [dict(t=d+'T04:00:00Z', o=760, h=762, l=758, c=760, v=100)
            for d in required_days(now)]


def test_holiday_weekend_and_current_day_exclusion(db):
    seed(db, MONDAY)
    days=required_days(MONDAY)
    assert len(days)==15 and days[-1]=='2026-09-18'
    assert '2026-09-07' not in days
    with db.tx() as c:
        for i in range(100):
            db.append(c,'daily','alpaca','SPY',session('2026-09-21')[0],
                      dict(o=760,h=999+i,l=100,c=760,v=100))
        assert evidence(db,c,MONDAY)['atr14_daily']==4


def test_missing_session_recovers_before_open_and_does_not_refetch_ready(db):
    now=session('2026-09-21')[0]-3600
    calls=[]
    client=collector(db,{'bars':bars(now)},calls)
    first=asyncio.run(collect(client,now))
    assert first['status']=='ready' and first['atr14_daily']==4
    assert len(calls)==1 and calls[0]['timeframe']=='1Day'
    assert asyncio.run(collect(client,now+60))['status']=='ready'
    assert len(calls)==1


@pytest.mark.parametrize('invalid',['pagination','nan','ohlc','duplicate','current_day'])
def test_invalid_recovery_does_not_write_daily_rows(db,invalid):
    rows=bars(MONDAY)
    response={'bars':rows}
    if invalid=='pagination': response['next_page_token']='more'
    if invalid=='nan': rows[-1]['h']=float('nan')
    if invalid=='ohlc': rows[-1]['l']=800
    if invalid=='duplicate': rows.append(rows[-1])
    if invalid=='current_day': rows[-1]['t']='2026-09-21T04:00:00Z'
    with pytest.raises(ValueError):
        asyncio.run(collect(collector(db,response,[]),MONDAY))
    with db.tx() as c:
        assert db.recent(c,'daily','SPY')==[]


def test_incomplete_provider_days_remain_explicitly_blocked(db):
    rows=bars(MONDAY)[:-1]
    result=asyncio.run(collect(collector(db,{'bars':rows},[]),MONDAY))
    assert result['status']=='blocked'
    assert result['missing_or_invalid_days']==['2026-09-18']
    assert result['atr14_daily'] is None


def test_latest_invalid_revision_is_not_silently_replaced_with_older_bar(db):
    seed(db,MONDAY)
    with db.tx() as c:
        db.append(c,'daily','alpaca','SPY',session('2026-09-18')[0],
                  dict(o=760,h=700,l=758,c=760,v=1))
        assert evidence(db,c,MONDAY)['missing_or_invalid_days']==['2026-09-18']


def test_recovery_allows_later_breakout_without_rewriting_opening_report(db):
    seed(db,MONDAY)
    with db.tx() as c:
        c.execute(delete(events).where(events.c.kind=='daily'))
    worker=BriefWorker(db,Config(local=True))
    opening=worker.tick(MONDAY)
    assert opening['decision']=='WAIT — DATA BLOCKED: prior daily ATR evidence incomplete'
    payload=delivery_payload(dict(id=1,payload=opening),MONDAY)
    assert 'DATA BLOCKED — prior daily ATR' in payload['content']
    assert 'usable sessions' not in json.dumps(payload)
    assert 'embeds' not in payload
    assert asyncio.run(collect(collector(db,{'bars':bars(MONDAY)},[]),MONDAY+60))['status']=='ready'
    # Fresh complete minutes for the next scheduled check; no invented past entry.
    later=session('2026-09-21')[0]+1807
    seed(db,later)
    result=worker.tick(later)
    assert result['decision']=='CALL SETUP CONFIRMED'
    with db.tx() as c:
        assert db.get(c,opening['id'])==opening


def test_closed_session_never_requests_history(db):
    calls=[]
    assert asyncio.run(collect(collector(db,{},calls),at('2026-09-20T09:00:00'))) is None
    assert calls==[]
