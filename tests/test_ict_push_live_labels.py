"""ICT Discord live labeling (2026-10-31): a live survivor trade must
not post to Discord labeled 'Paper trade — watch only'.

Pins: (1) paper entry/exit payloads and formatted content are
byte-for-byte the pre-change output ('live' absent or False);
(2) a live entry (trade['live_send'], set by survivors at booking)
and a live exit (position['traderspost_sent'], the field the
engine's TradersPost close gates read) carry payload['live'] True in
BOTH outbox flavors and render the LIVE footer; (3) deliver_one posts
the live footer end to end.
"""
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from compass import ict_push
from compass.store import Store, push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'
NOW = 1793300000.0

PAPER_FOOTER = '_Paper trade — watch only_'
LIVE_ENTRY_FOOTER = '🔴 **LIVE** — order queued to TradersPost'
LIVE_EXIT_FOOTER = '🔴 **LIVE** — closed via TradersPost'

ENTRY = {'detector': 'survivor_s2', 'strategy': 'survivor_s2_asia_fade',
         'symbol': 'MCL.v.0', 'side': 'LONG', 'entry': 100.0,
         'stop': 99.0, 'target': 102.0, 'rr': 2.0, 'qty': 1,
         'time_str': 'Mon 09:15 CT', 'at': NOW}
EXIT = {'type': 'exit', 'detector': 'survivor_s2', 'strategy': '',
        'symbol': 'MCL.v.0', 'side': 'LONG', 'entry': 100.0,
        'exit': 102.0, 'pnl': 200.0, 'pnl_str': '+$200.00',
        'result': 'TARGET HIT', 'emoji': '🎯',
        'time_str': 'Mon 11:15 CT', 'at': NOW}

LEGACY_ENTRY = (
    '🟢 **ICT Survivor S2** LONG MCL.v.0\n'
    'Entry `100.00` → Target `102.00` | Stop `99.00`\n'
    'R:R `2.00` · Qty `1` · Mon 09:15 CT\n'
    '_Paper trade — watch only_')
LEGACY_EXIT = (
    '🎯 **ICT Survivor S2** LONG MCL.v.0 — TARGET HIT\n'
    'Entry `100.00` → Exit `102.00`\n'
    'P&L **+$200.00** · Mon 11:15 CT\n'
    '_Paper trade — watch only_')


def cfg(unified=False):
    return SimpleNamespace(push_outbox_unified=unified,
                           ict_push_enabled=True, ict_push_webhook=URL)


def make_db(tmp_path, name):
    s = Store('sqlite:///' + str(tmp_path / name))
    s.initialize()
    return s


# ---------------------------------------------------------------------
# Formatters: paper byte-for-byte, live footer swapped, body untouched
# ---------------------------------------------------------------------
def test_format_paper_entry_unchanged():
    assert ict_push.format_message(dict(ENTRY))['content'] == LEGACY_ENTRY
    p = dict(ENTRY, live=False)
    assert ict_push.format_message(p)['content'] == LEGACY_ENTRY


def test_format_live_entry_footer():
    content = ict_push.format_message(dict(ENTRY, live=True))['content']
    assert content == LEGACY_ENTRY.replace(PAPER_FOOTER, LIVE_ENTRY_FOOTER)
    assert PAPER_FOOTER not in content


def test_format_paper_exit_unchanged():
    assert ict_push.format_exit_message(dict(EXIT))['content'] == LEGACY_EXIT
    p = dict(EXIT, live=False)
    assert ict_push.format_exit_message(p)['content'] == LEGACY_EXIT


def test_format_live_exit_footer():
    content = ict_push.format_exit_message(dict(EXIT, live=True))['content']
    assert content == LEGACY_EXIT.replace(PAPER_FOOTER, LIVE_EXIT_FOOTER)
    assert PAPER_FOOTER not in content


# ---------------------------------------------------------------------
# Producers: 'live' lands in both outbox flavors, from the trade dict
# ---------------------------------------------------------------------
def _one_payload(db, unified):
    table = push_outbox if unified else ict_push.outbox
    with db.tx() as conn:
        rows = [dict(r) for r in conn.execute(select(table)).mappings()]
    assert len(rows) == 1
    return rows[0]['payload']


def _entry_trade(**kw):
    t = {'id': 'ict-survivor_s2-MCL.v.0-1-t1', 'detector': 'survivor_s2',
         'strategy': 'survivor_s2_asia_fade', 'symbol': 'MCL.v.0',
         'side': 'long', 'entry': 100.0, 'stop': 99.75, 'target': 100.5,
         'qty': 1, 'entered_at': NOW}
    t.update(kw)
    return t


def _exit_trade(**kw):
    t = {'id': 'ict-survivor_s2-MCL.v.0-1-t1', 'detector': 'survivor_s2',
         'symbol': 'MCL.v.0', 'side': 'long', 'entry': 100.0,
         'exit': 100.5, 'pnl': 250.0, 'exit_reason': 'target_hit',
         'exited_at': NOW}
    t.update(kw)
    return t


def test_maybe_queue_live_flag_both_flavors(tmp_path):
    for unified in (False, True):
        db = make_db(tmp_path, 'u.db' if unified else 'l.db')
        c = cfg(unified)
        with db.tx() as conn:
            assert ict_push.maybe_queue(
                db, conn, c, _entry_trade(live_send=True))
        assert _one_payload(db, unified)['live'] is True
        db2 = make_db(tmp_path, 'u2.db' if unified else 'l2.db')
        with db2.tx() as conn:
            assert ict_push.maybe_queue(db2, conn, c, _entry_trade())
        assert _one_payload(db2, unified)['live'] is False


def test_maybe_queue_exit_live_flag_both_flavors(tmp_path):
    for unified in (False, True):
        db = make_db(tmp_path, 'u.db' if unified else 'l.db')
        c = cfg(unified)
        with db.tx() as conn:
            assert ict_push.maybe_queue_exit(
                db, conn, c, _exit_trade(traderspost_sent=True))
        assert _one_payload(db, unified)['live'] is True
        db2 = make_db(tmp_path, 'u2.db' if unified else 'l2.db')
        with db2.tx() as conn:
            assert ict_push.maybe_queue_exit(
                db2, conn, c, _exit_trade(traderspost_sent=False))
        assert _one_payload(db2, unified)['live'] is False


# ---------------------------------------------------------------------
# End to end: deliver_one posts the live entry footer
# ---------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return httpx.Response(200, json={'id': '123'})


def test_deliver_live_entry_posts_live_footer(tmp_path):
    db = make_db(tmp_path, 'd.db')
    with db.tx() as conn:
        assert ict_push.maybe_queue(
            db, conn, cfg(), _entry_trade(live_send=True))
    fc = FakeClient()
    assert ict_push.deliver_one(db, fc, URL, now=NOW)
    content = fc.calls[0][1]['json']['content']
    assert LIVE_ENTRY_FOOTER in content
    assert PAPER_FOOTER not in content
