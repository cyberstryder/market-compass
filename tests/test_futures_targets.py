"""Regression: future_targets() must never emit non-raw symbols.

On 2026-09-29 the ICT paper detectors opened MES/MNQ positions whose trade
symbols are configured aliases (e.g. 'MES.c.0'). future_targets() added those
aliases verbatim into the Databento subscription made with
stype_in='raw_symbol'. Databento rejected the subscription, the stream died,
and the supervisor's reconnect loop re-sent the same poisoned symbol list for
~20 hours while MES/MNQ quotes and bars stayed frozen.

Subscriptions must contain only dated raw contracts (e.g. 'MESZ6'): alias
position symbols resolve to the lead contract as of entry, dated position
symbols pass through unchanged.
"""
from datetime import datetime

from compass.config import Config
from compass.futures import lead_contract, risk_day
from compass.instruments import DATED
from compass.market import CT
from compass.providers import Collectors
from compass.store import Store

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=CT).timestamp()


def make_collector(tmp_path, monkeypatch):
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    db = Store('sqlite:///' + str(tmp_path / 'targets.db'))
    db.initialize()
    return db, Collectors(db, Config(local=True, databento='test-key'))


def test_alias_position_symbol_resolves_to_raw_lead(tmp_path, monkeypatch):
    db, collector = make_collector(tmp_path, monkeypatch)
    with db.tx() as c:
        db.put(c, 'position:ict:turtle_soup:MES.c.0',
               {'status': 'open', 'asset': 'future', 'symbol': 'MES.c.0',
                'entered_at': NOW})
        db.put(c, 'position:ict:turtle_soup:MNQ.c.0',
               {'status': 'open', 'asset': 'future', 'symbol': 'MNQ.c.0',
                'entered_at': NOW})
    _, symbols = collector.future_targets()
    assert 'MES.c.0' not in symbols
    assert 'MNQ.c.0' not in symbols
    assert all(DATED.fullmatch(s) for s in symbols), symbols
    assert lead_contract('MES', risk_day(NOW))['raw_symbol'] in symbols
    assert lead_contract('MNQ', risk_day(NOW))['raw_symbol'] in symbols


def test_dated_position_symbol_passes_through(tmp_path, monkeypatch):
    db, collector = make_collector(tmp_path, monkeypatch)
    with db.tx() as c:
        db.put(c, 'position:ict:turtle_soup:MES.c.0',
               {'status': 'open', 'asset': 'future', 'symbol': 'MESZ6@42001581',
                'entered_at': NOW})
    _, symbols = collector.future_targets()
    assert 'MESZ6' in symbols
    assert all(DATED.fullmatch(s) for s in symbols), symbols


def test_closed_and_non_index_positions_ignored(tmp_path, monkeypatch):
    db, collector = make_collector(tmp_path, monkeypatch)
    with db.tx() as c:
        db.put(c, 'position:ict:turtle_soup:MES.c.0',
               {'status': 'closed', 'asset': 'future', 'symbol': 'MES.c.0',
                'entered_at': NOW})
        db.put(c, 'position:ict:turtle_soup:SIL.v.0',
               {'status': 'open', 'asset': 'future', 'symbol': 'SIL.v.0',
                'entered_at': NOW})
    _, symbols = collector.future_targets()
    assert all(DATED.fullmatch(s) for s in symbols), symbols
    assert 'SIL.v.0' not in symbols
