"""Josh 2026-10-26: rich embed format for Smoothers entries and exits —
but only the information the compact text renderers currently show.

Pins format_payload(): ENTRY / :target / :close publications render as
Discord embeds (username 'Smoothers', legacy layout colors) whose field set
is exactly the current _format_smoothers_entry/_exit field set — no
legacy-only fields (touch-bar OHLC, entry return-vs-entry, signal numbers,
footer/disclaimer text). Non-Smoothers and non-entry/exit publications keep
the text body. Gate behavior is pinned separately in
tests/test_smoothers_exit_alerts.py and is unaffected.
"""
import json
import httpx
from datetime import datetime
from sqlalchemy import select
from test_projects import db
from compass.alert_format import CT
from compass.alert_ownership import format_payload
from compass.native_outbox import outbox, deliver_one
from compass.native_smoothers import queue_signal

T0 = int(datetime(2026, 10, 5, 9, 0, tzinfo=CT).timestamp())
QTS = int(datetime(2026, 10, 9, 10, 15, tzinfo=CT).timestamp())
HOOKS = {'smoothers': 'https://discord.com/api/webhooks/123/test'}

META_C = dict(symbol='O:META261007C00745000', underlying='META', expiration='2026-10-07', type='CALL', strike=745)
COP_C = dict(symbol='O:COP261009C00127000', underlying='COP', expiration='2026-10-09', type='CALL', strike=127)


def row(payload, pub):
    return dict(id='native:xyz', source='smoothers', symbol=pub['contract']['symbol'], ts=T0,
                payload={**payload, 'publication': pub})


def entry_row(contract=None, **over):
    contract = contract or META_C
    payload = dict(id='sig1', project='smoothers', contract=contract, track='swing', status='native_option_entry',
                   entry=7.88, underlying_target=751.61, entry_price=744.17, target_price=751.61,
                   est_return_pct=53.2, week='2026-10-05', model_entry_time=T0 + 1800,
                   quote=dict(bid=7.80, ask=7.95, ts=T0), expires_at=T0 + 1800,
                   exit_rule='Underlying target, otherwise Friday close; no premium stop configured')
    payload.update(over)
    pub = dict(trade_id='abc123', category='smoothers', event='ENTRY', contract=contract, horizon='swing')
    return row(payload, pub)


def exit_row(**over):
    payload = dict(id='sig2', project='smoothers', contract=COP_C, track='swing', status='native_option_exit',
                   entry=2.01, underlying_target=128.80, entry_price=124.50, target_price=128.80,
                   quote=dict(bid=2.72, ask=3.30, ts=QTS), exit_reason='target', signal_status='WIN',
                   exit_underlying=128.80,
                   exit_rule='Underlying target, otherwise Friday close; no premium stop configured')
    payload.update(over)
    pub = dict(trade_id='def456', category='smoothers', event='EXIT', contract=COP_C, horizon='swing')
    return row(payload, pub)


def fields_of(body):
    embed = body['embeds'][0]
    return embed, [(f['name'], f['value']) for f in embed['fields']]


def test_entry_embed_exact_current_fields():
    body = format_payload(entry_row())
    assert body['username'] == 'Smoothers' and 'content' not in body
    embed, fields = fields_of(body)
    assert embed['title'] == 'SMOOTHERS | ENTRY | META 745 CALL · 2026-10-07'
    assert embed['color'] == 3066993
    assert fields == [
        ('Underlying Entry', '$744.17 (ref 09:30 CT)'),
        ('Underlying Target', '$751.61'),
        ('Est Return', '+53%'),
        ('Option Quote', '$7.80/$7.95'),
        ('Strike', '$745'),
        ('DTE', '3'),
        ('Enter By', '09:30 CT'),
        ('Exit', 'Underlying target, otherwise Friday close; no premium stop configured'),
    ]
    assert embed['fields'][-1]['inline'] is False
    assert 'footer' not in embed and 'description' not in embed


def test_entry_embed_put_color_and_live_underlying():
    put = {**META_C, 'symbol': 'O:META261007P00745000', 'type': 'PUT'}
    body = format_payload(entry_row(contract=put))
    embed, _ = fields_of(body)
    assert embed['color'] == 15158332
    assert embed['title'] == 'SMOOTHERS | ENTRY | META 745 PUT · 2026-10-07'
    body = format_payload(entry_row(live_underlying=dict(status='available', price=750.25, ts=T0 + 2700)))
    embed, fields = fields_of(body)
    assert ('Live Underlying', '$750.25 @ 09:45 CT') in fields
    assert fields.index(('Live Underlying', '$750.25 @ 09:45 CT')) == 2


def test_entry_embed_no_quote_and_missing_optionals():
    body = format_payload(entry_row(quote={}, entry_price=None, target_price=None, underlying_target=None,
                                    est_return_pct=None, week=None, expires_at=None, exit_rule=None))
    embed, fields = fields_of(body)
    assert fields == [
        ('Option Quote', 'quote unavailable'),
        ('Strike', '$745'),
        ('Exit', 'Underlying target, otherwise Friday close'),
    ]


def test_exit_target_embed_exact_current_fields():
    body = format_payload(exit_row())
    assert body['username'] == 'Smoothers' and 'content' not in body
    embed, fields = fields_of(body)
    assert embed['title'] == 'SMOOTHERS | EXIT | COP 127 CALL · 2026-10-09'
    assert embed['color'] == 2067276
    assert fields == [
        ('Result', 'Target hit · underlying reached $128.80'),
        ('Option Quote', '$2.72/$3.30 @ 10:15 CT'),
        ('Modeled Entry', '$2.01 (+50% ref)'),
    ]


def test_exit_target_embed_no_quote():
    body = format_payload(exit_row(quote={}))
    embed, fields = fields_of(body)
    assert fields == [
        ('Result', 'Target hit · underlying reached $128.80'),
        ('Option Quote', 'unavailable'),
    ]


def test_exit_close_embed_loss_and_unresolved():
    body = format_payload(exit_row(exit_reason='close', signal_status='LOSS', exit_underlying=124.10, quote={}))
    embed, fields = fields_of(body)
    assert embed['color'] == 10038562
    assert fields == [
        ('Result', 'Week closed · target $128.80 not hit'),
        ('Underlying at Close', '$124.10'),
    ]
    body = format_payload(exit_row(exit_reason='close', signal_status='UNRESOLVED', exit_underlying=None, quote={}))
    embed, fields = fields_of(body)
    assert fields == [('Result', 'Week closed · outcome unresolved (missing observations)')]


def test_non_smoothers_and_non_entry_exit_keep_text_body():
    r = exit_row()
    r['payload']['publication'] = dict(trade_id='x', category='smoothers', event='DATA GAP',
                                       contract=COP_C, horizon='swing')
    body = format_payload(r)
    assert 'content' in body and 'embeds' not in body and body['username'] == 'Market Compass'


def test_delivered_exit_is_embed_end_to_end(db):
    # Gate fix + format change together: a batch-only position's target exit
    # now delivers, and the wire body is the rich embed, not text.
    now = 1790400000
    with db.tx() as c:
        db.put(c, 'native:ownership:smoothers', dict(owner='compass', previous_sender_paused=True,
               accepted_at=now - 90000, effective_from=now - 100000, epoch='test'))
        p = dict(id='sig-crm', ticker='CRM',
                 contract={'symbol': 'CRM261030C00247500', 'underlying': 'CRM',
                           'expiration': '2026-10-30', 'type': 'CALL', 'strike': 247.5},
                 entry_premium=6.44, target_price=251.10, entry_price=247.10, week='2026-10-26',
                 status='WIN', model_entry_time=now - 3600, exit_underlying=251.44,
                 exit_quote={'bid': 8.10, 'ask': 8.30, 'quote_at_ms': now * 1000})
        queue_signal(db, c, p, 'target', {'content': 'legacy'}, now, now)
    sent = []
    def handler(req):
        sent.append(req.content.decode())
        return httpx.Response(200, json={'id': '4242'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db, client, HOOKS, True, now + 1)
    with db.tx() as c:
        assert c.execute(select(outbox.c.status)).scalar_one() == 'delivered'
    body = json.loads(sent[0])
    assert body['username'] == 'Smoothers' and 'content' not in body
    embed = body['embeds'][0]
    assert embed['title'] == 'SMOOTHERS | EXIT | CRM 247.5 CALL · 2026-10-30'
    assert ('Result', 'Target hit · underlying reached $251.10') in [(f['name'], f['value']) for f in embed['fields']]
