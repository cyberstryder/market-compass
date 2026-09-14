import asyncio
import copy
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from compass.app import create_app
from compass.config import Config
from compass.projects import context_for, futures_record, ingest, poll_source, records, snapshot
from compass.store import Store

TOKEN = "isolated-observer-test-" + "x" * 32


@pytest.fixture
def db(tmp_path):
    db = Store("sqlite:///" + str(tmp_path / "projects.db"))
    db.initialize()
    yield db
    db.engine.dispose()


def source(now, **updates):
    return {"id": "source-entry-123", "symbol": "SPY", "source_ts": now,
        "status": "tracking", "strategy": "Original ORB", "entry": 100.0,
        "original": {"options_dte": "after signal day"}, **updates}


def fill(now, **updates):
    return {"ticker": "MNQ1!", "action": "buy", "position": "long", "prev_position": "flat",
        "quantity": "1", "price": "24000.25", "order_id": "Long", "interval": "3", "version": "0.10.3",
        "bar_time": datetime.fromtimestamp(now - 60, timezone.utc).isoformat(),
        "time": datetime.fromtimestamp(now, timezone.utc).isoformat(), **updates}


def test_idempotent_updates_and_first_context_never_rewritten(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "quote:SPY", {"ts": now - 1, "bid": 100, "ask": 101})
        assert ingest(db, c, "morning", source(now), now)
        assert not ingest(db, c, "morning", source(now), now + 2)
        db.put(c, "quote:SPY", {"ts": now + 60, "bid": 110, "ask": 111})
        assert ingest(db, c, "morning", source(now, status="observed_60m"), now + 60)
        row = c.execute(select(records)).mappings().one()
        assert row["context"]["quote"]["bid"] == 100
        assert row["context"]["captured_at"] == now
        assert row["payload"]["original"]["options_dte"] == "after signal day"
        assert len(db.recent(c, "project_update")) == 2
        assert db.recent(c, "alert") == []
        assert db.prefix(c, "position:") == {}


def test_old_import_has_no_hindsight_market_context_or_notification(db):
    now = time.time()
    with db.tx() as c:
        ingest(db, c, "morning", source(now - 86400), now, notify=True)
        saved = c.execute(select(records.c.context)).scalar_one()
        assert saved["purpose"] == "historical_import" and "quote" not in saved
        assert not db.recent(c, "alert")


def test_same_id_in_different_projects_is_independent(db):
    now = time.time()
    with db.tx() as c:
        ingest(db, c, "morning", source(now), now)
        ingest(db, c, "smoothers", source(now), now)
        assert c.execute(select(func.count()).select_from(records)).scalar_one() == 2
        with pytest.raises(ValueError, match="identity changed"):
            ingest(db, c, "morning", source(now, symbol="QQQ"), now)


def test_gold_does_not_inherit_equity_gamma_or_fake_databento_quote(db):
    with db.tx() as c:
        ctx = context_for(db, c, "MGC1!", time.time())
        assert ctx["quote"] is None and ctx["exposure_symbol"] is None
        assert ctx["quote_basis"] == "no_independent_MGC_quote_feed"
        ctx = context_for(db, c, "MNQ1!", time.time())
        assert ctx["exposure_symbol"] == "QQQ" and ctx["exposure_relationship"] == "cross_asset_context"


def test_current_fetch_does_not_make_unknown_matrix_clock_current(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "matrix:SPY", {"source_ts": None, "received": now, "strikes": []})
        assert context_for(db, c, "SPY", now)["matrix"]["freshness"] == "source_time_unknown"


def test_source_recovery_rechecks_mutable_outcomes_and_late_records(db):
    now = time.time()
    cfg = Config(local=True, morning_url="https://morning.example", morning_token=TOKEN)
    rows = [source(now - 100)]
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return httpx.Response(200, json={"schema_version": 1, "project": "morning", "mode": "observe_only",
            "records": rows, "next": None})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await poll_source(db, cfg, "morning", client)
            rows[0] = source(now - 100, status="observed_60m")
            rows.append(source(now - 1000, id="late-arrival"))
            await poll_source(db, cfg, "morning", client)
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(records)).scalar_one() == 2
        assert snapshot(db, c, cfg, now)["projects"][0]["status"] == "connected"
        assert db.prefix(c, "position:") == {}
    assert len(requests) == 2


def test_connection_goes_stale_without_deleting_observations(db):
    now = time.time()
    with db.tx() as c:
        db.put(c, "health:project_morning", {"status": "connected", "checked_at": now - 30})
        ingest(db, c, "morning", source(now - 100), now)
        result = snapshot(db, c, Config(local=True), now)
        assert result["projects"][0]["status"] == "stale"
        assert result["projects"][0]["record_count"] == 1


def test_receiving_endpoint_does_not_claim_unsaved_mirror_is_active(db):
    with db.tx() as c:
        result = snapshot(db,c,Config(local=True,futures_observer_token=TOKEN,observer_streams=("mnq",)),time.time())
        future = result["projects"][2]
        assert future["status"] == "partial"
        assert future["streams"][0]["status"] == "awaiting_first_event"
        assert future["streams"][1]["status"] == "configuration_pending"
        assert result["projects"][0]["name"] == "Morning Algo"


@pytest.mark.parametrize("updates", [{"ticker": "MES1!"}, {"version": "0.10.2"}, {"interval": "5"},
    {"price": "nan"}, {"quantity": "inf"}, {"time": "2026-09-14T09:30:00"}, {"order_id": ""}])
def test_invalid_futures_observation_rejected(updates):
    now = time.time()
    with pytest.raises(ValueError): futures_record("mnq", fill(now, **updates), now)


def test_scoped_webhook_auth_dedup_and_no_order_route(tmp_path):
    cfg = Config(local=True, role="web", db="sqlite:///" + str(tmp_path / "app.db"),
        password="private-owner-password", futures_observer_token=TOKEN, morning_token=TOKEN + "morning")
    app = create_app(cfg)
    path = "/hooks/projects/futures/" + TOKEN + "/mnq"
    with TestClient(app) as client:
        assert client.post(path.replace(TOKEN, "wrong"), json=fill(time.time())).status_code == 401
        assert client.get("/api/projects", headers={"Authorization": "Bearer " + TOKEN}).status_code == 401
        assert client.get("/api/integrations/context?symbol=SPY").status_code == 401
        r = client.get("/api/integrations/context?symbol=SPY", headers={"Authorization": "Bearer " + TOKEN + "morning"})
        assert r.status_code == 200 and r.json()["project"] == "morning"
        event = fill(time.time() - 120)  # Historical fixture must not queue Discord.
        assert client.post(path, json=event).status_code == 202
        assert client.post(path, json=event).json()["status"] == "duplicate"
        assert client.post(path, content="x" * 8193).status_code == 413
        with app.state.db.tx() as c:
            assert not app.state.db.recent(c, "alert")
            assert not app.state.db.prefix(c, "position:")
            assert c.execute(select(func.count()).select_from(records)).scalar_one() == 1
        assert not any("/orders" in r.path for r in app.routes)


def pine_alert(now, stream="mnq", action="buy", reason="entry"):
    # JSON contract of the saved v0.10.3/v0.7.3 tpEntry/tpExit functions.
    # All order-generating Pine calls disable fill alerts; alert() emits this.
    root, version = ("MNQ", "0.10.3") if stream == "mnq" else ("MGC", "0.7.3")
    price = 24000.25 if stream == "mnq" else 3600.1
    event = {"ticker": root + "1!", "action": action, "orderType": "market", "price": price,
        "time": datetime.fromtimestamp(now, timezone.utc).isoformat(), "interval": "3",
        "extras": {"version": root + version, "reason": reason}}
    if action == "exit":
        event.update(cancel=True, ignoreTradingWindows=True)
    else:
        direction = 1 if action == "buy" else -1
        event.update(sentiment="long" if direction == 1 else "short", quantity=1,
            quantityType="fixed_quantity", cancel=False,
            stopLoss={"type": "stop", "stopPrice": price - direction * 10},
            takeProfit={"limitPrice": price + direction * 15})
    return event


@pytest.mark.parametrize("stream,action", [("mnq", "buy"), ("mnq", "sell"), ("mgc", "sell")])
def test_actual_pine_entry_contract_preserves_bracket_without_claiming_broker_fill(stream, action):
    now = time.time()
    event = pine_alert(now, stream, action)
    row = futures_record(stream, event, now)
    assert row["status"] == "entry_alert" and row["qty"] == 1
    assert row["entry"] == row["source_price"] == event["price"]
    assert row["stop"] == event["stopLoss"]["stopPrice"]
    assert row["target"] == event["takeProfit"]["limitPrice"]
    assert row["fill_price"] is None and row["outcome"]["broker_pnl"] is None
    assert row["outcome"]["complete_trade_history"] is False
    assert row["original"] == event
    dated = {**event, "ticker": ("MNQ" if stream == "mnq" else "MGC") + "Z2026"}
    assert futures_record(stream, dated, now)["symbol"] == dated["ticker"]


@pytest.mark.parametrize("stream", ["mnq", "mgc"])
@pytest.mark.parametrize("reason,status", [("session_cutoff", "flatten_alert"), ("60-bar timeout", "exit_alert"),
    ("Time exit", "exit_alert"), ("entry_context_guard", "exit_alert")])
def test_custom_exit_is_an_instruction_even_without_a_prior_entry(stream, reason, status):
    now = time.time()
    row = futures_record(stream, pine_alert(now, stream, "exit", reason), now)
    assert row["status"] == status
    assert row["qty"] is None and row["entry"] is None and row["fill_price"] is None
    assert row["outcome"]["position"] == "unverified"
    assert row["outcome"]["bracket_fills_observed"] is False


@pytest.mark.parametrize("update", [
    {"ticker": "MES1!"}, {"ticker": "MNQZ26"}, {"interval": "5"},
    {"extras": {"version": "MGC0.7.3", "reason": "entry"}},
    {"extras": {"version": "MNQ0.10.3", "reason": ""}},
    {"price": True}, {"price": "nan"}, {"quantity": True}, {"quantity": 2},
    {"sentiment": "short"}, {"stopLoss": {"type": "stop", "stopPrice": 25000}},
    {"takeProfit": {"limitPrice": 23000}}, {"orderType": "limit"}, {"cancel": True},
    {"time": "2026-09-14T09:30:00"}, {"time": "2020-01-01T00:00:00Z"},
])
def test_invalid_custom_alerts_fail_closed(update):
    now = time.time()
    event = copy.deepcopy(pine_alert(now))
    event.update(update)
    with pytest.raises(ValueError): futures_record("mnq", event, now)


def test_mgc_has_no_long_entry_and_exit_cannot_claim_position_size():
    now = time.time()
    with pytest.raises(ValueError): futures_record("mgc", pine_alert(now, "mgc", "buy"), now)
    event = pine_alert(now, action="exit", reason="session_cutoff")
    with pytest.raises(ValueError): futures_record("mnq", {**event, "quantity": 1}, now)


def test_custom_alert_ingress_deduplicates_notifications_without_opening_or_closing_positions(tmp_path):
    cfg = Config(local=True, role="web", db="sqlite:///" + str(tmp_path / "pine.db"),
        password="private-owner-password", futures_observer_token=TOKEN)
    app = create_app(cfg)
    path = "/hooks/projects/futures/" + TOKEN + "/mnq"
    with TestClient(app) as client:
        entry = pine_alert(time.time() - 1)
        assert client.post(path, json=entry).json()["broker_order_sent"] is False
        assert client.post(path, json=entry).json()["status"] == "duplicate"
        cutoff = pine_alert(time.time() - 1, action="exit", reason="session_cutoff")
        assert client.post(path, json=cutoff).status_code == 202
        assert client.post(path, json=cutoff).json()["status"] == "duplicate"
        with app.state.db.tx() as c:
            assert c.execute(select(func.count()).select_from(records)).scalar_one() == 2
            alerts = app.state.db.recent(c, "alert")
            assert len(alerts) == 2
            assert {a["payload"]["source_event"] for a in alerts} == {"entry_alert", "flatten_alert"}
            assert all("fill_price" not in a["payload"] for a in alerts)
            assert not app.state.db.prefix(c, "position:") and not app.state.db.prefix(c, "risk:")
            state = app.state.db.get(c, "project_stream:mnq")
            assert state["last_source_ts"] > time.time() - 10
