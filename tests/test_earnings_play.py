"""Earnings play regression tests.

Bugs found 2026-10-19 (full-project audit):
1. get_earnings_gap read b['c']/b['o'] off db.recent() event rows, but bars
   are nested under 'payload' — KeyError crashed the scan every tick (the
   engine swallows it), so the strategy never fired.
2. The alert's publication.contract.symbol was the bare underlying ticker,
   so zero_dte_paper would book the stock itself as the option.
"""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass.earnings_play import get_earnings_gap, resolve_0dte_contract, scan_earnings_plays
from compass import zero_dte_paper

CT = ZoneInfo("America/Chicago")
# Wednesday 2026-10-21, 10:00 AM CT — inside the 9:45-10:30 entry window.
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=CT).timestamp()
PREV_TS = datetime(2026, 10, 20, 14, 0, tzinfo=CT).timestamp()
OPEN_TS = datetime(2026, 10, 21, 8, 30, tzinfo=CT).timestamp()
RECENT_TS = datetime(2026, 10, 21, 9, 55, tzinfo=CT).timestamp()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'earn.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _seed_bars(db, c):
    db.append_bars(c, 'bar', 'test', [
        ('NFLX', PREV_TS, {'o': 179, 'h': 181, 'l': 178, 'c': 180.0, 'v': 100}),
        ('NFLX', OPEN_TS, {'o': 190.0, 'h': 191, 'l': 189, 'c': 190.5, 'v': 200}),
        ('NFLX', RECENT_TS, {'o': 190.5, 'h': 193, 'l': 190, 'c': 192.0, 'v': 300}),
    ])


def _seed_chain(db, c):
    db.put(c, 'chain:NFLX', {'asof': NOW, 'contracts': [
        {'symbol': 'NFLX261021C00185000', 'type': 'call', 'strike': 185, 'expiry': '2026-10-21', 'multiplier': 100},
        {'symbol': 'NFLX261021C00190000', 'type': 'call', 'strike': 190, 'expiry': '2026-10-21', 'multiplier': 100},
        {'symbol': 'NFLX261023C00190000', 'type': 'call', 'strike': 190, 'expiry': '2026-10-23', 'multiplier': 100},
        {'symbol': 'NFLX261021P00190000', 'type': 'put', 'strike': 190, 'expiry': '2026-10-21', 'multiplier': 100},
    ]})


def _seed_earnings(db, c):
    db.put(c, 'intel:earnings:2026-10-20',
           {'opra_earnings': [{'symbol': 'NFLX', 'time': 'AMC', 'date': '2026-10-20'}]})


def test_gap_reads_nested_bar_payloads(db):
    with db.tx() as c:
        _seed_earnings(db, c)
        _seed_bars(db, c)
        info = get_earnings_gap(db, c, 'NFLX', NOW)
    assert info is not None
    assert info['prev_close'] == 180.0
    assert info['open_price'] == 190.0
    assert info['gap_pct'] == pytest.approx(5.56, abs=0.01)
    assert info['gap_direction'] == 'up'


def test_resolve_contract_picks_nearest_expiry_atm_call(db):
    with db.tx() as c:
        _seed_chain(db, c)
        contract = resolve_0dte_contract(db, c, 'NFLX', 'CALL', 192.0, NOW)
    assert contract['symbol'] == 'NFLX261021C00190000'
    assert contract['strike'] == 190
    assert contract['type'] == 'call'
    assert contract['expiry'] == '2026-10-21'


def test_resolve_contract_none_on_stale_chain(db):
    with db.tx() as c:
        _seed_chain(db, c)
        db.put(c, 'chain:NFLX', {'asof': NOW - 3600, 'contracts': [
            {'symbol': 'NFLX261021C00190000', 'type': 'call', 'strike': 190,
             'expiry': '2026-10-21', 'multiplier': 100}]})
        assert resolve_0dte_contract(db, c, 'NFLX', 'CALL', 192.0, NOW) is None


def test_scan_signal_carries_real_contract(db):
    cfg = SimpleNamespace(option_0dte_max_dte=7)
    with db.tx() as c:
        _seed_earnings(db, c)
        _seed_bars(db, c)
        _seed_chain(db, c)
        signals = scan_earnings_plays(db, c, cfg, NOW, ['NFLX'])
    assert len(signals) == 1
    assert signals[0]['contract']['symbol'] == 'NFLX261021C00190000'
    assert signals[0]['option_type'] == 'CALL'


def test_scan_without_chain_no_signal_and_not_marked_traded(db):
    cfg = SimpleNamespace(option_0dte_max_dte=7)
    with db.tx() as c:
        _seed_earnings(db, c)
        _seed_bars(db, c)
        signals = scan_earnings_plays(db, c, cfg, NOW, ['NFLX'])
        assert signals == []
        # Not marked traded: a later tick with a fresh chain can still fire.
        assert not db.get(c, 'earnings_traded:NFLX:2026-10-21')


def _seed_scanner_alert(db, c, contract):
    db.put(c, 'zero_dte_paper:scan_ts', NOW - 100)
    db.append(c, 'alert', 'setup_study', 'NFLX', NOW - 10, {
        'publication': {'category': 'options_0dte', 'contract': contract,
                        'status': 'option_setup_new'}}, key='earn-alert-1')


def test_scanner_rejects_bare_underlying_symbol(db):
    with db.tx() as c:
        _seed_scanner_alert(db, c, {'symbol': 'NFLX', 'underlying': 'NFLX'})
        opened = zero_dte_paper._open_scanner(db, c, NOW, NOW + 3600)
        assert opened == 0
        assert db.prefix(c, 'trade:0dte-') == {}


def test_scanner_accepts_real_option_contract(db):
    with db.tx() as c:
        _seed_scanner_alert(db, c, {'symbol': 'NFLX261021C00190000', 'underlying': 'NFLX'})
        db.put(c, 'quote:NFLX261021C00190000', {'bid': 2.0, 'ask': 2.1, 'ts': NOW})
        opened = zero_dte_paper._open_scanner(db, c, NOW, NOW + 3600)
        assert opened == 1
        trades = db.prefix(c, 'trade:0dte-')
        trade = next(iter(trades.values()))
        assert trade['symbol'] == 'NFLX261021C00190000'
        assert trade['entry'] == 2.11
