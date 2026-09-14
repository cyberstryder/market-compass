import copy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from compass.app import create_app
from compass.alert_format import message_for
from compass.config import Config
from compass.projects import ingest, records as originals
from compass.secondary import (Secondary, VERSION, advance_measurement, assess, build_report,
    capture, comparisons, reviews, resolve_symbol, snapshot, source_candidate, start_measurement)
from compass.store import Store, events, identity

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "secondary.db"))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(now=NOW, price=100):
    return {"ts": now, "bid": price - .01, "ask": price + .01,
        "bid_size": 10, "ask_size": 10, "source": "test"}


def context(now=NOW):
    return {"captured_at": now, "market_symbol": "SPY", "price_basis": "same_underlying",
        "quote": quote(now), "technical": {"status": "ready", "asof": now - 1, "atr14": 2,
            "vwap": 99, "ema9": 100, "ema21": 99, "htf15_bias": 1},
        "exposure": {"status": "missing_or_stale"}, "flow": [], "peers": [], "levels": []}


def candidate(now=NOW, **values):
    return {"project": "morning", "source_key": "source-key", "source_id": "source-1", "symbol": "SPY",
        "side": "long", "source_ts": now, "strategy": "Original ORB", "source_version": "v1", "entry": 100,
        "stop": 98, "target": 104, **values}


def source(now=NOW, **values):
    return {"id": "source-1", "symbol": "SPY", "source_ts": now, "status": "tracking",
        "strategy": "Original ORB", "version": "v1", "side": "long", "entry": 100,
        "target": 104, "original": {}, **values}


def seed(db, now=NOW, symbol="SPY", bias=1):
    with db.tx() as c:
        db.put(c, "quote:" + symbol, quote(now))
        db.put(c, "scanner_features:" + symbol, {**context(now)["technical"], "htf15_bias": bias})


def test_optional_options_context_does_not_block_price_review():
    result = assess(candidate(), context(), NOW)
    assert result["verdict"] == "supported" and result["accepted"]
    assert len(result["optional_missing"]) == 2
    assert "option profitability" in result["scope"]


@pytest.mark.parametrize("change,expected", [
    ({"htf15_bias": -1}, "watch"), ({"htf15_bias": None}, "watch"),
    ({"status": "warming_up"}, "insufficient_data"), ({"asof": NOW + 60}, "insufficient_data"),
])
def test_confirmation_and_missing_data_are_distinct(change, expected):
    inputs = context()
    inputs["technical"].update(change)
    assert assess(candidate(), inputs, NOW)["verdict"] == expected


@pytest.mark.parametrize("stamp", [NOW - 6, NOW + 1])
def test_stale_or_future_quote_cannot_support_a_review(stamp):
    inputs = context()
    inputs["quote"]["ts"] = stamp
    decision = assess(candidate(), inputs, NOW)
    assert decision["verdict"] == "insufficient_data"
    assert start_measurement(candidate(), inputs, decision, NOW)["state"] == "unmeasurable"


def test_already_invalidated_or_extended_entry_is_rejected():
    assert assess(candidate(stop=101), context(), NOW)["verdict"] == "rejected"
    assert assess(candidate(target=99), context(), NOW)["verdict"] == "rejected"
    assert assess(candidate(entry=97), context(), NOW)["verdict"] == "rejected"


def test_gex_sign_is_not_a_direction_vote_but_conflicting_flow_is_visible():
    inputs = context()
    inputs["exposure"] = {"status": "current", "gex": [{"strike": 100, "gex": -999999}], "vex": [{"strike": 100, "vex": -999999}]}
    assert assess(candidate(), inputs, NOW)["verdict"] == "supported"
    inputs["flow"] = [{"sentiment": None}, {"sentiment": "bearish"}]
    assert assess(candidate(), inputs, NOW)["verdict"] == "watch"
    inputs["flow"] = []
    inputs["levels"] = [{"kind": "gex_concentration", "price": 100.2}]
    assert assess(candidate(), inputs, NOW)["verdict"] == "watch"


def test_smoothers_uses_completed_daily_bias_instead_of_scalp_bias():
    inputs = context()
    inputs["technical"]["htf15_bias"] = -1
    inputs["daily"] = {"status": "ready", "bias": 1}
    weekly = candidate(project="smoothers", target_atr_mult=.3)
    assert assess(weekly, inputs, NOW)["verdict"] == "supported"
    inputs["daily"]["bias"] = -1
    assert assess(weekly, inputs, NOW)["verdict"] == "watch"


def test_continuous_levels_are_not_compared_to_dated_contracts():
    inputs = context()
    inputs.update(price_basis="linked_contract_context", market_symbol="MNQZ6@1")
    decision = assess(candidate(project="futures", symbol="MNQ1!", entry=24000, stop=23990, target=24015), inputs, NOW)
    assert decision["verdict"] == "supported"
    assert any("Continuous-chart" in text for text in decision["cautions"])


def test_contract_resolution_keeps_mes_and_mnq_and_rolls_distinct(db):
    cfg = Config(local=True)
    with db.tx() as c:
        db.put(c, "quote:MESZ6@1", quote())
        db.put(c, "quote:MNQZ6@2", quote(price=25000))
        assert resolve_symbol(db, c, candidate(symbol="MES1!"), cfg, NOW) == ("MESZ6@1", "linked_contract_context")
        assert resolve_symbol(db, c, candidate(symbol="MESZ2026"), cfg, NOW) == ("MESZ6@1", "same_contract")
        assert resolve_symbol(db, c, candidate(symbol="MESU2026"), cfg, NOW)[0] is None
        assert resolve_symbol(db, c, candidate(symbol="MESZ2016"), cfg, NOW)[0] is None
        inputs = capture(db, c, candidate(project="futures", symbol="MGC1!"), cfg, NOW)
        assert inputs["quote"] is None and inputs["exposure"]["symbol"] is None


def test_weekly_model_clock_and_candidate_creation_clock_are_kept_separate():
    payload = source(NOW - 300, source_time_basis="model_entry_time", status="open",
        original={"created_at": datetime.fromtimestamp(NOW, timezone.utc).isoformat(), "target_atr_mult": .3})
    a = source_candidate("smoothers", payload, "weekly")
    assert a["source_ts"] == NOW - 300 and a["available_at"] == NOW
    inputs = context()
    inputs["daily"] = {"status": "ready", "bias": 1}
    result = assess(a, inputs, NOW)
    assert result["timely"] and result["source_reference_age_seconds"] == 300
    assert result["latency_seconds"] == 0
    assert not assess(a, context(NOW + 61), NOW + 61)["timely"]


def test_night_futures_ignore_closed_cash_flow_as_confirmation(db):
    night = datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc).timestamp()
    seed(db, night, "MNQZ6@1")
    with db.tx() as c:
        db.put(c, "matrix:QQQ", {"source_ts": night, "strikes": [{"strike": 100, "gex": 99, "vex": 22}]})
        db.put(c, "matrix:unusual_activity", {"rows": [{"symbol": "QQQ", "source_ts": night,
            "score": 95, "premium": 1000000, "sentiment": "bearish"}]})
        inputs = capture(db, c, candidate(night, project="futures", symbol="MNQ1!"), Config(local=True), night)
        assert inputs["exposure"]["status"] == "cash_market_closed" and not inputs["flow"]
        assert assess(candidate(night, project="futures", symbol="MNQ1!"), inputs, night)["verdict"] == "supported"


def test_original_unchanged_review_frozen_and_duplicate_delivery_not_rescored(db):
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db)
    original = source()
    with db.tx() as c:
        ingest(db, c, "morning", original, NOW)
    worker.tick(NOW)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        frozen = copy.deepcopy(row["inputs"])
        assert row["verdict"] == "supported"
        assert c.execute(select(originals.c.payload)).scalar_one() == original
        db.put(c, "quote:SPY", quote(NOW + 61, price=1))
        ingest(db, c, "morning", source(status="observed_60m", outcome={"latest": {"price": 1}}), NOW + 61)
    worker.tick(NOW + 61)
    worker.tick(NOW + 62)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row["verdict"] == "supported" and row["inputs"] == frozen
        assert row["source_outcome"]["status"] == "observed_60m"
        assert len(db.recent(c, "alert")) == 1
        assert not db.prefix(c, "position:") and not db.prefix(c, "risk:")
        assert not db.recent(c, "paper_decision")


def test_secondary_worker_recovers_archived_checkpoint_after_restart_delay(db):
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW-1)
    seed(db)
    with db.tx() as c:
        ingest(db,c,'morning',source(),NOW)
    worker.tick(NOW)
    with db.tx() as c:
        before = dict(c.execute(select(reviews)).mappings().one())
        alert_count = len(db.recent(c,'alert'))
        c.execute(db.insert(events).values(key='checkpoint-fixture',kind='quote',source='alpaca',
            symbol='SPY',ts=NOW+900,received=NOW+900.1,payload=quote(NOW+900,102)))
        db.put(c,'quote:SPY',quote(NOW+1000,90))
        # The prior process lease must expire before a replacement can run.
        from compass.store import leases
        c.execute(leases.update().values(until=0))
    Secondary(db,Config(local=True)).tick(NOW+1000)
    with db.tx() as c:
        after = dict(c.execute(select(reviews)).mappings().one())
        assert after['measurements']['horizons']['15']['price'] == 102
        assert after['measurements']['horizons']['15']['observation_source'] == 'retained_quote'
        assert after['decision'] == before['decision'] and after['inputs'] == before['inputs']
        assert len(db.recent(c,'alert')) == alert_count


def test_activation_skips_existing_history_and_late_import_never_alerts(db):
    with db.tx() as c:
        ingest(db, c, "morning", source(NOW - 100), NOW - 100)
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db)
    with db.tx() as c:
        ingest(db, c, "morning", source(NOW - 200, id="late"), NOW)
    worker.tick(NOW)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row["source_id"] == "late" and row["verdict"] == "insufficient_data"
        assert not row["decision"]["timely"] and not db.recent(c, "alert")


def test_futures_exit_and_secondary_notifications_do_not_create_new_candidates(db):
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db, symbol="MESZ6@1")
    with db.tx() as c:
        ingest(db, c, "futures", source(symbol="MNQ1!", status="exit_alert", side="exit"), NOW)
        db.append(c, "alert", "scanner", "MESZ6@1", NOW,
            {"id": "native-mes", "status": "setup_triggered", "strategy": "compass-scanner-v2:trend_pullback",
             "side": "long", "entry": 100, "stop": 98, "target": 104}, "native-mes-alert")
    worker.tick(NOW)
    worker.tick(NOW + 1)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row["project"] == "compass_futures" and row["symbol"] == "MESZ6@1"
        assert len(db.recent(c, "alert")) == 2


def test_one_bad_candidate_cannot_block_the_next(db):
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db)
    with db.tx() as c:
        db.append(c, "project_update", "morning", "SPY", NOW, {"bad": "missing identity"}, "malformed")
        ingest(db, c, "morning", source(), NOW)
    worker.tick(NOW)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one() == 1
        assert len(db.recent(c, "secondary_error")) == 1


def test_late_committing_lower_event_id_is_not_lost_behind_a_cursor(db):
    with db.tx() as c:
        c.execute(db.insert(events).values(id=100, key="existing-higher-id", kind="bar", source="test",
            symbol="SPY", ts=NOW - 100, received=NOW - 100, payload={}))
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db)
    with db.tx() as c:
        ingest(db, c, "morning", source(), NOW)
        event = db.recent(c, "project_update")[0]
        # Model a producer that reserved a lower ID, then committed later.
        c.execute(events.update().where(events.c.id == event["id"]).values(id=50))
    worker.tick(NOW)
    worker.tick(NOW + 1)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one() == 1
        assert len(db.recent(c, "alert")) == 1


def test_all_decisions_receive_identical_measurement_rules_and_signed_returns():
    inputs = context()
    a = candidate()
    selected = start_measurement(a, inputs, assess(a, inputs, NOW), NOW)
    rejected = start_measurement(a, inputs, assess(candidate(stop=101), inputs, NOW), NOW)
    assert selected == rejected
    next_quote = quote(NOW + 900, price=102)
    long = advance_measurement(selected, "long", next_quote, NOW + 900)
    short = advance_measurement(rejected, "short", next_quote, NOW + 900)
    assert long["horizons"]["15"]["return_pct"] == pytest.approx(2)
    assert short["horizons"]["15"]["return_pct"] == pytest.approx(-2)


def test_missed_checkpoint_is_not_backfilled_and_next_session_cannot_add_excursions():
    a, inputs = candidate(), context()
    m = start_measurement(a, inputs, assess(a, inputs, NOW), NOW)
    m = advance_measurement(m, "long", quote(NOW + 1000, price=102), NOW + 1000)
    assert m["horizons"]["15"]["status"] == "missing"
    old_max = m["max_price"]
    m = advance_measurement(m, "long", quote(NOW + 86400, price=999), NOW + 86400)
    assert m["state"] == "complete" and m["max_price"] == old_max
    assert m["horizons"]["60"]["status"] == "missing"


def test_measurements_never_cross_session_boundary():
    late = datetime(2026, 9, 14, 19, 55, tzinfo=timezone.utc).timestamp()
    a, inputs = candidate(late), context(late)
    m = start_measurement(a, inputs, assess(a, inputs, late), late)
    assert m["state"] == "complete"
    assert all(p["status"] == "session_boundary" for p in m["horizons"].values())


def test_comparison_counts_missed_winners_and_excludes_missing_data():
    rows = []
    for verdict, value in (("supported", 2), ("rejected", 3), ("watch", -1), ("insufficient_data", 100)):
        rows.append({"project": "morning", "candidate": candidate(), "version": VERSION, "verdict": verdict,
            "decision": {"timely": True}, "source_outcome": {},
            "measurements": {"horizons": {"60": {"status": "observed", "return_pct": value}}}})
    result = comparisons(rows)[0]
    h = result["horizons"][2]
    assert h["measured"] == 3 and h["selected"] == 1
    assert h["original_mean_pct"] == pytest.approx(4 / 3)
    assert h["selected_mean_pct"] == 2 and h["filter_per_candidate_pct"] == pytest.approx(2 / 3)
    assert h["missed_positive"] == 1 and h["avoided_negative"] == 1
    assert result["counts"]["insufficient_data"] == 1


def test_secondary_alert_is_distinct_and_late_delivery_loses_current_entry_wording(db):
    worker = Secondary(db, Config(local=True))
    worker.tick(NOW - 1)
    seed(db)
    with db.tx() as c:
        ingest(db, c, "morning", source(), NOW)
    worker.tick(NOW)
    with db.tx() as c:
        event = db.recent(c, "alert")[0]
    message = message_for(event, NOW)
    assert "MORNING ALGO | SECONDARY SUPPORTED | SPY" in message
    assert "Original strategy continues independently" in message
    assert "Review reference" in message and "Simulated entry" not in message
    assert "Original ref: source-1" in message and len(message) <= 1900
    assert "SECONDARY HISTORICAL" in message_for(event, NOW + 180)


def test_review_api_requires_owner_auth_and_dashboard_reports_no_comparison_yet(tmp_path):
    cfg = Config(local=True, role="web", db="sqlite:///" + str(tmp_path / "web.db"), password="owner-password-long")
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/api/secondary").status_code == 401
        assert client.get('/api/secondary/report?since=0').status_code == 401
        assert client.get("/api/secondary/record?id=" + "a" * 64).status_code == 401
        assert client.post("/login", json={"password": cfg.password}).status_code == 200
        import time
        report=client.get('/api/secondary/report',params={'since':time.time()-60})
        assert report.status_code==200 and report.json()['window']['reviewed']==0
        assert client.get('/api/secondary/report?since=nan').status_code==422
        assert client.get('/api/secondary/report?since=0').status_code==422
        data = client.get("/api/secondary").json()
        assert data["comparisons"] == [] and not data["originals_changed"]
        assert client.get("/api/secondary/record?id=bad").status_code == 422
        assert client.get("/api/secondary/record?id=" + "a" * 64).status_code == 404
        assert "Secondary review" in client.get("/").text
