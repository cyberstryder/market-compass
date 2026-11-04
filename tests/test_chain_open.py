"""Morning chain snapshot (compass/chain_open.py, 2026-11-03): the
chain_eod twin under kind 'chain_open' — kind isolation (events,
manifests, leases, health), idempotency, and the retention
registrations the OMC registration froze."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import compass.app  # noqa: F401
from compass import chain_eod, chain_open
from compass.archive_prune import EVENT_POLICY
from compass.copy_archive import RETENTION
from compass.store import Store, events

DATE = '2026-11-04'
NOW = datetime(2026, 11, 4, 14, 35, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'open.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def provider_contract(symbol='AAA261107C00100000', strike=100.0,
                      expiry='2026-11-07', type_='call', **kw):
    o = {'symbol': symbol, 'underlying': 'AAA', 'expiry': expiry,
         'strike': strike, 'type': type_, 'multiplier': 100, 'oi': 10,
         'oi_date': None, 'gamma': 0.05, 'delta': 0.5, 'theta': -0.03,
         'vega': 0.12, 'iv': 0.25, 'volume': 100,
         'quote': {'ts': NOW, 'bid': 1.0, 'ask': 1.2, 'bid_size': 3,
                   'ask_size': 4}}
    o.update(kw)
    return o


def seed_close(db, symbol, close=100.0):
    with db.tx() as c:
        db.append(c, 'bar', 'test', symbol, NOW - 300, {'c': close})


def make_fetch(fail=()):
    async def fetch(symbol, expiry_start, expiry_end):
        if symbol in fail:
            raise RuntimeError('provider boom')
        return ([provider_contract()], True, 'massive')
    return fetch


def count_kind(db, kind):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(events)
                         .where(events.c.kind == kind)).scalar()


def test_morning_snapshot_kind_isolation_and_idempotency(db):
    for s in ('AAA', 'BBB'):
        seed_close(db, s)
    cfg = SimpleNamespace(watch_symbols=('AAA', 'BBB'))
    m = asyncio.run(chain_open.run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                            fetch=make_fetch()))
    assert m['totals']['ok'] == 2 and m['totals']['error'] == 0
    assert count_kind(db, 'chain_open') == 2
    assert count_kind(db, 'chain_eod') == 0  # the twin never writes EOD
    # Manifest lives under the morning kind's key; the EOD manifest for
    # the same date is untouched/empty.
    assert chain_open.read_manifest(db, DATE)['complete'] is True
    assert chain_eod.read_manifest(db, DATE)['symbols'] == {}
    # Payloads carry the kind marker the scorer's freshness reads use.
    with db.tx() as c:
        rows = db.export_events(c, 'chain_open', symbol='AAA', limit=5)
    assert rows[0]['payload']['snapshot_kind'] == 'chain_open'
    assert rows[0]['payload']['date'] == DATE
    # Health surfaced under the morning kind.
    with db.tx() as c:
        health = db.get(c, 'health:chain_open')
    assert health and health['status'] == 'available'
    # Idempotent re-run: nothing re-fetched, nothing duplicated.
    m2 = asyncio.run(chain_open.run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                             fetch=make_fetch()))
    assert m2['totals']['ok'] == 2
    assert count_kind(db, 'chain_open') == 2


def test_morning_snapshot_failure_isolation(db):
    for s in ('AAA', 'BAD'):
        seed_close(db, s)
    cfg = SimpleNamespace(watch_symbols=('AAA', 'BAD'))
    m = asyncio.run(chain_open.run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                            fetch=make_fetch(fail=('BAD',))))
    assert m['totals']['ok'] == 1 and m['totals']['error'] == 1
    assert m['symbols']['BAD']['status'] == 'error'


def test_background_reports_already_complete(db):
    seed_close(db, 'AAA')
    cfg = SimpleNamespace(watch_symbols=('AAA',))
    asyncio.run(chain_open.run_snapshot(db, cfg, now=NOW, date_str=DATE,
                                        fetch=make_fetch()))
    out = chain_open.start_snapshot_background(db, cfg, date_str=DATE, now=NOW)
    assert out.get('already_complete') is True


def test_endpoint_is_cron_token_gated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from compass.config import Config
    from compass.app import create_app, CRON_TOKEN_PATHS
    assert '/api/admin/chain-open-snapshot' in CRON_TOKEN_PATHS
    token = 'test-cron' + '-token-0123456789abcdef'
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', token)
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'app.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    store = Store(cfg.db)
    store.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    client.__enter__()
    try:
        r = client.get('/api/admin/chain-open-snapshot')
        assert r.status_code == 401
        r = client.get('/api/admin/chain-open-snapshot',
                       headers={'Authorization': 'Bearer ' + token})
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True and body['symbols'] == {}
        assert body['totals']['ok'] == 0
    finally:
        client.__exit__(None, None, None)


def test_retention_registrations():
    """The frozen substrate spec: chain_open is registered with the
    copy pump and the prune policy at the permanence horizon, like
    chain_eod — never an unregistered orphan kind."""
    assert RETENTION['chain_open'] == RETENTION['chain_eod']
    kinds = [k for pol in EVENT_POLICY.values() for k in pol['kinds']]
    assert 'chain_open' in kinds
    pol = next(p for p in EVENT_POLICY.values() if 'chain_open' in p['kinds'])
    assert pol['retention_days'] == 36500 and pol['requires_archive'] is True
