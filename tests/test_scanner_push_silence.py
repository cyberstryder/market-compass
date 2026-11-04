"""Scanner setup lifecycle push silence (Josh, 2026-11-03).

Simulated compass-scanner setups keep being detected, recorded and
shadow-scored, but their lifecycle alerts (setup_triggered /
setup_invalidated / setup_result) enqueue as SUPPRESSED Discord jobs
while their family is not bookable under scanner_shadow's promotion /
seat state. The gate is eligibility, not a family list: promoting and
seating a family returns its pushes with no code change, and the barred
check wins over promotion+seating.
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from compass.alerts import (DeliveryWorker, enqueue_routes,
                            scanner_family_bookable, scanner_setup_silence)
from compass.config import Config
from compass.store import Store, discord_jobs, events

NOW = time.time()

BARRED_PAYLOAD = {
    'strategy': 'compass-scanner-v2:level_breakdown', 'rule': 'level_breakdown',
    'asset': 'future', 'side': 'short', 'symbol': 'MGC.v.0',
}
SHADOW_PAYLOAD = {
    'strategy': 'compass-scanner-v2:exposure_level_break',
    'rule': 'exposure_level_break', 'asset': 'future', 'side': 'long',
    'symbol': 'GC.v.0',
}


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'silence.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def add_alert(db, source, symbol, payload):
    with db.tx() as c:
        db.append(c, 'alert', source, symbol, NOW, payload)
        return c.execute(select(events).order_by(events.c.id.desc())
                         .limit(1)).mappings().one()['id']


def jobs(db):
    with db.tx() as c:
        return list(c.execute(select(discord_jobs)
                              .order_by(discord_jobs.c.event_id)).mappings())


def test_lifecycle_events_suppressed_at_enqueue_but_still_recorded(db):
    ids = [
        add_alert(db, 'scanner', 'MGC.v.0',
                  {**BARRED_PAYLOAD, 'status': 'setup_triggered'}),
        add_alert(db, 'scanner', 'MGC.v.0',
                  {**BARRED_PAYLOAD, 'status': 'setup_invalidated',
                   'reason': 'Underlying invalidation reached', 'setup_id': 'x'}),
        add_alert(db, 'setup_study', 'MGC.v.0',
                  {**BARRED_PAYLOAD, 'status': 'setup_result',
                   'mode': 'SIMULATED', 'study_status': 'closed'}),
    ]
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    rows = jobs(db)
    assert [r['event_id'] for r in rows] == ids
    for r in rows:
        assert r['status'] == 'suppressed'
        assert r['confirmation']['gate'] == 'scanner-push-silence-v1'
        assert r['confirmation']['family'] == 'level_breakdown'
        assert r['confirmation']['action'] == 'suppressed'
    # Recording is untouched: the alert events themselves still exist.
    with db.tx() as c:
        recorded = list(c.execute(select(events).where(
            events.c.kind == 'alert').order_by(events.c.id)).mappings())
    assert [r['id'] for r in recorded] == ids
    assert recorded[1]['payload']['status'] == 'setup_invalidated'


def test_unbarred_shadow_family_is_also_silent(db):
    # exposure_level_break is NOT barred; it is silent purely because it
    # holds no promotion/seat — the gate is eligibility, not the barred list.
    add_alert(db, 'scanner', 'GC.v.0', {**SHADOW_PAYLOAD, 'status': 'setup_triggered'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (row,) = jobs(db)
    assert row['status'] == 'suppressed'
    assert row['confirmation']['family'] == 'exposure_level_break'


def test_promoted_and_seated_family_pushes_return(db):
    cfg = SimpleNamespace(scanner_promoted_families='exposure_level_break',
                          odte_book_seated_names='exposure_level_break:SPY',
                          scanner_paper_seats='')
    add_alert(db, 'scanner', 'GC.v.0', {**SHADOW_PAYLOAD, 'status': 'setup_invalidated'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW, cfg)
    (row,) = jobs(db)
    assert row['status'] == 'pending'


def test_promoted_without_seats_stays_silent(db):
    cfg = SimpleNamespace(scanner_promoted_families='exposure_level_break',
                          odte_book_seated_names='', scanner_paper_seats='')
    add_alert(db, 'scanner', 'GC.v.0', {**SHADOW_PAYLOAD, 'status': 'setup_triggered'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW, cfg)
    (row,) = jobs(db)
    assert row['status'] == 'suppressed'


def test_barred_wins_over_promotion_and_seating(db):
    cfg = SimpleNamespace(scanner_promoted_families='level_breakdown',
                          odte_book_seated_names='level_breakdown:SPY',
                          scanner_paper_seats='')
    assert scanner_family_bookable('level_breakdown', cfg) is False
    add_alert(db, 'scanner', 'MGC.v.0', {**BARRED_PAYLOAD, 'status': 'setup_triggered'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW, cfg)
    (row,) = jobs(db)
    assert row['status'] == 'suppressed'


def test_bookable_rule_reads_promotion_state():
    assert scanner_family_bookable('level_breakdown') is False
    assert scanner_family_bookable('exposure_level_break') is False
    seated = SimpleNamespace(scanner_promoted_families='flow_price_breakout',
                             odte_book_seated_names='flow_price_breakout:AAPL',
                             scanner_paper_seats='')
    assert scanner_family_bookable('flow_price_breakout', seated) is True


def test_kept_surfaces_are_not_gated(db):
    # ICT futures paper alert (engine source, no scanner family): untouched.
    ict = add_alert(db, 'engine', 'MGCZ6', {
        'status': 'entered', 'asset': 'future', 'strategy': 'ict-survivor-s4',
        'side': 'short'})
    # The 0DTE book's option_setup_new status is never in the gated class,
    # even on a payload carrying a scanner family.
    row = {'payload': {**BARRED_PAYLOAD, 'status': 'option_setup_new'}}
    assert scanner_setup_silence(row, None, NOW) is None
    # Non-scanner lifecycle lookalikes (no compass-scanner family): untouched.
    row = {'payload': {'status': 'setup_triggered', 'strategy': 'earnings-play'}}
    assert scanner_setup_silence(row, None, NOW) is None
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = [j for j in jobs(db) if j['event_id'] == ict]
    assert job['status'] == 'pending'


def test_send_time_belt_suppresses_queued_backlog(db):
    event_id = add_alert(db, 'scanner', 'MGC.v.0',
                         {**BARRED_PAYLOAD, 'status': 'setup_invalidated'})
    with db.tx() as c:
        c.execute(discord_jobs.insert().values(
            event_id=event_id, route='futures', status='pending', queued_at=NOW,
            confirmation={}))
        row = dict(c.execute(select(events).where(
            events.c.id == event_id)).mappings().one())
    worker = DeliveryWorker(db, Config(local=True))
    posted = []

    def handler(request):
        posted.append(str(request.url))
        return httpx.Response(200, json={'id': '999'})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.send(client, 'https://discord.com/api/webhooks/1/x',
                              row, 'futures', NOW)
    asyncio.run(run())
    assert posted == []
    (job,) = jobs(db)
    assert job['status'] == 'suppressed'
    assert job['confirmation']['gate'] == 'scanner-push-silence-v1'
