import asyncio
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse,parse_qs
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from compass.market import CT,day
from compass.futures import futures_session,risk_day,selection,future_levels,prior_rth
from compass.future_history import recover
from compass.engine import Engine,candidate
from compass.config import Config
from compass.store import Store,flow_records
from compass.providers import Collectors,FeedError
from compass.greek_diagnostics import diagnose
from compass.flow_recovery import collect,ingest
from compass.vendor import flow_page
from compass.app import create_app
from compass.alerts import dispatch
from compass.readiness import quote_checks


def stamp(value): return datetime.fromisoformat(value).replace(tzinfo=CT).timestamp()
SUNDAY=stamp("2026-09-13T17:16:00")
MONDAY=stamp("2026-09-14T08:46:00")


@pytest.fixture
def db(tmp_path):
    s=Store("sqlite:///"+str(tmp_path/"fixes.db"))
    s.initialize()
    yield s
    s.engine.dispose()


def quote(now,bid=100,ask=100.25):
    return {"ts":now,"bid":bid,"ask":ask,"bid_size":10,"ask_size":10}


def bars(start,count=16):
    return [{"id":i+1,"ts":start+i*60,"payload":{"o":100,"h":101 if i<15 else 103,
        "l":99,"c":100 if i<15 else 102,"v":100}} for i in range(count)]


@pytest.mark.parametrize("value,opened",[("2026-09-13T16:59:00",False),("2026-09-13T17:00:00",True),
    ("2026-09-14T00:01:00",True),("2026-09-14T15:14:00",True),("2026-09-14T15:20:00",False),
    ("2026-09-14T15:40:00",True),("2026-09-14T16:01:00",False),("2026-09-14T17:00:00",True),
    ("2026-09-19T12:00:00",False)])
def test_futures_session_includes_overnight_and_excludes_pauses(value,opened):
    assert futures_session(stamp(value))["is_open"] is opened


def test_cme_risk_day_roll_dst_and_early_close():
    assert risk_day(SUNDAY)==risk_day(stamp("2026-09-14T00:01:00"))=="2026-09-14"
    assert risk_day(stamp("2026-09-14T17:01:00"))=="2026-09-15"
    assert selection(("MES.c.0","MNQ.c.0"),SUNDAY)[0]["raw_symbol"]=="MESZ6"
    assert selection(("MES.c.0",),stamp("2026-09-11T12:00:00"))[0]["raw_symbol"]=="MESU6"
    winter=futures_session(stamp("2026-11-01T17:01:00"))
    assert datetime.fromtimestamp(winter["open"],CT).hour==17
    early=futures_session(stamp("2026-11-27T09:00:00"))
    assert early["close"]<stamp("2026-11-27T16:00:00")
    assert early["flatten_at"]<early["close"]


def test_globex_range_switches_at_cash_open_and_missing_minutes_block():
    overnight=bars(SUNDAY-960)
    context=future_levels(overnight,SUNDAY)
    assert context["or_complete"] and context["range_name"]=="Globex"
    assert candidate("MESZ6@1",overnight[-2],overnight[-1],context,SUNDAY)["strategy"]=="futures-orb15-v2"
    assert not future_levels(overnight[1:],SUNDAY)["or_complete"]
    assert not future_levels(overnight,MONDAY-900)["or_complete"]
    regular=bars(MONDAY-960)
    assert future_levels(overnight+regular,MONDAY)["range_name"]=="RTH"
    assert future_levels(overnight+regular,MONDAY)["or_complete"]


def test_overnight_entry_and_overdue_flatten_keep_original_day_deadline(db):
    engine=Engine(db,Config(local=True))
    sig={"id":"overnight","symbol":"MESZ6@1","side":"long","signal_time":SUNDAY,
        "stop_distance":1,"strategy":"fixture","track":"intraday"}
    with db.tx() as c:
        db.put(c,"quote:MESZ6@1",quote(SUNDAY))
        assert engine.enter(c,sig,SUNDAY)
        p=db.get(c,"position:MESZ6@1")
        assert p["flatten_at"]==stamp("2026-09-14T15:45:00")
        assert db.get(c,"risk:2026-09-14")["entries"]==1
        later=stamp("2026-09-14T17:02:00")
        db.put(c,"quote:MESZ6@1",quote(later))
        engine.exits(c,later)
        assert db.get(c,"position:MESZ6@1")["exit_reason"]=="session_flatten"


def test_old_contract_quote_cannot_pass_active_contract_readiness():
    quotes={"MESU6@1":quote(SUNDAY)}
    result=quote_checks((),("MES.c.0",),quotes,[],{"futures":True,"equities":False},SUNDAY,selection(("MES.c.0",),SUNDAY))
    assert result[0]["symbol"]=="MESZ6" and not result[0]["quote_ready"]


def test_option_selection_continues_after_first_fresh_contract_fails_risk(db):
    e=Engine(db,Config(local=True,risk=100))
    sig={"id":"underlying","symbol":"SPY","side":"long","signal_time":MONDAY,
        "signal_price":100,"stop_distance":1,"track":"intraday"}
    contracts=[{"symbol":"O:EXPENSIVE","expiry":"2026-09-14","type":"call","strike":100,"multiplier":100},
        {"symbol":"O:ELIGIBLE","expiry":"2026-09-14","type":"call","strike":101,"multiplier":100}]
    with db.tx() as c:
        db.put(c,"chain:SPY",{"asof":MONDAY,"contracts":contracts})
        db.put(c,"quote:O:EXPENSIVE",quote(MONDAY,10,10.01))
        db.put(c,"quote:O:ELIGIBLE",quote(MONDAY,2,2.01))
        e.options(c,sig,MONDAY)
        assert db.get(c,"position:O:EXPENSIVE") is None
        p=db.get(c,"position:O:ELIGIBLE")
        assert p["status"]=="open" and p["initial_risk"]<=100
        assert p["selection_rejections"][0]["reason"]=="One unit exceeds risk or stop invalid"
        assert len(db.recent(c,"alert"))==1


def historical_fixture(cost=.001,fail=False,missing=False):
    _,start,end=prior_rth("2026-09-14")
    calls=[]
    def resolve(**kwargs):
        return {"result":{s:[{"s":str(i)}] for i,s in enumerate(kwargs["symbols"],1)}}
    def get_range(**kwargs):
        calls.append(kwargs)
        if fail: raise RuntimeError("fixture download interrupted")
        def replay(callback):
            for i,s in enumerate(kwargs["symbols"],1):
                for t in range(int(start),int(end),60):
                    if missing and t==start: continue
                    callback(SimpleNamespace(instrument_id=i,ts_event=int(t*1e9),
                        open=int(100e9),high=int(101e9),low=int(99e9),close=int(100e9),volume=10))
        return SimpleNamespace(replay=replay)
    client=SimpleNamespace(symbology=SimpleNamespace(resolve=resolve),metadata=SimpleNamespace(get_cost=lambda **kw:cost,get_record_count=lambda **kw:390),
        timeseries=SimpleNamespace(get_range=get_range))
    return client,calls


def test_sparse_history_is_complete_only_after_matching_provider_count(db):
    client,calls=historical_fixture(missing=True)
    client.metadata.get_record_count=lambda **kwargs:389
    async def run():
        collector=Collectors(db,Config(local=True))
        try:
            recover(collector,client,SUNDAY)
            recover(collector,client,SUNDAY+3600)
        finally: await collector.close()
    asyncio.run(run())
    assert len(calls)==1
    with db.tx() as c:
        job=list(db.prefix(c,"recovery:futures:").values())[0]
        assert job["status"]=="available" and job["provider_record_counts"]["MESZ6"]==389
        coverage=db.get(c,"historycoverage:MESZ6@1:2026-09-11")
        rows=db.recent(c,"bar","MESZ6@1",limit=400)
        assert not future_levels(rows,SUNDAY)["prior_complete"]
        assert future_levels(rows,SUNDAY,coverage)["prior_complete"]
        assert len(rows)==389  # No forward-filled or synthetic minute.


@pytest.mark.parametrize('legacy_truncation',[False,True])
def test_four_futures_roots_have_room_for_a_complete_session(db,legacy_truncation):
    from compass.store import identity
    cfg=Config(local=True,futures=('ES.c.0','NQ.c.0','MES.c.0','MNQ.c.0'))
    targets=selection(cfg.futures,SUNDAY)
    names=[row['raw_symbol'] for row in targets]
    prior,start,end=prior_rth(targets[0]['trading_day'])
    key='recovery:futures:'+identity(prior,names)
    if legacy_truncation:
        with db.tx() as c:
            db.put(c,key,{'status':'partial','day':prior,'symbols':names,'start':start,'end':end,
                'counts':{name:300 for name in names},'provider_record_counts':{name:390 for name in names},
                'count_verified_at':SUNDAY-60,'estimated_usd':0,'detail':'Known legacy cap'})
    client,calls=historical_fixture(cost=0)
    async def run():
        collector=Collectors(db,cfg)
        try:
            recover(collector,client,SUNDAY)
            recover(collector,client,SUNDAY+3600)
        finally: await collector.close()
    asyncio.run(run())
    assert len(calls)==1 and calls[0]['limit']==1560
    with db.tx() as c:
        job=db.get(c,key)
        assert job['status']=='available' and job['record_limit']==1560
        assert job['counts']=={name:390 for name in names}


@pytest.mark.parametrize("cost,fail,missing,status,count",[(.001,False,False,"available",1),(.2,False,False,"blocked",0),
    (.001,True,False,"error",1),(.001,False,True,"partial",1)])
def test_history_is_bounded_persistent_and_never_retries_uncertain_paid_call(db,cost,fail,missing,status,count):
    client,calls=historical_fixture(cost,fail,missing)
    async def run():
        collector=Collectors(db,Config(local=True,history_budget=.1))
        try:
            recover(collector,client,SUNDAY)
            recover(collector,client,SUNDAY+3600)
        finally: await collector.close()
    asyncio.run(run())
    assert len(calls)==count
    with db.tx() as c:
        job=list(db.prefix(c,"recovery:futures:").values())[0]
        assert job["status"]==status
        if status=="available":
            assert job["counts"]=={"MESZ6":390,"MNQZ6":390}
            assert db.recent(c,"bar","MESU6@1")==[]
            rows=db.recent(c,"bar","MESZ6@1",limit=400)
            assert future_levels(rows,SUNDAY)["prior_complete"]
        if status=="partial": assert job["counts"]["MESZ6"]==389


def test_massive_raw_greek_omissions_are_explained_by_expiry_and_moneyness(db):
    async def run():
        collector=Collectors(db,Config(local=True,massive="fixture"))
        async def get(*args,**kwargs):
            return {"results":[{"details":{"ticker":f"O:{i}","expiration_date":"2026-09-14","strike_price":strike,
                "contract_type":"call","shares_per_contract":100},"open_interest":100,"greeks":g}
                for i,(strike,g) in enumerate([(100,{}),(80,{"gamma":None}),(101,{"gamma":.1}),(102,{"gamma":"invalid"})])]}
        collector.get=get
        try: return await collector.massive_chain("QQQ")
        finally: await collector.close()
    contracts,complete=asyncio.run(run())
    result=diagnose(contracts,100,SUNDAY-86400,SUNDAY,complete)
    assert result["target_expiry"]=="2026-09-14"
    assert result["totals"]["gamma_absent"]==1 and result["totals"]["gamma_null"]==1
    assert result["totals"]["gamma_invalid"]==1 and result["totals"]["gamma_usable"]==1
    assert result["target_near_atm"]["contracts"]==3
    assert any(r["moneyness"]=="deep_ITM_10pct" for r in result["groups"])
    assert diagnose(contracts,None,None,SUNDAY,complete)["target_near_atm"]["contracts"]==0


def test_massive_overlapping_pages_merge_contracts_and_preserve_newest_quote(db):
    async def run():
        collector=Collectors(db,Config(local=True,massive="fixture"))
        row={"details":{"ticker":"O:QQQ","expiration_date":"2026-09-14","strike_price":100,"contract_type":"call","shares_per_contract":100},
            "greeks":{"gamma":.1},"last_quote":{"last_updated":MONDAY*1e9}}
        pages=[{"results":[row],"next_url":"https://api.massive.com/v3/snapshot/options/QQQ?cursor=2"},
            {"results":[{**row,"greeks":{},"last_quote":{"last_updated":(MONDAY-60)*1e9}}]}]
        async def get(*args,**kwargs): return pages.pop(0)
        collector.get=get
        try:
            contracts,complete=await collector.massive_chain("QQQ")
            assert complete and len(contracts)==1 and contracts[0]["gamma"]==.1
            assert collector.chain_paging["QQQ"]["overlaps_merged"]==1
            assert collector.chain_paging["QQQ"]["conflicting_overlaps"]==1
        finally: await collector.close()
    asyncio.run(run())


def test_massive_next_url_cursor_reaches_http_transport(db):
    requests=[]
    async def run():
        collector=Collectors(db,Config(local=True,massive="fixture"))
        await collector.client.aclose()
        def respond(request):
            requests.append(request)
            assert request.url.params["apiKey"]=="fixture"
            if len(requests)==1:
                return httpx.Response(200,json={"results":[],"next_url":"https://api.massive.com/v3/snapshot/options/SPY?cursor=next%2Bpage%3D&order=asc"})
            assert request.url.params["cursor"]=="next+page="
            assert request.url.params["order"]=="asc"
            return httpx.Response(200,json={"results":[]})
        collector.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            _,complete=await collector.massive_chain("SPY")
            assert complete and len(requests)==2
        finally: await collector.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure",["timeout","max_connections"])
def test_option_auth_never_waits_forever_or_hides_connection_rejection(db,monkeypatch,failure):
    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def send(self,*args): pass
        async def recv(self):
            if failure=="timeout": raise asyncio.TimeoutError
            return json.dumps([{"ev":"status","status":"max_connections","message":"fixture connection limit"}])
    monkeypatch.setattr("compass.providers.websockets.connect",lambda *a,**kw:Socket())
    async def run():
        collector=Collectors(db,Config(local=True,massive="fixture"))
        collector.option_symbols={"O:FIXTURE"}
        try:
            with pytest.raises(FeedError,match="timed out" if failure=="timeout" else "max_connections"):
                await collector.options()
        finally: await collector.close()
    asyncio.run(run())


def flow_fixture(page,total=800,correction=False):
    return {"page":page,"pageSize":100,"total":total,"data":[{"id":i,"ticker":"SPY",
        "tradeTime":"2026-09-14T14:00:00Z","premium":70000 if correction else 60000}
        for i in range((page-1)*100,min(page*100,total))]}


def test_flow_resume_survives_restart_beyond_500_and_updates_corrections(db):
    requested=[]
    async def run(correction=False):
        collector=Collectors(db,Config(local=True))
        async def request(path,label):
            p=int(parse_qs(urlparse(path).query)["page"][0])
            requested.append(p)
            return flow_fixture(p,correction=correction),MONDAY
        collector.matrix_request=request
        try: return await collect(collector,MONDAY)
        finally: await collector.close()
    first=asyncio.run(run())
    assert requested==[1,2,3,4,5]
    assert first["recovery"]["estimated_gap"]==300
    requested.clear()
    second=asyncio.run(run(True))
    assert requested==[1,5,6,7,8]
    assert second["unique_rows"]==800 and second["recovery"]["estimated_gap"]==0
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(flow_records)).scalar_one()==800
        assert c.execute(select(flow_records.c.payload).where(flow_records.c.vendor_id=="0")).scalar_one()["premium"]==70000


def test_flow_bad_page_does_not_advance_checkpoint_and_prior_gap_recovers(db):
    part=flow_page(flow_fixture(1,200),MONDAY)
    ingest(db,"2026-09-14",1,part,MONDAY)
    with pytest.raises(ValueError): ingest(db,"2026-09-14",2,part,MONDAY)
    with db.tx() as c:
        assert db.get(c,"recovery:flow:2026-09-14")["next_page"]==2
    requests=[]
    next_day=stamp("2026-09-15T08:00:00")
    async def run():
        collector=Collectors(db,Config(local=True))
        async def request(path,label):
            params=parse_qs(urlparse(path).query)
            requests.append(params)
            p=int(params["page"][0])
            return flow_fixture(p,200 if params["timeFrame"]==["yesterday"] else 0),next_day
        collector.matrix_request=request
        try: return await collect(collector,next_day)
        finally: await collector.close()
    result=asyncio.run(run())
    assert [r["timeFrame"] for r in requests]==[["today"],["yesterday"]]
    assert result["prior_gaps"]==[]


def test_explicit_discord_test_is_authenticated_idempotent_and_never_a_position(tmp_path):
    cfg=Config(local=True,role="web",db="sqlite:///"+str(tmp_path/"alert.db"),password="test-password-long-enough",
        secret="test-secret-long-enough",stocks=("SPY",),futures=())
    app=create_app(cfg)
    request={"request_id":str(uuid.uuid4())}
    with TestClient(app) as web:
        assert web.post("/api/alerts/test",json=request).status_code==401
        web.post("/login",json={"password":cfg.password})
        app.state.db.health("discord","connected","fixture")
        one=web.post("/api/alerts/test",json=request)
        assert one.status_code==200
        assert web.post("/api/alerts/test",json=request).json()==one.json()
        assert web.post("/api/alerts/test",json={"request_id":str(uuid.uuid4())}).status_code==429
        with app.state.db.tx() as c:
            assert app.state.db.prefix(c,"position:")=={}
            rows=app.state.db.recent(c,"alert")
            assert len(rows)==1 and rows[0]["payload"]["mode"]=="TEST"
        def response(req):
            payload=json.loads(req.content)
            assert payload["content"].startswith("[TEST — NO TRADE]")
            assert payload["allowed_mentions"]=={"parse":[]}
            return httpx.Response(200,json={"id":"12345678"})
        async def send():
            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                await dispatch(client,app.state.db,"https://discord.com/api/webhooks/123/fixture",rows[0])
        asyncio.run(send())
        state=web.get("/api/state").json()
        assert state["delivery"]["last_test_confirmation"]["event_id"]==one.json()["event_id"]
        assert state["delivery"]["pending"]==0 and state["trades"]==[]
