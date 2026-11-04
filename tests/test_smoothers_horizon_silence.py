"""Wednesday-horizon push silence (Josh, 2026-11-04).

The Wednesday horizon is a measurement — whether holding to Wednesday
beats exiting earlier — and its per-pick WEDNESDAY HORIZON notices are
bookkeeping, not alerts. Josh: "it didn't require discord alerts.
Those [the normal exits] can remain the normal exits until we have
clarification on how long to hold."

Pins:
- score_horizon still scores and stores the horizon outcome exactly
  as before (scored and unavailable flavors), and the notice row is
  still written to the outbox — but as 'suppressed' under gate
  wednesday-horizon-silence-v1, payload untouched;
- a horizon row queued before the gate existed (status 'pending')
  never drains: deliver_one terminally resolves it as suppressed
  under the same gate, while a normal Smoothers row queued beside it
  delivers normally;
- the gate is scoped to this producer only: other smoothers event
  keys and other programs are not matched;
- the full Wednesday measurement still lands with the gate on:
  horizon + Tuesday-entry cell + cohort-exit cell stamps on the pick
  and both weekly summary state rows, with the suppressed horizon
  row the only outbox row the passes produce.
"""
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from test_projects import db  # noqa: F401  (shared Store fixture)
from compass import smoothers_messages as messages
from compass.native_outbox import (outbox as native_outbox, queue,
                                   deliver_one, wednesday_horizon_row,
                                   WEDNESDAY_HORIZON_GATE)
from compass.native_smoothers import VERSION, weekly, save, monitor
from compass.smoothers_handoff import sessions
from compass.chain_eod import event_key as chain_event_key

WEEK = '2026-09-14'
CAL = sessions(WEEK)
MON, TUE, WED = CAL[0], CAL[1], CAL[2]
ENTRY = CAL[0]['open'] + 3600
CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
            'strike': 102, 'expiration': '2026-09-18', 'type': 'CALL'}
NOW = datetime(2026, 9, 18, 20, 5, tzinfo=timezone.utc).timestamp()
URL = 'https://discord.com/api/webhooks/123/test'


def _quote(bid, ask):
    return dict(status='available', bid=bid, ask=ask,
                midpoint=(bid + ask) / 2, quote_at_ms=int(ENTRY * 1000))


class _Provider:
    def quote(self, contract):
        return dict(status='available', bid=0.4, ask=0.5,
                    quote_at_ms=int((WED['close'] + 300) * 1000))


def _open_pick(**over):
    p = dict(id='DEMO', week=WEEK, ticker='DEMO', status='OPEN',
             direction='CALL', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, created_at=ENTRY,
             last_seen_at=WED['close'] - 60, last_underlying=99.0,
             contract=dict(CONTRACT), quote=_quote(1.9, 2.0))
    p.update(over)
    return p


def _seed(db, pick, chains):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        save(db, c, pick)
        for date, contracts in chains.items():
            close = next(s['close'] for s in CAL if s['date'] == date)
            db.append(c, 'chain_eod', 'massive', pick['ticker'], close + 600,
                      {'date': date, 'symbol': pick['ticker'],
                       'contracts': contracts},
                      key=chain_event_key(date, pick['ticker']))


def _row(symbol, bid, ask):
    return {'symbol': symbol, 'quote': {'bid': bid, 'ask': ask}}


def _stored(db, pid='DEMO'):
    with db.tx() as c:
        return c.execute(select(weekly.c.payload)
                         .where(weekly.c.id == pid)).scalar_one()


def _outbox_rows(db):
    with db.tx() as c:
        return {r['event_key']: r for r in
                c.execute(select(native_outbox)).mappings()}


# ------------------------------------------------------- enqueue gate

def test_horizon_notice_queues_suppressed_scored_flavor(db):
    _seed(db, _open_pick(), {'2026-09-16': [_row(CONTRACT['symbol'], 2.50, 2.60)]})
    monitor(db, _Provider(), WED['close'] + 600)
    saved = _stored(db)
    # Scoring is byte-identical: premium fixed at the horizon, pick
    # itself stays OPEN for Thu/Fri touch statistics.
    assert saved['status'] == 'OPEN'
    h = saved['horizon']
    assert h['status'] == 'scored' and h['chain_bid'] == 2.50
    assert h['premium_net'] == 48.68  # (2.50-2.00)*100 - 1.32
    rows = _outbox_rows(db)
    assert set(rows) == {'DEMO:horizon'}
    row = rows['DEMO:horizon']
    assert row['status'] == 'suppressed'
    assert row['delivery']['gate'] == WEDNESDAY_HORIZON_GATE
    assert row['delivery']['gate'] == 'wednesday-horizon-silence-v1'
    # The notice itself is still recorded, payload untouched.
    assert row['payload'] == messages.horizon(saved)
    assert row['payload']['content'].startswith('WEDNESDAY HORIZON — DEMO 102C')


def test_horizon_notice_queues_suppressed_unavailable_flavor(db):
    _seed(db, _open_pick(), {'2026-09-16': [_row('OTHER261016C00100000', 1.0, 1.1)]})
    monitor(db, _Provider(), WED['close'] + 600)
    h = _stored(db)['horizon']
    assert h['status'] == 'unavailable'
    assert h['reason'] == 'contract_not_in_wednesday_chain'
    row = _outbox_rows(db)['DEMO:horizon']
    assert row['status'] == 'suppressed'
    assert row['delivery']['gate'] == WEDNESDAY_HORIZON_GATE
    assert 'premium outcome unmeasurable (contract_not_in_wednesday_chain)' \
        in row['payload']['content']


# -------------------------------------------- measurement still lands

def test_wednesday_stamps_land_with_gate_on(db):
    _seed(db, _open_pick(), {
        '2026-09-14': [_row(CONTRACT['symbol'], 1.70, 1.80)],
        '2026-09-15': [_row(CONTRACT['symbol'], 2.00, 2.10)],
        '2026-09-16': [_row(CONTRACT['symbol'], 2.50, 2.60)]})
    monitor(db, _Provider(), WED['close'] + 600)
    saved = _stored(db)
    assert saved['horizon']['status'] == 'scored'
    tue = saved['tuesday_cell']
    assert tue['status'] == 'scored'
    assert tue['monday_net'] == 48.68 and tue['tuesday_net'] == 38.68
    cohort = saved['cohort_exit']
    assert cohort['status'] == 'scored' and cohort['cohort'] == 'shallow'
    assert cohort['baseline_net'] == 48.68 and cohort['delta_net'] == 0
    with db.tx() as c:
        tue_summary = db.get(c, VERSION + ':week:' + WEEK + ':tuesday_cell', {})
        cohort_summary = db.get(c, VERSION + ':week:' + WEEK + ':cohort_exit', {})
    assert tue_summary['scored'] == 1
    assert cohort_summary['scored'] == 1
    assert cohort_summary['identity_ok'] is True
    # The cells queue nothing; the suppressed horizon row is the only
    # outbox row the Wednesday passes produce.
    rows = _outbox_rows(db)
    assert set(rows) == {'DEMO:horizon'}
    assert rows['DEMO:horizon']['status'] == 'suppressed'


# ------------------------------------------------- send-time backlog

def _own(db, c, program='smoothers', epoch='e1', at=NOW):
    db.put(c, 'native:ownership:' + program,
           dict(owner='compass', previous_sender_paused=True,
                accepted_at=at, epoch=epoch))


def _client(sent):
    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={'id': '12345'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pre_gate_pending_horizon_row_never_drains(db):
    with db.tx() as c:
        _own(db, c)
        # Queued the pre-gate way (no suppress_gate): lands 'pending'.
        queue(db, c, 'smoothers', 'DEMO:horizon',
              {'content': 'WEDNESDAY HORIZON — DEMO 102C'}, NOW,
              event_time=NOW, cohort_time=NOW)
        queue(db, c, 'smoothers', 'DEMO:close',
              {'content': 'WEEK CLOSED — DEMO'}, NOW,
              event_time=NOW, cohort_time=NOW)
    rows = _outbox_rows(db)
    assert rows['DEMO:horizon']['status'] == 'pending'
    sent = []
    with _client(sent) as client:
        for i in range(4):
            deliver_one(db, client, {'smoothers': URL}, True, NOW + 2 + i)
    # Only the normal close alert was sent; the horizon notice never
    # reached the webhook and is terminally suppressed, gate on record.
    assert len(sent) == 1 and b'WEEK CLOSED' in sent[0].content
    rows = _outbox_rows(db)
    assert rows['DEMO:close']['status'] == 'delivered'
    horizon = rows['DEMO:horizon']
    assert horizon['status'] == 'suppressed'
    assert horizon['delivery']['gate'] == WEDNESDAY_HORIZON_GATE


# ------------------------------------------------------------ scoping

def test_gate_scoped_to_the_horizon_producer(db):
    assert wednesday_horizon_row('smoothers', 'DEMO:horizon')
    assert wednesday_horizon_row('smoothers_shared', 'DEMO:horizon')
    assert not wednesday_horizon_row('smoothers', 'DEMO:close')
    assert not wednesday_horizon_row('smoothers', 'DEMO:target')
    assert not wednesday_horizon_row('smoothers', '2026-09-14:summary')
    assert not wednesday_horizon_row('morning', 'DEMO:horizon')
    # Ungated smoothers rows keep the pre-gate behavior exactly:
    # pending under ownership, shadow without it.
    with db.tx() as c:
        _own(db, c)
        queue(db, c, 'smoothers', 'DEMO:target', {'content': 'hit'}, NOW,
              event_time=NOW, cohort_time=NOW)
    assert _outbox_rows(db)['DEMO:target']['status'] == 'pending'
