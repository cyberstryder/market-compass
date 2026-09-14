import asyncio
import json
import time
import httpx
import pytest
from compass.alerts import dispatch, DeliveryError, outbox_status
from compass.config import Config
from compass.exposure import calculate
from compass.providers import Collectors, select_contracts
from compass.flow_recovery import collect as collect_flow
from compass.readiness import decorate_health, quote_checks
from compass.store import Store
from compass.vendor import matrix_summary, flow_page
from compass.app import create_app
from fastapi.testclient import TestClient


NOW = 1789398000.0


@pytest.fixture
def db(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "premarket.db"))
    store.initialize()
    yield store
    store.engine.dispose()


def matrix_fixture():
    # Synthetic numbers; shape observed in the paid response. Null is not zero.
    return {"success": True, "cached": True, "data": {
        "underlyingSymbol": "SPY", "snapshotTime": "2026-09-12T18:02:19.599Z", "spotPrice": 100,
        "expirations": ["2026-09-14", "2026-09-15"], "rows": [
            {"strike": 100, "gex": [0, None], "vex": [None, None]},
            {"strike": 101, "gex": [-4, 10], "vex": [2, None]}]}}


def flow_row(identity=1):
    # Documented fields; this fixture is not a real market observation.
    return {"id": identity, "ticker": "SPY", "tradeTime": "2026-09-14T14:00:00Z",
        "type": "CALL", "strike": 100, "expiry": "09/14/26", "premium": 60000,
        "volume": 10, "openInterest": None, "sentiment": "Bullish", "score": 90,
        "tradeType": "SWEEP", "isRepeatFlow": False, "isRedacted": False}


def page(rows, number=1, total=3):
    return {"data": rows, "page": number, "pageSize": 2, "total": total,
        "filters": {"timeFrame": "today", "minPremium": 50000},
        "aggregates": {"totalPremium": 180000, "bullishPremium": 180000, "bearishPremium": 0}}


def test_matrix_preserves_cached_source_time_nulls_and_expiration_alignment():
    result = matrix_summary(matrix_fixture(), NOW)
    assert result["source_ts"] < NOW - 86400
    assert result["received"] == NOW and result["cached"] is True
    assert result["gex"] == 6 and result["vex"] == 2
    assert result["strikes"][0]["gex"] == 0 and result["strikes"][0]["vex"] is None
    assert result["expirations"][1]["gex"] == 10
    assert result["expirations"][1]["vex"] is None
    assert result["populated_cells"]["gex"] == 3 and result["cells"] == 4
    assert "unverified" in result["methodology"]


@pytest.mark.parametrize("change", ["alignment", "duplicate", "timestamp", "bad_number"])
def test_matrix_schema_drift_never_silently_shifts_cells(change):
    payload = matrix_fixture()
    if change == "alignment": payload["data"]["rows"][1]["gex"] = [10]
    if change == "duplicate": payload["data"]["rows"][1]["strike"] = 100
    if change == "bad_number": payload["data"]["rows"][1]["gex"] = ["oops", 10]
    if change == "timestamp":
        payload["data"]["snapshotTime"] = "2026-09-14T09:00:00"
        result = matrix_summary(payload, NOW)
        assert result["source_ts"] is None and result["status"] == "partial"
    else:
        with pytest.raises(ValueError): matrix_summary(payload, NOW)


def test_flow_missing_time_and_redaction_are_counted_not_dated_at_fetch():
    bad = {**flow_row(2), "tradeTime": None}
    result = flow_page(page([flow_row(), bad]), NOW)
    assert result["rejected"] == 1 and len(result["rows"]) == 1
    assert result["rows"][0]["source_ts"] != NOW
    assert result["rows"][0]["open_interest"] is None
    assert result["rows"][0]["classification"] == "SWEEP"
    assert flow_page(page([{**flow_row(), "isRedacted": True}]), NOW)["rejected"] == 1
    assert flow_page(page([], total=0), NOW)["rows"] == []


def test_flow_paging_deduplicates_overlaps_and_repeated_polls(db):
    async def run():
        collector = Collectors(db, Config(local=True, matrix="fake", stocks=("SPY",)))
        with db.tx() as c:
            db.put(c, "matrix:SPY", {"received":time.time()})
        requested = []
        async def request(path, label):
            requested.append(path)
            return (page([flow_row(1), flow_row(2)]) if "page=1&" in path
                else page([flow_row(2), flow_row(3)], number=2)), NOW
        collector.matrix_request = request
        try:
            await collect_flow(collector,NOW,page_cap=2)
            await collect_flow(collector,NOW,page_cap=2)
        finally: await collector.close()
        assert len(requested) == 4
        with db.tx() as c:
            assert len(db.recent(c, "vendor_flow")) == 3
            summary = db.get(c, "matrix:unusual_activity")
            assert summary["unique_rows"] == 3 and summary["fetched_rows"] == 4
            assert summary["limited"] is False
            assert summary["source_ts"] != summary["received"]
    asyncio.run(run())


def test_flow_page_cap_is_visible(db):
    async def run():
        collector = Collectors(db, Config(local=True, matrix="fake", stocks=("SPY",)))
        with db.tx() as c:
            db.put(c, "matrix:SPY", {"received":time.time()})
        count = 0
        async def request(path, label):
            nonlocal count
            count += 1
            return page([flow_row(count)], number=count, total=1000), NOW
        collector.matrix_request = request
        try: await collect_flow(collector,NOW,page_cap=2)
        finally: await collector.close()
        assert count == 2
        with db.tx() as c:
            summary = db.get(c, "matrix:unusual_activity")
            assert summary["limited"] is True and summary["status"] == "partial"
    asyncio.run(run())


def test_closed_exchange_does_not_hide_dead_worker_or_provider_error():
    health = [{"name": "databento_futures", "status": "receiving", "detail": "Stream", "checked_at": NOW-5000, "source_ts": NOW-5000}]
    workers = {"worker:collector": {"at": NOW-2}}
    closed = {"equities": False, "futures": False}
    assert decorate_health(health, workers, closed, NOW)[0]["status"] == "market_closed"
    assert decorate_health(health, {}, closed, NOW)[0]["status"] == "stale"
    assert decorate_health([{**health[0], "status": "error"}], workers, closed, NOW)[0]["status"] == "error"
    assert decorate_health(health, workers, {**closed, "futures": True}, NOW)[0]["status"] == "stale"


def test_connected_without_events_is_not_open_market_readiness():
    health = [{"name": "alpaca_stocks", "status": "connected", "detail": "Subscribed", "checked_at": NOW-300}]
    workers = {"worker:collector": {"at": NOW-1}}
    markets = {"equities": True, "futures": False}
    assert decorate_health(health, workers, markets, NOW)[0]["status"] == "waiting"
    quotes = {"SPY": {"bid": 100, "ask": 100.01, "bid_size": 5, "ask_size": 5, "ts": NOW-1}}
    checks = quote_checks(("SPY", "QQQ"), ("MES.c.0",), quotes, [], markets, NOW)
    assert [row["status"] for row in checks] == ["ready", "missing", "market_closed"]
    quotes["SPY"]["ts"] = NOW-6
    assert quote_checks(("SPY",), (), quotes, [], markets, NOW)[0]["quote_ready"] is False
    quotes["SPY"].update(ts=NOW, bid_size=None)
    assert quote_checks(("SPY",), (), quotes, [], markets, NOW)[0]["quote_ready"] is False


def test_input_coverage_is_independent_of_price_and_missing_vanna_is_null():
    base = {"underlying": "SPY", "strike": 100, "type": "call", "multiplier": 100, "gamma": .1, "oi": 0, "expiry": "2026-09-18", "iv": None}
    result = calculate([base, {**base, "oi": None}], None, NOW, "fixture")
    assert result["status"] == "blocked" and result["coverage"] == .5
    assert result["usable_gex"] == 1 and result["missing_oi"] == 1
    assert result["gex"] is None
    priced = calculate([base], 100, NOW, "fixture")
    assert priced["gex"] == 0 and priced["strikes"][0]["vex"] is None


def test_stream_selection_balances_nearby_calls_and_puts_without_old_price_fills():
    contracts = [{"symbol": f"{side}{strike}", "type": side, "strike": strike, "expiry": "2026-09-14", "multiplier": 100}
        for side in ("call", "put") for strike in (50, 99, 100, 101, 150)]
    selected = select_contracts(contracts, 100, 4)
    assert len(selected) == 4 and sum(row["type"] == "call" for row in selected) == 2
    assert all(abs(row["strike"]-100) <= 1 for row in selected)
    assert select_contracts(contracts, None, 4) == []


def test_discord_requires_saved_message_and_retains_queue_on_ambiguous_response(db):
    with db.tx() as c:
        db.append(c, "alert", "fixture", "SPY", NOW, {"status": "skipped", "reason": "Fixture only"})
        row = db.recent(c, "alert")[0]
    replies = [httpx.Response(204), httpx.Response(429, json={"retry_after": 2}),
        httpx.Response(200, json={}), httpx.Response(200, json={"id": "987654321"})]
    def handler(request):
        assert request.url.params["wait"] == "true"
        assert request.url.params["thread_id"] == "456"
        assert b"[SIMULATED]" in request.content
        return replies.pop(0)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(3):
                with pytest.raises(DeliveryError):
                    await dispatch(client, db, "https://discord.com/api/webhooks/123/fake?thread_id=456&wait=false", row)
                with db.tx() as c:
                    assert outbox_status(db, c, NOW)["pending"] == 1
                    assert db.get(c, "outbox:discord") is None
            await dispatch(client, db, "https://discord.com/api/webhooks/123/fake?thread_id=456", row)
    asyncio.run(run())
    with db.tx() as c:
        assert outbox_status(db, c, NOW)["pending"] == 0
        assert db.get(c, "outbox:discord:confirmation")["message_id"] == "987654321"
        assert len(db.recent(c, "alert_delivery")) == 1


@pytest.mark.parametrize("status,has_text,expected", [("completed",True,200),("incomplete",True,502),("completed",False,502)])
def test_grounded_answer_preserves_vendor_context_and_rejects_incomplete_output(tmp_path,monkeypatch,status,has_text,expected):
    cfg=Config(local=True,role="web",db="sqlite:///"+str(tmp_path/"ask.db"),
        password="test-password-with-enough-length",secret="test-signing-secret-with-enough-length",openai="fake-key",model="gpt-5-mini")
    calls=[]
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,headers,json):
            calls.append(json)
            return httpx.Response(200,json={"status":status,
                "incomplete_details":{"reason":"max_output_tokens"} if status=="incomplete" else None,
                "usage":{"output_tokens":6000,"output_tokens_details":{"reasoning_tokens":5900}},
                "output":[{"content":[{"type":"output_text","text":"Source is a previous snapshot."}]}] if has_text else []})
    monkeypatch.setattr("compass.app.httpx.AsyncClient",lambda **kwargs:Client())
    app=create_app(cfg)
    with TestClient(app) as client:
        with app.state.db.tx() as c:
            app.state.db.put(c,"matrix:SPY",matrix_summary(matrix_fixture(),NOW))
        client.post("/login",json={"password":cfg.password})
        result=client.post("/api/ask",json={"question":"What is the SPY vendor context?"})
        assert result.status_code==expected
        with app.state.db.tx() as c:
            health=app.state.db.get(c,"health:assistant")
            assert health["status"]==("available" if expected==200 else "error")
            assert health["generation"]["reasoning_tokens"]==5900
    assert len(calls)==1  # No automatic paid retry on incomplete or empty output.
    vendor=json.loads(calls[0]["input"])["market_context"]["matrix"]["matrix:SPY"]
    assert vendor["gex"]==6 and vendor["source_asof"] is not None
    assert "gex_cells" not in vendor["strikes"][0]
    assert calls[0]["reasoning"]["effort"]=="low" and calls[0]["max_output_tokens"]==6000
