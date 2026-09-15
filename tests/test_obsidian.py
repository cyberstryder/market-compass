import asyncio
from datetime import datetime,timezone
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select,func
from compass.obsidian import ingest_page,ideas,feed_events,normalize,tick,poll,contracts,validate_url
from compass.config import Config
from compass.secondary import reviews
from compass.store import events
from compass.universe import data_symbols
from test_secondary import db,NOW,seed


def event(i=1,kind='new',**kwargs):
    return dict(id=i,type=kind,ticker='JNJ',strike=202.5,pc='Put',expiration='2026-09-18',
        percentage=None if kind=='new' else 42,time=datetime.fromtimestamp(NOW,timezone.utc).isoformat(),**kwargs)


def test_replay_idempotence_and_ambiguous_contract_updates(db):
    page=dict(events=[event(),event(2),event(3,'update')],cursor=3)
    ingest_page(db,'fixture',page,NOW);ingest_page(db,'fixture',page,NOW+1)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(ideas)).scalar_one()==2
        rows=c.execute(select(feed_events).order_by(feed_events.c.vendor_id)).mappings().all()
        assert len(rows)==3 and rows[-1]['association']=='ambiguous' and rows[-1]['idea_id'] is None
        assert rows[-1]['payload']['vendor_percentage']==42
        assert db.get(c,'obsidian:cursor:fixture')==3


def test_bad_envelope_rolls_back_cursor_and_malformed_event_is_quarantined(db):
    with pytest.raises(ValueError): ingest_page(db,'fixture',dict(events=[event()],cursor=0),NOW)
    bad={**event(),'strike':-1}
    ingest_page(db,'fixture',dict(events=[bad],cursor=1),NOW)
    with db.tx() as c:
        assert db.get(c,'obsidian:cursor:fixture')==1
        assert c.execute(select(feed_events.c.association)).scalar_one()=='quarantined'
        assert c.execute(select(func.count()).select_from(ideas)).scalar_one()==0


def test_historical_import_never_gets_reconstructed_entry(db):
    ingest_page(db,'fixture',dict(events=[event()],cursor=1),NOW+120)
    tick(db,Config(local=True),NOW+120)
    with db.tx() as c:
        assert c.execute(select(ideas.c.measurements)).scalar_one()['anchor_status']=='late_import'
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one()==0
        assert 'JNJ' in data_symbols(db,c,Config(local=True),NOW+120)
        assert contracts(c,NOW+120)==['O:JNJ260918P00202500']


def test_independent_long_put_measurement_and_secondary_no_notification(db,monkeypatch):
    from compass.providers import Collectors
    seed(db,NOW,symbol='JNJ',bias=-1)
    monkeypatch.setattr('compass.store.time.time',lambda:NOW)
    ingest_page(db,'fixture',dict(events=[event(),event(2,'update')],cursor=2),NOW)
    q=dict(ts=NOW,bid=2,ask=2.1,bid_size=1,ask_size=1)
    with db.tx() as c: db.put(c,'quote:O:JNJ260918P00202500',q)
    tick(db,Config(local=True),NOW)
    monkeypatch.setattr('compass.store.time.time',lambda:NOW+5)
    with db.tx() as c:
        nextq={**q,'ts':NOW+5,'bid':2.5,'ask':2.6}
        db.append(c,'quote','test','O:JNJ260918P00202500',NOW+5,nextq)
        db.put(c,'quote:O:JNJ260918P00202500',nextq)
    tick(db,Config(local=True),NOW+5)
    with db.tx() as c:
        m=c.execute(select(ideas.c.measurements)).scalar_one()
        assert m['liquidation_pct']==pytest.approx((2.5/2.1-1)*100)
        assert m['samples']==2 and m['anchor_ask']==2.1
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one()==1
        assert not db.recent(c,'alert')
        assert c.execute(select(feed_events.c.association).where(feed_events.c.vendor_id==2)).scalar_one()=='matched'


def test_poll_uses_saved_cursor_and_never_exposes_private_link_on_error(db):
    url='https://www.accessobsidian.com/api/feed/'+'private_test_token_not_real'
    def request(req):
        assert req.url.params['since']=='0'
        raise httpx.ConnectError('private: '+url,request=req)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
            with pytest.raises(RuntimeError) as error:
                await poll(SimpleNamespace(cfg=SimpleNamespace(obsidian_url=url),db=db,client=client))
            assert url not in str(error.value) and 'private_test_token_not_real' not in str(error.value)
    asyncio.run(run())


@pytest.mark.parametrize('url',['http://www.accessobsidian.com/api/feed/'+'a'*30,
 'https://www.accessobsidian.com.evil.test/api/feed/'+'a'*30,
 'https://www.accessobsidian.com/api/feed/'+'a'*30+'?redirect=1'])
def test_private_link_validation(url):
    with pytest.raises(ValueError):validate_url(url)


def test_live_watchlist_can_wait_until_market_open_for_observed_baseline(db):
    ingest_page(db,'fixture',dict(events=[event()],cursor=1),NOW)
    later=NOW+3600
    with db.tx() as c:
        db.put(c,'quote:O:JNJ260918P00202500',dict(ts=later,bid=2,ask=2.1,bid_size=1,ask_size=1))
    tick(db,Config(local=True),later)
    with db.tx() as c:
        m=c.execute(select(ideas.c.measurements)).scalar_one()
        assert m['anchor_status']=='observed' and m['anchor_delay_seconds']==3600
        assert m['anchor_ts']==later
