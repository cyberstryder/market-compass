"""Regression: live HORIZON survivor entries never posted to Discord
(2026-11-04 — Josh flagged that neither live entry that morning, S2
04:26 CT nor S3 07:31 CT, produced an alert; production health
ict_push read error/TypeError).

Pinned cause: survivors S1/S3 book with stop=None/target=None BY
CONSTRUCTION (timed exits, no bracket). maybe_queue copies the Nones
into the payload, and the drainer's format_message rendered levels
with '%.2f' % payload.get('target', 0) — the key exists with value
None, so the default never applied and the format raised
TypeError: must be real number, not NoneType. deliver_one only
caught httpx.HTTPError, so the TypeError escaped, the row stranded
in 'sending', and run() latched the ict_push health row at error.
Every test trade before this file carried numeric stop/target, so
the class was invisible in CI.

These tests drive the exact production shapes (built field-for-field
from survivors.submit_survivor's filled dict, at this morning's real
prices/timestamps), pin the bracket rendering byte-identical, pin
the drainer's fail-closed behavior for a payload the formatter
cannot render, and pin the survivors queue call site surfacing a
queue failure in health instead of swallowing it silently.
"""
from datetime import timezone, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from compass import ict_push, survivors
from compass.config import Config
from compass.store import Store, push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'
# This morning's real bookings (2026-11-04): S2 filled 94.64 at
# 04:26 CT (id ts 1793787945), S3 filled 4044.1 (signal ts
# 1793798999, 07:29 CT).
S2_NOW = 1793787945.0
S3_NOW = 1793798999.0

LIVE_ENTRY_FOOTER = '📊 **TRACKING** — measurement only · no order was placed or sent anywhere'
PENDING_EXIT_FOOTER = '📊 **TRACKING** — closed · fill not yet verified'


def cfg(unified=False):
    return SimpleNamespace(push_outbox_unified=unified,
                           ict_push_enabled=True, ict_push_webhook=URL)


def make_db(tmp_path, name):
    s = Store('sqlite:///' + str(tmp_path / name))
    s.initialize()
    return s


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return httpx.Response(200, json={'id': '123'})


def _rows(db, unified=False):
    table = push_outbox if unified else ict_push.outbox
    with db.tx() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings()]


def _s3_entry(**kw):
    """submit_survivor's filled dict for S3, field-for-field: a
    horizon trade, so stop/target are None on the record."""
    t = {'id': 'ict-survivor_s3-MGC.v.0-1793798999-t1',
         'detector': 'survivor_s3', 'strategy': 'survivor_s3_london_mr60',
         'symbol': 'MGC.v.0', 'side': 'long', 'direction': 'long',
         'qty': 1, 'quantity': 1, 'entry': 4044.1, 'signal_price': 4045.6,
         'stop': None, 'target': None, 'status': 'open',
         'entered_at': S3_NOW, 'source': 'survivors',
         'survivor_strategy': 'S3', 'no_bracket': True,
         'signal_rr': None, 'live_send': True}
    t.update(kw)
    return t


def _s1_entry(**kw):
    t = _s3_entry(id='ict-survivor_s1-MNQ.c.0-1793798999-t1',
                  detector='survivor_s1',
                  strategy='survivor_s1_rvwap_fade', symbol='MNQ.c.0',
                  side='short', direction='short', entry=20000.0,
                  signal_price=20001.0, survivor_strategy='S1')
    t.update(kw)
    return t


def _s2_entry(**kw):
    t = {'id': 'ict-survivor_s2-MCL.v.0-1793787945-t1',
         'detector': 'survivor_s2', 'strategy': 'survivor_s2_asia_low_fade',
         'symbol': 'MCL.v.0', 'side': 'long', 'direction': 'long',
         'qty': 1, 'quantity': 1, 'entry': 94.64, 'signal_price': 94.93,
         'stop': 93.82, 'target': 97.15, 'status': 'open',
         'entered_at': S2_NOW, 'source': 'survivors',
         'survivor_strategy': 'S2', 'no_bracket': False,
         'live_send': True}
    t.update(kw)
    return t


# ---------------------------------------------------------------------
# The pinned failure: horizon entries must queue AND deliver
# ---------------------------------------------------------------------
def test_s3_horizon_entry_queues_pending_levels_stay_none(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(), _s3_entry())
    (row,) = _rows(db)
    assert row['status'] == 'pending'
    assert row['payload']['live'] is True
    # The payload is the record: levels stay None (byte-identical
    # semantics) — the FIX is in rendering, not in the stored row.
    assert row['payload']['stop'] is None
    assert row['payload']['target'] is None


def test_s3_horizon_entry_delivers(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(), _s3_entry())
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 60)
    (row,) = _rows(db)
    assert row['status'] == 'delivered'
    content = client.calls[0][1]['json']['content']
    assert content == (
        '🟢 **ICT Survivor S3** LONG MGC.v.0\n'
        'Entry `4044.10` · no stop/target — timed exit\n'
        'R:R `0.00` · Qty `1` · Wed 07:29 CT\n'
        + LIVE_ENTRY_FOOTER)


def test_s1_horizon_entry_delivers(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(), _s1_entry())
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 60)
    content = client.calls[0][1]['json']['content']
    assert content == (
        '🔴 **ICT Survivor S1** SHORT MNQ.c.0\n'
        'Entry `20000.00` · no stop/target — timed exit\n'
        'R:R `0.00` · Qty `1` · Wed 07:29 CT\n'
        + LIVE_ENTRY_FOOTER)


def test_s3_horizon_entry_delivers_unified(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(unified=True),
                                    _s3_entry())
    (row,) = _rows(db, unified=True)
    assert row['status'] == 'pending' and row['channel'] == 'ict'
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 60,
                                table=push_outbox, channel='ict')
    assert client.calls[0][1]['json']['content'].endswith(
        LIVE_ENTRY_FOOTER)


def test_s3_horizon_exit_delivers_with_pending_footer(tmp_path):
    db = make_db(tmp_path, 'a.db')
    trade = _s3_entry(status='closed', exit=4045.9, pnl=18.0,
                      exit_reason='session_flatten',
                      exited_at=S3_NOW + 1900, traderspost_sent=True)
    trade.pop('live_send')
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(), trade)
    (row,) = _rows(db)
    assert row['status'] == 'pending'
    assert row['payload']['fill_state'] == 'pending'
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 1960)
    content = client.calls[0][1]['json']['content']
    assert 'Entry `4044.10` → Exit `4045.90`' in content
    assert content.endswith(PENDING_EXIT_FOOTER)


# ---------------------------------------------------------------------
# Bracket rendering is byte-identical after the fix
# ---------------------------------------------------------------------
def test_s2_bracket_entry_rendering_unchanged(tmp_path):
    db = make_db(tmp_path, 'a.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(db, conn, cfg(), _s2_entry())
    client = FakeClient()
    assert ict_push.deliver_one(db, client, URL, now=S2_NOW + 60)
    content = client.calls[0][1]['json']['content']
    assert content == (
        '🟢 **ICT Survivor S2** LONG MCL.v.0\n'
        'Entry `94.64` → Target `97.15` | Stop `93.82`\n'
        'R:R `3.06` · Qty `1` · Wed 04:25 CT\n'
        + LIVE_ENTRY_FOOTER)


# ---------------------------------------------------------------------
# Drainer: an unrenderable payload fails its row, never strands it
# ---------------------------------------------------------------------
def test_unrenderable_row_fails_closed_and_drainer_moves_on(tmp_path):
    db = make_db(tmp_path, 'a.db')
    bad_payload = {'detector': None, 'strategy': 'x', 'symbol': 'MGC.v.0',
                   'side': 'LONG', 'entry': 1.0, 'stop': 0.5,
                   'target': 2.0, 'rr': 1.0, 'qty': 1,
                   'time_str': 'Wed 07:29 CT', 'at': S3_NOW, 'live': True}
    with db.tx() as conn:
        conn.execute(ict_push.outbox.insert().values(
            id='badrow', status='pending', created=S3_NOW,
            payload=bad_payload, delivery={'attempts': 0}))
        assert ict_push.maybe_queue(db, conn, cfg(), _s2_entry())
    client = FakeClient()
    # The bad row is attempted first (oldest): no raise escapes, the
    # row is failed with its cause recorded — not left in 'sending'.
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 60)
    rows = {r['id']: r for r in _rows(db)}
    assert rows['badrow']['status'] == 'failed'
    assert 'AttributeError' in rows['badrow']['delivery']['error']
    assert client.calls == []
    # And the loop moves on: the good row behind it still delivers.
    assert ict_push.deliver_one(db, client, URL, now=S3_NOW + 61)
    assert len(client.calls) == 1
    rows = {r['id']: r for r in _rows(db)}
    assert [r['status'] for r in rows.values()] == ['failed', 'delivered']


# ---------------------------------------------------------------------
# Survivors call site: a queue failure marks health, never blocks
# ---------------------------------------------------------------------
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def sdb(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'sv.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def scfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.traderspost_enabled = False
    return c


def _sig(strat, **kw):
    base = {'strategy': strat, 'dir': survivors.DIRECTION[strat],
            'close': 100.0, 'ts': NOW - 60, 'tod': 720,
            'sdate': '2026-09-28', 'atr_t': 100.0, 'bar_index': 0}
    if strat in survivors.HORIZON:
        base['hold'] = survivors.HORIZON[strat]
    base.update(kw)
    return base


def test_survivor_queue_failure_marks_health_and_still_books(
        sdb, scfg, monkeypatch):
    def boom(db_, c_, cfg_, trade):
        raise RuntimeError('queue exploded')
    monkeypatch.setattr(ict_push, 'maybe_queue', boom)
    with sdb.tx() as c:
        r = survivors.submit_survivor(sdb, c, scfg, NOW, 'S3',
                                      'MGC.v.0', _sig('S3'))
    assert r['submitted']  # booking is never blocked by a push failure
    with sdb.tx() as c:
        pos = sdb.get(c, 'position:ict:survivor_s3:MGC.v.0:t1')
        marker = sdb.get(c, 'health:ict_push_queue')
    assert pos is not None and pos['status'] == 'open'
    assert marker is not None and marker['status'] == 'error'
    assert 'RuntimeError' in marker['detail']
    assert 'queue exploded' in marker['detail']
