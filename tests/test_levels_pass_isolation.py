"""Levels-pass isolation (2026-10-22 freeze root-cause fix).

The per-symbol levels loop used to run inside Engine.tick's single
transaction, re-fetching up to 8000 bars per active symbol. When
Postgres slowed, the transaction hit the transaction timeout mid-loop,
the connection was terminated, and every later tick section (swings,
scanner, 0DTE, ICT detectors, flatten) starved for 34 hours — cursors
never advanced, so each tick retried the full backlog and died again.

_levels_pass now runs each symbol in its own transaction with its own
failure boundary. These tests pin that behavior: a symbol whose bar
query raises must not stop the pass, must not advance its own cursor,
and must leave a health:levels_error record.
"""
import time

import pytest

from compass.config import Config
from compass.store import Store
from compass.engine import Engine

NOW = 1760000000.0  # fixed point in time; bars end here


def make_engine(tmp_path, stocks=('SPY',)):
    cfg = Config()
    cfg.db = 'sqlite:///' + str(tmp_path / 'levels.db')
    cfg.local = True
    cfg.paper_trading = False
    cfg.orb_setups = False
    cfg.setup_study = False
    cfg.stocks = tuple(stocks)
    db = Store(cfg.db)
    db.initialize()
    return Engine(db, cfg), db


def seed_bars(db, symbol, n=60, end=NOW):
    items = []
    for i in range(n):
        ts = end - (n - 1 - i) * 60
        px = 100.0 + i * 0.1
        items.append((symbol, ts, {'o': px, 'h': px + 0.2, 'l': px - 0.2,
                                   'c': px + 0.1, 'v': 1000}))
    with db.tx() as c:
        db.append_bars(c, 'bar', 'fixture', items)
        db.put(c, 'latestbar:' + symbol, end)


def test_levels_pass_processes_symbol(tmp_path):
    engine, db = make_engine(tmp_path)
    seed_bars(db, 'SPY')
    engine._levels_pass(NOW + 60)
    with db.tx() as c:
        assert db.get(c, 'levels:SPY') is not None
        assert db.get(c, 'cursor:SPY') == NOW


def test_one_poison_symbol_does_not_stop_the_pass(tmp_path, monkeypatch):
    engine, db = make_engine(tmp_path, stocks=('TSLA', 'SPY'))
    seed_bars(db, 'TSLA')
    seed_bars(db, 'SPY')

    real_recent = Store.recent

    def poisoned(self, c, kind, symbol=None, limit=100, since=None):
        if symbol == 'TSLA':
            raise RuntimeError('transaction timeout (simulated)')
        return real_recent(self, c, kind, symbol, limit=limit, since=since)

    monkeypatch.setattr(Store, 'recent', poisoned)
    engine._levels_pass(NOW + 60)  # must not raise

    with db.tx() as c:
        # The healthy symbol still processed fully.
        assert db.get(c, 'levels:SPY') is not None
        assert db.get(c, 'cursor:SPY') == NOW
        # The poisoned symbol advanced nothing.
        assert db.get(c, 'levels:TSLA') is None
        assert db.get(c, 'cursor:TSLA') is None
        # And the failure is on record.
        err = db.get(c, 'health:levels_error')
        assert err and err['symbol'] == 'TSLA'
        assert 'transaction timeout' in err['error']
