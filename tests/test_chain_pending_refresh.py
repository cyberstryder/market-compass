"""On-demand chain refresh for live selection pendings (2026-11-03).

Josh's ruling on the Gate-2 first read: move the DATA, not the gate.
Selection enforces a 120s chain-age gate while the background rotation
leaves non-focus chains 15m-2h old; the 'pending' chain lane promotes
symbols with live selection pendings to focus-equivalent cadence
(due at 45s chain age, stalest first, <=2 fetches per cycle).

Also covers the retry-path freshness alignment: retry_options now
judges the underlying quote with the book's 60s decision-time bound
(SCANNER_TRIGGER_QUOTE_MAX_AGE) instead of fresh()'s 5s default.

2026-11-04 lane repair: the lane read its live set by prefix()-hauling
both pending prefixes in full. Pending rows are never deleted, so the
haul grew with the accumulated terminal population until the lane's
opening transaction died on the production statement timeout under
RTH load -- zero selections on the decisive behavioral read despite
the lane testing green here (the original tests seeded 1-3 rows).
The live set now comes from Store.waiting_pendings (server-side
status/expiry filter + projection); the tests below pin the filter,
selection over an accumulated population, and the no-full-haul
structure.

2026-11-05 supersession: Josh widened the selection chain-age gate
120s -> 900s and added the audit-imminence rendezvous + snapshot
quote pinning (tests/test_chain_lane_fix.py). The 120s figures in
this docstring describe the 2026-11-03 design as built.
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
    # Selection now observes a freshly fetched chain (age ~0, inside any gate).
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
    # Updated 2026-11-14 (funnel fix Defect 1): the per-cycle cap is
    # 6 (was 2) and the order is window imminence, then staleness
    # (was pure stalest-first). CCC carries the earliest expiry, so
    # it is served first despite holding the freshest stale chain.
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    for symbol, age_s, exp in (('AAA', 5000, NOW + 1700),
                               ('BBB', 4000, NOW + 1790),
                               ('CCC', 3000, NOW + 600),
                               ('DDD', 30, NOW + 1790),
                               ('EEE', 5000, NOW + 1790)):
        seed_chain(db, symbol, age_s)
        seed_pending(db, symbol, 'p-' + symbol, expires_at=exp)
    with db.tx() as c:
        db.put(c, 'chain_retry:EEE', {'at': NOW, 'retry_at': NOW + 300})

    async def run():
        # Cycle 1: all three due names in ONE cycle (cap 6),
        # imminence order CCC -> AAA -> BBB. DDD's chain is 30s old
        # (below the 45s due bound); EEE is in failure backoff.
        await collector.chains('pending')
        assert calls == ['CCC', 'AAA', 'BBB']
        # Cycle 2: pendings are still live, but every chain is now
        # younger than the 45s bound -- no refetch storm.
        await collector.chains('pending')
        assert calls == ['CCC', 'AAA', 'BBB']
        await collector.close()

    asyncio.run(run())


def test_pending_lane_cap_six_and_fetches_overlap(db, monkeypatch):
    # Defect 1A/1B pins: with 8 due pendings, one cycle fetches
    # exactly PENDING_CHAIN_PER_CYCLE (6), in imminence order, and
    # the fetches actually overlap (gather) instead of serializing.
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    names = ['N%d' % i for i in range(1, 9)]
    stocks = ('SPY', 'QQQ', 'IWM') + tuple(names)
    collector = Collectors(db, Config(local=True, stocks=stocks,
                                      option_focus=3, massive='fixture'))
    calls = []
    inflight = {'now': 0, 'max': 0}

    async def chain(symbol):
        calls.append(symbol)
        inflight['now'] += 1
        inflight['max'] = max(inflight['max'], inflight['now'])
        await asyncio.sleep(0.01)
        inflight['now'] -= 1
        return [contract(symbol)], True

    collector.massive_chain = chain
    for i, symbol in enumerate(names):
        seed_chain(db, symbol, 5000)
        # Later names expire later: imminence order is N1..N8 even
        # though every chain is equally stale.
        seed_pending(db, symbol, 'p-' + symbol,
                     expires_at=NOW + 100 * (i + 1))

    async def run():
        await collector.chains('pending')
        assert calls == names[:6]
        assert inflight['max'] == 6
        await collector.chains('pending')
        assert calls == names
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


def test_waiting_pendings_filters_server_side(db):
    # The lane's live set comes from Store.waiting_pendings: status and
    # window predicates run server-side, so terminal history and
    # closed-window rows never cross into the lane's transaction.
    def put(prefix, key, row):
        with db.tx() as c:
            db.put(c, prefix + key, row)

    sig = lambda s: {'id': 'x', 'symbol': s, 'side': 'long'}
    put('pending_research_options:', 'live1',
        {'signal': sig('AAA'), 'expires_at': NOW + 600, 'status': 'waiting'})
    put('pending_options:', 'live2',
        {'signal': sig('BBB'), 'expires_at': NOW + 300, 'status': 'waiting'})
    put('pending_research_options:', 'closed_window',
        {'signal': sig('CCC'), 'expires_at': NOW - 5, 'status': 'waiting'})
    put('pending_research_options:', 'done',
        {'signal': sig('DDD'), 'expires_at': NOW + 600, 'status': 'expired'})
    put('pending_research_options:', 'observed',
        {'signal': sig('EEE'), 'expires_at': NOW + 600, 'status': 'observed'})
    put('pending_research_options:', 'no_expiry',
        {'signal': sig('AAA'), 'status': 'waiting'})
    put('pending_research_options:', 'no_signal',
        {'expires_at': NOW + 600, 'status': 'waiting'})
    with db.tx() as c:
        rows = db.waiting_pendings(c, NOW)
    by_key = {r['key']: r for r in rows}
    assert set(by_key) == {'pending_research_options:live1',
                           'pending_options:live2',
                           'pending_research_options:no_signal'}
    assert by_key['pending_research_options:live1']['symbol'] == 'AAA'
    assert by_key['pending_research_options:live1']['expires_at'] == NOW + 600
    assert by_key['pending_options:live2']['symbol'] == 'BBB'
    # A waiting row without a signal projects a null symbol; the lane
    # skips it exactly as the full-haul code skipped a missing symbol.
    assert by_key['pending_research_options:no_signal']['symbol'] is None


def test_pending_lane_selects_with_accumulated_population(db, monkeypatch):
    # Production shape the original tests never modeled: pending rows
    # are never deleted, so thousands of terminal rows (with their
    # last_selection audits) sit under the same prefixes as the live
    # ones. The lane must still select the live pending's symbol.
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    audit = {'version': '0dte-selection-v3', 'status': 'blocked',
             'reason': 'No recent options chain', 'chain_age_seconds': 5000.0,
             'rejections': [{'symbol': 'O:X', 'reason': 'stale'}] * 4,
             'underlying_evidence': {'basis': 'x' * 120}}
    with db.tx() as c:
        for i in range(3000):
            db.put(c, 'pending_research_options:old-%d' % i,
                   {'signal': {'id': 'old-%d' % i, 'symbol': 'BBB',
                               'side': 'long'},
                    'created_at': NOW - 90000, 'expires_at': NOW - 80000,
                    'status': 'expired' if i % 2 else 'observed',
                    'research_only': True, 'last_selection': audit})
    seed_chain(db, 'AAA', 5000)
    seed_chain(db, 'BBB', 5000)
    seed_pending(db, 'AAA', 'live-a')

    async def run():
        await collector.chains('pending')
        assert calls == ['AAA']
        await collector.close()

    asyncio.run(run())


from tests.test_smoothers_postgres_handoff import pg


def test_waiting_pendings_on_postgres(pg):
    """Dialect proof for Store.waiting_pendings: the JSONB path
    predicates, the float cast and the COLLATE "C" key range must
    select the same live set on Postgres (production's dialect) as on
    sqlite. Runs only where the pg fixture's isolated service exists
    (CI); skipped on a bare dev box."""
    sig = lambda s: {'id': 'x', 'symbol': s, 'side': 'long',
                     'signal_price': 100.0}
    with pg.tx() as c:
        pg.put(c, 'pending_research_options:pg-live',
               {'signal': sig('AAA'), 'expires_at': NOW + 600,
                'status': 'waiting'})
        pg.put(c, 'pending_options:pg-live2',
               {'signal': sig('BBB'), 'expires_at': NOW + 300,
                'status': 'waiting'})
        pg.put(c, 'pending_research_options:pg-done',
               {'signal': sig('CCC'), 'expires_at': NOW + 600,
                'status': 'expired'})
        pg.put(c, 'pending_research_options:pg-closed',
               {'signal': sig('DDD'), 'expires_at': NOW - 5,
                'status': 'waiting'})
        rows = pg.waiting_pendings(c, NOW)
    by_key = {r['key']: r for r in rows}
    assert set(by_key) == {'pending_research_options:pg-live',
                           'pending_options:pg-live2'}
    assert by_key['pending_research_options:pg-live']['symbol'] == 'AAA'
    assert by_key['pending_research_options:pg-live']['signal_price'] == 100.0


def test_pending_lane_does_not_haul_pending_prefixes(db, monkeypatch):
    # Structural pin for the 2026-11-04 repair: chains('pending') must
    # never full-haul the pending prefixes via Store.prefix (that haul
    # is what outgrew the lane's opening transaction in production).
    # Fails on the pre-repair code, which prefix()-read both prefixes.
    from compass.store import Store
    orig_prefix = Store.prefix

    def guarded(self, c, prefix):
        assert prefix not in ('pending_options:',
                              'pending_research_options:'), \
            'pending lane full-hauled %s' % prefix
        return orig_prefix(self, c, prefix)

    monkeypatch.setattr(Store, 'prefix', guarded)
    calls = []
    collector = make_collector(db, monkeypatch, calls)
    seed_chain(db, 'AAA', 5000)
    seed_pending(db, 'AAA', 'p-guard')

    async def run():
        await collector.chains('pending')
        assert calls == ['AAA']
        await collector.close()

    asyncio.run(run())


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
