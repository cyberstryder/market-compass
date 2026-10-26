"""Josh 2026-10-26: Smoothers exits alert for every tracked position.

Before this change, an exit publication only delivered when the position's
entry had itself been a delivered detailed ENTRY — which exists only for the
hardcoded BEST_10. Batch-only signals (e.g. CRM/ORCL/INTC/PANW on 2026-10-26,
whose :entry intents are 'missing') had their :target/:close publications
suppressed at assessment with "No published entry for this lifecycle".

These tests pin the new rule end-to-end through queue_signal -> deliver_one:
batch-only and missing-intent exits deliver; a linked BEST_10-style entry+exit
still delivers without the new admission flag; entries are unchanged; other
programs keep the published-entry requirement.
"""
import httpx
from datetime import datetime
from sqlalchemy import select
from test_projects import db
from compass.alert_format import CT
from compass.alert_ownership import assess, read_trade
from compass.native_outbox import outbox, deliver_one
from compass.native_smoothers import queue_signal

NOW = 1790400000
FRI = int(datetime(2026, 10, 30, 15, 0, tzinfo=CT).timestamp())
HOOK = 'https://discord.com/api/webhooks/123/test'
HOOKS = {'smoothers': HOOK}


def own(db, now):
    with db.tx() as c:
        db.put(c, 'native:ownership:smoothers', dict(owner='compass', previous_sender_paused=True,
            accepted_at=now - 90000, effective_from=now - 100000, epoch='test'))


def ok_client(sent):
    def handler(req):
        sent.append(req.content.decode())
        return httpx.Response(200, json={'id': '4242'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def signal(sig, ticker, contract, now, **over):
    p = dict(id=sig, ticker=ticker, contract=contract, entry_premium=6.44,
             target_price=251.10, entry_price=247.10, week='2026-10-26', status='OPEN',
             model_entry_time=now - 3600, quote={'bid': 6.40, 'ask': 6.48, 'quote_at_ms': now * 1000})
    p.update(over)
    return p


CRM_CONTRACT = {'symbol': 'CRM261030C00247500', 'underlying': 'CRM',
                'expiration': '2026-10-30', 'type': 'CALL', 'strike': 247.5}
PANW_CONTRACT = {'symbol': 'PANW261030C00392500', 'underlying': 'PANW',
                 'expiration': '2026-10-30', 'type': 'CALL', 'strike': 392.5}
META_CONTRACT = {'symbol': 'META261030P00635000', 'underlying': 'META',
                 'expiration': '2026-10-30', 'type': 'PUT', 'strike': 635}


def rows_by_key(db):
    with db.tx() as c:
        return {r.event_key: r for r in c.execute(select(outbox)).mappings()}


def test_batch_only_position_target_exit_delivers(db):
    # Mirrors CRM on 2026-10-26: featured, traded, but not in BEST_10, so no
    # detailed entry publication was ever queued — only the compact batch,
    # which creates no publication link. Its target exit must still alert.
    own(db, NOW)
    p = signal('sig-crm', 'CRM', CRM_CONTRACT, NOW, status='WIN',
               exit_underlying=251.44, exit_quote={'bid': 8.10, 'ask': 8.30, 'quote_at_ms': NOW * 1000})
    with db.tx() as c:
        queue_signal(db, c, p, 'target', {'content': 'legacy'}, NOW, NOW)
    sent = []
    with ok_client(sent) as client:
        assert deliver_one(db, client, HOOKS, True, NOW + 1)
    row = rows_by_key(db)['sig-crm:target']
    assert row['status'] == 'delivered'
    assert row['delivery']['attempts'] == 1
    assert row['delivery']['publication']['event'] == 'EXIT'
    assert row['delivery']['publication']['exit_admitted_without_entry'] is True
    assert len(sent) == 1 and 'SMOOTHERS | EXIT | CRM 247.5 CALL' in sent[0] and 'Target hit' in sent[0]


def test_missing_intent_position_friday_close_delivers(db):
    # Mirrors PANW resolving at the Friday close: no entry intent at all, and
    # the contract expires that same day (classification at exit time would
    # call it 0DTE — the alert must still be a Smoothers exit).
    own(db, FRI)
    p = signal('sig-panw', 'PANW', PANW_CONTRACT, FRI, status='LOSS', week='2026-10-26',
               entry_premium=12.94, target_price=397.24, entry_price=392.10,
               exit_underlying=390.10, exit_quote={'bid': 9.0, 'ask': 9.4, 'quote_at_ms': FRI * 1000})
    with db.tx() as c:
        queue_signal(db, c, p, 'close', {'content': 'legacy'}, FRI, FRI)
    sent = []
    with ok_client(sent) as client:
        assert deliver_one(db, client, HOOKS, True, FRI + 1)
    row = rows_by_key(db)['sig-panw:close']
    assert row['status'] == 'delivered'
    assert row['delivery']['publication']['category'] == 'smoothers'
    assert row['delivery']['publication']['exit_admitted_without_entry'] is True
    assert len(sent) == 1 and 'SMOOTHERS | EXIT | PANW 392.5 CALL' in sent[0] and 'Week closed' in sent[0]


def test_best10_entry_then_exit_still_delivers_without_flag(db):
    # The pre-existing path is untouched: a detailed entry is admitted and
    # delivered (entries unchanged), and its exit follows the link with no
    # exit-admission flag involved.
    own(db, NOW)
    p = signal('sig-meta', 'META', META_CONTRACT, NOW, entry_premium=13.00,
               target_price=620.00, entry_price=640.00)
    with db.tx() as c:
        queue_signal(db, c, p, 'entry', {'content': 'legacy'}, NOW, NOW)
    sent = []
    with ok_client(sent) as client:
        assert deliver_one(db, client, HOOKS, True, NOW + 1)
        p.update(status='WIN', exit_underlying=619.50,
                 exit_quote={'bid': 20.0, 'ask': 20.4, 'quote_at_ms': (NOW + 60) * 1000})
        with db.tx() as c:
            queue_signal(db, c, p, 'target', {'content': 'legacy'}, NOW + 60, NOW + 60)
        assert deliver_one(db, client, HOOKS, True, NOW + 61)
    rows = rows_by_key(db)
    assert rows['sig-meta:entry']['status'] == 'delivered'
    assert rows['sig-meta:target']['status'] == 'delivered'
    assert 'exit_admitted_without_entry' not in rows['sig-meta:target']['delivery']['publication']
    assert 'SMOOTHERS | ENTRY | META 635 PUT' in sent[0]
    assert 'SMOOTHERS | EXIT | META 635 PUT' in sent[1]


def test_assess_admits_smoothers_exit_without_entry_link(db):
    pub = dict(id='sig-mu', project='smoothers', contract={'symbol': 'MU261030C00890000'},
               track='swing', status='native_option_exit', entry=26.45, underlying_target=900.87,
               quote=dict(bid=31.25, ask=31.55, ts=NOW), exit_reason='target', outcome='unresolved',
               signal_status='WIN', exit_underlying=901.53)
    record = dict(id='native:row1', source='smoothers', symbol='MU261030C00890000', ts=NOW, payload=pub)
    with db.tx() as c:
        d = assess(db, c, record, NOW)
        assert d['action'] == 'publish' and d['event'] == 'EXIT' and d['category'] == 'smoothers'
        assert d['exit_admitted_without_entry'] is True
        _, trade = read_trade(db, c, d['trade_id'])
        assert trade['category'] == 'smoothers' and trade['state'] == 'unresolved'


def test_other_program_exit_without_entry_still_research(db):
    # The published-entry requirement is only lifted for the smoothers
    # program; any other native exit with no entry lifecycle stays research.
    pub = dict(id='m1', status='native_option_exit', contract={'symbol': 'QQQ261002C00500000'},
               track='swing', quote=dict(bid=2, ask=2.1, ts=NOW), exit_reason='target', outcome='unresolved')
    record = dict(id='native:row2', source='morning', symbol='QQQ261002C00500000', ts=NOW, payload=pub)
    with db.tx() as c:
        d = assess(db, c, record, NOW)
    assert d['action'] == 'research' and d['reason'] == 'No published entry for this lifecycle'
