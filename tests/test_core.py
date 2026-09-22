import asyncio
from datetime import datetime
import pytest
import httpx
from fastapi.testclient import TestClient
from compass.config import Config
from compass.store import Store,events
from compass.market import fresh,levels,session,NY,ts
from compass.exposure import calculate,vanna
from compass.engine import Engine,candidate,fill
from compass.providers import Collectors,FeedError
from compass.app import create_app

NOW=datetime(2026,9,14,9,46,tzinfo=NY).timestamp()
START=NOW-16*60

@pytest.fixture
def db(tmp_path):
    s=Store("sqlite:///"+str(tmp_path/"test.db"))
    s.initialize()
    yield s
    s.engine.dispose()

@pytest.fixture
def cfg(tmp_path):
    return Config(paper_trading=True,db="sqlite:///"+str(tmp_path/"web.db"),local=True,role="web",
        password="test-password-for-tests-only",secret="test-signing-secret-for-tests-only",
        stocks=("SPY",),futures=(),alpaca_key="",alpaca_secret="",massive="",databento="",matrix="",discord="",openai="")

def quote(now=NOW,bid=102.10,ask=102.11):
    return {"ts":now,"bid":bid,"ask":ask,"bid_size":100,"ask_size":100,"source":"fixture"}

def bars():
    out=[]
    for i in range(16):
        b={"o":100,"h":101,"l":99,"c":100,"v":100,"vw":100}
        if i==15: b.update(h=102.2,c=102)
        out.append({"id":i+1,"ts":START+i*60,"payload":b})
    return out

def signal():
    return {"id":"fixture-signal","symbol":"SPY","side":"long","signal_time":NOW,
        "signal_price":102,"stop_distance":1,"strategy":"fixture","track":"intraday"}

def test_stale_missing_locked_and_future_quotes_rejected():
    assert fresh(quote(),NOW)
    assert not fresh(quote(NOW-6),NOW)
    assert not fresh(quote(NOW+2),NOW)
    assert not fresh(quote(bid=102,ask=102),NOW)
    assert not fresh({**quote(),"ask_size":0},NOW)
    assert not fresh(None,NOW)

def test_closed_bar_opening_range_and_holiday():
    rows=bars()
    assert levels(rows,START+14*60)["or_complete"] is False
    assert levels(rows,NOW)["or_complete"] is True
    assert session("2026-12-25") is None
    # A future candle cannot alter the completed opening range.
    assert levels(rows+[{"id":99,"ts":NOW+60,"payload":{"h":1000,"l":1,"c":999,"o":2,"v":10}}],NOW)["or_high"]==101
    assert not levels([r for r in rows if r["id"]!=3],NOW)["or_complete"]

def test_signal_requires_new_closed_fresh_cross():
    r=bars()
    s=candidate("SPY",r[-2],r[-1],levels(r,NOW),NOW)
    assert s and s["side"]=="long"
    assert candidate("SPY",r[-2],r[-1],levels(r,NOW),NOW+91) is None
    assert candidate("SPY",r[-2],r[-1],levels(r,NOW),NOW-1) is None

def test_missing_open_interest_does_not_become_zero():
    base={"symbol":"O:TEST","underlying":"SPY","strike":102,"type":"call","multiplier":100,"gamma":.1,"expiry":"2026-09-14","iv":.3}
    e=calculate([{**base,"oi":None},{**base,"oi":0}],102,NOW,"fixture")
    assert e["contracts"]==2 and e["usable_gex"]==1 and e["coverage"]==.5
    assert e["gex"]==0 and e["status"]=="partial"
    assert calculate([{**base,"oi":None}],102,NOW,"fixture")["gex"] is None

def test_vex_units_and_expiration():
    base={"underlying":"SPY","strike":102,"type":"call","multiplier":100,"gamma":.1,"expiry":"2026-09-14","iv":.3,"oi":100}
    e=calculate([base],102,NOW,"fixture")
    assert e["usable_vex"]==1
    assert vanna(100,100,.2,0) is None
    assert calculate([base],None,NOW,"fixture")["status"]=="blocked"

def test_idempotent_record_and_contract_isolation(db):
    with db.tx() as c:
        assert db.append(c,"bar","fixture","MESU6@100",NOW,{"c":100},"same-key")
        assert not db.append(c,"bar","fixture","MESU6@100",NOW,{"c":100},"same-key")
        db.append(c,"bar","fixture","MESZ6@200",NOW,{"c":200})
        assert len(db.recent(c,"bar","MESU6@100"))==1
        assert db.recent(c,"bar","MESZ6@200")[0]["payload"]["c"]==200

def test_simulation_does_not_fill_without_quote(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        assert e.enter(c,signal(),NOW) is False
        assert db.get(c,"position:SPY") is None
        assert db.recent(c,"alert")[0]["payload"]["status"]=="skipped"

def test_entry_gap_exit_and_costs(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,"quote:SPY",quote())
        assert e.enter(c,signal(),NOW)
        p=db.get(c,"position:SPY")
        assert p["entry"]>quote()["ask"]
        db.put(c,"quote:SPY",quote(NOW+2,bid=p["stop"]-2,ask=p["stop"]-1.99))
        e.exits(c,NOW+2)
        p=db.get(c,"position:SPY")
        assert p["status"]=="closed" and p["exit"]<p["stop"] and p["pnl"]<0

def test_stale_exit_leaves_position_unresolved(db,cfg):
    e=Engine(db,cfg)
    with db.tx() as c:
        db.put(c,"quote:SPY",quote())
        e.enter(c,signal(),NOW)
        e.exits(c,NOW+20)
        assert db.get(c,"position:SPY")["status"]=="open"
        assert any(a["payload"]["status"]=="management_blocked" for a in db.recent(c,"alert"))

def test_engine_transaction_rolls_back_signal_position_and_cursor(db,cfg,monkeypatch):
    e=Engine(db,cfg)
    with db.tx() as c:
        for b in bars(): db.append(c,"bar","fixture","SPY",b["ts"],b["payload"])
        db.put(c,"quote:SPY",quote())
    def fail(*args): raise RuntimeError("crash fixture")
    monkeypatch.setattr(e,"options",fail)
    with pytest.raises(RuntimeError): e.tick(NOW)
    with db.tx() as c:
        assert db.get(c,"position:SPY") is None
        assert db.get(c,"cursor:SPY") is None
        assert not db.recent(c,"signal")
    monkeypatch.setattr(e,"options",lambda *args,**kwargs:None)
    e.tick(NOW)
    e.tick(NOW)
    with db.tx() as c:
        assert len(db.prefix(c,"trade:"))==1
        assert len(db.recent(c,"signal"))==1

def test_worker_lease_has_one_owner(db):
    with db.tx() as c:
        assert db.lease(c,"engine","one")
        assert not db.lease(c,"engine","two")

def test_auth_private_state_and_origin_guard(cfg):
    app=create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/health").status_code==200
        assert client.get("/api/state").status_code==401
        assert client.post("/login",json={"password":"bad"}).status_code==401
        assert client.post("/login",json={"password":cfg.password}).status_code==200
        response=client.get("/api/state")
        assert response.status_code==200
        assert response.json()["quotes"]=={}
        assert cfg.password not in response.text and cfg.secret not in response.text
        assert client.post("/api/ask",json={"question":"Hello"},headers={"Origin":"https://elsewhere.example"}).status_code==403
        assert client.post("/api/ask",json={"question":"What do we know?"}).json()["configured"] is False

def test_unconfigured_password_never_exposes_data(cfg):
    cfg.password=""
    with TestClient(create_app(cfg)) as client:
        assert client.get("/health").status_code==200
        assert client.get("/api/state").status_code==503
        assert "Setup required" in client.get("/login").text

def test_massive_pagination_rejects_unexpected_host(db,cfg):
    async def scenario():
        c=Collectors(db,cfg)
        c.cfg.massive="fixture-secret"
        await c.client.aclose()
        c.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"results":[],"next_url":"https://attacker.invalid/collect"})))
        with pytest.raises(FeedError,match="Unexpected pagination host"): await c.massive_chain("SPY")
        await c.close()
    asyncio.run(scenario())

def test_timestamp_units():
    assert ts(1700000000000000000)==1700000000
    assert ts(1700000000000)==1700000000

def test_databento_sdk_contract_is_supported():
    import databento as db
    assert all(hasattr(db.Live,name) for name in ["subscribe","start","stop","block_for_close","symbology_map"])
    assert db.OHLCVMsg and db.MBP1Msg and db.SymbolMappingMsg

def test_alpaca_unmatched_contracts_remain_in_coverage(db,cfg):
    async def scenario():
        collector=Collectors(db,cfg)
        await collector.client.aclose()
        metadata={"option_contracts":[{"symbol":"SPY260914C00102000","expiration_date":"2026-09-14","strike_price":"102","type":"call","size":"100","open_interest":"100"}]}
        snapshots={"snapshots":{"SPY260914P00102000":{"greeks":{"gamma":.1}}}}
        def handler(request):
            return httpx.Response(200,json=metadata if request.url.host=="paper-api.alpaca.markets" else snapshots)
        collector.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        contracts,complete=await collector.alpaca_chain("SPY")
        assert complete and len(contracts)==2
        assert {o["join_status"] for o in contracts}=={"missing_snapshot","missing_contract_metadata"}
        assert calculate(contracts,102,NOW,"alpaca")["usable_gex"]==0
        await collector.close()
    asyncio.run(scenario())

def test_databento_constructs_on_background_thread_without_network():
    import databento as db
    from concurrent.futures import ThreadPoolExecutor
    def construct():
        client=db.Live(key="fixture-not-a-real-key",reconnect_policy="none",slow_reader_behavior="warn")
        assert not client.is_connected()
        client.terminate()
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(construct).result(timeout=10)
