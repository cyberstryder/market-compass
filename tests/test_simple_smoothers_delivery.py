"""Smoothers stats count only delivered alerts (Josh 2026-10-14, audit #6).

The receipt of record is the native outbox: a per-signal ':entry' row
for individually alerted picks, or the weekly compact batch naming the
ticker. Measured picks whose entry alert never delivered are excluded
from the headline stats and bucketed as 'undelivered'; pre-handoff
cohorts (original sender, delivery unrecorded here) keep counting.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from compass.store import Store, identity
from compass import simple
from compass.native_smoothers import weekly as smoothers_weekly
from compass.native_outbox import outbox as native_outbox

CT = ZoneInfo("America/Chicago")
NOW = time.time()
CUTOFF = NOW - 30 * 86400  # handoff effective_from
POST = NOW - 7 * 86400     # a native-era cohort time
PRE = NOW - 60 * 86400     # a pre-handoff cohort time


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'simple-delivery.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _payload(net_pnl, resolved_at, *, sid, week, ticker, status='WIN',
             tier=None, cohort=None):
    # Same quote construction as test_simple: entry ask 2.0, exit bid
    # chosen so net = (bid-2.0)*100 - 1.32 (realized value differs by
    # rounding; _realized() gives the exact figure).
    exit_bid = round((net_pnl + 1.32) / 100 + 2.0, 2)
    contract = {'symbol': 'DEMO260925C00100000', 'multiplier': 100}

    def q(bid, ask, at_ms):
        return dict(status='available', bid=bid, ask=ask, quote_at_ms=at_ms,
                    quote_age_seconds=1, contract=contract)
    p = dict(id=sid, week=week, ticker=ticker, status=status,
             contract=contract, resolution_time=resolved_at,
             quote=q(1.9, 2.0, int(resolved_at * 1000) - 60000),
             exit_quote=q(exit_bid, exit_bid + 0.1, int(resolved_at * 1000)),
             model_entry_time=cohort, created_at=cohort)
    if tier:
        p['quality_tier'] = tier
    return p


def _realized(net_pnl):
    exit_bid = round((net_pnl + 1.32) / 100 + 2.0, 2)
    return round((exit_bid - 2.0) * 100 - 1.32, 2)


def _signal(db, c, sid, week, ticker, payload, status='WIN'):
    c.execute(db.insert(smoothers_weekly).values(
        id=sid, week=week, ticker=ticker, status=status, payload=payload))


def _own(db, c):
    db.put(c, 'native:ownership:smoothers', dict(
        owner='compass', previous_sender_paused=True,
        accepted_at=CUTOFF - 100, epoch='epoch-1',
        effective_from=CUTOFF, effective_week='2026-09-14'))


def _outbox(db, c, event_key, status, payload=None, program='smoothers'):
    c.execute(db.insert(native_outbox).values(
        id=identity('native-outbox-v1', program, event_key),
        program=program, event_key=event_key, created=NOW - 3600,
        status=status, payload=payload or {'content': 'alert'},
        delivery={'attempts': 0}))


def test_delivered_entry_counts_failed_entry_bucketed(db):
    with db.tx() as c:
        _own(db, c)
        _signal(db, c, 'a1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW, sid='a1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        _signal(db, c, 'b1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW, sid='b1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        _outbox(db, c, 'a1:entry', 'delivered')
        _outbox(db, c, 'b1:entry', 'failed')
        out = simple.summary(db, c, NOW)
        cal = simple.calendar(db, c, NOW,
                              datetime.fromtimestamp(NOW, CT).year,
                              datetime.fromtimestamp(NOW, CT).month)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1 and sm['stats']['wins'] == 1
    assert sm['stats']['pnl'] == _realized(50.0)
    assert sm['stats']['target_hits'] == 1  # b1 is also a WIN; excluded
    assert sm['stats']['undelivered'] == 1
    assert sm['stats']['undelivered_pnl'] == _realized(50.0)
    drill = {d['name']: d['stats'] for d in sm['drill']}
    assert drill['DEMO']['tracked'] == 1
    assert drill['DEMO']['undelivered'] == 1
    assert drill['DEMO']['undelivered_pnl'] == _realized(50.0)
    # The P&L calendar is part of the stats: undelivered P&L stays off it.
    day = datetime.fromtimestamp(NOW, CT).date().isoformat()
    assert cal['days'][day]['smoothers'] == {'pnl': _realized(50.0), 'count': 1}


def test_post_cutoff_missing_record_is_undelivered(db):
    with db.tx() as c:
        _own(db, c)
        _signal(db, c, 'm1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW, sid='m1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 0
    assert sm['stats']['undelivered'] == 1


def test_compact_batch_is_the_entry_alert_for_non_featured_picks(db):
    content = ("SMOOTHERS | SIGNALS (1/1) | 2026-10-12\n"
               "GSX CALL $50.00→$52.00 Q40.0 | GS PUT $100.00→$95.00 Q55.0")
    shadow = ("SMOOTHERS | SIGNALS (1/1) | 2026-10-19\n"
              "ABC CALL $20.00→$21.00 Q45.0")
    with db.tx() as c:
        _own(db, c)
        # GS is named (as its own token) in a delivered batch: counts.
        _signal(db, c, 'c1', '2026-10-12', 'GS',
                _payload(50.0, NOW, sid='c1', week='2026-10-12',
                         ticker='GS', cohort=POST))
        _outbox(db, c, '2026-10-12:compact:0', 'delivered',
                payload={'content': content})
        # ABC is named in a batch that stayed shadow: never alerted.
        _signal(db, c, 'e1', '2026-10-19', 'ABC',
                _payload(50.0, NOW, sid='e1', week='2026-10-19',
                         ticker='ABC', cohort=POST))
        _outbox(db, c, '2026-10-19:compact:0', 'shadow',
                payload={'content': shadow})
        # GS in a later week whose batch names only GSX: the substring
        # must not count as delivery for GS.
        _signal(db, c, 'g1', '2026-10-26', 'GS',
                _payload(50.0, NOW, sid='g1', week='2026-10-26',
                         ticker='GS', cohort=POST))
        _outbox(db, c, '2026-10-26:compact:0', 'delivered',
                payload={'content': "SMOOTHERS | SIGNALS (1/1) | 2026-10-26\n"
                                    "GSX CALL $50.00→$52.00 Q40.0"})
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1 and sm['stats']['wins'] == 1
    assert sm['stats']['undelivered'] == 2


def test_pre_handoff_cohorts_keep_counting(db):
    with db.tx() as c:
        _own(db, c)
        # No delivery record at all: pre-handoff delivery was the
        # original sender's and is unrecorded in this store.
        _signal(db, c, 'h1', '2026-08-31', 'DEMO',
                _payload(50.0, NOW, sid='h1', week='2026-08-31',
                         ticker='DEMO', cohort=PRE))
        # A shadow preview row is likewise not evidence of non-delivery
        # for a pre-handoff cohort.
        _signal(db, c, 'i1', '2026-08-31', 'DEMO',
                _payload(-20.0, NOW, sid='i1', week='2026-08-31',
                         ticker='DEMO', status='LOSS', cohort=PRE),
                status='LOSS')
        _outbox(db, c, 'i1:entry', 'shadow')
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 2 and sm['stats']['wins'] == 1
    assert 'undelivered' not in sm['stats']


def test_no_ownership_record_keeps_historical_basis(db):
    with db.tx() as c:
        _signal(db, c, 'j1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW, sid='j1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1
    assert 'undelivered' not in sm['stats']


def test_shared_destination_delivery_counts(db):
    with db.tx() as c:
        _own(db, c)
        _signal(db, c, 's1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW, sid='s1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        _outbox(db, c, 's1:entry', 'failed')
        _outbox(db, c, 's1:entry', 'delivered', program='smoothers_shared')
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 1
    assert 'undelivered' not in sm['stats']


def test_tiers_are_filtered_by_delivery_not_by_tier(db):
    with db.tx() as c:
        _own(db, c)
        # A+ delivered win counts; A suppressed win does not;
        # B delivered loss counts; WATCH pending win does not.
        _signal(db, c, 't1', '2026-10-12', 'AAA',
                _payload(50.0, NOW, sid='t1', week='2026-10-12',
                         ticker='AAA', tier='A+', cohort=POST))
        _outbox(db, c, 't1:entry', 'delivered')
        _signal(db, c, 't2', '2026-10-12', 'BBB',
                _payload(50.0, NOW, sid='t2', week='2026-10-12',
                         ticker='BBB', tier='A', cohort=POST))
        _outbox(db, c, 't2:entry', 'suppressed')
        _signal(db, c, 't3', '2026-10-12', 'CCC',
                _payload(-20.0, NOW, sid='t3', week='2026-10-12',
                         ticker='CCC', tier='B', status='LOSS', cohort=POST),
                status='LOSS')
        _outbox(db, c, 't3:entry', 'delivered')
        _signal(db, c, 't4', '2026-10-12', 'DDD',
                _payload(50.0, NOW, sid='t4', week='2026-10-12',
                         ticker='DDD', tier='WATCH', cohort=POST))
        _outbox(db, c, 't4:entry', 'pending')
        out = simple.summary(db, c, NOW)
    sm = out['types']['smoothers']
    assert sm['stats']['tracked'] == 2 and sm['stats']['wins'] == 1
    assert sm['stats']['pnl'] == round(_realized(50.0) + _realized(-20.0), 2)
    assert sm['stats']['undelivered'] == 2
    assert sm['stats']['undelivered_pnl'] == round(2 * _realized(50.0), 2)
    drill = {d['name']: d['stats'] for d in sm['drill']}
    assert drill['AAA']['tracked'] == 1 and drill['AAA']['wins'] == 1
    assert drill['CCC']['tracked'] == 1 and drill['CCC']['wins'] == 0
    # Tickers whose only measured picks were undelivered carry no
    # counted drill entry; the headline bucket accounts for them.
    assert 'BBB' not in drill and 'DDD' not in drill


def test_detail_views_keep_and_mark_undelivered(db):
    with db.tx() as c:
        _own(db, c)
        _signal(db, c, 'd1', '2026-10-12', 'DEMO',
                _payload(50.0, NOW - 7 * 86400, sid='d1', week='2026-10-12',
                         ticker='DEMO', cohort=POST))
        _outbox(db, c, 'd1:entry', 'delivered')
        _signal(db, c, 'd2', '2026-10-19', 'DEMO',
                _payload(50.0, NOW, sid='d2', week='2026-10-19',
                         ticker='DEMO', cohort=POST))
        _outbox(db, c, 'd2:entry', 'failed')
        hist = simple.detail(db, c, NOW, 'smoothers', 'DEMO')
        week = simple.detail(db, c, NOW, 'smoothers', 'current-week')
    # History keeps both weeks; only the undelivered one is marked.
    assert len(hist['items']) == 2
    newest, oldest = hist['items'][0], hist['items'][1]
    assert ['Alert', 'Not delivered — excluded from stats'] in newest['rows']
    assert all(row[0] != 'Alert' for row in oldest['rows'])
    # The current-week view (the undelivered week is the latest) marks
    # the pick too, without dropping it.
    assert len(week['items']) == 1
    assert ['Alert', 'Not delivered — excluded from stats'] in week['items'][0]['rows']
