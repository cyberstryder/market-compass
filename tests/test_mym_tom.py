"""MYM Turn-of-Month forward shadow (2026-11-18, Josh):
registration files/mym-tom-forward-shadow-registration-2026-11-18.md
under the gauntlet registration
files/mym-session-rules-registration-2026-11-10.md (sha c5c27d5f).

Pins: (1) the build-blocking PARITY GATE — the Compass machine
reproduces the 2026-11-10 gauntlet's 47 MYM TOM trades exactly
(embedded real-data fixture for CI; the full year-1 panel run
whenever the goal workspace is present); (2) the Pine trading-day
calendar semantics (first-session init, month change, tdCount,
isLast via next weekday with holidays NOT skipped, short weeks);
(3) machine fill semantics (second-bar entry + 1 tick, 15:00 exit
- 1 tick, fallback exit, one trade per session); (4) the live
lifecycle on a sqlite store (bootstrap watermark, sample rule,
episodes, alerts, state summary); (5) the store.py table
registration regression; (6) the /api/simple tracking block.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.config import Config
from compass.mym_tom import (episodes, new_state, on_bar, run_bars,
                             scan)
from compass.store import Store, mym_tom_push_outbox

FIXTURE = json.loads((Path(__file__).parent / 'fixtures'
                      / 'mym_tom_parity.json').read_text())
GOAL_HIDDEN = ('/home/hatch/workspace/goals/market-compass-trading-'
               'improvements/hidden_files')
GAUNTLET_DIR = os.path.join(GOAL_HIDDEN, 'mym-session-rules-2026-11-10')
SURV_HARNESS = os.path.join(
    GOAL_HIDDEN, 'claude-survivors-gauntlet-2026-10-28', 'harness_claude')
TRADE_FIELDS = (('sdate', 'sdate'), ('entry_idx', 'i'),
                ('exit_idx', 'j'), ('entry_fill', 'entry'),
                ('exit_fill', 'exit'), ('net', 'pnl'),
                ('stress_net', 'pnl_stress'))


def _assert_trades_equal(got, want):
    assert len(got) == len(want)
    for g, w in zip(got, want):
        for gf, wf in TRADE_FIELDS:
            assert g[gf] == w[wf], (gf, g, w)


# ---------------------------------------------------------------------------
# Parity gate

def test_parity_fixture():
    """Embedded real-data fixture: the panel's first eight sessions
    (2025-10-27 -> 2025-11-05) reproduce the gauntlet's four TOM
    trades in that window row-for-row (all fields exact)."""
    bars = [{'ts': r[0], 'o': r[1], 'h': r[2], 'l': r[3], 'c': r[4]}
            for r in FIXTURE['bars']]
    trades, _ = run_bars(bars)
    _assert_trades_equal(trades, FIXTURE['trades'])


@pytest.mark.skipif(not os.path.isdir(GAUNTLET_DIR),
                    reason='goal workspace not present (CI)')
def test_parity_full_panel():
    """THE parity gate: the full year-1 MYM panel (loaded via the
    gauntlet's own sim.load path, degraded bars dropped, Panama
    back-adjusted) reproduces the gauntlet's 47 TOM trades exactly,
    and the cell's headline stats match the frozen record."""
    spec = importlib.util.spec_from_file_location(
        'sim', os.path.join(SURV_HARNESS, 'sim.py'))
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)
    df = sim.load('MYM')
    bars = [{'ts': int(ts), 'o': float(o), 'h': float(h),
             'l': float(l), 'c': float(c)}
            for ts, o, h, l, c in zip(df.ts.values, df.o.values,
                                      df.h.values, df.l.values,
                                      df.c.values)]
    trades, _ = run_bars(bars)
    import csv
    with open(os.path.join(GAUNTLET_DIR, 'results',
                           'trades_MYM_TOM.csv')) as f:
        want = [{'sdate': row['sdate'], 'i': int(row['i']),
                 'j': int(row['j']), 'entry': float(row['entry']),
                 'exit': float(row['exit']), 'pnl': float(row['pnl']),
                 'pnl_stress': float(row['pnl_stress'])}
                for row in csv.DictReader(f)]
    _assert_trades_equal(trades, want)
    assert len(trades) == 47
    net = sum(t['net'] for t in trades)
    stress = sum(t['stress_net'] for t in trades)
    gross_win = sum(t['net'] for t in trades if t['net'] > 0)
    gross_loss = -sum(t['net'] for t in trades if t['net'] < 0)
    assert net == 2655.0
    assert stress == 2561.0
    assert round(gross_win / gross_loss, 3) == 2.005


# ---------------------------------------------------------------------------
# Trading-day calendar semantics (synthetic sessions)
#
# A minimal session: the 17:00 trigger bar, the 17:01 fill bar, one
# overnight bar, and the 15:00 exit bar. Prices are flat except where
# a test moves them; the calendar verdicts are read off the machine's
# 'session' events.

def _session_bars(sdate, o2=101.0, x1500=110.0, with_exit=True):
    bars = [{'sdate': sdate, 'tod': 1020, 'o': 100.0, 'h': 100.0,
             'l': 100.0, 'c': 100.0, 'ts': None},
            {'sdate': sdate, 'tod': 1021, 'o': o2, 'h': o2,
             'l': o2, 'c': o2, 'ts': None},
            {'sdate': sdate, 'tod': 60, 'o': 105.0, 'h': 105.0,
             'l': 105.0, 'c': 105.0, 'ts': None}]
    if with_exit:
        bars.append({'sdate': sdate, 'tod': 900, 'o': x1500,
                     'h': x1500, 'l': x1500, 'c': x1500, 'ts': None})
    return bars


def _run_sessions(sessions):
    st = new_state()
    events = []
    for bars in sessions:
        for b in bars:
            events.extend(on_bar(st, b))
    return st, events


def _session_events(events):
    return [e for e in events if e['type'] == 'session']


def test_calendar_first_session_init_and_month_change():
    """Pine init: the first observed session is count 0 / known
    false (never TOM by count); the count increments within the
    month while known stays false; the month change sets count 1
    and known true, and counts 1-3 are TOM."""
    dates = ['2025-11-24', '2025-11-25', '2025-11-26',
             '2025-12-01', '2025-12-02', '2025-12-03', '2025-12-04']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    sev = _session_events(events)
    assert [(e['tdcount'], e['known']) for e in sev] == [
        (0, False), (1, False), (2, False),
        (1, True), (2, True), (3, True), (4, True)]
    assert [e['istom'] for e in sev] == [
        False, False, False, True, True, True, False]
    entries = [e for e in events if e['type'] == 'entry']
    assert [e['sdate'] for e in entries] == [
        '2025-12-01', '2025-12-02', '2025-12-03']


def test_calendar_islast_friday_month_end():
    """isLast: a Friday whose next weekday falls in the next month
    is TOM regardless of count (2025-10-31, the panel's first TOM
    trade)."""
    dates = ['2025-10-27', '2025-10-28', '2025-10-29', '2025-10-30',
             '2025-10-31']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    sev = _session_events(events)
    assert sev[-1]['sdate'] == '2025-10-31'
    assert sev[-1]['islast'] is True and sev[-1]['istom'] is True
    assert all(e['istom'] is False for e in sev[:-1])


def test_calendar_islast_uses_weekday_not_next_session():
    """Holidays are NOT skipped: Wed 2025-11-26's next weekday is
    Thu 11-27 (Thanksgiving, no session) in the SAME month, so
    11-26 is not isLast even though the next actual session is in
    December. (The panel has no late-November TOM trade for exactly
    this reason.)"""
    dates = ['2025-11-24', '2025-11-25', '2025-11-26', '2025-12-01']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    sev = {e['sdate']: e for e in _session_events(events)}
    assert sev['2025-11-26']['islast'] is False
    assert sev['2025-11-26']['istom'] is False
    assert sev['2025-12-01']['istom'] is True


def test_calendar_short_week_counts_sessions_not_days():
    """A short week (a holiday removes sessions) still counts
    SESSIONS: the first three sessions of the month are TOM however
    the calendar days fall. Sessions Mon/Wed/Fri of the first week
    of Dec 2025 (Tue/Thu hypothetically missing) are counts 1-3."""
    dates = ['2025-11-26', '2025-12-01', '2025-12-03', '2025-12-05',
             '2025-12-08']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    sev = _session_events(events)
    assert [(e['sdate'], e['tdcount'], e['istom']) for e in sev[1:]] == [
        ('2025-12-01', 1, True), ('2025-12-03', 2, True),
        ('2025-12-05', 3, True), ('2025-12-08', 4, False)]


def test_calendar_mid_month_start_cannot_trade_until_month_change():
    """A machine whose first observed session is mid-month (the
    live bootstrap case): count starts at 0 / known false, so a
    session that is truly the month's 2nd trading day does NOT
    trade; the calendar only arms at the first observed month
    change (isLast excepted)."""
    dates = ['2025-11-04', '2025-11-05', '2025-11-06', '2025-12-01']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    entries = [e['sdate'] for e in events if e['type'] == 'entry']
    assert entries == ['2025-12-01']


def test_calendar_year_boundary_islast():
    """Wed 2025-12-31's next weekday is Thu 2026-01-01 — a different
    month (and year): isLast fires."""
    dates = ['2025-12-29', '2025-12-30', '2025-12-31']
    _, events = _run_sessions([_session_bars(d) for d in dates])
    sev = _session_events(events)
    assert sev[-1]['islast'] is True and sev[-1]['istom'] is True


# ---------------------------------------------------------------------------
# Machine fill semantics

def test_fill_math_and_stress():
    """Entry = second bar open + 1 tick; exit = 15:00 bar open
    - 1 tick; P&L = (exit - entry) x $0.50 - $3.00; stress = P&L
    - $2.00 (the p90 arm's 4 extra ticks)."""
    sessions = [_session_bars('2025-11-26'),
                _session_bars('2025-12-01', o2=200.0, x1500=210.0)]
    _, events = _run_sessions(sessions)
    closes = [e for e in events if e['type'] == 'close']
    assert len(closes) == 1
    ev = closes[0]
    assert ev['entry_fill'] == 201.0
    assert ev['exit_fill'] == 209.0
    assert ev['reason'] == 'exit_1500'
    assert ev['net'] == round((209.0 - 201.0) * 0.5 - 3.0, 2) == 1.0
    assert ev['stress_net'] == -1.0


def test_fallback_exit_without_1500_bar():
    """A TOM session with no bar in the 15:00 window (an early
    close): the trade exits at the session's last bar open - 1 tick
    when the next session starts."""
    tom = _session_bars('2025-12-01', o2=200.0, with_exit=False)
    tom.append({'sdate': '2025-12-01', 'tod': 719, 'o': 190.0,
                'h': 190.0, 'l': 190.0, 'c': 190.0, 'ts': None})
    nxt = _session_bars('2025-12-02')
    _, events = _run_sessions([_session_bars('2025-11-26'), tom, nxt])
    closes = [e for e in events if e['type'] == 'close'
              and e['sdate'] == '2025-12-01']
    assert len(closes) == 1
    assert closes[0]['reason'] == 'fallback'
    assert closes[0]['exit_fill'] == 189.0


def test_no_entry_when_session_has_one_bar():
    """A one-bar session cannot fill (the gauntlet's fill index
    requires a second bar) — and the calendar still advances."""
    one = [{'sdate': '2025-12-01', 'tod': 1020, 'o': 100.0,
            'h': 100.0, 'l': 100.0, 'c': 100.0, 'ts': None}]
    _, events = _run_sessions([_session_bars('2025-11-26'), one,
                               _session_bars('2025-12-02')])
    entries = [e for e in events if e['type'] == 'entry']
    assert [e['sdate'] for e in entries] == ['2025-12-02']


def test_one_trade_per_session_and_no_reentry():
    """However the session's bars move after the exit, there is no
    second entry: the entry exists only at the session's second
    bar."""
    s = _session_bars('2025-12-01', o2=200.0, x1500=210.0)
    s.append({'sdate': '2025-12-01', 'tod': 901, 'o': 220.0,
              'h': 220.0, 'l': 220.0, 'c': 220.0, 'ts': None})
    _, events = _run_sessions([_session_bars('2025-11-26'), s])
    assert len([e for e in events if e['type'] == 'entry']) == 1
    assert len([e for e in events if e['type'] == 'close']) == 1


# ---------------------------------------------------------------------------
# Store registration + live lifecycle

@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'mym_tom.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _cfg():
    return Config(local=True, stocks=('AMD',), watchlist=(),
                  sector_etfs=(), swing_ideas=False,
                  morning_enabled=False, discovery=False)


def test_tables_registered(db):
    names = set(inspect(db.engine).get_table_names())
    assert 'mym_tom_episodes' in names
    assert 'mym_tom_push_outbox_v1' in names


def _seed_window(db):
    rows = [[r[0], r[1], r[2], r[3], r[4], 1000.0]
            for r in FIXTURE['bars']]
    with db.tx() as c:
        db.put(c, 'bar_window:MYM.c.0', rows)


def test_live_lifecycle(db):
    cfg = _cfg()
    first_ts = FIXTURE['bars'][0][0]
    last_ts = FIXTURE['bars'][-1][0]
    # Bootstrap pass on an empty window: nothing fed, cursor set.
    with db.tx() as c:
        s0 = scan(db, c, cfg, first_ts - 3600)
    assert s0['ran'] and s0['fed'] == 0 and s0['entries'] == 0
    assert s0['first_session'] is None

    _seed_window(db)
    with db.tx() as c:
        s1 = scan(db, c, cfg, last_ts + 7200)
    assert s1['fed'] == len(FIXTURE['bars'])
    assert s1['entries'] == len(FIXTURE['trades'])
    assert s1['closes'] == len(FIXTURE['trades'])
    # The fixture starts at the panel's first bar (a 17:00 session
    # open), so its first session is sample-eligible.
    assert s1['first_session'] == '2025-10-27'

    with db.tx() as c:
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        state = db.get(c, 'mym_tom:state')
        outbox = c.execute(select(
            mym_tom_push_outbox.c.payload)).scalars().all()
    assert len(rows) == len(FIXTURE['trades'])
    assert all(p['status'] == 'closed' for p in rows)
    assert sorted(p['result']['net'] for p in rows) == \
        sorted(t['pnl'] for t in FIXTURE['trades'])
    want_net = round(sum(t['pnl'] for t in FIXTURE['trades']), 2)
    want_stress = round(sum(t['pnl_stress'] for t in FIXTURE['trades']), 2)
    assert state['counts'] == {'created': 4, 'open': 0, 'closed': 4}
    assert state['stats']['n'] == 4
    assert state['stats']['net'] == want_net
    assert state['stats']['stress_net'] == want_stress
    assert state['first_session'] == '2025-10-27'
    # Alerts: ENTRY + EXIT per episode.
    assert len(outbox) == 2 * len(FIXTURE['trades'])
    types = sorted(p['type'] for p in outbox)
    assert types.count('entry') == len(FIXTURE['trades'])
    assert types.count('exit') == len(FIXTURE['trades'])

    # A later pass feeds nothing new (cursor idempotence).
    with db.tx() as c:
        s2 = scan(db, c, cfg, last_ts + 10800)
    assert s2['fed'] == 0 and s2['entries'] == 0 and s2['closes'] == 0


def test_bootstrap_mid_session_sample_rule(db):
    """Bootstrap with the store already holding bars of the session
    in progress (2025-11-03, a TOM session): that session is
    calendar context only — it cannot trade even though the
    machine's calendar starts inside it — and the first eligible
    session is the next one."""
    cfg = _cfg()
    bars = FIXTURE['bars']
    # Split inside the 2025-11-03 session (its bars start after the
    # five October sessions: 1242+1342+1357+1377+1356 = 6674 bars).
    split = 6674 + 100
    with db.tx() as c:
        db.put(c, 'bar_window:MYM.c.0',
               [[r[0], r[1], r[2], r[3], r[4], 1000.0]
                for r in bars[:split]])
    with db.tx() as c:
        s0 = scan(db, c, cfg, bars[split - 1][0] + 120)
    assert s0['ran'] and s0['fed'] == 0
    _seed_window(db)
    last_ts = bars[-1][0]
    with db.tx() as c:
        s1 = scan(db, c, cfg, last_ts + 7200)
        state = db.get(c, 'mym_tom:state')
        rows = c.execute(select(episodes.c.payload)).scalars().all()
    assert s1['fed'] == len(bars) - split
    assert s1['entries'] == 0 and s1['closes'] == 0
    assert rows == []
    assert state['first_session'] == '2025-11-04'


def test_tracking_block(db):
    from compass.simple import _tracking_mym_tom
    with db.tx() as c:
        empty = _tracking_mym_tom(db, c, 0)
    assert empty['available'] is False
    assert empty['counts'] == {'created': 0, 'open': 0, 'closed': 0}
    assert empty['n'] == 0 and empty['net'] == 0.0
    assert empty['pf'] is None and empty['stress_net'] == 0.0
    assert empty['verdict'] == {'min_trades': 50, 'min_weeks': 12,
                                 'first_read_trades': 12}
    assert empty['first_session'] is None

    cfg = _cfg()
    first_ts = FIXTURE['bars'][0][0]
    last_ts = FIXTURE['bars'][-1][0]
    with db.tx() as c:
        scan(db, c, cfg, first_ts - 3600)
    _seed_window(db)
    with db.tx() as c:
        scan(db, c, cfg, last_ts + 7200)
        block = _tracking_mym_tom(db, c, last_ts + 7200)
    assert block['available'] is True
    assert block['first_session'] == '2025-10-27'
    assert block['counts']['closed'] == len(FIXTURE['trades'])
    assert block['n'] == len(FIXTURE['trades'])
    assert block['net'] == round(
        sum(t['pnl'] for t in FIXTURE['trades']), 2)
