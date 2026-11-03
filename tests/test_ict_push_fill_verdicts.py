"""ICT Discord exit labels — fill-verdict renderings (2026-11-03).

Josh's decision: live survivor EXIT alerts must show whether the
close is broker-confirmed or unconfirmed, instead of the bare
'🔴 LIVE — closed via TradersPost' that only meant "sent and gated
live" (the 2026-11-02 S3 phantom: both legs rejected at Tradovate
while the alert read as a live close).

Pins: (1) the three rendering classes — confirmed / affirmatively
not filled (entry or exit leg rejected or no broker order) /
pending verification — as six exact footers, swapped in only for a
live payload carrying 'fill_state'; (2) paper payloads byte-for-byte
unchanged even if a fill_state is present, and a live payload
without fill_state (rows queued pre-change, non-survivor exits)
keeps the generic LIVE footer; (3) the producer: maybe_queue_exit
stamps fill_state from the verdicts recorded at queue time for live
survivor exits ONLY — a non-survivor exit with verdicts on record
gets no stamp, and a never-sent survivor exit gets none either;
(4) deliver_one posts the pending footer end to end (the common
rendering: at alert time the exit verdict normally does not exist
yet, and it must read as "not yet verified", not as an alarm).
"""
from types import SimpleNamespace

import httpx
from sqlalchemy import select

from compass import fill_confirmation as fc
from compass import ict_push
from compass.store import Store, push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'
NOW = 1793300000.0

PAPER_FOOTER = '_Paper trade — watch only_'
LIVE_EXIT_FOOTER = '🔴 **LIVE** — closed via TradersPost'

EXIT = {'type': 'exit', 'detector': 'survivor_s2', 'strategy': '',
        'symbol': 'MCL.v.0', 'side': 'LONG', 'entry': 100.0,
        'exit': 102.0, 'pnl': 200.0, 'pnl_str': '+$200.00',
        'result': 'TARGET HIT', 'emoji': '🎯',
        'time_str': 'Mon 11:15 CT', 'at': NOW}

LEGACY_EXIT = (
    '🎯 **ICT Survivor S2** LONG MCL.v.0 — TARGET HIT\n'
    'Entry `100.00` → Exit `102.00`\n'
    'P&L **+$200.00** · Mon 11:15 CT\n'
    '_Paper trade — watch only_')


def footer_for(state):
    return ict_push.FILL_FOOTERS[state]


def cfg(unified=False):
    return SimpleNamespace(push_outbox_unified=unified,
                           ict_push_enabled=True, ict_push_webhook=URL)


def make_db(tmp_path, name):
    s = Store('sqlite:///' + str(tmp_path / name))
    s.initialize()
    return s


# ---------------------------------------------------------------------
# Formatter: the six verdict footers, and nothing else moves
# ---------------------------------------------------------------------
def test_format_confirmed_footer():
    p = dict(EXIT, live=True, fill_state='confirmed')
    content = ict_push.format_exit_message(p)['content']
    assert content == LEGACY_EXIT.replace(PAPER_FOOTER,
                                          footer_for('confirmed'))
    assert PAPER_FOOTER not in content


def test_format_pending_footer():
    p = dict(EXIT, live=True, fill_state='pending')
    content = ict_push.format_exit_message(p)['content']
    assert content == LEGACY_EXIT.replace(PAPER_FOOTER,
                                          footer_for('pending'))
    assert 'not yet verified' in content


def test_format_not_filled_footers():
    for state in ('entry_rejected', 'entry_no_order',
                  'exit_rejected', 'exit_no_order'):
        p = dict(EXIT, live=True, fill_state=state)
        content = ict_push.format_exit_message(p)['content']
        assert content == LEGACY_EXIT.replace(PAPER_FOOTER,
                                              footer_for(state)), state
        assert PAPER_FOOTER not in content


def test_format_fill_state_ignored_when_not_live():
    # Paper payloads render byte-identically even with a stray
    # fill_state — the swap is gated on the live flag.
    p = dict(EXIT, fill_state='confirmed')
    assert ict_push.format_exit_message(p)['content'] == LEGACY_EXIT
    p = dict(EXIT, live=False, fill_state='exit_rejected')
    assert ict_push.format_exit_message(p)['content'] == LEGACY_EXIT


def test_format_live_without_fill_state_keeps_generic_footer():
    # Rows queued before this change (and non-survivor live exits)
    # carry no fill_state: the 2026-10-31 footer, unchanged.
    p = dict(EXIT, live=True)
    content = ict_push.format_exit_message(p)['content']
    assert content == LEGACY_EXIT.replace(PAPER_FOOTER, LIVE_EXIT_FOOTER)
    p = dict(EXIT, live=True, fill_state='bogus')
    content = ict_push.format_exit_message(p)['content']
    assert content == LEGACY_EXIT.replace(PAPER_FOOTER, LIVE_EXIT_FOOTER)


# ---------------------------------------------------------------------
# Producer: maybe_queue_exit stamps fill_state from recorded verdicts
# ---------------------------------------------------------------------
TID = 'ict-survivor_s4-MGC.v.0-1793632218-t1'


def _exit_position(**kw):
    """The closed position copy as engine.exits() hands it over."""
    t = {'id': TID, 'detector': 'survivor_s4',
         'strategy': 'survivor_s4_ny_mr60_fade', 'symbol': 'MGC.v.0',
         'side': 'short', 'qty': 1, 'entry': 3967.5, 'exit': 3949.1,
         'pnl': 181.0, 'status': 'closed', 'entered_at': NOW - 3600,
         'exited_at': NOW, 'exit_reason': 'target_hit',
         'traderspost_sent': True, 'source': 'survivors',
         'survivor_strategy': 'S4'}
    t.update(kw)
    return t


def _seed(db, position, verdicts=()):
    """Trade row in the DB + broker verdicts recorded per leg
    ((leg, verdict, price) triples) through the real seam."""
    with db.tx() as c:
        db.put(c, 'trade:' + position['id'], dict(position))
        for leg, verdict, price in verdicts:
            fc.record_verdict(db, c, position['id'], leg, verdict, NOW,
                              price=price, source='test_broker_check')


def _queue_and_read(db, position, unified=False):
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(unified), position)
    table = push_outbox if unified else ict_push.outbox
    with db.tx() as conn:
        rows = [dict(r) for r in conn.execute(select(table)).mappings()]
    assert len(rows) == 1
    return rows[0]['payload']


def test_queue_confirmed_state(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),
                    ('exit', 'filled', 3949.10)))
    payload = _queue_and_read(db, pos)
    assert payload['live'] is True
    assert payload['fill_state'] == 'confirmed'


def test_queue_entry_rejected_state(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'rejected', None),
                    ('exit', 'rejected', None)))
    assert _queue_and_read(db, pos)['fill_state'] == 'entry_rejected'


def test_queue_entry_no_order_state(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'no_order', None),))
    assert _queue_and_read(db, pos)['fill_state'] == 'entry_no_order'


def test_queue_exit_rejected_state(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),
                    ('exit', 'rejected', None)))
    assert _queue_and_read(db, pos)['fill_state'] == 'exit_rejected'


def test_queue_exit_no_order_state(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),
                    ('exit', 'no_order', None)))
    assert _queue_and_read(db, pos)['fill_state'] == 'exit_no_order'


def test_queue_pending_states(tmp_path):
    # Entry confirmed, exit not yet verdicted — the normal alert-time
    # state — and the no-verdicts-at-all state both read as pending.
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),))
    assert _queue_and_read(db, pos)['fill_state'] == 'pending'
    db2 = make_db(tmp_path, 'b.db')
    pos2 = _exit_position()
    _seed(db2, pos2)
    assert _queue_and_read(db2, pos2)['fill_state'] == 'pending'


def test_queue_non_survivor_gets_no_stamp(tmp_path):
    # A live ICT (non-survivor) exit renders exactly as before, even
    # with verdicts on record: the stamp is gated on the survivor
    # source marker, not on verdict presence.
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position(source='ict_paper')
    _seed(db, pos, (('entry', 'filled', 3971.30),
                    ('exit', 'filled', 3949.10)))
    payload = _queue_and_read(db, pos)
    assert payload['live'] is True
    assert 'fill_state' not in payload
    content = ict_push.format_exit_message(payload)['content']
    assert content.endswith(LIVE_EXIT_FOOTER)


def test_queue_never_sent_survivor_gets_no_stamp(tmp_path):
    db = make_db(tmp_path, 'a.db')
    pos = _exit_position(traderspost_sent=False)
    _seed(db, pos)
    payload = _queue_and_read(db, pos)
    assert payload['live'] is False
    assert 'fill_state' not in payload
    content = ict_push.format_exit_message(payload)['content']
    assert content.endswith(PAPER_FOOTER)


def test_queue_fill_state_both_outbox_flavors(tmp_path):
    for unified in (False, True):
        db = make_db(tmp_path, 'u.db' if unified else 'l.db')
        pos = _exit_position()
        _seed(db, pos, (('entry', 'filled', 3971.30),
                        ('exit', 'filled', 3949.10)))
        payload = _queue_and_read(db, pos, unified=unified)
        assert payload['fill_state'] == 'confirmed'


# ---------------------------------------------------------------------
# End to end: deliver_one posts the verdict footer
# ---------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return httpx.Response(200, json={'id': '123'})


def test_deliver_pending_exit_posts_pending_footer(tmp_path):
    db = make_db(tmp_path, 'd.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),))
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(), pos)
    fake = FakeClient()
    assert ict_push.deliver_one(db, fake, URL, now=NOW)
    content = fake.calls[0][1]['json']['content']
    # The footer is the last line; line equality (not substring) is
    # what separates the verdict footer from the generic LIVE one,
    # whose text prefixes it.
    assert content.split('\n')[-1] == footer_for('pending')
    assert PAPER_FOOTER not in content


def test_deliver_confirmed_exit_posts_confirmed_footer(tmp_path):
    db = make_db(tmp_path, 'd.db')
    pos = _exit_position()
    _seed(db, pos, (('entry', 'filled', 3971.30),
                    ('exit', 'filled', 3949.10)))
    with db.tx() as conn:
        assert ict_push.maybe_queue_exit(db, conn, cfg(), pos)
    fake = FakeClient()
    assert ict_push.deliver_one(db, fake, URL, now=NOW)
    content = fake.calls[0][1]['json']['content']
    assert content.split('\n')[-1] == footer_for('confirmed')
    assert PAPER_FOOTER not in content
