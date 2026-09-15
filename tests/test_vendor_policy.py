import asyncio
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import select,func
import pytest
from compass.vendor_freshness import context_check, cache_window, CACHE_POLICY
from compass.research import Feed,normalize
from compass.secondary import Secondary,reviews,vendor_comparison,capture
from compass.config import Config
from compass.flow_recovery import collect
from compass.store import flow_records
from test_secondary import db,seed,candidate,NOW
from test_premarket import flow_row


def stamp(value): return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize('value,ttl',[
 ('2026-09-14T15:00:00+00:00',900),('2026-09-14T22:00:00+00:00',21600),
 ('2026-09-13T15:00:00+00:00',86400),('2026-09-07T15:00:00+00:00',86400),
 ('2026-11-27T18:01:00+00:00',21600)])
def test_vendor_calendar_policy(value,ttl):
    assert cache_window(stamp(value))==ttl


def test_rolling_context_is_not_live_confirmation_and_does_not_hide_poll_failure():
    item=dict(source_ts=NOW-899,received=NOW,cache_policy=CACHE_POLICY)
    check=context_check(item,NOW)
    assert check['eligible_for_context'] and not check['eligible_for_live_confirmation']
    assert not context_check(item,NOW+1)['eligible_for_context']
    assert not context_check({**item,'received':NOW-181},NOW)['eligible_for_context']
    assert not context_check({**item,'vendor_stale':True},NOW)['eligible_for_context']
    assert not context_check({**item,'source_ts':NOW,'received':NOW-1},NOW)['eligible_for_context']


@pytest.mark.parametrize('endpoint',['matrix','apex','apex-evolution'])
def test_only_snapshot_time_is_authoritative(endpoint):
    feed=Feed('test','test','/gex/SPY/'+endpoint,300,8,'exposure')
    value=normalize(feed,{'data':{'computedAt':'2026-09-14T15:00:00Z'},'freshness':{'computedAt':'2026-09-14T15:00:00Z'}},NOW)
    assert value['source_ts'] is None and value['cache_policy']==CACHE_POLICY


def test_paired_decision_preserves_vendor_levels_and_removes_only_vendor_for_baseline(db):
    seed(db,NOW)
    with db.tx() as c:
        db.put(c,'matrix_levels:SPY',dict(source_ts=NOW-800,received=NOW,cache_policy=CACHE_POLICY,
            levels=[dict(price=100.1,kind='gex_concentration',known_at=NOW-850)]))
        Secondary(db,Config(local=True)).review(c,candidate(),{},NOW)
        row=dict(c.execute(select(reviews)).mappings().one())
        pair=row['decision']['tradermatrix_comparison']
        assert pair['with_vendor']=='watch' and pair['without_vendor']=='supported'
        assert pair['changed'] and pair['evidence_present']
        assert row['inputs']['levels'][0]['source_ts']==NOW-800
        assert not row['inputs']['levels'][0]['context_check']['eligible_for_live_confirmation']
        row['measurements']['horizons']['15']={'status':'observed','return_pct':-1}
        report=vendor_comparison([row])
        assert report['horizons'][0]['avoided_losers']==1
        assert report['horizons'][0]['selection_delta_per_candidate_pct']==1
        assert report['horizons'][1]['pending']==1
        assert vendor_comparison([{**row,'decision':{'timely':True}}])['excluded_legacy']==1


def test_shifted_pages_recheck_head_and_deduplicate_same_page(db):
    calls=[]
    async def request(path,label):
        page=int(path.split('page=')[1].split('&')[0]); calls.append(page)
        ids=[1,1] if len(calls)==1 else [1,2] if page==2 else [3,1]
        return dict(success=True,data=[flow_row(i) for i in ids],total=3,page=page,pageSize=2),NOW
    result=asyncio.run(collect(SimpleNamespace(db=db,matrix_request=request),NOW,page_cap=3))
    assert calls==[1,2,1]
    assert result['unique_rows']==3
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(flow_records)).scalar_one()==3
    assert result['collection_status']=='receiving'


def test_unknown_and_invalid_snapshot_clocks_fail_closed():
    for source in (None, 0, NOW+1, 1e30):
        check=context_check(dict(cache_policy=CACHE_POLICY,source_ts=source,received=NOW),NOW)
        assert not check['eligible_for_context']


def test_three_request_budget_advances_instead_of_repeating_overlap(db):
    from test_open_fixes import flow_fixture
    calls=[]
    async def request(path,label):
        page=int(path.split('page=')[1].split('&')[0]); calls.append(page)
        return flow_fixture(page,500),NOW
    collector=SimpleNamespace(db=db,matrix_request=request)
    for _ in range(4): asyncio.run(collect(collector,NOW,page_cap=3))
    assert calls==[1,2,1,1,3,1,1,4,1,1,5,1]
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(flow_records)).scalar_one()==500


def test_production_flow_budget_rechecks_head_and_advances_with_prior_day_gap(db,monkeypatch):
    from compass.providers import Collectors
    from compass.flow_recovery import ingest
    from compass.vendor import flow_page
    from test_open_fixes import flow_fixture
    from urllib.parse import parse_qs,urlparse
    from datetime import timedelta,date
    from compass.market import day
    prior=(date.fromisoformat(day(NOW))-timedelta(days=1)).isoformat()
    ingest(db,prior,1,flow_page(flow_fixture(1,800),NOW-86400),NOW-86400)
    calls=[]
    monkeypatch.setattr('compass.providers.time.time',lambda:NOW)
    async def run():
        collector=Collectors(db,Config(local=True))
        async def request(path,label):
            params=parse_qs(urlparse(path).query)
            page=int(params['page'][0]);frame=params['timeFrame'][0];calls.append((frame,page))
            return flow_fixture(page,800),NOW
        collector.matrix_request=request
        try:
            first=await collector.flow(); second=await collector.flow()
            return first,second
        finally: await collector.close()
    first,second=asyncio.run(run())
    assert first['page_cap']==second['page_cap']==4
    assert calls==[('today',1),('yesterday',2),('today',2),('today',1),
                   ('today',1),('yesterday',3),('today',3),('today',1)]
    assert second['recovery']['next_page']==4 and second['unique_rows']==300
