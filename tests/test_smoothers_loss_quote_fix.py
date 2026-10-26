"""LOSS/UNRESOLVED resolutions must capture an exit quote, and the Simple
headline must count WIN/LOSS by status (Claude §6, Josh 2026-10-26).

Old behavior: native_smoothers fetched exit_quote only on the target-touch
(WIN) path, so Friday LOSS/UNRESOLVED resolutions carried net_pnl=None and
simple.py dropped them silently — the public win rate was
(pnl>0 wins) / (measured picks), e.g. 98.1% with every loss invisible.
New behavior: the closure fetches the quote at resolution time with the
WIN path's semantics; the headline counts delivered resolved picks by
status, keeps option P&L as a labeled measured subset (with its n), and
tallies resolved-but-unmeasured picks as 'excluded', never dropped.
"""
import time

import pytest
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass.store import Store
from compass import simple
from compass.native_smoothers import VERSION, weekly, save, monitor
from compass.native_outbox import outbox as native_outbox
from compass.smoothers_handoff import sessions
from compass.smoothers_scorecard import option_result

WEEK = '2026-09-14'
CAL = sessions(WEEK)
CLOSE = CAL[-1]['close']
ENTRY = CAL[0]['open'] + 3600
CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
            'strike': 102, 'expiration': '2026-09-18'}


def _q(bid, ask, at_s):
    return dict(status='available', bid=bid, ask=ask,
                quote_at_ms=int(at_s * 1000), quote_age_seconds=1,
                contract=CONTRACT)


def _open_pick(**over):
    p = dict(id='x', week=WEEK, ticker='DEMO', status='OPEN',
             direction='CALL', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, created_at=ENTRY,
             last_seen_at=CLOSE - 60, last_underlying=99.0,
             contract=dict(CONTRACT), quote=_q(1.9, 2.0, ENTRY))
    p.update(over)
    return p


def _seed(db, pick):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        save(db, c, pick)


def _stored(db, pid='x'):
    with db.tx() as c:
        return c.execute(select(weekly.c.payload)
                         .where(weekly.c.id == pid)).scalar_one()


class _Provider:
    """quote() only: at close+600 no session is active, so monitor never
    asks for bars on this path — it goes straight to the closure block."""
    def __init__(self, result):
        self.result = result
        self.calls = []

    def quote(self, contract):
        self.calls.append(contract)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ---------------------------------------------------------------- monitor

def test_loss_resolution_fetches_exit_quote(db):
    _seed(db, _open_pick())
    provider = _Provider(_q(0.4, 0.5, CLOSE + 300))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'LOSS'
    assert provider.calls == [CONTRACT]
    assert saved['exit_quote']['bid'] == 0.4
    # The captured quote makes the loss measurable: (0.4-2.0)*100 - 1.32.
    assert option_result(saved)['net_pnl'] == -161.32
    with db.tx() as c:
        keys = list(c.execute(select(native_outbox.c.event_key)).scalars())
    assert 'x:close' in keys


def test_unresolved_resolution_also_fetches_exit_quote(db):
    # last print is not the final minute -> UNRESOLVED, but the quote is
    # still captured at resolution with the same semantics.
    _seed(db, _open_pick(last_seen_at=CLOSE - 120))
    provider = _Provider(_q(0.4, 0.5, CLOSE + 300))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'UNRESOLVED'
    assert provider.calls == [CONTRACT]
    assert saved['exit_quote']['bid'] == 0.4


def test_loss_resolution_quote_failure_still_resolves_loss(db):
    _seed(db, _open_pick())
    provider = _Provider(RuntimeError('provider down'))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'LOSS'
    assert saved['exit_quote'] == {'status': 'unavailable',
                                   'reason': 'RuntimeError'}
    assert option_result(saved)['net_pnl'] is None


def test_loss_resolution_without_contract_records_unavailable(db):
    _seed(db, _open_pick(contract=None))
    provider = _Provider(_q(0.4, 0.5, CLOSE + 300))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'LOSS'
    assert provider.calls == []
    assert saved['exit_quote'] == {'status': 'unavailable',
                                   'reason': 'no_contract'}


# ----------------------------------------------------------------- simple

NOW = time.time()


@pytest.fixture
def sdb(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'simple-loss.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _payload(net_pnl, resolved_at, *, status='WIN', exit_quote='ok'):
    exit_bid = round((net_pnl + 1.32) / 100 + 2.0, 2)
    contract = {'symbol': 'DEMO260925C00100000', 'multiplier': 100}

    def q(bid, ask, at_ms):
        return dict(status='available', bid=bid, ask=ask, quote_at_ms=at_ms,
                    quote_age_seconds=1, contract=contract)
    p = dict(ticker='DEMO', status=status, contract=contract,
             resolution_time=resolved_at,
             quote=q(1.9, 2.0, int(resolved_at * 1000) - 60000))
    if exit_quote == 'ok':
        p['exit_quote'] = q(exit_bid, exit_bid + 0.1,
                            int(resolved_at * 1000))
    elif exit_quote == 'unavailable':
        p['exit_quote'] = {'status': 'unavailable', 'reason': 'no_contract'}
    return p


def _realized(net_pnl):
    exit_bid = round((net_pnl + 1.32) / 100 + 2.0, 2)
    return round((exit_bid - 2.0) * 100 - 1.32, 2)


def _insert(sdb, c, sid, week, status, payload):
    c.execute(sdb.insert(weekly).values(
        id=sid, week=week, ticker='DEMO', status=status, payload=payload))


def test_headline_counts_status_and_tallies_excluded(sdb):
    with sdb.tx() as c:
        _insert(sdb, c, 'w1', '2026-10-05', 'WIN',
                _payload(50.0, NOW, status='WIN'))
        # The historical shape of the bug: a LOSS with no exit quote.
        _insert(sdb, c, 'l1', '2026-09-28', 'LOSS',
                _payload(-30.0, NOW, status='LOSS', exit_quote='missing'))
        _insert(sdb, c, 'w2', '2026-09-21', 'WIN',
                _payload(10.0, NOW, status='WIN',
                         exit_quote='unavailable'))
        _insert(sdb, c, 'u1', '2026-09-14', 'UNRESOLVED',
                _payload(0.0, NOW, status='UNRESOLVED',
                         exit_quote='missing'))
        _insert(sdb, c, 'o1', '2026-10-12', 'OPEN',
                _payload(0.0, NOW, status='OPEN', exit_quote='missing'))
        out = simple.summary(sdb, c, NOW)
    st = out['types']['smoothers']['stats']
    # Win rate is WIN / (WIN+LOSS) by status over resolved picks.
    assert st['tracked'] == 3 and st['wins'] == 2
    assert st['win_rate'] == 66.7
    assert st['target_hits'] == 2
    # P&L is the measured subset, with its n.
    assert st['measured'] == 1 and st['pnl'] == _realized(50.0)
    # Resolved-but-unmeasured picks are tallied, not dropped: the
    # quoteless LOSS, the unavailable-quote WIN, and the UNRESOLVED.
    assert st['excluded'] == 3 and st['unresolved'] == 1
    drill = {d['name']: d['stats']
             for d in out['types']['smoothers']['drill']}
    assert drill['DEMO']['tracked'] == 3
    assert drill['DEMO']['excluded'] == 3


def test_status_win_with_negative_option_pnl_counts_as_win(sdb):
    # Target hit whose option still lost money: the underlying outcome
    # and the option P&L are separate facts; the win rate tracks the
    # former, the measured subset reports the latter.
    with sdb.tx() as c:
        _insert(sdb, c, 'w1', '2026-10-05', 'WIN',
                _payload(-20.0, NOW, status='WIN'))
        out = simple.summary(sdb, c, NOW)
    st = out['types']['smoothers']['stats']
    assert st['tracked'] == 1 and st['wins'] == 1
    assert st['win_rate'] == 100.0
    assert st['measured'] == 1 and st['pnl'] == _realized(-20.0)
    assert st['excluded'] == 0


def test_excluded_population_is_delivered_picks_only(sdb):
    # The Oct 14 delivered-alert rule still bounds the population: an
    # undelivered pick is neither in the headline nor in 'excluded';
    # a measured undelivered pick lands in the undelivered bucket.
    from compass.native_outbox import outbox as ob
    from compass.store import identity
    cutoff = NOW - 30 * 86400
    post = NOW - 7 * 86400
    with sdb.tx() as c:
        sdb.put(c, 'native:ownership:smoothers', dict(
            owner='compass', previous_sender_paused=True,
            accepted_at=cutoff - 100, epoch='epoch-1',
            effective_from=cutoff, effective_week='2026-09-14'))
        p1 = _payload(-30.0, NOW, status='LOSS', exit_quote='missing')
        p1.update(id='l1', week='2026-10-12', model_entry_time=post,
                  created_at=post)
        _insert(sdb, c, 'l1', '2026-10-12', 'LOSS', p1)
        p2 = _payload(50.0, NOW, status='WIN')
        p2.update(id='w1', week='2026-10-12', model_entry_time=post,
                  created_at=post)
        _insert(sdb, c, 'w1', '2026-10-12', 'WIN', p2)
        for key, status in (('l1:entry', 'failed'), ('w1:entry', 'failed')):
            c.execute(sdb.insert(ob).values(
                id=identity('native-outbox-v1', 'smoothers', key),
                program='smoothers', event_key=key, created=NOW - 3600,
                status=status, payload={'content': 'alert'},
                delivery={'attempts': 0}))
        out = simple.summary(sdb, c, NOW)
    st = out['types']['smoothers']['stats']
    assert st['tracked'] == 0 and st['excluded'] == 0
    assert st['undelivered'] == 1
    assert st['undelivered_pnl'] == _realized(50.0)
