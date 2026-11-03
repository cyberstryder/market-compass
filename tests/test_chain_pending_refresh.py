"""On-demand chain refresh for live selection pendings (2026-11-03).

Josh's ruling on the Gate-2 first read: move the DATA, not the gate.
Selection enforces a 120s chain-age gate while the background rotation
leaves non-focus chains 15m-2h old; the 'pending' chain lane promotes
symbols with live selection pendings to focus-equivalent cadence
(due at 45s chain age, stalest first, <=2 fetches per cycle).

Also covers the retry-path freshness alignment: retry_options now
judges the underlying quote with the book's 60s decision-time bound
(SCANNER_TRIGGER_QUOTE_MAX_AGE) instead of fresh()'s 5s default.
"""
import asyncio
from datetime import datetime

import pytest

from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.providers import Collectors
from compass.engine import Engine
from compass.scanner import Scanner
# Importing the app registers every table-defining module before
# Store.initialize() runs create_all (the lazy-import table lesson);
# without it the fixture DB lacks swing_ideas_v1 / spy_timing_arms_v1.
from compass.app import create_app  # noqa: F401

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()
STOCKS = ('SPY', 'QQQ', 'IWM', 'AAA', 'BBB', 'CCC', 'DDD', 'EEE')


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'pending_chains.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def quote(t=NOW, bid=101.10, ask=101.11):
    return {'ts': t, 'bid': bid, 'ask': ask, 'bid_size': 100, 'ask_size': 100,
            'source': 'fixture'}


def contract(symbol):
    return {'symbol': 'O:' + symbol, 'underlying': symbol, 'strike': 100,
            'type': 'call', 'multiplier': 100, 'expiry': '2026-09-14',
            'oi': 100, 'gamma': .1, 'iv': .2,
            'quote': quote(bid=1, ask=1.01)}


def seed_chain(db, symbol, age_s):
    with db.tx() as c:
        db.put(c, 'quote:' + symbol, quote(bid=99.99, ask=100.01))
        db.put(c, 'chain:' + symbol,
               {'asof': NOW - age_s, 'contracts': [], 'source': 'fixture',
                'complete': True})
        db.put(c, 'chain_inventory:' + symbol,
               {'contracts': 5, 'at': NOW - age_s})


def seed_pending(db, symbol, key, expires_at=NOW + 1790, status='waiting'):
    signal = {'id': key, 'symbol': symbol, 'side': 'long', 'invalidation': 99,
              'signal_price': 100, 'signal_time': NOW - 10,
              'strategy': 'compass-scanner-v2:session_sweep_reclaim',
              'context': {'atr14': 1}}
    with db.tx() as c:
        db.put(c, 'pending_research_options:' + key,
               {'signal': signal, 'created_at': NOW - 10,
                'expires_at': expires_at, 'status': status,
                'research_only': True})


def make_collector(db, monkeypatch, calls):
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    # option_focus=3 keeps focus == the index core, so AAA..EEE are
    # background names served only by rotation (or the pending lane).
    collector = Collectors(db, Config(local=True, stocks=STOCKS,
                                      option_focus=3, massive='fixture'))

    async def chain(symbol):
        calls.append(symbol)
        return [contract(symbol)], True

    collector.massive_chain = chain
    return collector


def test_pending_symbol_refreshed_ahead_of_rotation(db, monkeypatch):
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'AAA', 5000)
    seed_chain(db, 'BBB', 5000)
    for symbol in ('CCC', 'DDD', 'EEE'):
        seed_chain(db, symbol, 10)
    seed_pending(db, 'AAA', 'p1')

    async def run():
        # The pending lane takes AAA (live pending, stale chain) even
        # though the background rotation has not reached it.
        await collector.chains('pending')
        assert calls == ['AAA']
        # Rotation then proceeds exactly as before for the rest.
        await collector.chains('background')
        assert calls == ['AAA', 'BBB']
        await collector.close()

    asyncio.run(run())
    # Selection now observes a chain inside its 120s gate.
    with db.tx() as c:
        chain = db.get(c, 'chain:AAA')
        assert NOW - chain['asof'] <= 120
        audit = {}
        signal = {'id': 'p1', 'symbol': 'AAA', 'side': 'long',
                  'invalidation': 99, 'signal_price': 100,
                  'signal_time': NOW - 10,
                  'strategy': 'compass-scanner-v2:session_sweep_reclaim'}
        Engine(db, collector.cfg).options(c, signal, NOW, quiet=True,
                                           diagnostics=audit,
                                           research_only=True)
        assert audit['reason'] != 'No recent options chain'
        assert audit['chain_age_seconds'] <= 120
        assert audit['checked_contracts'] >= 1


def test_pending_lane_bound_cooldown_and_backoff(db, monkeypatch):
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    for symbol, age_s in (('AAA', 5000), ('BBB', 4000), ('CCC', 3000),
                          ('DDD', 30), ('EEE', 5000)):
        seed_chain(db, symbol, age_s)
        seed_pending(db, symbol, 'p-' + symbol)
    with db.tx() as c:
        db.put(c, 'chain_retry:EEE', {'at': NOW, 'retry_at': NOW + 300})

    async def run():
        # Cycle 1: cap of 2, stalest first. DDD's chain is 30s old
        # (below the 45s due bound); EEE is in failure backoff.
        await collector.chains('pending')
        assert calls == ['AAA', 'BBB']
        # Cycle 2: only CCC remains due.
        await collector.chains('pending')
        assert calls == ['AAA', 'BBB', 'CCC']
        # Cycle 3: pendings are still live, but every chain is now
        # younger than the 45s bound -- no refetch storm.
        await collector.chains('pending')
        assert calls == ['AAA', 'BBB', 'CCC']
        await collector.close()

    asyncio.run(run())


def test_no_live_pending_rotation_behavior_unchanged(db, monkeypatch):
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'AAA', 5000)
    seed_chain(db, 'BBB', 4000)
    for symbol in ('CCC', 'DDD', 'EEE'):
        seed_chain(db, symbol, 10)
    seed_chain(db, 'SPY', 100)
    seed_chain(db, 'QQQ', 10)
    seed_chain(db, 'IWM', 10)
    # AAA's only pending already expired: it must not trigger the lane.
    seed_pending(db, 'AAA', 'old', expires_at=NOW - 10)

    async def run():
        await collector.chains('pending')
        assert calls == []
        # Background rotation still takes the oldest due name first.
        await collector.chains('background')
        assert calls == ['AAA']
        # Index lane still takes the due index (SPY at 100s >= 45s;
        # QQQ/IWM at 10s are not due).
        await collector.chains('indices')
        assert calls == ['AAA', 'SPY']
        await collector.close()

    asyncio.run(run())


def _retry_signal():
    return {'id': 'one', 'symbol': 'SPY', 'side': 'long',
            'invalidation': 100, 'signal_price': 101,
            'signal_time': NOW - 40, 'context': {'atr14': 1}}


def _run_retry(db, cfg, quote_row):
    with db.tx() as c:
        db.put(c, 'pending_research_options:one',
               {'signal': _retry_signal(), 'created_at': NOW - 40,
                'expires_at': NOW + 600, 'status': 'waiting',
                'research_only': True})
        Scanner(db, cfg).retry_options(c, {'SPY': quote_row}, NOW,
                                       Engine(db, cfg))
        return db.get(c, 'pending_research_options:one')


def test_retry_options_accepts_30s_old_quote(db):
    # 30s old: stale under the old 5s default, fresh under the book's
    # 60s decision-time standard. Selection must be attempted (and
    # here die at the chain gate, not the freshness gate).
    cfg = Config(paper_trading=True, local=True, stocks=('SPY',), futures=())
    row = _run_retry(db, cfg, quote(t=NOW - 30))
    audit = row['last_selection']
    assert audit['version'] == '0dte-selection-v3'
    assert audit['reason'] == 'No recent options chain'


def test_retry_options_rejects_90s_old_quote(db):
    # 90s old: beyond the 60s standard -- still waits on freshness,
    # and the recorded evidence agrees with the decision.
    cfg = Config(paper_trading=True, local=True, stocks=('SPY',), futures=())
    row = _run_retry(db, cfg, quote(t=NOW - 90))
    audit = row['last_selection']
    assert row['status'] == 'waiting'
    assert audit['reason'] == 'Fresh underlying quote required'
    assert audit['underlying_evidence']['age_seconds'] == 90
    assert audit['underlying_evidence']['fresh'] is False
