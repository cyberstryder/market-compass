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
"""
import asyncio
from datetime import datetime

import pytest

from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.providers import Collectors
from compass.engine import (Engine, CHAIN_MAX_AGE_SECONDS,
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


def run_audit(db, symbol='AAA', **kw):
    cfg = Config(local=True, stocks=STOCKS, option_focus=3,
                 massive='fixture')
    audit = {}
    with db.tx() as c:
        Engine(db, cfg).options(c, signal(symbol, **kw), NOW,
                                quiet=True, diagnostics=audit,
                                research_only=True)
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
    assert CHAIN_REFRESH_REQUEST_TTL_S == 120


def test_audit_at_500s_chain_age_passes_the_gate(db):
    # Fails pre-change: the 120s gate blocked this audit outright
    # (reason 'No recent options chain', zero contracts checked).
    # Post-change the audit reaches the contract layer, where the
    # snapshot quote -- stamped at the snapshot's asof -- is honestly
    # 500s old and dies on the unchanged 60s freshness bound.
    seed_chain(db, 'AAA', 500, contracts=[contract('AAA', quote_ts=NOW - 520)])
    audit = run_audit(db)
    assert audit['reason'] != 'No recent options chain'
    assert audit['chain_age_seconds'] == 500
    assert audit['checked_contracts'] == 1
    (rejection,) = audit['rejections']
    assert rejection['reason'] == 'Missing or invalid fresh quote'
    assert rejection['quote_age_seconds'] == 500
    assert rejection['quote_source'] == 'fixture:snapshot'
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
    # Fails pre-change: the block happened but no request row existed.
    seed_chain(db, 'AAA', 950)
    audit = run_audit(db)
    assert audit['reason'] == 'No recent options chain'
    with db.tx() as c:
        row = db.get(c, 'chain_refresh_request:AAA')
    assert row is not None
    assert row['symbol'] == 'AAA'
    assert row['at'] == NOW
    assert row['expires_at'] == NOW + CHAIN_REFRESH_REQUEST_TTL_S
    assert row['chain_age_seconds'] == 950


def test_absent_chain_also_writes_refresh_request(db):
    # No chain row at all: same block, same rendezvous request.
    audit = run_audit(db)
    assert audit['reason'] == 'No recent options chain'
    with db.tx() as c:
        row = db.get(c, 'chain_refresh_request:AAA')
    assert row is not None and row['symbol'] == 'AAA'


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
    # Cap is 2: the requested name first, then the stalest pending.
    assert calls == ['CCC', 'BBB']
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
    # quote wins, stamped at the snapshot's asof.
    q = audit_quote(chain, o, stale_stored)
    assert q['ts'] == NOW - 10 and q['bid'] == 2.0
    assert q['quote_origin'] == 'chain_snapshot'
    # Streamed row at least as new: it wins untouched.
    fresh_stored = {'ts': NOW - 5, 'bid': 3.0, 'ask': 3.01,
                    'source': 'stream'}
    assert audit_quote(chain, o, fresh_stored) is fresh_stored
    # No stored row: the snapshot quote stands alone.
    assert audit_quote(chain, o, None)['ts'] == NOW - 10
    # No embedded quote / no snapshot clock: the stored row is all
    # there is.
    bare = {'symbol': 'O:AAA'}
    assert audit_quote(chain, bare, stale_stored) is stale_stored
    assert audit_quote({}, o, stale_stored) is stale_stored
    assert audit_quote(chain, o, None) is not None
    assert audit_quote({}, bare, None) is None


def test_audit_judges_snapshot_quote_when_stored_row_stale(db):
    # The diagnosis Q4 shape: chain fetched 10s ago, its embedded
    # quote carries a 300s-old vendor timestamp, and the stored
    # quote row is absent entirely. Fails pre-change: the candidate
    # died on 'Missing or invalid fresh quote'. Post-change the audit
    # judges the snapshot quote (age 10s) and moves past freshness.
    seed_chain(db, 'AAA', 10,
               contracts=[contract('AAA', quote_ts=NOW - 300)])
    audit = run_audit(db, signal_time=NOW - 400)
    assert audit['checked_contracts'] == 1
    reasons = [r['reason'] for r in audit['rejections']]
    assert 'Missing or invalid fresh quote' not in reasons
    evidence = (audit['rejections'] or
                [audit.get('selected_quote_evidence')])[0]
    assert evidence['quote_source'] == 'fixture:snapshot'
    assert evidence['quote_age_seconds'] == 10


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
