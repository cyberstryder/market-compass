"""ICT paper push silence (Josh, 2026-11-03 — "silence both paper
surfaces", after the Discord push audit).

Surface 1 — ict_push channel: paper entries/exits queue in
'suppressed' status (the row is still written; the drainer only ever
selects 'pending', so a paper push can never post). Live entries
(live_send) and live exits (traderspost_sent) queue as 'pending'
with byte-identical payloads — fill-verdict footers included (the
deep footer matrix stays pinned in test_ict_push_fill_verdicts.py).

Surface 2 — the engine futures mirror on #futures: engine futures
lifecycle alerts (closed / management_blocked) enqueue as suppressed
Discord jobs when their trade is paper, and stay pending when the
trade was actually sent live. The distinction is read from the
records: a close alert's payload is the position dict (carrying
traderspost_sent / live_send — production evidence: 3 of 27 closed
rows in the audit window were live survivor closes), and a
management_blocked alert carries the trade_id, resolved against the
trade record. Recording is untouched on both surfaces.
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from compass import fill_confirmation as fc
from compass import ict_push
from compass.alerts import (DeliveryWorker, enqueue_routes,
                            futures_mirror_silence)
from compass.config import Config
from compass.store import Store, discord_jobs, events, push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'
NOW = 1793300000.0

LIVE_ENTRY_FOOTER = '🔴 **LIVE** — order queued to TradersPost'
CONFIRMED_FOOTER = '🔴 **LIVE** — closed via TradersPost · fill confirmed at broker'
PAPER_FOOTER = '_Paper trade — watch only_'


def cfg(unified=False):
    return SimpleNamespace(push_outbox_unified=unified,
                           ict_push_enabled=True, ict_push_webhook=URL)


def make_db(tmp_path, name):
    s = Store('sqlite:///' + str(tmp_path / name))
    s.initialize()
    return s


def _rows(db, unified):
    table = push_outbox if unified else ict_push.outbox
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


def _entry_trade(**kw):
    t = {'id': 'ict-aoi_fade-MES.c.0-1-t1', 'detector': 'aoi_fade',
         'strategy': 'ict-aoi-fade', 'symbol': 'MES.c.0',
         'side': 'long', 'entry': 100.0, 'stop': 99.0, 'target': 102.0,
         'qty': 1, 'entered_at': NOW, 'source': 'ict_paper'}
    t.update(kw)
    return t


def _exit_trade(**kw):
    t = {'id': 'ict-aoi_fade-MES.c.0-1-t1', 'detector': 'aoi_fade',
         'strategy': 'ict-aoi-fade', 'symbol': 'MES.c.0',
         'side': 'long', 'entry': 100.0, 'exit': 102.0, 'pnl': 200.0,
         'exit_reason': 'target_hit', 'exited_at': NOW,
         'source': 'ict_paper'}
    t.update(kw)
    return t


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return httpx.Response(200, json={'id': '123'})


# ---------------------------------------------------------------------
# Surface 1: ict_push — paper suppressed at enqueue, live untouched
# ---------------------------------------------------------------------
def test_paper_entry_suppressed_both_flavors(tmp_path):
    for unified in (False, True):
        db = make_db(tmp_path, 'u.db' if unified else 'l.db')
        with db.tx() as conn:
            row_id = ict_push.maybe_queue(db, conn, cfg(unified), _entry_trade())
        assert row_id  # the record is still written
        (row,) = _rows(db, unified)
        assert row['status'] == 'suppressed'
        assert row['payload']['live'] is False
        # The drainer can never pick it up: nothing posts.
        client = FakeClient()
        if unified:
            assert not ict_push.deliver_one(db, client, URL, now=NOW,
                                            table=push_outbox, channel='ict')
        else:
            assert not ict_push.deliver_one(db, client, URL, now=NOW)
        assert client.calls == []


def test_live_entry_pending_and_delivers_byte_identical(tmp_path):
    db = make_db(tmp_path, 'a.db')
    trade = _entry_trade(id='ict-survivor_s2-MCL.v.0-1-t1',
                         detector='survivor_s2',
                         strategy='survivor_s2_asia_fade', symbol='MCL.v.0',
                         live_send=True, source='survivors')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(), trade)
    (row,) = _rows(db, False)
    assert row['status'] == 'pending'
    assert row['payload']['live'] is True
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=NOW)
    body = client.calls[0][1]['json']
    assert body == ict_push.format_message(row['payload'])
    assert LIVE_ENTRY_FOOTER in body['content']
    assert PAPER_FOOTER not in body['content']


def test_paper_exit_suppressed_both_flavors(tmp_path):
    for unified in (False, True):
        db = make_db(tmp_path, 'u.db' if unified else 'l.db')
        with db.tx() as conn:
            row_id = ict_push.maybe_queue_exit(
                db, conn, cfg(unified), _exit_trade(traderspost_sent=False))
        assert row_id
        (row,) = _rows(db, unified)
        assert row['status'] == 'suppressed'
        assert row['payload']['live'] is False
        client = FakeClient()
        if unified:
            assert not ict_push.deliver_one(db, client, URL, now=NOW,
                                            table=push_outbox, channel='ict')
        else:
            assert not ict_push.deliver_one(db, client, URL, now=NOW)
        assert client.calls == []


def test_never_sent_survivor_exit_suppressed(tmp_path):
    # A survivor trade that was never sent is a paper trade: its exit
    # records as a suppressed row (the fill-verdict stamp logic is
    # unaffected — it still stamps nothing for a never-sent trade).
    db = make_db(tmp_path, 'a.db')
    trade = _exit_trade(id='ict-survivor_s4-MGC.v.0-1-t1',
                        detector='survivor_s4', symbol='MGC.v.0',
                        traderspost_sent=False, source='survivors')
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(), trade)
    (row,) = _rows(db, False)
    assert row['status'] == 'suppressed'
    assert 'fill_state' not in row['payload']


def test_live_survivor_exit_pending_with_verdict_footer(tmp_path):
    db = make_db(tmp_path, 'a.db')
    tid = 'ict-survivor_s4-MGC.v.0-1793632218-t1'
    trade = _exit_trade(id=tid, detector='survivor_s4',
                        strategy='survivor_s4_ny_mr60_fade', symbol='MGC.v.0',
                        side='short', entry=3967.5, exit=3949.1, pnl=181.0,
                        traderspost_sent=True, source='survivors')
    with db.tx() as conn:
        db.put(conn, 'trade:' + tid, dict(trade))
        fc.record_verdict(db, conn, tid, 'entry', 'filled', NOW,
                          price=3971.30, source='test_broker_check')
        fc.record_verdict(db, conn, tid, 'exit', 'filled', NOW,
                          price=3949.10, source='test_broker_check')
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(), trade)
    (row,) = _rows(db, False)
    assert row['status'] == 'pending'
    assert row['payload']['live'] is True
    assert row['payload']['fill_state'] == 'confirmed'
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=NOW)
    content = client.calls[0][1]['json']['content']
    assert content.endswith(CONFIRMED_FOOTER)


# ---------------------------------------------------------------------
# Surface 2: engine futures mirror — paper suppressed, live-linked kept
# ---------------------------------------------------------------------
PAPER_CLOSED = {'mode': 'SIMULATED', 'status': 'closed', 'asset': 'future',
                'id': 'ict-aoi_fade-MES.c.0-1-t1', 'strategy': 'ict-aoi-fade',
                'side': 'long', 'entry': 100.0, 'exit': 99.0, 'pnl': -50.0,
                'exit_reason': 'stop', 'source': 'ict_paper'}
LIVE_CLOSED = {'mode': 'SIMULATED', 'status': 'closed', 'asset': 'future',
               'id': 'ict-survivor_s3-MGC.v.0-1793607435-t1',
               'strategy': 'survivor_s3_london_mr60',
               'side': 'long', 'entry': 3908.6, 'exit': 3910.2, 'pnl': 18.0,
               'exit_reason': 'session_flatten', 'source': 'survivors',
               'traderspost_sent': True, 'live_send': True}
BLOCKED = {'mode': 'SIMULATED', 'status': 'management_blocked',
           'asset': 'future', 'strategy': 'ict-aoi-fade', 'side': 'long',
           'reason': 'No fresh exit quote; position remains unresolved'}


def add_alert(db, source, symbol, payload, ts=NOW):
    with db.tx() as c:
        db.append(c, 'alert', source, symbol, ts, payload)
        return c.execute(select(events).order_by(events.c.id.desc())
                         .limit(1)).mappings().one()['id']


def jobs(db):
    with db.tx() as c:
        return list(c.execute(select(discord_jobs)
                              .order_by(discord_jobs.c.event_id)).mappings())


def test_mirror_paper_closed_suppressed_but_recorded(tmp_path):
    db = make_db(tmp_path, 'a.db')
    event_id = add_alert(db, 'engine', 'MES.c.0', dict(PAPER_CLOSED))
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['event_id'] == event_id
    assert job['status'] == 'suppressed'
    assert job['confirmation']['gate'] == 'futures-mirror-silence-v1'
    with db.tx() as c:
        recorded = list(c.execute(select(events).where(
            events.c.kind == 'alert')).mappings())
    assert [r['id'] for r in recorded] == [event_id]
    assert recorded[0]['payload']['status'] == 'closed'


def test_mirror_live_closed_stays_pending(tmp_path):
    db = make_db(tmp_path, 'a.db')
    add_alert(db, 'engine', 'MGC.v.0', dict(LIVE_CLOSED))
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['status'] == 'pending'


def test_mirror_live_send_only_flag_stays_pending(tmp_path):
    db = make_db(tmp_path, 'a.db')
    payload = dict(LIVE_CLOSED)
    del payload['traderspost_sent']
    add_alert(db, 'engine', 'MGC.v.0', payload)
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['status'] == 'pending'


def test_mirror_blocked_without_record_suppressed(tmp_path):
    db = make_db(tmp_path, 'a.db')
    add_alert(db, 'engine', 'MES.c.0',
              {**BLOCKED, 'trade_id': 'ict-aoi_fade-MES.c.0-1-t1'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['status'] == 'suppressed'
    assert job['confirmation']['gate'] == 'futures-mirror-silence-v1'


def test_mirror_blocked_live_via_trade_record_stays_pending(tmp_path):
    db = make_db(tmp_path, 'a.db')
    tid = 'ict-survivor_s2-MCL.v.0-1-t1'
    with db.tx() as c:
        db.put(c, 'trade:' + tid,
               {'id': tid, 'source': 'survivors', 'traderspost_sent': True})
    add_alert(db, 'engine', 'MCL.v.0', {**BLOCKED,
                                        'strategy': 'survivor_s2_asia_fade',
                                        'trade_id': tid})
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['status'] == 'pending'


def test_mirror_blocked_paper_via_trade_record_suppressed(tmp_path):
    db = make_db(tmp_path, 'a.db')
    tid = 'ict-aoi_fade-MES.c.0-1-t1'
    with db.tx() as c:
        db.put(c, 'trade:' + tid,
               {'id': tid, 'source': 'ict_paper', 'traderspost_sent': False,
                'live_send': False})
    add_alert(db, 'engine', 'MES.c.0', {**BLOCKED, 'trade_id': tid})
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = jobs(db)
    assert job['status'] == 'suppressed'


def test_mirror_outside_class_untouched(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as c:
        # Futures entry alerts are not in the gated class.
        assert futures_mirror_silence(
            db, c, {'symbol': 'MES.c.0',
                    'payload': {'status': 'entered', 'asset': 'future'}},
            NOW) is None
        # Options and stock closes are not the futures mirror.
        assert futures_mirror_silence(
            db, c, {'symbol': 'AAPL260102C00200000',
                    'payload': {'status': 'closed', 'asset': 'option'}},
            NOW) is None
        assert futures_mirror_silence(
            db, c, {'symbol': 'AAPL',
                    'payload': {'status': 'closed', 'asset': 'stock'}},
            NOW) is None
    entered = add_alert(db, 'engine', 'MES.c.0', {
        'mode': 'SIMULATED', 'status': 'entered', 'asset': 'future',
        'strategy': 'ict-aoi-fade', 'side': 'long'})
    with db.tx() as c:
        enqueue_routes(db, c, NOW)
    (job,) = [j for j in jobs(db) if j['event_id'] == entered]
    assert job['status'] == 'pending'


def test_mirror_send_time_belt_suppresses_queued_backlog(tmp_path):
    db = make_db(tmp_path, 'a.db')
    event_id = add_alert(db, 'engine', 'MES.c.0', dict(PAPER_CLOSED))
    with db.tx() as c:
        c.execute(discord_jobs.insert().values(
            event_id=event_id, route='futures', status='pending',
            queued_at=NOW, confirmation={}))
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
    assert job['confirmation']['gate'] == 'futures-mirror-silence-v1'
