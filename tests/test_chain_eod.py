"""EOD option-chain snapshot archive (compass/chain_eod.py, 2026-10-26).

Covers: archive shaping (strike band, DTE window, greeks+IV retained), the
Massive theta/vega discard fix on the snapshot path only, per-date
idempotency, per-symbol failure isolation, archive registry membership,
and cron-token gating of the trigger endpoint.
"""
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from compass.store import Store, events
from compass import chain_eod
from compass.chain_eod import filter_contracts, run_snapshot, read_manifest

DATE = '2026-10-26'
NOW = datetime(2026, 10, 26, 21, 5, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'eod.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def provider_contract(symbol='AAA260102C00100000', strike=100.0,
                      expiry='2026-11-02', type_='call', **kw):
    o = {'symbol': symbol, 'underlying': 'AAA', 'expiry': expiry,
         'strike': strike, 'type': type_, 'multiplier': 100, 'oi': 10,
         'oi_date': None, 'gamma': 0.05, 'delta': 0.5, 'theta': -0.03,
         'vega': 0.12, 'iv': 0.25, 'volume': 100,
         'quote': {'ts': NOW, 'bid': 1.0, 'ask': 1.2, 'bid_size': 3,
                   'ask_size': 4}}
    o.update(kw)
    return o


# ---------- shaping: band, window, greeks ----------

def test_filter_band_edges_and_dte_window():
    contracts = [
        provider_contract('IN_HI', strike=110.0),          # +10% edge: in
        provider_contract('OUT_HI', strike=110.01),        # just outside
        provider_contract('IN_LO', strike=90.0),           # -10% edge: in
        provider_contract('OUT_LO', strike=89.99),
        provider_contract('IN_DTE', strike=100.0, expiry='2026-12-25'),  # +60
        provider_contract('OUT_DTE', strike=100.0, expiry='2026-12-26'),  # +61
        provider_contract('OUT_PAST', strike=100.0, expiry='2026-10-25'),
        provider_contract('NO_STRIKE', strike=None),
        provider_contract(None, strike=100.0),
    ]
    rows = filter_contracts(contracts, close=100.0, date_str=DATE)
    assert [r['symbol'] for r in rows] == ['IN_LO', 'IN_HI', 'IN_DTE']


def test_filter_retains_greeks_and_iv_and_quote():
    rows = filter_contracts([provider_contract()], close=100.0, date_str=DATE)
    (r,) = rows
    assert r['theta'] == -0.03 and r['vega'] == 0.12 and r['iv'] == 0.25
    assert r['delta'] == 0.5 and r['gamma'] == 0.05
    assert r['quote'] == {'ts': NOW, 'bid': 1.0, 'ask': 1.2,
                          'bid_size': 3, 'ask_size': 4}
    # Hoisted/diagnostic fields do not leak into the archive schema.
    assert 'underlying' not in r and 'multiplier' not in r
    assert 'gamma_field' not in r and 'greek_fields' not in r


# ---------- Massive greek discard: snapshot path only ----------

def _massive_collectors():
    from compass.providers import Collectors
    coll = Collectors.__new__(Collectors)
    coll.cfg = SimpleNamespace(massive='KEY', chain_dte=90)
    coll.chain_paging = {}

    async def fake_get(url, headers=None, params=None):
        return {'results': [{
            'details': {'ticker': 'SPY260102C00500000',
                        'expiration_date': '2026-01-02', 'strike_price': 500,
                        'contract_type': 'call', 'shares_per_contract': 100},
            'greeks': {'delta': 0.5, 'gamma': 0.05, 'theta': -0.03,
                       'vega': 0.12},
            'last_quote': {'bid': 1.0, 'ask': 1.2, 'last_updated': 0},
            'open_interest': 10, 'implied_volatility': 0.25,
            'day': {'volume': 100}}]}
    coll.get = fake_get
    return coll


def test_massive_chain_default_still_drops_theta_vega():
    coll = _massive_collectors()
    contracts, complete = asyncio.run(
        coll.massive_chain('SPY', expiry_start=DATE, expiry_end='2026-12-25'))
    assert complete and len(contracts) == 1
    # Live intraday behavior is pinned: no theta/vega keys.
    assert 'theta' not in contracts[0] and 'vega' not in contracts[0]


def test_massive_chain_full_greeks_retains_theta_vega():
    coll = _massive_collectors()
    contracts, _ = asyncio.run(coll.massive_chain(
        'SPY', expiry_start=DATE, expiry_end='2026-12-25', full_greeks=True))
    assert contracts[0]['theta'] == -0.03 and contracts[0]['vega'] == 0.12


def test_fetch_chain_passes_full_greeks_and_falls_back():
    calls = {}

    class FakeCollectors:
        cfg = SimpleNamespace(massive='KEY')

        async def massive_chain(self, symbol, **kw):
            calls['massive'] = kw
            raise RuntimeError('massive down')

        async def alpaca_chain(self, symbol, **kw):
            calls['alpaca'] = kw
            return ([provider_contract()], True)

    contracts, complete, source = asyncio.run(chain_eod.fetch_chain(
        FakeCollectors(), 'AAA', DATE, '2026-12-25'))
    assert source == 'alpaca' and complete and len(contracts) == 1
    assert calls['massive'].get('full_greeks') is True
    assert calls['massive'].get('expiry_end') == '2026-12-25'


# ---------- run: idempotency + failure isolation ----------

def seed_close(db, symbol, close=100.0):
    with db.tx() as c:
        db.append(c, 'bar', 'test', symbol, NOW - 300, {'c': close})


def count_events(db):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(events)
                         .where(events.c.kind == 'chain_eod')).scalar()


def make_fetch(fail=()):
    calls = []

    async def fetch(symbol, expiry_start, expiry_end):
        calls.append(symbol)
        if symbol in fail:
            raise RuntimeError('provider boom')
        return ([provider_contract()], True, 'massive')
    fetch.calls = calls
    return fetch


def test_run_snapshot_isolates_failures_and_is_idempotent(db):
    for s in ('AAA', 'BBB', 'BAD'):
        seed_close(db, s)
    cfg = SimpleNamespace(watch_symbols=('AAA', 'BBB', 'BAD'))
    fetch = make_fetch(fail=('BAD',))
    m = asyncio.run(run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                 fetch=fetch))
    assert m['totals'] == {'ok': 2, 'error': 1, 'contracts': 2, 'bytes': m['totals']['bytes']}
    assert m['symbols']['BAD']['status'] == 'error'
    assert 'provider boom' in m['symbols']['BAD']['detail']
    assert count_events(db) == 2
    assert m['symbols']['AAA']['bytes'] > 0

    # Re-run the same date: ok symbols are not re-fetched or re-stored,
    # the failed symbol is retried (and recorded again), nothing duplicates.
    fetch2 = make_fetch(fail=('BAD',))
    m2 = asyncio.run(run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                  fetch=fetch2))
    assert fetch2.calls == ['BAD']
    assert count_events(db) == 2
    assert m2['totals']['ok'] == 2 and m2['totals']['error'] == 1

    # Once the provider recovers, the retry completes the day.
    fetch3 = make_fetch()
    m3 = asyncio.run(run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                  fetch=fetch3))
    assert fetch3.calls == ['BAD']
    assert m3['totals']['ok'] == 3 and m3['totals']['error'] == 0
    assert count_events(db) == 3
    assert read_manifest(db, DATE)['complete'] is True


def test_run_snapshot_records_missing_close_as_error(db):
    cfg = SimpleNamespace(watch_symbols=('GHOST',))
    fetch = make_fetch()
    m = asyncio.run(run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                 fetch=fetch))
    assert m['symbols']['GHOST']['error'] == 'no_underlying_close'
    assert fetch.calls == []  # never fetched without a band reference
    assert count_events(db) == 0


def test_start_background_reports_already_complete(db):
    seed_close(db, 'AAA')
    cfg = SimpleNamespace(watch_symbols=('AAA',))
    asyncio.run(run_snapshot(db, cfg, now=NOW, date_str=DATE,
                             fetch=make_fetch()))
    out = chain_eod.start_snapshot_background(db, cfg, date_str=DATE, now=NOW)
    assert out['already_complete'] is True and out['ok'] is True


# ---------- registry ----------

def test_kind_registered_for_archive_and_never_pruned():
    from compass import copy_archive, archive_prune, archive_prune_copy
    assert copy_archive.RETENTION['chain_eod'] == 7
    assert 'chain_eod' not in archive_prune_copy.PUMP_SKIPPED_KINDS
    policies = [v for v in archive_prune.EVENT_POLICY.values()
                if 'chain_eod' in v['kinds']]
    assert len(policies) == 1
    # Permanence horizon: never a prune candidate in any planning window.
    assert policies[0]['retention_days'] >= 36500


# ---------- endpoint gating ----------

def test_endpoint_is_cron_token_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from compass.config import Config
    from compass.app import create_app, CRON_TOKEN_PATHS
    assert '/api/admin/chain-eod-snapshot' in CRON_TOKEN_PATHS
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', 'test-cron-token-0123456789abcdef')
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'app.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    store = Store(cfg.db)
    store.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    client.__enter__()
    try:
        r = client.get('/api/admin/chain-eod-snapshot')
        assert r.status_code == 401
        r = client.get('/api/admin/chain-eod-snapshot',
                       headers={'Authorization': 'Bearer test-cron-token-0123456789abcdef'})
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True and body['symbols'] == {}
        assert body['totals']['ok'] == 0
    finally:
        client.__exit__(None, None, None)
