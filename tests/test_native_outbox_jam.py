"""Audit #5: native outbox jam — stale-epoch rows and dead-parent rows used
to be silently skipped by deliver_one on every pass forever. They now reach
a recorded terminal state, and epoch-stale rows (including Friday ':close'
exits, which the smoothers repair endpoint never covered) have an
idempotent repair path."""
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from test_projects import db
from compass.native_outbox import outbox, queue, deliver_one, sweep_jammed, repair_stale

NOW = datetime(2026, 9, 18, 20, 5, tzinfo=timezone.utc).timestamp()  # a Friday close
URL = 'https://discord.com/api/webhooks/123/test'


def _own(db, c, program, epoch, at=NOW):
    db.put(c, 'native:ownership:' + program,
           dict(owner='compass', previous_sender_paused=True, accepted_at=at, epoch=epoch))


def _client(sent):
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={'id': '12345'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_stale_epoch_row_terminally_resolved_not_skipped_forever(db):
    with db.tx() as c:
        _own(db, c, 'morning', 'a')
        queue(db, c, 'morning', 'stale', {'content': 'stale'}, NOW)
        # Ownership re-armed under a new epoch: rows queued under 'a' are no
        # longer covered by the current grant.
        _own(db, c, 'morning', 'b', at=NOW + 1)
        queue(db, c, 'morning', 'fresh', {'content': 'fresh'}, NOW + 1)
    sent = []
    with _client(sent) as client:
        assert deliver_one(db, client, {'morning': URL}, True, NOW + 2)
        assert not deliver_one(db, client, {'morning': URL}, True, NOW + 3)
    assert len(sent) == 1 and b'fresh' in sent[0].content
    with db.tx() as c:
        rows = {r['event_key']: r for r in c.execute(select(outbox)).mappings()}
        stale = rows['stale']
        assert stale['status'] == 'suppressed'
        assert stale['delivery']['error'] == 'stale_owner_epoch'
        assert stale['delivery']['stale_owner_epoch'] == 'a'
        assert stale['delivery']['current_owner_epoch'] == 'b'
        assert rows['fresh']['status'] == 'delivered'
        health = db.get(c, 'health:native_outbox_jam', {})
        assert health.get('status') == 'resolved' and health.get('resolved') == 1


def test_repair_revives_friday_close_exactly_once(db):
    with db.tx() as c:
        _own(db, c, 'smoothers', 'a')
        queue(db, c, 'smoothers', 'pos1:close', {'content': 'Friday close'}, NOW,
              event_time=NOW, cohort_time=NOW)
        _own(db, c, 'smoothers', 'b', at=NOW + 1)
    sent = []
    with _client(sent) as client:
        # The drainer terminally resolves the jammed close; it does not send it.
        assert not deliver_one(db, client, {'smoothers': URL}, True, NOW + 2)
        assert [r['event_key'] for r in repair_stale(db, now=NOW + 3)] == ['pos1:close']
        # Repair is idempotent: already-armed rows are left alone.
        assert repair_stale(db, now=NOW + 4) == []
        assert deliver_one(db, client, {'smoothers': URL}, True, NOW + 5)
        assert not deliver_one(db, client, {'smoothers': URL}, True, NOW + 6)
        # Delivered rows are never re-armed.
        assert repair_stale(db, now=NOW + 7) == []
    assert len(sent) == 1 and b'Friday close' in sent[0].content
    with db.tx() as c:
        row = c.execute(select(outbox)).mappings().one()
        assert row['status'] == 'delivered'
        assert row['delivery']['repair_bypass'] is True
        assert row['delivery']['owner_epoch'] == 'b'


def test_parent_dead_row_terminal_and_not_repairable(db):
    with db.tx() as c:
        _own(db, c, 'morning', 'a')
        queue(db, c, 'morning', 'signal', {'content': 'entry'}, NOW)
        queue(db, c, 'morning', 'option', {'content': 'entry + option'}, NOW + 1,
              parent_event='signal')
        c.execute(outbox.update().where(outbox.c.event_key == 'signal')
                  .values(status='suppressed'))
    sent = []
    with _client(sent) as client:
        assert not deliver_one(db, client, {'morning': URL}, True, NOW + 2)
    assert sent == []
    with db.tx() as c:
        row = c.execute(select(outbox).where(outbox.c.event_key == 'option')).mappings().one()
        assert row['status'] == 'suppressed'
        assert row['delivery']['error'] == 'parent_not_delivered'
    # A dead parent can never produce the message id the child must PATCH,
    # so repair deliberately leaves the row terminal.
    assert repair_stale(db, now=NOW + 3) == []
    with db.tx() as c:
        row = c.execute(select(outbox).where(outbox.c.event_key == 'option')).mappings().one()
        assert row['status'] == 'suppressed'


def test_repair_bypass_row_survives_second_rearm(db):
    # 7f9e750 behavior preserved: a row an operator already repaired keeps
    # flowing when the epoch moves again before the drainer reaches it.
    with db.tx() as c:
        _own(db, c, 'morning', 'a')
        queue(db, c, 'morning', 'repaired', {'content': 'healed'}, NOW)
        row = c.execute(select(outbox)).mappings().one()
        d = dict(row['delivery'])
        d['repair_bypass'] = True
        c.execute(outbox.update().where(outbox.c.id == row['id']).values(delivery=d))
        _own(db, c, 'morning', 'b', at=NOW + 1)
    sent = []
    with _client(sent) as client:
        assert deliver_one(db, client, {'morning': URL}, True, NOW + 2)
    assert len(sent) == 1
    with db.tx() as c:
        row = c.execute(select(outbox)).mappings().one()
        assert row['status'] == 'delivered'
        assert row['delivery']['owner_epoch'] == 'b'


def test_healthy_rows_untouched_by_sweep_and_repair(db):
    with db.tx() as c:
        _own(db, c, 'morning', 'a')
        queue(db, c, 'morning', 'ok', {'content': 'ok'}, NOW)
    with db.tx() as c:
        assert sweep_jammed(db, c, NOW + 1) == []
    assert repair_stale(db, now=NOW + 1) == []
    with db.tx() as c:
        assert c.execute(select(outbox.c.status)).scalar_one() == 'pending'
