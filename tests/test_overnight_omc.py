"""Overnight Morning-Change forward scorer (2026-11-03, Josh):
registration freeze 23a1f84a — the range-relative direction rule,
contract selection (DTE [1,7], delta [0.30,0.50] target 0.40), the
G2 gate, the morning exit with its 09:30 ET freshness guards, the
earnings exclusion at exit-pricing time, the sample-start rule, and
the store.py table registration regression (the bb1adfa lesson)."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.active_observations import inventory
from compass.config import Config
from compass.market import session
from compass.overnight_omc import (OvernightOMC, direction_signal,
                                   earnings_verdict, episodes,
                                   fresh_morning_ts, select_contract)
from compass.store import Store

NY = ZoneInfo('America/New_York')
D0 = '2026-11-04'          # entry session (Wed)
D1 = '2026-11-05'          # exit session (Thu morning)
NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc).timestamp()
SYMBOLS = ['UPX', 'DOWNX', 'GATED', 'GAPX', 'EARNX', 'STALEX', 'FLATX']

FRESH_TS = datetime(2026, 11, 5, 9, 36, tzinfo=NY).timestamp()
STALE_TS = datetime(2026, 11, 5, 8, 0, tzinfo=NY).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'omc.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def omc_cfg():
    return Config(local=True, stocks=tuple(SYMBOLS), watchlist=(),
                  sector_etfs=(), swing_ideas=False, morning_enabled=False,
                  discovery=False)


def bar(o, h, l, c):
    return dict(o=o, h=h, l=l, c=c, v=10000)


def seed_bars(db):
    """11-03 closes set up 11-04 signals: UPX/EARNX/STALEX/GATED/GAPX
    close at the top of a decisive up day; DOWNX mirrors it down;
    FLATX closes mid-range (no signal)."""
    hist = {
        '2026-11-02': bar(100, 100.5, 99.5, 100),
        '2026-11-03': bar(100, 100.5, 99.5, 100),
    }
    up_day = bar(100.5, 104, 100, 103.9)     # loc .975, disp +.975
    down_day = bar(99.5, 100, 96, 96.1)     # loc .025, disp -.975
    flat_day = bar(100, 102, 98, 100)       # loc .50
    days = {'UPX': up_day, 'EARNX': up_day, 'STALEX': up_day,
            'GATED': up_day, 'GAPX': up_day, 'DOWNX': down_day,
            'FLATX': flat_day}
    with db.tx() as c:
        for sym in SYMBOLS:
            for d, b in hist.items():
                db.append(c, 'daily', 'test', sym, session(d)[0], b,
                          'day:' + sym + d)
            db.append(c, 'daily', 'test', sym, session(D0)[0], days[sym],
                      'day:' + sym + D0)


def contract(symbol, expiry, strike, typ, delta, bid, ask, ts=None, iv=0.30):
    return {'symbol': symbol, 'expiry': expiry, 'strike': strike, 'type': typ,
            'oi': 500, 'delta': delta, 'gamma': 0.05, 'theta': -0.03,
            'vega': 0.1, 'iv': iv, 'volume': 200,
            'quote': {'ts': NOW if ts is None else ts, 'bid': bid, 'ask': ask,
                      'bid_size': 5, 'ask_size': 5}}


def seed_snapshot(db, kind, sym, date_str, contracts, underlying_close,
                  close_ts=None):
    payload = {'date': date_str, 'symbol': sym, 'source': 'test',
               'complete': True, 'asof': NOW, 'snapshot_kind': kind,
               'underlying_close': underlying_close,
               'underlying_close_ts': NOW if close_ts is None else close_ts,
               'contracts': contracts}
    with db.tx() as c:
        db.append(c, kind, 'test', sym, NOW, payload,
                  kind + ':' + date_str + ':' + sym)


def seed_morning_manifest(db, ok_symbols, error_symbols=()):
    syms = {s: {'status': 'ok', 'contracts': 5} for s in ok_symbols}
    syms.update({s: {'status': 'error', 'error': 'boom'}
                 for s in error_symbols})
    with db.tx() as c:
        db.put(c, 'chain_open:' + D1,
               {'date': D1, 'started_at': NOW - 100, 'finished_at': NOW - 60,
                'running': False, 'fatal': None, 'symbols': syms})


def seed_all(db):
    seed_bars(db)
    # Entry snapshots (EOD of D0). DTE-3 expiry = 2026-11-07.
    seed_snapshot(db, 'chain_eod', 'UPX', D0, [
        contract('UPXC1', '2026-11-07', 104, 'call', 0.42, 1.90, 2.00),
        contract('UPXC2', '2026-11-07', 103, 'call', 0.45, 1.95, 2.05),
        contract('UPXP1', '2026-11-07', 104, 'put', -0.42, 1.0, 1.1),
        contract('UPXC0', '2026-11-04', 104, 'call', 0.40, 1.0, 1.1),
    ], 103.9)
    seed_snapshot(db, 'chain_eod', 'DOWNX', D0, [
        contract('DOWNP1', '2026-11-07', 96, 'put', -0.38, 1.50, 1.58),
    ], 96.1)
    seed_snapshot(db, 'chain_eod', 'GATED', D0, [
        contract('GATEDC1', '2026-11-07', 104, 'call', 0.40, 1.00, 2.10),
    ], 103.9)
    seed_snapshot(db, 'chain_eod', 'GAPX', D0, [
        contract('GAPXP1', '2026-11-07', 104, 'put', -0.40, 1.0, 1.1),
    ], 103.9)
    seed_snapshot(db, 'chain_eod', 'EARNX', D0, [
        contract('EARNXC1', '2026-11-07', 104, 'call', 0.40, 2.00, 2.10),
    ], 103.9)
    seed_snapshot(db, 'chain_eod', 'STALEX', D0, [
        contract('STALEXC1', '2026-11-07', 104, 'call', 0.40, 2.00, 2.10),
    ], 103.9)
    # Morning snapshots (D1) + manifest.
    seed_morning_manifest(db, SYMBOLS)
    seed_snapshot(db, 'chain_open', 'UPX', D1, [
        contract('UPXC1', '2026-11-07', 104, 'call', 0.55, 2.60, 2.70,
                 ts=FRESH_TS),
    ], 105.0, close_ts=FRESH_TS)
    seed_snapshot(db, 'chain_open', 'DOWNX', D1, [
        contract('DOWNP1', '2026-11-07', 96, 'put', -0.55, 1.20, 1.30,
                 ts=FRESH_TS),
    ], 97.0, close_ts=FRESH_TS)
    seed_snapshot(db, 'chain_open', 'EARNX', D1, [
        contract('EARNXC1', '2026-11-07', 104, 'call', 0.60, 3.00, 3.10,
                 ts=FRESH_TS),
    ], 104.0, close_ts=FRESH_TS)
    seed_snapshot(db, 'chain_open', 'STALEX', D1, [
        contract('STALEXC1', '2026-11-07', 104, 'call', 0.60, 3.00, 3.10,
                 ts=STALE_TS),
    ], 104.0, close_ts=FRESH_TS)
    seed_snapshot(db, 'chain_open', 'GATED', D1, [], 104.5, close_ts=FRESH_TS)
    seed_snapshot(db, 'chain_open', 'GAPX', D1, [], 104.5, close_ts=FRESH_TS)
    # Exit-session EOD snapshot for the secondary readout (UPX only).
    seed_snapshot(db, 'chain_eod', 'UPX', D1, [
        contract('UPXC1', '2026-11-07', 104, 'call', 0.50, 2.40, 2.50),
    ], 104.8)
    # Earnings: EARNX announces on the exit session -> excluded.
    with db.tx() as c:
        db.put(c, 'intel:earnings:' + D0,
               {'date': D0, 'opra_earnings': [],
                'all_earnings': [{'symbol': 'EARNX', 'date': D1,
                                  'time': 'BMO'}],
                'total_events': 1, 'fetched_at': 'test'})


def get_episodes(db):
    with db.tx() as c:
        return {p['symbol']: p for p in
                c.execute(select(episodes.c.payload)).scalars().all()}


# ---------------------------------------------------------------------------
# Pure helpers

def test_direction_signal_rule():
    up = bar(100.5, 104, 100, 103.9)
    side, loc, disp = direction_signal(up, 100.0)
    assert side == 'up' and loc == pytest.approx(0.975)
    assert disp == pytest.approx(0.975)
    down = bar(99.5, 100, 96, 96.1)
    assert direction_signal(down, 100.0)[0] == 'down'
    # Closed at highs but the day itself went nowhere vs prior close:
    # loc passes, disp fails -> no signal (the M3 distinction).
    assert direction_signal(bar(100.2, 104, 100, 103.9), 103.8)[0] is None
    # Strong displacement but faded into the close: disp passes on the
    # day, loc fails -> no signal.
    assert direction_signal(bar(96, 104, 96, 98.0), 92.0)[0] is None
    # No range / no prior close -> no signal.
    assert direction_signal(bar(5, 5, 5, 5), 5.0)[0] is None
    assert direction_signal(up, None)[0] is None


def test_select_contract_bands():
    snap = {'date': D0, 'underlying_close': 100.0, 'contracts': [
        contract('C0DTE', D0, 100, 'call', 0.40, 1.0, 1.1),
        contract('C8DTE', '2026-11-12', 100, 'call', 0.40, 1.0, 1.1),
        contract('CHI', '2026-11-07', 100, 'call', 0.55, 1.0, 1.1),
        contract('CLO', '2026-11-07', 100, 'call', 0.29, 1.0, 1.1),
        contract('CPUT', '2026-11-07', 100, 'put', -0.40, 1.0, 1.1),
        contract('CWIN', '2026-11-07', 101, 'call', 0.41, 1.0, 1.1),
        contract('CNEAR', '2026-11-05', 99, 'call', 0.38, 1.0, 1.1),
    ]}
    # DTE 0 and 8 are out of band; delta .55/.29 out; the put is the
    # other side. Between the two in-band calls, |delta-.40| decides.
    assert select_contract(snap, 'up')['symbol'] == 'CWIN'
    assert select_contract(snap, 'down')['symbol'] == 'CPUT'


def test_fresh_morning_ts():
    assert fresh_morning_ts(datetime(2026, 11, 5, 9, 30, tzinfo=NY).timestamp(), D1)
    assert fresh_morning_ts(FRESH_TS, D1)
    assert not fresh_morning_ts(datetime(2026, 11, 5, 9, 29, tzinfo=NY).timestamp(), D1)
    assert not fresh_morning_ts(STALE_TS, D1)
    assert not fresh_morning_ts(FRESH_TS, D0)   # right time, wrong date
    assert not fresh_morning_ts(None, D1)


def test_earnings_verdict_rule():
    evs = [{'symbol': 'AAA', 'date': D1, 'time': 'AMC'},
           {'symbol': 'BBB', 'date': D0, 'time': 'BMO'},
           {'symbol': 'CCC', 'date': D0, 'time': 'AMC'},
           {'symbol': 'DDD', 'date': D0, 'time': None},
           {'symbol': 'EEE', 'date': '2026-11-09', 'time': 'AMC'}]
    assert earnings_verdict(evs, 'AAA', D0, D1) is not None   # exit-date
    assert earnings_verdict(evs, 'BBB', D0, D1) is None       # entry BMO
    assert earnings_verdict(evs, 'CCC', D0, D1) is not None   # entry AMC
    assert earnings_verdict(evs, 'DDD', D0, D1) is not None   # unknown
    assert earnings_verdict(evs, 'EEE', D0, D1) is None       # other date
    assert earnings_verdict(evs, 'ZZZ', D0, D1) is None       # other name


# ---------------------------------------------------------------------------
# Lifecycle

def test_full_lifecycle(db):
    seed_all(db)
    scorer = OvernightOMC(db, omc_cfg())
    scorer.process(NOW)
    eps = get_episodes(db)
    assert set(eps) == {'UPX', 'DOWNX', 'GATED', 'GAPX', 'EARNX', 'STALEX'}
    with db.tx() as c:
        state = db.get(c, 'overnight_omc:state', {})
        assert state['sample_start'] == D0  # stamped, §11
        assert state['counts'] == {
            'created': 6, 'opened': 4, 'closed': 2, 'open': 0,
            'data_gap': 2, 'gate_excluded': 1, 'earnings_excluded': 1}
        assert state['underlying_crosscheck']['n'] == 2
        assert state['gate_excluded_underlying']['n'] == 1
        assert state['earnings_excluded_underlying']['n'] == 1
        assert state['secondary_eod_exit']['n'] == 1
        inv = inventory(db, c, NOW)
        assert 'UPXC1' not in inv['options']  # resolved: nothing pinned
    up = eps['UPX']
    assert up['status'] == 'closed' and up['outcome'] == 'win'
    assert up['entry']['contract_symbol'] == 'UPXC1'  # decoys rejected
    assert up['pnl'] == pytest.approx((2.60 - 2.00) * 100 - 1.30)
    assert up['underlying_morning']['bps'] == pytest.approx(
        (105.0 - 103.9) / 103.9 * 1e4)
    assert up['secondary_eod']['pnl'] == pytest.approx(
        (2.40 - 2.00) * 100 - 1.30)
    assert up['signal']['side'] == 'up' and up['signal']['exit_date'] == D1
    down = eps['DOWNX']
    assert down['status'] == 'closed'
    assert down['pnl'] == pytest.approx((1.20 - 1.58) * 100 - 1.30)
    assert down['underlying_morning']['bps'] == pytest.approx(
        -(97.0 - 96.1) / 96.1 * 1e4)
    gated = eps['GATED']
    assert gated['status'] == 'gate_excluded' and gated['entry'] is None
    assert gated['underlying_morning'] is not None  # gate gets graded
    gap = eps['GAPX']
    assert gap['status'] == 'data_gap'
    assert gap['entry_data_gap']['reason'] == 'no_eligible_executable_contract'
    earn = eps['EARNX']
    assert earn['status'] == 'earnings_excluded' and earn['pnl'] is None
    assert earn['earnings_exclusion']['event_date'] == D1
    assert earn['underlying_morning'] is not None  # excluded size prints
    stale = eps['STALEX']
    assert stale['status'] == 'data_gap'
    assert stale['exit_data_gap']['reason'] == 'stale_morning_quote'
    kinds = [r['payload']['event'] for r in _events(db)]
    for want in ('bootstrap', 'session_census', 'episode_created',
                 'entry_priced', 'gate_excluded', 'entry_data_gap',
                 'earnings_excluded', 'exit_priced', 'exit_data_gap'):
        assert want in kinds, want


def _events(db):
    with db.tx() as c:
        return db.recent(c, 'overnight_omc', limit=500)


def test_sample_waits_for_morning_substrate(db):
    """No chain_open manifest anywhere -> no sample start, no
    episodes, and the state says so (never backfilled later)."""
    seed_bars(db)
    seed_snapshot(db, 'chain_eod', 'UPX', D0, [
        contract('UPXC1', '2026-11-07', 104, 'call', 0.42, 1.90, 2.00),
    ], 103.9)
    scorer = OvernightOMC(db, omc_cfg())
    scorer.process(NOW)
    assert get_episodes(db) == {}
    with db.tx() as c:
        state = db.get(c, 'overnight_omc:state', {})
        assert not state.get('sample_start')
        assert state['counts']['created'] == 0


def test_morning_manifest_symbol_error_is_data_gap(db):
    seed_all(db)
    # STALEX's morning snapshot failed at the manifest level instead.
    with db.tx() as c:
        m = db.get(c, 'chain_open:' + D1)
        m['symbols']['STALEX'] = {'status': 'error', 'error': 'boom'}
        db.put(c, 'chain_open:' + D1, m)
    scorer = OvernightOMC(db, omc_cfg())
    scorer.process(NOW)
    stale = get_episodes(db)['STALEX']
    assert stale['status'] == 'data_gap'
    assert stale['exit_data_gap']['reason'] == 'symbol_error'


def test_open_episode_pins_contract(db):
    """Mid-flight (entry priced, morning not yet decided): the
    contract is pinned in the OPRA inventory (scorecard §4.1)."""
    seed_bars(db)
    seed_snapshot(db, 'chain_eod', 'UPX', D0, [
        contract('UPXC1', '2026-11-07', 104, 'call', 0.42, 1.90, 2.00),
    ], 103.9)
    # Sample start already stamped by an earlier bootstrap; D1's
    # morning snapshot has not landed yet (no manifest at all), so
    # the exit cannot decide and the episode stays open.
    with db.tx() as c:
        db.put(c, 'overnight_omc:state', {'sample_start': D0})
    scorer = OvernightOMC(db, omc_cfg())
    scorer.process(datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc).timestamp())
    eps = get_episodes(db)
    assert eps['UPX']['status'] == 'open' and eps['UPX']['entry']
    with db.tx() as c:
        inv = inventory(db, c, NOW)
        assert 'UPXC1' in inv['options']


def test_omc_episodes_table_created_without_module_import(tmp_path):
    """Production import order (regression, the bb1adfa lesson): a
    fresh interpreter that never imports overnight_omc (or
    compass.app) must still get the table from compass.store alone."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    code = (
        "from compass.store import Store\n"
        "from sqlalchemy import inspect\n"
        f"s = Store('sqlite:///{tmp_path}/fresh.db')\n"
        "s.initialize()\n"
        "names = inspect(s.engine).get_table_names()\n"
        "assert 'overnight_omc_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]
