"""Exit-silence published-entry exemption (Josh, approved 2026-11-17).

Monday 2026-11-16: three published 0DTE trades (QQQ 667C, AMD 510P,
HIMS 18P) exited correctly and wrote setup_result events, but every
Discord exit post was suppressed by scanner-push-silence-v1 because
no scanner family is bookable (PROMOTED_FAMILIES / PAPER_SEATS are
empty). The fix exempts lifecycle events LINKED TO A PUBLISHED ENTRY
— the publication:source link alert_ownership.assess() writes when
it admits the entry — so assess() decides their fate instead.
Unpublished lifecycle events stay silenced exactly as before.
"""
import asyncio
import time
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from compass.alert_ownership import CT
from compass.alerts import (DeliveryWorker, enqueue_routes,
                            scanner_setup_silence, SCANNER_PUSH_GATE)
from compass.config import Config
from compass.store import Store, discord_jobs, events
from compass.setup_study import trials as setup_trials

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=CT).timestamp()
CONTRACT = 'O:QQQ261116C00667000'
TRIAL = 'trial-published-1'
SIGNAL = 'signal-published-1'
FAMILY = {'strategy': '0dte-compass-scanner-v2:level_breakdown',
          'rule': 'level_breakdown'}


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'exit_silence.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def entry_payload(**over):
    p = {**FAMILY, 'id': TRIAL, 'source_id': SIGNAL,
         'status': 'option_setup_new', 'entry': 2.93, 'stop': 1.5,
         'target': 4.5, 'side': 'long', 'asset': 'option',
         'last_quote': {'ts': NOW, 'bid': 2.90, 'ask': 2.93}}
    p.update(over)
    return p


def exit_payload(**over):
    p = {**FAMILY, 'id': TRIAL, 'source_id': SIGNAL,
         'status': 'setup_result', 'mode': 'SIMULATED',
         'study_status': 'closed', 'outcome': 'loss', 'pnl': -93.30,
         'entry': 2.93, 'exit': 2.01, 'exit_reason': 'stop',
         'side': 'long', 'asset': 'option'}
    p.update(over)
    return p


def add_alert(db, source, symbol, payload, ts=NOW):
    with db.tx() as c:
        db.append(c, 'alert', source, symbol, ts, payload)
        return c.execute(select(events).order_by(events.c.id.desc())
                         .limit(1)).mappings().one()['id']


def jobs_by_id(db):
    with db.tx() as c:
        return {r['event_id']: r for r in
                c.execute(select(discord_jobs)).mappings()}


def publish_entry(db):
    """Admit the entry through the real enqueue/assess path."""
    entry_id = add_alert(db, 'setup_study', CONTRACT, entry_payload())
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    job = jobs_by_id(db)[entry_id]
    assert job['status'] == 'pending'
    assert job['confirmation']['action'] == 'publish'
    return entry_id


def test_published_exit_is_not_silenced_and_reaches_assess(db):
    publish_entry(db)
    exit_id = add_alert(db, 'setup_study', CONTRACT, exit_payload(), ts=NOW + 3600)
    # Unit level: with the publication link on record, no silence.
    with db.tx() as c:
        row = dict(c.execute(select(events).where(
            events.c.id == exit_id)).mappings().one())
        assert scanner_setup_silence(row, None, NOW, db, c) is None
    # Enqueue level: the job is pending under assess()'s own publish
    # decision (EXIT), not suppressed under the silence gate.
    with db.tx() as c:
        enqueue_routes(db, c, NOW + 3600)
    job = jobs_by_id(db)[exit_id]
    assert job['status'] == 'pending'
    assert job['confirmation']['action'] == 'publish'
    assert job['confirmation']['event'] == 'EXIT'
    assert job['confirmation'].get('gate') != SCANNER_PUSH_GATE


def test_published_exit_actually_sends_at_delivery_time(db):
    entry_id = publish_entry(db)
    exit_id = add_alert(db, 'setup_study', CONTRACT, exit_payload(), ts=NOW + 3600)
    with db.tx() as c:
        enqueue_routes(db, c, NOW + 3600)
        # The entry really delivered (as Monday's entries did).
        c.execute(discord_jobs.update().where(
            discord_jobs.c.event_id == entry_id).values(status='sent'))
        row = dict(c.execute(select(events).where(
            events.c.id == exit_id)).mappings().one())
    worker = DeliveryWorker(db, Config(local=True))
    posted = []

    def handler(request):
        posted.append(request.content.decode())
        return httpx.Response(200, json={'id': '424242'})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.send(client, 'https://discord.com/api/webhooks/1/x',
                              row, 'options_0dte', NOW + 3600)
    asyncio.run(run())
    assert len(posted) == 1 and 'EXIT' in posted[0]
    assert jobs_by_id(db)[exit_id]['status'] == 'sent'


def test_unpublished_exit_remains_silenced(db):
    exit_id = add_alert(db, 'setup_study', CONTRACT, exit_payload())
    with db.tx() as c:
        row = dict(c.execute(select(events).where(
            events.c.id == exit_id)).mappings().one())
        decision = scanner_setup_silence(row, None, NOW, db, c)
        assert decision is not None
        assert decision['gate'] == SCANNER_PUSH_GATE
        enqueue_routes(db, c, NOW)
    job = jobs_by_id(db)[exit_id]
    assert job['status'] == 'suppressed'
    assert job['confirmation']['gate'] == SCANNER_PUSH_GATE


def test_supporting_link_does_not_exempt(db):
    # A supporting study's lifecycle is not the published entry's:
    # assess() suppresses it separately, so the silence must not
    # treat its link as an exemption either.
    from compass.alert_ownership import source_key
    from compass.store import identity
    with db.tx() as c:
        key = source_key({'source': 'setup_study',
                          'payload': exit_payload(), 'id': 1})
        db.put(c, 'publication:source:' + key,
               {'trade_id': 'someone-elses-trade', 'supporting': True})
        row = {'id': 1, 'source': 'setup_study', 'symbol': CONTRACT,
               'ts': NOW, 'payload': exit_payload()}
        assert scanner_setup_silence(row, None, NOW, db, c) is not None


def test_option_setup_new_behavior_unchanged(db):
    # Entries were never in the silenced class and still are not,
    # with or without a db handle / publication link.
    row = {'id': 1, 'source': 'setup_study', 'symbol': CONTRACT,
           'ts': NOW, 'payload': entry_payload()}
    assert scanner_setup_silence(row, None, NOW) is None
    with db.tx() as c:
        assert scanner_setup_silence(row, None, NOW, db, c) is None
    entry_id = add_alert(db, 'setup_study', CONTRACT, entry_payload())
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    job = jobs_by_id(db)[entry_id]
    assert job['status'] == 'pending'
    assert job['confirmation']['action'] == 'publish'
    assert job['confirmation']['event'] == 'ENTRY'


def test_triggered_and_invalidated_linked_to_published_trial(db):
    publish_entry(db)
    # setup_triggered names its trial via setup_trial_id.
    triggered = {**FAMILY, 'id': SIGNAL, 'setup_trial_id': TRIAL,
                 'status': 'setup_triggered', 'side': 'long'}
    # setup_invalidated (alert_context shape) names only the signal
    # via setup_id; the trial is resolved through the trials table.
    with db.tx() as c:
        c.execute(db.insert(setup_trials).values(
            id=TRIAL, source_id=SIGNAL, symbol=CONTRACT,
            strategy=FAMILY['strategy'], side='long',
            version='setup-outcomes-v4', status='closed',
            started=NOW, finished=NOW + 60, payload={'id': TRIAL}))
        trow = {'id': 90, 'source': 'scanner', 'symbol': 'QQQ',
                'ts': NOW, 'payload': triggered}
        assert scanner_setup_silence(trow, None, NOW, db, c) is None
        invalidated = {**FAMILY, 'status': 'setup_invalidated',
                       'setup_id': SIGNAL, 'side': 'long'}
        irow = {'id': 91, 'source': 'scanner', 'symbol': 'QQQ',
                'ts': NOW, 'payload': invalidated}
        assert scanner_setup_silence(irow, None, NOW, db, c) is None
    # The same events with NO published entry stay silenced.
    other = {**triggered, 'setup_trial_id': 'trial-never-published'}
    orow = {'id': 92, 'source': 'scanner', 'symbol': 'QQQ',
            'ts': NOW, 'payload': other}
    with db.tx() as c:
        assert scanner_setup_silence(orow, None, NOW, db, c) is not None
