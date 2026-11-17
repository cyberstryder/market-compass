"""Funnel fix 1C (2026-11-16): chain_refresh_request at pending creation.

Josh's approval 2026-11-15 on the funnel-fix design
(files/odte-funnel-fix-design-2026-11-14.md, Defect 1C): the rendezvous
request is written when a research pending is CREATED (Engine.
observe_setup), not only when a chain-blocked audit lands in the
pending's final 600s (Engine.options, 2026-11-05/12). A snapshot
pinned from before the signal can never satisfy the post-signal rule,
so the refresh must already be in flight while the pending matures
(Monday 2026-11-16 funnel read: fresh-after-signal was the largest
contract rejection, 2,276 of 7,248).

Same row shape, same TTL cap, same consumer as the audit-imminent
write; the audit-imminent path itself is pinned by
tests/test_chain_lane_fix.py and must keep passing unchanged.

Negative controls: the creation tests fail on the pre-change code
(observe_setup wrote no request); the no-duplicate test fails if the
write ever leaves the creation guard.
"""
from datetime import datetime

import pytest

from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.engine import Engine, CHAIN_REFRESH_REQUEST_TTL_S
# Importing the app registers every table-defining module before
# Store.initialize() runs create_all (the lazy-import table lesson).
from compass.app import create_app  # noqa: F401

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'odte_1c.db'))
    store.initialize()
    yield store
    store.engine.dispose()


@pytest.fixture
def cfg():
    return Config(local=True, stocks=('SPY',), futures=('MES.c.0',),
                  extra_futures=(), databento='', alpaca_key='',
                  alpaca_secret='', matrix='', massive='', discord='')


def signal(id='s1', **overrides):
    base = dict(id=id, symbol='SPY', strategy='compass-scanner-fixture',
                side='long', signal_time=NOW, signal_price=100,
                invalidation=99, track='intraday')
    base.update(overrides)
    return base


def test_pending_creation_writes_chain_refresh_request(db, cfg):
    e = Engine(db, cfg)
    with db.tx() as c:
        e.observe_setup(c, signal(), NOW)
        assert db.get(c, 'pending_research_options:s1') is not None
        row = db.get(c, 'chain_refresh_request:SPY')
    assert row is not None
    assert row['symbol'] == 'SPY'
    assert row['priority'] == 100
    assert row['at'] == NOW
    assert row['reason'] == 'pending_created'
    # No chain row seeded: age is recorded as None, exactly as the
    # audit-imminent write records an absent chain.
    assert row['chain_age_seconds'] is None
    # The request never outlives its pending (30-min window) and is
    # capped by the same TTL as the audit-imminent write.
    assert row['expires_at'] == NOW + CHAIN_REFRESH_REQUEST_TTL_S
    assert row['expires_at'] < NOW + 1800


def test_creation_request_records_current_chain_age(db, cfg):
    e = Engine(db, cfg)
    with db.tx() as c:
        db.put(c, 'chain:SPY', {'asof': NOW - 700, 'contracts': [],
                                'source': 'fixture'})
        e.observe_setup(c, signal(), NOW)
        row = db.get(c, 'chain_refresh_request:SPY')
    assert row is not None
    assert row['chain_age_seconds'] == 700


def test_repeat_observe_of_same_signal_writes_no_duplicate(db, cfg):
    # observe_setup is called on every scanner pass for a live
    # candidate; the creation guard (pending row absent) is what
    # bounds the write to once per pending.
    e = Engine(db, cfg)
    with db.tx() as c:
        e.observe_setup(c, signal(), NOW)
        e.observe_setup(c, signal(), NOW + 30)
        rows = db.prefix(c, 'chain_refresh_request:')
        row = db.get(c, 'chain_refresh_request:SPY')
    assert len(rows) == 1
    assert row['at'] == NOW
    assert row['expires_at'] == NOW + CHAIN_REFRESH_REQUEST_TTL_S


def test_new_pending_same_symbol_refreshes_the_single_row(db, cfg):
    # One row per symbol by key: a second pending on the same symbol
    # re-requests (its window needs its own post-signal observation)
    # by overwriting the same row, never by adding a second one.
    e = Engine(db, cfg)
    with db.tx() as c:
        e.observe_setup(c, signal('s1'), NOW)
        e.observe_setup(c, signal('s2'), NOW + 30)
        rows = db.prefix(c, 'chain_refresh_request:')
        row = db.get(c, 'chain_refresh_request:SPY')
    assert len(rows) == 1
    assert row['at'] == NOW + 30


def test_no_pending_no_request(db, cfg):
    # A signal with no stop creates no research pending (observe_setup
    # skips it) and must not leave a refresh request behind either.
    e = Engine(db, cfg)
    s = signal()
    del s['invalidation']
    with db.tx() as c:
        e.observe_setup(c, s, NOW)
        assert db.get(c, 'pending_research_options:s1') is None
        assert db.get(c, 'chain_refresh_request:SPY') is None
