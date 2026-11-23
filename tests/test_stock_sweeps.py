"""Stock Sweeps forward shadow (2026-11-16, Josh): registration
files/stock-sweeps-forward-shadow-registration-2026-11-16.md.

Pins: (1) the build-blocking PARITY GATE — the Compass machine
reproduces the frozen screen's verified trade logs exactly (embedded
AMD fixture for CI; the full 23-symbol staged-data run whenever the
goal workspace is present); (2) contract selection under Josh's
nearest-expiry amendment; (3) premium arithmetic; (4) 15-minute
aggregation; (5) the live lifecycle on a sqlite store (bootstrap
watermark, episodes, alerts outbox, premium pricing, state summary);
(6) the store.py table registration regression (the bb1adfa lesson).
"""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.active_observations import inventory
from compass.config import Config
from compass.stock_sweeps import (BOOK, _apply_event, _underlying_leg,
                                  aggregate_buckets,
                                  bucket_start_ts, episodes,
                                  first_quote_at_or_after, new_state,
                                  on_bar, premium_net, run_bars, scan,
                                  select_contract)
from compass.store import Store, stock_sweeps_push_outbox

FIXTURE = json.loads((Path(__file__).parent / 'fixtures'
                      / 'stock_sweeps_parity.json').read_text())
SCREEN_DATA = ('/home/hatch/workspace/goals/market-compass-trading-'
               'improvements/hidden_files/stock-sweeps-screen-2026-11-12'
               '/data/bars15')
SCREEN_TRADES = ('/home/hatch/workspace/goals/market-compass-trading-'
                 'improvements/hidden_files/stock-sweeps-full-list-'
                 '2026-11-16/trades')
TRADE_FIELDS = ('entry_date', 'exit_date', 'side', 'net', 'gross',
                'orders', 'reason', 'exit_idx')


def _assert_trades_equal(got, want):
    assert len(got) == len(want)
    for g, w in zip(got, want):
        for f in TRADE_FIELDS:
            assert g[f] == w[f], (f, g, w)


# ---------------------------------------------------------------------------
# Parity gate

def test_parity_fixture_amd():
    """Embedded real-data fixture: AMD's first six screen sessions
    reproduce the verified trade log row-for-row (all fields exact,
    floats bit-identical)."""
    trades, _ = run_bars(FIXTURE['bars'], FIXTURE['buffer'])
    got = [{f: t[f] for f in TRADE_FIELDS} for t in trades]
    _assert_trades_equal(got, FIXTURE['trades'])


@pytest.mark.skipif(not os.path.isdir(SCREEN_DATA),
                    reason='staged screen data not present (CI)')
def test_parity_full_book_staged_data():
    """THE parity gate: all 23 registered symbols over the staged
    screen window reproduce the verified full-list trade logs —
    4,431 trades, pooled +$57,619 (rounded)."""
    import csv

    import pandas as pd
    total_n, total_net = 0, 0.0
    for sym, buf in sorted(BOOK.items()):
        df = pd.read_parquet(os.path.join(SCREEN_DATA, f'{sym}.parquet'))
        df = df.sort_values(['date', 'minute']).reset_index(drop=True)
        bars = [{'date': str(d)[:10], 'minute': int(m), 'o': float(o),
                 'h': float(h), 'l': float(l), 'c': float(cc),
                 'v': float(v)}
                for d, m, o, h, l, cc, v in
                zip(df['date'], df['minute'], df['open'], df['high'],
                    df['low'], df['close'], df['volume'])]
        trades, _ = run_bars(bars, buf)
        with open(os.path.join(SCREEN_TRADES, f'trades_{sym}.csv')) as f:
            want = [{**r, 'side': int(r['side']), 'net': float(r['net']),
                     'gross': float(r['gross']),
                     'orders': int(r['orders']),
                     'exit_idx': int(r['exit_idx'])}
                    for r in csv.DictReader(f)]
        got = [{f: t[f] for f in TRADE_FIELDS} for t in trades]
        _assert_trades_equal(got, want)
        total_n += len(trades)
        total_net += sum(t['net'] for t in trades)
    assert total_n == 4431
    assert round(total_net) == 57619


# ---------------------------------------------------------------------------
# Machine semantics pins (synthetic)

def _bar(date, minute, o, h, l, c, v=1000.0):
    return {'date': date, 'minute': minute, 'o': o, 'h': h, 'l': l,
            'c': c, 'v': v}


def test_no_or_bar_no_entries():
    """Porting note 9: a session with no 09:30 bar has no OR — sweep
    tracking and entries stay inert all day."""
    st = new_state(0.50)
    evs = []
    for b in [_bar('2026-01-05', 585, 100, 101, 99, 100),
              _bar('2026-01-05', 600, 100, 102, 98, 99),
              _bar('2026-01-05', 615, 99, 100, 97, 100.5)]:
        evs += on_bar(st, b['date'], b['minute'], b['o'], b['h'],
                      b['l'], b['c'], b['v'])
    assert [e for e in evs if e['type'] == 'entry'] == []


def test_entry_window_bounds():
    """Entries evaluate only on 585 <= minute < 870 bars: a textbook
    sweep+reclaim completing on the 14:30 bar does not enter."""
    st = new_state(0.50)
    d = '2026-01-05'
    seq = [
        _bar(d, 570, 100, 101, 99, 100),          # OR: high 101 low 99
        _bar(d, 585, 100, 100.5, 98.0, 98.5),     # sweep of OR low
        _bar(d, 870, 98.5, 100, 98.4, 99.5),      # reclaim, but 14:30
    ]
    evs = []
    for b in seq:
        evs += on_bar(st, b['date'], b['minute'], b['o'], b['h'],
                      b['l'], b['c'], b['v'])
    assert [e for e in evs if e['type'] == 'entry'] == []
    # Same reclaim one bar earlier (14:15) does enter, long, at close.
    st = new_state(0.50)
    seq[2] = _bar(d, 855, 98.5, 100, 98.4, 99.5)
    evs = []
    for b in seq:
        evs += on_bar(st, b['date'], b['minute'], b['o'], b['h'],
                      b['l'], b['c'], b['v'])
    entries = [e for e in evs if e['type'] == 'entry']
    assert len(entries) == 1
    assert entries[0]['side'] == 1
    assert entries[0]['entry_px'] == 99.5
    assert entries[0]['stop'] == 98.4 - 0.50


# ---------------------------------------------------------------------------
# Premium selection (nearest-expiry amendment) + arithmetic

def _contract(symbol, expiry, strike, typ, delta, bid=2.0, ask=2.2,
              ts=0.0):
    return {'symbol': symbol, 'expiry': expiry, 'strike': strike,
            'type': typ, 'delta': delta,
            'quote': {'ts': ts, 'bid': bid, 'ask': ask}}


def test_select_contract_prefers_entry_day_expiry():
    """Amendment: the entry day's own expiry (0DTE) wins over a later
    weekly; |delta| nearest 0.50 within it."""
    entry = bucket_start_ts('2026-11-17', 600) + 900  # 10:15 ET entry
    contracts = [
        _contract('X261120C00100000', '2026-11-20', 100, 'call', 0.50),
        _contract('X261117C00100000', '2026-11-17', 100, 'call', 0.62),
        _contract('X261117C00105000', '2026-11-17', 105, 'call', 0.44),
        _contract('X261117P00100000', '2026-11-17', 100, 'put', -0.50),
    ]
    chosen = select_contract(contracts, 1, entry, spot=250.0)
    assert chosen['symbol'] == 'X261117C00105000'  # |0.44| nearer 0.50


def test_select_contract_next_expiry_and_put_side():
    entry = bucket_start_ts('2026-11-17', 600) + 900
    contracts = [
        _contract('X261113P00100000', '2026-11-13', 100, 'put', -0.50),
        _contract('X261120P00095000', '2026-11-20', 95, 'put', -0.51),
        _contract('X261120P00100000', '2026-11-20', 100, 'put', -0.30),
        _contract('X261120C00100000', '2026-11-20', 100, 'call', 0.50),
    ]
    chosen = select_contract(contracts, -1, entry, spot=97.0)
    # The expired 11-13 expiry is skipped; the put nearest 0.50 delta
    # on the next available expiry is chosen.
    assert chosen['symbol'] == 'X261120P00095000'
    assert select_contract(contracts, -1, entry + 10 * 86400, 97.0) is None


def test_premium_net_arithmetic():
    # No TP1: both contracts out at the exit bid.
    assert premium_net(2.20, None, 3.00, False) == \
        2 * 3.00 * 100 - 2 * 2.20 * 100 - 0.65 * 2 * 2
    # TP1: one contract at the TP1 bid, one at the exit bid.
    assert premium_net(2.20, 2.80, 3.00, True) == \
        (2.80 + 3.00) * 100 - 2 * 2.20 * 100 - 0.65 * 2 * 2


# ---------------------------------------------------------------------------
# Aggregation

def test_aggregate_buckets():
    d = '2026-11-17'
    base = bucket_start_ts(d, 570)

    def row(ts, o, h, l, c, v, id_=1):
        return {'ts': ts, 'id': id_,
                'payload': {'o': o, 'h': h, 'l': l, 'c': c, 'v': v}}
    rows = [
        row(base + 60, 10, 11, 9, 10.5, 100, id_=2),
        row(base, 9.5, 10, 9, 10, 50, id_=1),
        row(base, 9.5, 10, 9, 10, 50, id_=0),       # stale duplicate
        row(base - 3600, 1, 1, 1, 1, 1, id_=3),    # pre-RTH: excluded
        row(base + 900, 10.5, 12, 10, 11, 70, id_=4),
    ]
    buckets = aggregate_buckets(rows)
    assert [b['minute'] for b in buckets] == [570, 585]
    b0 = buckets[0]
    assert (b0['o'], b0['h'], b0['l'], b0['c'], b0['v']) == \
        (9.5, 11, 9, 10.5, 150)
    assert b0['start_ts'] == base


# ---------------------------------------------------------------------------
# Store registration + live lifecycle

@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'stock_sweeps.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _cfg():
    return Config(local=True, stocks=('AMD',), watchlist=(),
                  sector_etfs=(), swing_ideas=False,
                  morning_enabled=False, discovery=False)


def test_tables_registered(db):
    names = set(inspect(db.engine).get_table_names())
    assert 'stock_sweeps_episodes' in names
    assert 'stock_sweeps_push_outbox_v1' in names


def _seed_fixture_bars(db):
    """One 1-minute bar event per fixture bucket (aggregation of a
    single minute reproduces the bucket exactly)."""
    with db.tx() as c:
        for b in FIXTURE['bars']:
            ts = bucket_start_ts(b['date'], b['minute'])
            payload = {'o': b['o'], 'h': b['h'], 'l': b['l'],
                       'c': b['c'], 'v': b['v']}
            db.append(c, 'bar', 'test', 'AMD', ts, payload,
                      'bar:AMD:%d' % int(ts))


def _seed_chain(db, asof):
    contracts = [
        _contract('AMD261103C00250000', '2025-11-03', 250, 'call', 0.52,
                  bid=2.0, ask=2.2, ts=asof),
        _contract('AMD261103P00250000', '2025-11-03', 250, 'put', -0.48,
                  bid=2.0, ask=2.2, ts=asof),
        _contract('AMD261107C00250000', '2025-11-07', 250, 'call', 0.50,
                  bid=2.0, ask=2.2, ts=asof),
        _contract('AMD261107P00250000', '2025-11-07', 250, 'put', -0.50,
                  bid=2.0, ask=2.2, ts=asof),
    ]
    with db.tx() as c:
        db.put(c, 'chain:AMD', {'asof': asof, 'symbol': 'AMD',
                                'underlying_close': 250.0,
                                'contracts': contracts})


def test_live_lifecycle(db):
    cfg = _cfg()
    _seed_fixture_bars(db)
    t0 = bucket_start_ts('2025-10-27', 540)       # 09:00 ET, day one
    t1 = bucket_start_ts('2025-11-03', 960)       # 16:00 ET, day six
    _seed_chain(db, t1)
    with db.tx() as c:
        s0 = scan(db, c, cfg, t0)
    assert s0['ran'] and s0['fed'] == 0 and s0['entries'] == 0
    with db.tx() as c:
        s1 = scan(db, c, cfg, t1)
    assert s1['fed'] == len(FIXTURE['bars'])
    assert s1['entries'] == len(FIXTURE['trades'])
    assert s1['first_session'] == '2025-10-27'
    with db.tx() as c:
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        state = db.get(c, 'stock_sweeps:state')
        outbox = c.execute(select(
            stock_sweeps_push_outbox.c.payload)).scalars().all()
    assert len(rows) == len(FIXTURE['trades'])
    got_nets = sorted(p['underlying_leg']['net'] for p in rows)
    # The symbol field is never clobbered by the leg result.
    assert all(p['underlying'] == 'AMD' for p in rows)
    want_nets = sorted(t['net'] for t in FIXTURE['trades'])
    assert got_nets == want_nets
    # Every episode's premium measurement resolved against the seeded
    # chain (static quotes): priced, never gapped or estimated.
    for p in rows:
        assert p['premium_status'] == 'closed'
        assert p['status'] == 'closed'
        leg = p['legs']['tp1']
        want = premium_net(2.2, 2.0, 2.0, bool(leg['filled']))
        assert p['premium']['net'] == want
        # Nearest expiry after each entry instant in the seeded chain.
        assert p['contract']['expiry'] == '2025-11-03'
    assert state['counts']['created'] == len(FIXTURE['trades'])
    assert state['counts']['closed'] == len(FIXTURE['trades'])
    assert state['first_session'] == '2025-10-27'
    # Alerts: one ENTRY and one CLOSE per episode, queued in the
    # module's own outbox.
    types = sorted(p['type'] for p in outbox)
    assert types.count('entry') == len(FIXTURE['trades'])
    assert types.count('close') == len(FIXTURE['trades'])


def test_apply_event_close_keeps_underlying_symbol(db):
    """Regression (2026-11-22 /api/state 500): closing the underlying
    leg must store its result under 'underlying_leg' and leave the
    'underlying' symbol string intact while the row stays open."""
    bar = {'start_ts': bucket_start_ts('2025-10-27', 540)}
    entry_ev = {'type': 'entry', 'date': '2025-10-27', 'minute': 540,
                'side': 1, 'entry_px': 250.0, 'stop': 248.0,
                'or_mid': 249.0, 'or_high': 251.0, 'or_low': 247.0}
    close_ev = {'type': 'close', 'side': 1, 'reason': 'target',
                'entry_px': 250.0, 'exit_px': 255.0,
                'exit_date': '2025-10-27', 'gross': 500.0, 'net': 490.0,
                'orders': 2, 'tp1_done': True, 'tp1_gross': 250.0,
                'runner_gross': 250.0}
    open_eps, alerts = {}, []
    with db.tx() as c:
        p = _apply_event(db, c, 'AMD', entry_ev, bar, 1000.0, True,
                         open_eps, alerts)
        assert p['underlying'] == 'AMD'
        assert p['underlying_leg'] is None
        p = _apply_event(db, c, 'AMD', close_ev, bar, 1001.0, True,
                         open_eps, alerts)
    assert p['underlying'] == 'AMD'
    assert p['underlying_leg']['net'] == 490.0
    assert p['underlying_leg']['exit_date'] == '2025-10-27'
    assert p['stage'] == 'closed' and p['status'] == 'open'
    assert _underlying_leg(p) is p['underlying_leg']


def test_inventory_survives_legacy_dict_underlying(db):
    """Regression (2026-11-22 /api/state 500): a legacy open episode
    whose 'underlying' field already carries the leg-result dict
    (stage closed, premium leg still open) must not raise in
    inventory() — the dict is skipped, the contract still pins."""
    payload = {'id': 'ep-legacy', 'symbol': 'AMD', 'status': 'open',
               'stage': 'closed',
               'underlying': {'entry_px': 250.0, 'exit_px': 255.0,
                              'exit_date': '2025-10-27',
                              'reason': 'target', 'gross': 500.0,
                              'net': 490.0, 'orders': 2,
                              'tp1_done': True},
               'contract': {'symbol': 'AMD261103C00250000'}}
    with db.tx() as c:
        c.execute(db.insert(episodes).values(
            id='ep-legacy', underlying='AMD', status='open',
            created=1.0, updated=1.0, payload=payload))
        inv = inventory(db, c, 100.0)
    assert 'AMD261103C00250000' in inv['options']
    assert all(isinstance(s, str) for s in inv['stocks'])
    # The scorecard reader still resolves the legacy leg result.
    assert _underlying_leg(payload)['net'] == 490.0


def test_inventory_pins_open_episode_contract(db):
    cfg = _cfg()
    payload = {'id': 'ep-test', 'symbol': 'AMD', 'underlying': 'AMD',
               'status': 'open',
               'contract': {'symbol': 'AMD261103C00250000'}}
    with db.tx() as c:
        c.execute(db.insert(episodes).values(
            id='ep-test', underlying='AMD', status='open', created=1.0,
            updated=1.0, payload=payload))
        inv = inventory(db, c, 100.0)
    assert 'AMD261103C00250000' in inv['options']
    assert 'AMD' in inv['stocks']


def test_first_quote_at_or_after(db):
    chain = {'contracts': [
        _contract('AMD261103C00250000', '2025-11-03', 250, 'call', 0.52,
                  bid=2.0, ask=2.2, ts=500.0)]}
    with db.tx() as c:
        assert first_quote_at_or_after(db, c, chain,
                                       'AMD261103C00250000', 600.0) is None
        q = first_quote_at_or_after(db, c, chain,
                                    'AMD261103C00250000', 400.0)
    assert q == (500.0, 2.0, 2.2, 'chain')
