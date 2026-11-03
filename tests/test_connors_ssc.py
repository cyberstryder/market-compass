"""Connors RSI(2) SSC forward premium scorer (2026-11-03, Josh):
registration freeze f9ae84a1 — signal parity against the SSC gauntlet,
contract selection + G2 gate, episode lifecycle on the chain archive,
occupancy, the 2026-11-03 sample rule, and the store.py table
registration regression (the bb1adfa lesson)."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.active_observations import inventory
from compass.config import Config
from compass.connors_ssc import (ConnorsSSC, counted_trades, episodes,
                                 exit_quote_iv, select_contract, sma_series,
                                 spread_gate, wilder_rsi2)
from compass.market import session
from compass.store import Store
from compass.swing_signals import sessions_between

NOW = datetime(2027, 1, 4, 15, 0, tzinfo=timezone.utc).timestamp()
SESSIONS = list(sessions_between('2025-11-03', '2026-12-31'))
FREEZE = '2026-11-03'
SYMBOLS = ['SIG', 'GATED', 'GAP', 'ZERO', 'MISSX', 'OLD']


def first_on_or_after(dstr):
    return next(d for d in SESSIONS if d >= dstr)


PRE_DIP = first_on_or_after('2026-09-15')    # pre-freeze dip (OLD only)
POST_DIP = first_on_or_after('2026-11-16')   # post-freeze dip (all symbols)
DIP_I = SESSIONS.index(POST_DIP)
EXIT_D = SESSIONS[DIP_I + 2]


def closes_for(sym):
    """Gentle +0.08/day uptrend (close stays above its SMA(200)) with
    two-day -2.00 dips: RSI(2) prints ~3.8 on the first dip day (a
    signal) and lower on the second (occupied -> skipped)."""
    dips = {SESSIONS.index(PRE_DIP), SESSIONS.index(PRE_DIP) + 1}
    if sym == 'OLD':
        dips |= {DIP_I, DIP_I + 1}
    elif sym in SYMBOLS:
        dips |= {DIP_I, DIP_I + 1}
    out, level = {}, 100.0
    for i, d in enumerate(SESSIONS):
        if i == 0:
            level = 100.0
        elif i in dips:
            level -= 2.0
        else:
            level += 0.08
        out[d] = level
    return out


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'connors_ssc.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def ssc_cfg():
    return Config(local=True, stocks=tuple(SYMBOLS), watchlist=(),
                  sector_etfs=(), swing_ideas=False, morning_enabled=False,
                  discovery=False)


def seed_bars(db):
    with db.tx() as c:
        for sym in SYMBOLS:
            closes = closes_for(sym)
            prev = None
            for d in SESSIONS:
                close = closes[d]
                bar = dict(o=prev if prev is not None else close,
                           h=close * 1.005, l=close * 0.995, c=close, v=10000)
                db.append(c, 'daily', 'test', sym, session(d)[0], bar,
                          'day:' + sym + d)
                prev = close


def contract(symbol, expiry, strike, typ, delta, bid, ask, iv=0.30):
    return {'symbol': symbol, 'expiry': expiry, 'strike': strike, 'type': typ,
            'oi': 500, 'delta': delta, 'gamma': 0.05, 'theta': -0.03,
            'vega': 0.1, 'iv': iv, 'volume': 200,
            'quote': {'ts': NOW, 'bid': bid, 'ask': ask,
                      'bid_size': 5, 'ask_size': 5}}


def seed_snapshot(db, sym, date_str, contracts, underlying_close):
    payload = {'date': date_str, 'symbol': sym, 'source': 'test',
               'complete': True, 'asof': NOW,
               'underlying_close': underlying_close, 'contracts': contracts}
    with db.tx() as c:
        db.append(c, 'chain_eod', 'test', sym, NOW, payload,
                  'chain_eod:' + date_str + ':' + sym)


def expiry(days_after):
    return (date.fromisoformat(POST_DIP) + timedelta(days=days_after)).isoformat()


def seed_entry_snapshots(db):
    spot = closes_for('SIG')[POST_DIP]
    # SIG: the in-band delta-0.50 pick must win over an out-of-band DTE
    # decoy and a delta-0.52 in-band decoy.
    seed_snapshot(db, 'SIG', POST_DIP, [
        contract('SIGC1', expiry(9), round(spot), 'call', 0.50, 2.00, 2.10),
        contract('SIGC2', expiry(20), round(spot), 'call', 0.50, 0.95, 1.00),
        contract('SIGC3', expiry(9), round(spot) + 5, 'call', 0.52, 1.90, 1.95),
        contract('SIGP1', expiry(9), round(spot), 'put', -0.50, 1.0, 1.1),
    ], spot)
    # GATED: the only eligible pick fails G2 (spread 1.10 > gate 0.315).
    seed_snapshot(db, 'GATED', POST_DIP, [
        contract('GATEDC1', expiry(9), round(spot), 'call', 0.50, 1.00, 2.10),
    ], spot)
    # GAP: nothing eligible (a put and a delta-less call).
    seed_snapshot(db, 'GAP', POST_DIP, [
        contract('GAPP1', expiry(9), round(spot), 'put', -0.50, 1.0, 1.1),
        contract('GAPC9', expiry(9), round(spot), 'call', None, 1.0, 1.1),
    ], spot)
    for sym in ('ZERO', 'MISSX'):
        seed_snapshot(db, sym, POST_DIP, [
            contract(sym + 'C1', expiry(9), round(spot), 'call', 0.50, 2.00, 2.10),
        ], spot)
    # OLD intentionally gets no snapshot: its episode stays awaiting entry.


def seed_exit_snapshots(db):
    spot = closes_for('SIG')[EXIT_D]
    seed_snapshot(db, 'SIG', EXIT_D, [
        contract('SIGC1', expiry(9), 100, 'call', 0.55, 2.60, 2.70, iv=0.42),
    ], spot)
    seed_snapshot(db, 'ZERO', EXIT_D, [
        contract('ZEROC1', expiry(9), 100, 'call', 0.05, 0.00, 0.05, iv=0.60),
    ], spot)
    seed_snapshot(db, 'MISSX', EXIT_D, [
        contract('OTHER1', expiry(9), 100, 'call', 0.50, 1.00, 1.10),
    ], spot)


def get_episodes(db):
    with db.tx() as c:
        return {p['symbol']: p for p in
                c.execute(select(episodes.c.payload)).scalars().all()}


def at(d, hour=15):
    return datetime.fromisoformat(d).replace(hour=hour, tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Pure math

def test_wilder_rsi2_hand_computed():
    rsi = wilder_rsi2([10.0, 11.0, 10.5, 11.5])
    assert rsi[0] is None and rsi[1] is None
    # seed: ag=(1+0)/2=0.5, al=(0+0.5)/2=0.25 -> 100-100/(1+2)=66.67
    assert rsi[2] == pytest.approx(66.6667, abs=1e-3)
    # i=3: ag=(0.5+1)/2=0.75, al=(0.25+0)/2=0.125 -> 100-100/(1+6)=85.71
    assert rsi[3] == pytest.approx(85.7143, abs=1e-3)
    assert wilder_rsi2([1.0, 2.0, 3.0, 4.0])[3] == 100.0  # no losses
    sma = sma_series([float(i) for i in range(1, 202)])
    assert sma[198] is None and sma[199] == pytest.approx(100.5)
    assert sma[200] == pytest.approx(101.5)


def test_select_contract_bands_and_tiebreak():
    snap = {'date': '2026-11-16', 'underlying_close': 100.0, 'contracts': [
        contract('D4', '2026-11-20', 100, 'call', 0.50, 1.0, 1.1),   # DTE 4: out
        contract('D15', '2026-12-01', 100, 'call', 0.50, 1.0, 1.1),  # DTE 15: out
        contract('LO', '2026-11-25', 100, 'call', 0.29, 1.0, 1.1),   # delta out
        contract('HI', '2026-11-25', 100, 'call', 0.71, 1.0, 1.1),   # delta out
        contract('ND', '2026-11-25', 100, 'call', None, 1.0, 1.1),   # no delta
        contract('NB', '2026-11-25', 100, 'call', 0.50, 0.0, 1.1),   # no bid
        contract('PUT', '2026-11-25', 100, 'put', -0.50, 1.0, 1.1),  # put
        contract('FAR', '2026-11-28', 100, 'call', 0.40, 1.0, 1.1),  # |d-.5|=.10
        contract('NEAR', '2026-11-25', 100, 'call', 0.55, 1.0, 1.1),  # |d-.5|=.05
    ]}
    assert select_contract(snap)['symbol'] == 'NEAR'  # delta leads the tie-break
    # Equal |delta-0.50|: expiry closest to 7 DTE wins.
    snap2 = {'date': '2026-11-16', 'underlying_close': 100.0, 'contracts': [
        contract('E12', '2026-11-28', 100, 'call', 0.48, 1.0, 1.1),
        contract('E7', '2026-11-23', 100, 'call', 0.52, 1.0, 1.1),
    ]}
    assert select_contract(snap2)['symbol'] == 'E7'
    assert select_contract({'date': '2026-11-16', 'contracts': []}) is None
    zero = {'contracts': [contract('Z', '2026-11-25', 100, 'call', 0.5, 0.0, 0.4)]}
    assert exit_quote_iv(zero, 'Z') == (0.0, 0.4, NOW, 0.30)  # worthless is a price
    assert exit_quote_iv(zero, 'ZZ') is None


def test_spread_gate_boundaries():
    spread, gate, ok = spread_gate(2.00, 2.10)
    assert ok and spread == pytest.approx(0.10) and gate == pytest.approx(0.315)
    spread, gate, ok = spread_gate(0.85, 1.00)   # spread == gate passes
    assert ok and spread == pytest.approx(0.15) and gate == pytest.approx(0.15)
    spread, gate, ok = spread_gate(0.10, 0.20)   # $0.05 floor binds
    assert not ok and gate == pytest.approx(0.05)


def test_signal_parity_with_gauntlet():
    """The forward generator must BE the gauntlet's signal: counted
    trades, raw signal totals, and occupancy skips from the module's
    counted_trades over panel daily bars equal the SSC gauntlet's own
    outputs (hidden_files/connors-rsi2-ssc-2026-11-03)."""
    fx = json.loads((Path(__file__).parent / 'fixtures'
                     / 'connors_ssc_parity.json').read_text())
    for sym, d in fx['symbols'].items():
        trades, events = counted_trades(d['bars'])
        assert [t['date'] for t in trades] == d['trades'], sym
        assert len(events) == d['signals'], sym
        assert sum(1 for v in events.values() if v == 'skipped') == d['skipped'], sym


# ---------------------------------------------------------------------------
# Lifecycle

def test_full_lifecycle_gate_gap_and_counts(db):
    seed_bars(db)
    seed_entry_snapshots(db)
    seed_exit_snapshots(db)
    scorer = ConnorsSSC(db, ssc_cfg())
    scorer.process(at(SESSIONS[DIP_I + 1]))  # through the dip's next session
    eps = get_episodes(db)
    assert set(eps) == set(SYMBOLS)
    sig = eps['SIG']
    assert sig['status'] == 'open' and sig['premium_status'] == 'open'
    assert sig['entry']['contract_symbol'] == 'SIGC1'  # decoys rejected
    assert sig['entry']['ask'] == 2.10 and sig['entry']['iv'] == 0.30
    assert sig['planned_loss'] == pytest.approx(211.30)
    assert sig['signal']['date'] == POST_DIP and sig['signal']['rsi2'] <= 5
    with db.tx() as c:
        inv = inventory(db, c, at(SESSIONS[DIP_I + 1]))
        assert 'SIGC1' in inv['options']  # §4.1 pinning while open
        census = db.get(c, 'connors_ssc:state', {})['depth_census']
        assert census['eligible_count'] == len(SYMBOLS)
    scorer.process(NOW)  # through year end: exits stamp and price
    eps = get_episodes(db)
    sig = eps['SIG']
    assert sig['status'] == 'closed' and sig['outcome'] == 'win'
    assert sig['pnl'] == pytest.approx((2.60 - 2.10) * 100 - 1.30)
    assert sig['pnl_mid'] == pytest.approx((2.65 - 2.05) * 100 - 1.30)
    assert sig['friction_gap'] == pytest.approx(10.0)
    assert sig['exit']['iv'] == 0.42 and sig['exit']['reason'] == 'ssc_close'
    assert sig['underlying_exit']['date'] == EXIT_D
    assert sig['underlying_exit']['r'] == pytest.approx(
        (sig['underlying_exit']['price'] - sig['signal']['close'])
        / sig['signal']['risk'])
    gated = eps['GATED']
    assert gated['status'] == 'gate_excluded' and gated['entry'] is None
    assert gated['gate_exclusion']['contract_symbol'] == 'GATEDC1'
    assert gated['gate_exclusion']['spread'] == pytest.approx(1.10)
    assert gated['underlying_exit'] is not None  # gate itself gets graded
    gap = eps['GAP']
    assert gap['status'] == 'data_gap' and gap['entry'] is None
    assert gap['underlying_exit'] is not None  # underlying record completes
    zero = eps['ZERO']
    assert zero['status'] == 'closed'  # bid 0.00 is executable, not data_gap
    assert zero['pnl'] == pytest.approx((0.0 - 2.10) * 100 - 1.30)
    missx = eps['MISSX']
    assert missx['status'] == 'data_gap' and missx['exit'] is None
    assert missx['exit_data_gap']['reason'] == 'held_contract_absent_or_unquoted'
    old = eps['OLD']
    assert old['signal']['date'] == POST_DIP  # post-freeze episode exists...
    with db.tx() as c:
        counts = db.get(c, 'connors_ssc:state', {})['counts']
        assert counts == {'created': 6, 'opened': 3, 'closed': 2, 'open': 1,
                          'data_gap': 2, 'gate_excluded': 1}
        state = db.get(c, 'connors_ssc:state', {})
        assert state['underlying_crosscheck']['n'] == 2
        assert state['gate_excluded_underlying']['n'] == 1
        assert state['ask_lines']['1.00_5.00']['n'] == 2
        kinds = [r['payload']['event'] for r in db.recent(c, 'connors_ssc', limit=500)]
        for want in ('bootstrap', 'session_census', 'episode_created',
                     'entry_priced', 'gate_excluded', 'entry_data_gap',
                     'exit_triggered', 'exit_priced', 'exit_data_gap',
                     'underlying_closed_gate_excluded'):
            assert want in kinds, want
        censuses = [r['payload'] for r in db.recent(c, 'connors_ssc', limit=500)
                    if r['payload'].get('event') == 'session_census']
        assert all(p['date'] >= FREEZE for p in censuses)  # sample rule
        dip_next = next(p for p in censuses
                        if p['date'] == SESSIONS[DIP_I + 1])
        assert dip_next['skipped'] >= 1  # second dip day: occupied, counted


def test_sample_rule_no_backfill(db):
    seed_bars(db)
    scorer = ConnorsSSC(db, ssc_cfg())
    scorer.process(NOW)
    eps = get_episodes(db)
    # OLD dipped pre-freeze (Sep) and post-freeze (Nov): exactly one
    # episode, from the post-freeze signal. Nothing predates the freeze.
    assert len(eps) == len(SYMBOLS)
    assert all(p['signal']['date'] >= FREEZE for p in eps.values())
    assert eps['OLD']['signal']['date'] == POST_DIP


def test_connors_episodes_table_created_without_module_import(tmp_path):
    """Production import order (regression, the bb1adfa lesson):
    Store.initialize() runs meta.create_all at engine boot, BEFORE
    app.py lazily imports compass.connors_ssc. A fresh interpreter
    that never imports connors_ssc (or compass.app) must still get
    the table from compass.store alone."""
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
        "assert 'connors_ssc_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]
