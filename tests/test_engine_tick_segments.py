"""Regression tests for the 2026-10-23 engine tick fixes.

1. The MU / single-stock scalp segment runs on a LIVE connection inside
   tick(). Before the fix it executed after the ICT transaction had closed
   (ResourceClosedError swallowed by `except Exception: pass`), so no stock
   scalp trade was ever recorded in production.
2. TraderMatrix intel refresh happens outside any tick transaction and is
   retried hourly, not every tick, when the vendor fails.
3. A lost engine lease aborts the remainder of the tick (no second engine
   can run exits()/submits concurrently during a deployment overlap).
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from compass.store import Store
from compass.config import Config
from compass.engine import Engine

ET = ZoneInfo("America/New_York")
# A weekday at 10:00 ET: inside the spike-fade window (09:45-11:00 ET).
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=ET).timestamp()


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "t.db"))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg(tmp_path):
    return Config(local=True, role="all", db="sqlite:///" + str(tmp_path / "t.db"),
                  stocks=("SPY",), futures=(), alpaca_key="", alpaca_secret="", massive="",
                  databento="", matrix="", discord="", mu_scalp=True, setup_study=False,
                  scanner=False, option_ideas=False, swing_ideas=False, day_trading_board=False,
                  apex_magnet=False, tape_confirmed=False, flow_pulse=False, zero_dte_paper=False,
                  darkpool=False, iv_rank=False, squeeze=False, gap_continuation=False, breakouts=False)


def test_mu_scalp_segment_runs_on_live_connection(db, cfg, monkeypatch):
    """The scalp detectors are invoked with an OPEN connection and their
    result is visible after tick(); a dead-connection regression would
    leave health:mu_scalp absent (the old silent failure)."""
    from compass import mu_scalp, mu_paper
    seen = []

    def fake_spike(db_, c, now, symbol="MU"):
        # Prove the connection is live: a real round-trip must work.
        db_.put(c, "probe:" + symbol, {"at": now})
        seen.append(symbol)
        return None

    monkeypatch.setattr(mu_scalp, "check_spike_fade", fake_spike)
    monkeypatch.setattr(mu_paper, "manage_trades", lambda db_, c, now: db_.put(c, "probe:managed", True))
    Engine(db, cfg).tick(NOW)
    with db.tx() as c:
        assert seen == list(Engine.SCALP_SYMBOLS)
        assert db.get(c, "probe:MU") == {"at": NOW}
        assert db.get(c, "probe:managed") is True
        health = db.get(c, "health:mu_scalp")
        assert health and health["status"] == "running" and health["name"] == "mu_scalp"


def test_mu_scalp_signal_opens_paper_trade_once(db, cfg, monkeypatch):
    from compass import mu_scalp, mu_paper
    opened = []
    sig = {"dedupe_key": "spike:MU:1", "symbol": "MU", "side": "short", "entry": 100.0,
           "target": 99.0, "stop": 100.3, "kind": "spike_fade"}

    monkeypatch.setattr(mu_scalp, "check_spike_fade",
                        lambda db_, c, now, symbol="MU": sig if symbol == "MU" else None)
    monkeypatch.setattr(mu_scalp, "publish_alert",
                        lambda db_, c, s, now: db_.put(c, "alert:" + s["dedupe_key"], True))
    monkeypatch.setattr(mu_paper, "open_trade", lambda db_, c, s, now: opened.append(s["symbol"]))
    monkeypatch.setattr(mu_paper, "manage_trades", lambda db_, c, now: None)
    e = Engine(db, cfg)
    e.tick(NOW)
    e.tick(NOW + 60)
    assert opened == ["MU"]  # second tick deduped by alert key
    with db.tx() as c:
        assert db.get(c, "health:mu_scalp")["ran"] == {"gap": 0, "spike": 0}


def test_mu_scalp_failure_is_isolated_and_reported(db, cfg, monkeypatch):
    from compass import mu_scalp, mu_paper

    def boom(*a, **k):
        raise RuntimeError("scalp crash fixture")

    monkeypatch.setattr(mu_scalp, "check_spike_fade", boom)
    monkeypatch.setattr(mu_paper, "manage_trades", lambda db_, c, now: None)
    Engine(db, cfg).tick(NOW)  # must not raise
    with db.tx() as c:
        h = db.get(c, "health:mu_scalp")
        assert h["status"] == "error" and "scalp crash fixture" in h["detail"]
        assert db.get(c, "worker:engine")  # the rest of the tick completed


def test_intel_refresh_runs_outside_tick_transaction_and_backs_off(db, cfg, monkeypatch):
    from compass import tradermatrix_intel
    calls = []

    def failing_refresh(db_, c):
        # Must be called with c=None: the engine never hands it a tick connection.
        assert c is None
        calls.append(1)
        raise RuntimeError("vendor down")

    monkeypatch.setattr(tradermatrix_intel, "refresh_intel", failing_refresh)
    e = Engine(db, cfg)
    e.tick(NOW)
    e.tick(NOW + 2)
    e.tick(NOW + 60)
    assert len(calls) == 1  # retried hourly, not every tick
    e.tick(NOW + 3601)
    assert len(calls) == 2
    with db.tx() as c:
        assert db.get(c, "worker:engine")  # exits/ledgers segment still ran


def test_lost_lease_aborts_remaining_tick(db, cfg, monkeypatch):
    from compass import mu_scalp, mu_paper
    ran = []
    monkeypatch.setattr(mu_scalp, "check_spike_fade", lambda *a, **k: ran.append(1) or None)
    monkeypatch.setattr(mu_paper, "manage_trades", lambda *a, **k: None)
    e = Engine(db, cfg)
    original = db.lease
    state = {"n": 0}

    def lease(c, name, owner, seconds=30):
        if name == "engine":
            state["n"] += 1
            if state["n"] > 1:  # first acquisition succeeds, every renewal fails
                return False
        return original(c, name, owner, seconds)

    monkeypatch.setattr(db, "lease", lease)
    e.tick(NOW)  # must not raise
    assert ran == []  # later segments never executed under a lost lease
    with db.tx() as c:
        assert db.get(c, "health:mu_scalp") is None


def test_discord_permanent_rejection_is_not_retried(tmp_path):
    """A 4xx from Discord (bad body / dead webhook) marks the job failed
    instead of re-POSTing the same body every 15s forever."""
    import asyncio, httpx
    from sqlalchemy import select
    from compass.alerts import DeliveryWorker
    from compass.alert_ownership import assess
    from compass.store import discord_jobs, events
    from test_alert_ownership import row, NOW as T0
    store = Store("sqlite:///" + str(tmp_path / "d.db")); store.initialize()
    with store.tx() as c:
        r = row(); store.append(c, 'alert', r['source'], r['symbol'], T0, r['payload'])
        r = dict(c.execute(select(events).where(events.c.kind == 'alert')).mappings().one())
        a = assess(store, c, r, T0)
        c.execute(discord_jobs.insert().values(event_id=r['id'], route=a['category'], status='pending',
                                               queued_at=T0, confirmation=a))
    worker = DeliveryWorker(store, Config(local=True))
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(400, json={"message": "Invalid Form Body"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.send(client, "https://discord.com/api/webhooks/1/t", r, a['category'], T0)
    asyncio.run(run())
    assert len(calls) == 1
    with store.tx() as c:
        job = c.execute(select(discord_jobs)).mappings().one()
        assert job['status'] == 'failed'
        assert 'rejected' in job['confirmation']['reason']
    assert worker.retry_at[a['category']] >= T0 + 60
