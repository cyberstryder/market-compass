"""Chain-lane fix (2026-11-05): rendezvous + quote pinning + gate 900s.

Josh's 2026-11-05 decision on the chain-lane diagnosis
(files/chain-lane-diagnosis-2026-11-05.md): audits and chain refreshes
ran on unlinked clocks against a ~24x fetch-supply shortfall, and the
audits that passed the chain gate died on per-contract quote
freshness. Three changes, and only these:

1. The selection chain-age gate widens 120s -> 900s
   (Engine.CHAIN_MAX_AGE_SECONDS), superseding the 2026-11-03 'gate
   stays' ruling. The 60s per-contract freshness check is unchanged.
2. Rendezvous: a chain-blocked audit writes one
   chain_refresh_request:<symbol> row (TTL 120s); the pending chain
   lane serves outstanding requests first, within its existing
   per-cycle cap; the lane consumes a request when its fetch lands.
3. Quote pinning: Engine.options judges a candidate contract on the
   chain snapshot's embedded quote (stamped at the snapshot's asof)
   when the stored quote:<contract> row is older than the snapshot
   observation; a streamed row at least as new still wins.

Negative controls: every behavior test below fails on the pre-change
code (noted per test); the two marked GUARD pin behavior that must
NOT change and pass on both.

2026-11-12 addendum (Josh's decisions on that day's chain-lane
diagnosis, files/chain-lane-diagnosis-2026-11-12.md):
1. Freshness alignment: a snapshot-pinned quote is judged relative
   to its snapshot -- its age at capture (snapshot asof minus the
   quote's vendor ts) must be <= 60s, and the chain's own age stays
   the 900s gate's job. The Nov 5 expectation that a snapshot quote
   stale at capture (290s) is judged fresh is SUPERSEDED: that case
   now rejects on freshness, and the old-chain/fresh-at-capture case
   the Nov 5 code rejected now passes.
2. Rendezvous imminence restored: requests are written only when
   the pending's window closes within 600s, expire with the pending,
   and the TTL cap is 1200s (the lane's measured rotation time, not
   one ~60s cycle).
3. Rejection evidence carries premium and quote capture age.
(G1's removal is covered in tests/test_odte_book.py.)
"""
import asyncio
from datetime import datetime

import pytest

from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.providers import Collectors
from compass.engine import (Engine, CHAIN_MAX_AGE_SECONDS,
                            CHAIN_REFRESH_IMMINENT_S,
                            CHAIN_REFRESH_REQUEST_TTL_S, audit_quote)
# Importing the app registers every table-defining module before
# Store.initialize() runs create_all (the lazy-import table lesson).
from compass.app import create_app  # noqa: F401

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()
STOCKS = ('SPY', 'QQQ', 'IWM', 'AAA', 'BBB', 'CCC', 'DDD', 'EEE')


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'chain_lane_fix.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(t=NOW, bid=101.10, ask=101.11):
    return {'ts': t, 'bid': bid, 'ask': ask, 'bid_size': 100,
            'ask_size': 100, 'source': 'fixture'}


def contract(symbol, quote_ts=NOW - 10, bid=2.0, ask=2.01):
    return {'symbol': 'O:' + symbol, 'underlying': symbol, 'strike': 100,
            'type': 'call', 'multiplier': 100, 'expiry': '2026-09-14',
            'oi': 100, 'gamma': .1, 'iv': .2,
            'quote': {'ts': quote_ts, 'bid': bid, 'ask': ask,
                      'bid_size': 100, 'ask_size': 100}}


def signal(symbol='AAA', signal_time=NOW - 10):
    return {'id': 'sig-' + symbol, 'symbol': symbol, 'side': 'long',
            'invalidation': 99, 'signal_price': 100,
            'signal_time': signal_time,
            'strategy': 'compass-scanner-v2:session_sweep_reclaim',
            'context': {'atr14': 1}}


def seed_chain(db, symbol, age_s, contracts=()):
    with db.tx() as c:
        db.put(c, 'quote:' + symbol, quote(bid=99.99, ask=100.01))
        db.put(c, 'chain:' + symbol,
               {'asof': NOW - age_s, 'contracts': list(contracts),
                'source': 'fixture', 'complete': True})
        db.put(c, 'chain_inventory:' + symbol,
               {'contracts': 5, 'at': NOW - age_s})


def seed_pending(db, symbol, key):
    with db.tx() as c:
        db.put(c, 'pending_research_options:' + key,
               {'signal': signal(symbol), 'created_at': NOW - 10,
                'expires_at': NOW + 1790, 'status': 'waiting',
                'research_only': True})


def seed_request(db, symbol, at=NOW, ttl=120):
    with db.tx() as c:
        db.put(c, 'chain_refresh_request:' + symbol,
               {'symbol': symbol, 'priority': 100, 'at': at,
                'reason': 'selection_chain_stale',
                'expires_at': at + ttl})


def run_audit(db, symbol='AAA', pending_expires_at=None, **kw):
    cfg = Config(local=True, stocks=STOCKS, option_focus=3,
                 massive='fixture')
    audit = {}
    with db.tx() as c:
        Engine(db, cfg).options(c, signal(symbol, **kw), NOW,
                                quiet=True, diagnostics=audit,
                                research_only=True,
                                pending_expires_at=pending_expires_at)
    return audit


def make_collector(db, monkeypatch, calls):
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    collector = Collectors(db, Config(local=True, stocks=STOCKS,
                                      option_focus=3, massive='fixture'))

    async def chain(symbol):
        calls.append(symbol)
        return [contract(symbol)], True

    collector.massive_chain = chain
    return collector


# --- 1. The gate ----------------------------------------------------

def test_gate_constants():
    assert CHAIN_MAX_AGE_SECONDS == 900
    # 2026-11-12: TTL sized to the pending lane's measured rotation
    # (~12-23 min over the imminent set), not one ~60s cycle; the
    # imminence window is the final third of the 30-min window.
    assert CHAIN_REFRESH_REQUEST_TTL_S == 1200
    assert CHAIN_REFRESH_IMMINENT_S == 600


def test_audit_at_500s_chain_age_passes_the_gate(db):
    # Fails pre-change: the 120s gate blocked this audit outright
    # (reason 'No recent options chain', zero contracts checked).
    # Post-change the audit reaches the contract layer. The snapshot
    # quote's vendor ts is 520s old but it was only 20s old when the
    # chain was captured, so under the 2026-11-12 relative judgment
    # it PASSES freshness (the Nov 5 code, stamping it at asof and
    # judging 500s wall-clock, rejected it as a fresh quote) -- the
    # audit instead stops at the signal-recency guard, whose
    # observation time for a pinned quote is the snapshot's asof
    # (500s old, before this signal). The evidence now records both
    # ages and the premium.
    seed_chain(db, 'AAA', 500, contracts=[contract('AAA', quote_ts=NOW - 520)])
    audit = run_audit(db)
    assert audit['reason'] != 'No recent options chain'
    assert audit['chain_age_seconds'] == 500
    assert audit['checked_contracts'] == 1
    (rejection,) = audit['rejections']
    assert rejection['reason'] == 'Fresh quote after signal required'
    assert rejection['quote_age_seconds'] == 520
    assert rejection['quote_capture_age_seconds'] == 20
    assert rejection['quote_source'] == 'fixture:snapshot'
    assert rejection['premium'] == 2.01
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:AAA') is None


def test_gate_boundary_900_passes_901_blocks(db):
    # Fails pre-change: age 900 blocked under the 120s gate.
    seed_chain(db, 'AAA', 900, contracts=[contract('AAA')])
    audit = run_audit(db)
    assert audit['reason'] != 'No recent options chain'
    assert audit['checked_contracts'] == 1
    seed_chain(db, 'AAA', 901, contracts=[contract('AAA')])
    audit = run_audit(db)
    assert audit['reason'] == 'No recent options chain'
    assert audit['checked_contracts'] == 0


# --- 2. The rendezvous ----------------------------------------------

def test_chain_block_writes_refresh_request(db):
    # An audit-imminent pending (window closes in 300s) leaves a
    # request. Fails pre-2026-11-12 in the other direction: the old
    # code wrote on every block with a flat 120s expiry.
    seed_chain(db, 'AAA', 950)
    audit = run_audit(db, pending_expires_at=NOW + 300)
    assert audit['reason'] == 'No recent options chain'
    assert audit['refresh_requested'] is True
    with db.tx() as c:
        row = db.get(c, 'chain_refresh_request:AAA')
    assert row is not None
    assert row['symbol'] == 'AAA'
    assert row['at'] == NOW
    # The request expires with the pending (inside the TTL cap).
    assert row['expires_at'] == NOW + 300
    assert row['expires_at'] <= NOW + CHAIN_REFRESH_REQUEST_TTL_S
    assert row['chain_age_seconds'] == 950


def test_absent_chain_also_writes_refresh_request(db):
    # No chain row at all: same block, same rendezvous request.
    audit = run_audit(db, pending_expires_at=NOW + 300)
    assert audit['reason'] == 'No recent options chain'
    with db.tx() as c:
        row = db.get(c, 'chain_refresh_request:AAA')
    assert row is not None and row['symbol'] == 'AAA'


def test_chain_block_not_imminent_writes_no_request(db):
    # 2026-11-12: a pending with 1,500s of window left is served by
    # the lane's ordinary stalest-first pending order; the rendezvous
    # is reserved for the final stretch. Fails pre-change: the old
    # code wrote a request here.
    seed_chain(db, 'AAA', 950)
    audit = run_audit(db, pending_expires_at=NOW + 1500)
    assert audit['reason'] == 'No recent options chain'
    assert audit['refresh_requested'] is False
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:AAA') is None


def test_chain_block_without_known_expiry_writes_no_request(db):
    # No pending expiry passed and the signal carries none: the
    # audit cannot be shown imminent, so no request is written.
    seed_chain(db, 'AAA', 950)
    audit = run_audit(db)
    assert audit['reason'] == 'No recent options chain'
    assert audit['refresh_requested'] is False
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:AAA') is None


def test_chain_block_expired_window_writes_no_request(db):
    # A window that already closed cannot use a refreshed chain.
    seed_chain(db, 'AAA', 950)
    audit = run_audit(db, pending_expires_at=NOW - 5)
    assert audit['refresh_requested'] is False
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:AAA') is None


def test_chain_block_imminence_boundary(db):
    # 600s remaining is imminent; 601s is not.
    seed_chain(db, 'AAA', 950)
    seed_chain(db, 'BBB', 950)
    run_audit(db, pending_expires_at=NOW + CHAIN_REFRESH_IMMINENT_S)
    run_audit(db, 'BBB', pending_expires_at=NOW + CHAIN_REFRESH_IMMINENT_S + 1)
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:AAA') is not None
        assert db.get(c, 'chain_refresh_request:BBB') is None


def test_pending_lane_serves_request_first_and_consumes_it(db, monkeypatch):
    # Fails pre-change: the lane fetched stalest pendings BBB, AAA and
    # never knew CCC was being audited.
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'AAA', 2000)
    seed_chain(db, 'BBB', 3000)
    seed_chain(db, 'CCC', 1000)
    seed_pending(db, 'AAA', 'p-a')
    seed_pending(db, 'BBB', 'p-b')
    seed_request(db, 'CCC')

    async def run():
        await collector.chains('pending')
        await collector.close()

    asyncio.run(run())
    # The requested name first, then the pendings (equal expiries:
    # stalest first). The per-cycle cap was 2 when this test was
    # written; since the 2026-11-14 funnel fix it is 6, so AAA is
    # served in the same cycle instead of waiting for the next one.
    assert calls == ['CCC', 'BBB', 'AAA']
    with db.tx() as c:
        # Consumed on service; CCC's chain is now fresh.
        assert db.get(c, 'chain_refresh_request:CCC') is None
        assert NOW - db.get(c, 'chain:CCC')['asof'] <= 120


def test_pending_lane_serves_request_for_focus_symbol(db, monkeypatch):
    # Fails pre-change: no pendings at all meant no pending-lane fetch.
    # SPY sits in focus; the rendezvous must not wait for the focus
    # lane's rotation to reach it.
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'SPY', 2000)
    seed_request(db, 'SPY')

    async def run():
        await collector.chains('pending')
        await collector.close()

    asyncio.run(run())
    assert calls == ['SPY']
    with db.tx() as c:
        assert db.get(c, 'chain_refresh_request:SPY') is None


def test_pending_lane_ignores_expired_and_backed_off_requests(db, monkeypatch):
    # GUARD: TTL and failure backoff bound the rendezvous on both
    # code versions (the expired/backed-off names are never served;
    # pre-change the plain pending still was the only fetch).
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'AAA', 2000)
    seed_chain(db, 'CCC', 1000)
    seed_chain(db, 'DDD', 1000)
    seed_pending(db, 'AAA', 'p-a')
    seed_request(db, 'CCC', at=NOW - 300, ttl=120)  # expired at NOW-180
    seed_request(db, 'DDD')                          # fresh request...
    with db.tx() as c:                               # ...but in backoff
        db.put(c, 'chain_retry:DDD', {'at': NOW, 'retry_at': NOW + 300})

    async def run():
        await collector.chains('pending')
        await collector.close()

    asyncio.run(run())
    assert calls == ['AAA']


# --- 3. Quote pinning -------------------------------------------------

def test_audit_quote_resolution():
    # Fails pre-change: audit_quote does not exist.
    chain = {'asof': NOW - 10, 'source': 'massive'}
    stale_stored = {'ts': NOW - 300, 'bid': 9.9, 'ask': 10.0,
                    'source': 'stream'}
    o = contract('AAA', quote_ts=NOW - 300)
    # Stored row older than the snapshot observation: the snapshot
    # quote wins. Since 2026-11-12 it keeps its own vendor ts and
    # carries the snapshot's asof alongside (the Nov 5 code stamped
    # ts=asof), so freshness can be judged relative to the snapshot.
    q = audit_quote(chain, o, stale_stored)
    assert q['ts'] == NOW - 300 and q['bid'] == 2.0
    assert q['snapshot_asof'] == NOW - 10
    assert q['quote_origin'] == 'chain_snapshot'
    # Streamed row at least as new: it wins untouched.
    fresh_stored = {'ts': NOW - 5, 'bid': 3.0, 'ask': 3.01,
                    'source': 'stream'}
    assert audit_quote(chain, o, fresh_stored) is fresh_stored
    # No stored row: the snapshot quote stands alone.
    assert audit_quote(chain, o, None)['ts'] == NOW - 300
    # Embedded quote with no vendor ts: stamped at asof, as before.
    no_ts = contract('AAA')
    del no_ts['quote']['ts']
    q2 = audit_quote(chain, no_ts, None)
    assert q2['ts'] == NOW - 10 and q2['snapshot_asof'] == NOW - 10
    # No embedded quote / no snapshot clock: the stored row is all
    # there is.
    bare = {'symbol': 'O:AAA'}
    assert audit_quote(chain, bare, stale_stored) is stale_stored
    assert audit_quote({}, o, stale_stored) is stale_stored
    assert audit_quote(chain, o, None) is not None
    assert audit_quote({}, bare, None) is None


def test_audit_rejects_snapshot_quote_stale_at_capture(db):
    # The diagnosis Q4 shape: chain fetched 10s ago, its embedded
    # quote carries a 300s-old vendor timestamp, and the stored
    # quote row is absent entirely. The Nov 5 pinning judged this
    # quote fresh (stamped at asof, age 10s); Josh's 2026-11-12
    # relative judgment supersedes that: the quote was already 290s
    # old when the snapshot captured it, so vendor staleness inside
    # the snapshot still fails freshness.
    seed_chain(db, 'AAA', 10,
               contracts=[contract('AAA', quote_ts=NOW - 300)])
    audit = run_audit(db, signal_time=NOW - 400)
    assert audit['checked_contracts'] == 1
    (rejection,) = audit['rejections']
    assert rejection['reason'] == 'Missing or invalid fresh quote'
    assert rejection['quote_source'] == 'fixture:snapshot'
    assert rejection['quote_age_seconds'] == 300
    assert rejection['quote_capture_age_seconds'] == 290
    assert rejection['premium'] == 2.01


def test_audit_accepts_snapshot_quote_fresh_at_capture_old_chain(db):
    # The 2026-11-12 fix's positive case: the chain is 500s old
    # (inside the 900s gate) and its embedded quote was 20s old at
    # capture. The Nov 5 code judged the pinned quote 500s old
    # wall-clock and rejected it on freshness; the relative judgment
    # passes it, and the audit reaches the book gates (which reject
    # here on movement: no daily history in the fixture).
    seed_chain(db, 'AAA', 500,
               contracts=[contract('AAA', quote_ts=NOW - 520)])
    audit = run_audit(db, signal_time=NOW - 600)
    assert audit['checked_contracts'] == 1
    (rejection,) = audit['rejections']
    assert rejection['reason'] == 'movement_ineligible'
    assert rejection['quote_source'] == 'fixture:snapshot'
    assert rejection['quote_age_seconds'] == 520
    assert rejection['quote_capture_age_seconds'] == 20
    assert rejection['premium'] == 2.01
    assert rejection['spread'] == 0.01
    assert rejection['range20'] is None


def test_stream_quote_keeps_wall_clock_freshness(db):
    # GUARD (2026-11-12): the relative judgment applies only to
    # snapshot-pinned quotes. A streamed row wins resolution when it
    # is at least as new as the snapshot (here 100s newer) and is
    # then judged on the wall clock: 300s old at audit fails, even
    # though the snapshot's own embedded quote was fresh at capture.
    seed_chain(db, 'AAA', 400,
               contracts=[contract('AAA', quote_ts=NOW - 410)])
    with db.tx() as c:
        db.put(c, 'quote:O:AAA',
               {'ts': NOW - 300, 'bid': 3.0, 'ask': 3.01,
                'bid_size': 100, 'ask_size': 100,
                'source': 'fixture-stream'})
    audit = run_audit(db, signal_time=NOW - 600)
    assert audit['checked_contracts'] == 1
    (rejection,) = audit['rejections']
    assert rejection['reason'] == 'Missing or invalid fresh quote'
    assert rejection['quote_source'] == 'fixture-stream'
    assert rejection['quote_age_seconds'] == 300
    assert rejection['quote_capture_age_seconds'] is None
    assert rejection['premium'] == 3.01


def test_audit_prefers_fresher_stream_quote(db):
    # GUARD: pinning must not replace a live stream quote with an
    # older snapshot observation. Passes on both versions.
    seed_chain(db, 'AAA', 30,
               contracts=[contract('AAA', quote_ts=NOW - 300)])
    with db.tx() as c:
        db.put(c, 'quote:O:AAA',
               {'ts': NOW - 5, 'bid': 3.0, 'ask': 3.01, 'bid_size': 100,
                'ask_size': 100, 'source': 'fixture-stream'})
    audit = run_audit(db, signal_time=NOW - 400)
    assert audit['checked_contracts'] == 1
    evidence = (audit['rejections'] or
                [audit.get('selected_quote_evidence')])[0]
    assert evidence['quote_source'] == 'fixture-stream'
    assert evidence['quote_age_seconds'] == 5


# --- 4. The audit walk bound (2026-11-14 funnel fix, Defect 4) ------


def _walk_contracts(n):
    # n eligible same-day calls at strikes 100..100+n-1, every
    # snapshot quote stale at capture (290s), so the walk runs to its
    # bound instead of stopping at a pass.
    return [{'symbol': 'O:AAA%02d' % i, 'underlying': 'AAA',
             'strike': 100 + i, 'type': 'call', 'multiplier': 100,
             'expiry': '2026-09-14', 'oi': 100, 'gamma': .1, 'iv': .2,
             'quote': {'ts': NOW - 300, 'bid': 2.0, 'ask': 2.01,
                       'bid_size': 100, 'ask_size': 100}}
            for i in range(n)]


def test_audit_walk_checks_beyond_eight_contracts(db):
    # Fails pre-fix: the walk stopped at 8 and contracts 9-12 were
    # never examined (contracts_truncated stayed True at 12 eligible).
    seed_chain(db, 'AAA', 10, contracts=_walk_contracts(12))
    audit = run_audit(db, signal_time=NOW - 400)
    assert audit['eligible_contracts'] == 12
    assert audit['checked_contracts'] == 12
    assert audit['contracts_truncated'] is False
    assert len(audit['rejections']) == 12


def test_audit_walk_bound_is_24(db):
    # The bound still binds: 30 eligible contracts walk exactly 24,
    # nearest strikes first, and the truncation flag records it.
    seed_chain(db, 'AAA', 10, contracts=_walk_contracts(30))
    audit = run_audit(db, signal_time=NOW - 400)
    assert audit['eligible_contracts'] == 30
    assert audit['checked_contracts'] == 24
    assert audit['contracts_truncated'] is True
    assert audit['rejections'][0]['symbol'] == 'O:AAA00'
    assert audit['rejections'][-1]['symbol'] == 'O:AAA23'
