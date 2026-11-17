"""Futures Sweep (ORB HUD) forward shadow (2026-11-16, Josh):
registration files/futures-sweep-forward-shadow-registration-2026-11-16.md
plus its 2026-11-16 16:00 CT amendment (the unlimited configuration).

Pins: (1) the build-blocking PARITY GATE — the Compass machine
reproduces the cutoff-extension harness's HUD@11:30 cells (as-written
book) and the unlimited-baseline harness's cell (unlimited
configuration) exactly (embedded MES fixture for CI; the full 8-root
panel run whenever the goal workspace is present); (2) HUD machine
semantics (one trade/day vs unlimited re-entry, target-before-stop,
no exit on the entry bar, no entry on the 11:30 bar, cutoff flatten,
fallback flatten, extension filter); (3) 5-minute aggregation;
(4) the live lifecycle on a sqlite store (bootstrap watermark, both
configurations' episodes in one ledger, as-written-only alerts,
state summary); (5) the store.py table registration regression.
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.config import Config
from compass.futures_sweep import (CONFIGS, PANEL, ROOTS,
                                    aggregate_buckets, bucket_start_ts,
                                    episodes, run_bars, scan)
from compass.store import Store, futures_sweep_push_outbox

FIXTURE = json.loads((Path(__file__).parent / 'fixtures'
                      / 'futures_sweep_parity.json').read_text())
GOAL_HIDDEN = ('/home/hatch/workspace/goals/market-compass-trading-'
               'improvements/hidden_files')
EXT_DIR = os.path.join(GOAL_HIDDEN, 'futures-orb-cutoff-extension-2026-11-16')
UNL_DIR = os.path.join(GOAL_HIDDEN, 'futures-orb-unlimited-baseline-2026-11-16')
TRADE_FIELDS = (('entry_date', 'date'), ('side', 'side'),
                ('entry_idx', 'entry_idx'), ('exit_idx', 'exit_idx'),
                ('entry_fill', 'entry_fill'), ('exit_fill', 'exit_fill'),
                ('reason', 'reason'), ('net', 'net'))


def _assert_trades_equal(got, want):
    assert len(got) == len(want)
    for g, w in zip(got, want):
        for gf, wf in TRADE_FIELDS:
            assert g[gf] == w[wf], (gf, g, w)


def _load_hud():
    spec = importlib.util.spec_from_file_location(
        'engine_hud', os.path.join(EXT_DIR, 'engine_hud.py'))
    hud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hud)
    return hud


def _panel_bars(hud, root):
    _, _, days = hud.load_days_ext(root)
    bars = []
    for d in sorted(days):
        for (start, o, h, l, c) in days[d]:
            bars.append({'date': str(d), 'minute': start, 'o': o,
                         'h': h, 'l': l, 'c': c})
    return bars


# ---------------------------------------------------------------------------
# Parity gate

def test_parity_fixture_mes():
    """Embedded real-data fixture: MES's first six gauntlet sessions
    reproduce BOTH harnesses' verified trade logs row-for-row (all
    fields exact, floats bit-identical)."""
    trades, _ = run_bars(FIXTURE['bars'], FIXTURE['root'],
                         one_per_day=True)
    _assert_trades_equal(trades, FIXTURE['as_written'])
    trades, _ = run_bars(FIXTURE['bars'], FIXTURE['root'],
                         one_per_day=False)
    _assert_trades_equal(trades, FIXTURE['unlimited'])


@pytest.mark.skipif(not os.path.isdir(EXT_DIR),
                    reason='goal workspace not present (CI)')
def test_parity_full_panel_as_written():
    """THE parity gate (as-written book): all 8 roots over the
    gauntlet panel reproduce the extension harness's HUD@11:30
    cells exactly — 1,540 trades."""
    hud = _load_hud()
    results = json.loads(
        (Path(EXT_DIR) / 'results_all.json').read_text())
    total = 0
    for root in ROOTS:
        trades, _ = run_bars(_panel_bars(hud, root), root,
                             one_per_day=True)
        _assert_trades_equal(trades,
                             results[root]['cells']['690']['trades'])
        total += len(trades)
    assert total == 1540


@pytest.mark.skipif(not os.path.isdir(UNL_DIR),
                    reason='goal workspace not present (CI)')
def test_parity_full_panel_unlimited():
    """THE parity gate (unlimited configuration): all 8 roots over
    the gauntlet panel reproduce the unlimited-baseline harness's
    cell exactly — 2,871 trades; and the unlimited set is a
    superset of the as-written book's on every root."""
    hud = _load_hud()
    total = 0
    for root in ROOTS:
        ref = json.loads(
            (Path(UNL_DIR) / ('results_%s.json' % root)).read_text())
        bars = _panel_bars(hud, root)
        trades, _ = run_bars(bars, root, one_per_day=False)
        _assert_trades_equal(trades, ref['trades'])
        total += len(trades)
        aw, _ = run_bars(bars, root, one_per_day=True)
        key = lambda t: (t['entry_date'], t['entry_idx'], t['side'])
        assert {key(t) for t in aw} <= {key(t) for t in trades}
    assert total == 2871


# ---------------------------------------------------------------------------
# Machine semantics pins (synthetic, MES: tick 0.25, mult 5.0)

D = '2026-01-05'


def _bar(minute, o, h, l, c):
    return {'date': D, 'minute': minute, 'o': o, 'h': h, 'l': l,
            'c': c}


def _or_bars():
    # OR 09:30-09:45: high 101.0, low 100.0 (range 1.0).
    return [_bar(570, 100.2, 101.0, 100.0, 100.8),
            _bar(575, 100.8, 101.0, 100.0, 100.4),
            _bar(580, 100.4, 100.9, 100.0, 100.2)]


def test_one_trade_per_day_vs_unlimited_reentry():
    """The amendment's two configurations on a two-signal day: the
    as-written book takes only the first trade; the unlimited
    configuration re-enters on the later reclaim after the exit.
    The shared first trade is identical in both."""
    bars = _or_bars() + [
        _bar(585, 100.2, 100.6, 99.5, 100.5),   # sweep, no signal yet
        _bar(590, 100.5, 101.2, 100.4, 101.0),  # LONG (prev low swept)
        _bar(595, 101.0, 102.8, 100.9, 102.6),  # target 102.65 hit
        _bar(600, 102.6, 102.9, 100.4, 100.6),
        _bar(605, 100.6, 100.9, 99.6, 100.4),   # new sweep, no signal
        _bar(610, 100.4, 101.0, 100.2, 100.8),  # LONG again (unlimited)
        _bar(690, 100.8, 101.0, 100.6, 100.9),  # cutoff flatten
    ]
    aw, _ = run_bars(bars, 'MES', one_per_day=True)
    un, _ = run_bars(bars, 'MES', one_per_day=False)
    assert len(aw) == 1
    assert len(un) == 2
    assert aw[0]['entry_idx'] == un[0]['entry_idx'] == 4
    assert aw[0]['reason'] == un[0]['reason'] == 'target'
    assert aw[0]['net'] == un[0]['net']
    assert round(aw[0]['net'], 2) == 5.76
    assert un[1]['entry_idx'] == 8
    assert un[1]['reason'] == 'flatten'
    assert round(un[1]['net'], 2) == -1.99


def test_target_before_stop_on_both_crossed_bar():
    """HUD tracking order: on a bar crossing both bracket levels the
    TARGET fills first."""
    bars = _or_bars() + [
        _bar(585, 99.8, 100.6, 99.5, 100.5),   # LONG: stop 99.0, tgt 102.75
        _bar(590, 100.5, 103.0, 98.0, 101.0),  # crosses both levels
    ]
    trades, _ = run_bars(bars, 'MES', one_per_day=True)
    assert len(trades) == 1
    assert trades[0]['reason'] == 'target'
    assert trades[0]['exit_fill'] == 102.75
    assert round(trades[0]['net'], 2) == 8.76


def test_no_exit_on_entry_bar():
    """Exits evaluate only on bars after the entry bar: the entry
    bar's own high crossing the target does not close the trade
    (the stop, set from the entry bar's low, cannot be crossed on
    that bar by construction — the target cross is the live case)."""
    bars = _or_bars() + [
        _bar(585, 99.8, 103.5, 99.5, 100.5),   # LONG; high spans the target
        _bar(590, 100.5, 101.0, 100.2, 100.8),  # no bracket cross
        _bar(690, 100.8, 101.0, 100.6, 100.9),  # cutoff flatten
    ]
    trades, _ = run_bars(bars, 'MES', one_per_day=True)
    assert len(trades) == 1
    assert trades[0]['entry_idx'] == 3
    assert trades[0]['exit_idx'] == 5
    assert trades[0]['reason'] == 'flatten'


def test_no_entry_on_cutoff_bar():
    """Entries run 09:45-11:30: a reclaim completing on the 11:30
    bar itself is not a signal."""
    bars = _or_bars() + [
        _bar(585, 100.2, 100.6, 99.5, 99.8),   # sweep; close below OR low
        _bar(690, 99.9, 100.8, 99.7, 100.6),   # reclaim shape at 11:30
    ]
    trades, _ = run_bars(bars, 'MES', one_per_day=True)
    assert trades == []
    trades, _ = run_bars(bars, 'MES', one_per_day=False)
    assert trades == []


def test_flatten_fallback_without_cutoff_bar():
    """No 11:30 bar in the feed: an open position resolves at the
    close of the last bar starting <= 11:30."""
    bars = _or_bars() + [
        _bar(585, 99.8, 100.6, 99.5, 100.5),   # LONG
        _bar(590, 100.5, 101.0, 100.2, 100.8),
        _bar(685, 100.8, 101.1, 100.6, 101.05),  # last bar of the feed
    ]
    trades, _ = run_bars(bars, 'MES', one_per_day=True)
    assert len(trades) == 1
    assert trades[0]['reason'] == 'flatten_fallback'
    assert trades[0]['exit_idx'] == 5
    assert trades[0]['exit_fill'] == 101.05


def test_extension_filter_blocks_deep_probe():
    """A probe deeper than 1.0x the OR range invalidates the side:
    the later reclaim shape does not enter."""
    bars = _or_bars() + [
        _bar(585, 100.2, 100.6, 98.8, 99.5),   # probe to 98.8 (>1.0x range)
        _bar(590, 99.6, 100.8, 99.4, 100.6),   # reclaim shape, still blocked
        _bar(690, 100.6, 100.9, 100.3, 100.7),
    ]
    for odp in (True, False):
        trades, _ = run_bars(bars, 'MES', one_per_day=odp)
        assert trades == []


def test_aggregate_buckets():
    d = '2026-11-17'
    base = bucket_start_ts(d, 570)

    def row(minute, o, h, l, c):
        return {'ts': bucket_start_ts(d, minute), 'o': o, 'h': h,
                'l': l, 'c': c, 'v': 100.0}
    rows = [row(571, 10.0, 11.0, 9.0, 10.5),
            row(570, 9.5, 10.0, 9.0, 10.0),
            row(574, 10.5, 12.0, 10.0, 11.0),
            row(585, 11.0, 11.5, 10.8, 11.2),
            row(569, 1.0, 1.0, 1.0, 1.0),      # before the OR: dropped
            row(725, 2.0, 2.0, 2.0, 2.0)]     # past FEED_HI: dropped
    buckets = aggregate_buckets(rows)
    assert [b['minute'] for b in buckets] == [570, 585]
    b0 = buckets[0]
    assert (b0['o'], b0['h'], b0['l'], b0['c']) == (9.5, 12.0, 9.0, 11.0)
    assert b0['start_ts'] == base
    assert b0['date'] == d


# ---------------------------------------------------------------------------
# Store registration + live lifecycle

@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'futures_sweep.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _cfg():
    return Config(local=True, stocks=('AMD',), watchlist=(),
                  sector_etfs=(), swing_ideas=False,
                  morning_enabled=False, discovery=False)


def test_tables_registered(db):
    names = set(inspect(db.engine).get_table_names())
    assert 'futures_sweep_episodes' in names
    assert 'futures_sweep_push_outbox_v1' in names


def _seed_window(db):
    """The fixture sessions as a bar_window for MES.c.0: one
    1-minute row per 5-minute bucket (a single minute aggregates to
    exactly that bucket)."""
    rows = [[bucket_start_ts(b['date'], b['minute']), b['o'], b['h'],
             b['l'], b['c'], 1000.0] for b in FIXTURE['bars']]
    with db.tx() as c:
        db.put(c, 'bar_window:MES.c.0', rows)


def test_live_lifecycle(db):
    cfg = _cfg()
    t0 = bucket_start_ts(FIXTURE['dates'][0], 540)   # 09:00 ET, day one
    with db.tx() as c:
        s0 = scan(db, c, cfg, t0)
    assert s0['ran'] and s0['fed'] == 0 and s0['entries'] == 0
    assert s0['first_session'] is None

    _seed_window(db)
    t1 = bucket_start_ts(FIXTURE['dates'][-1], 780)  # 13:00 ET, last day
    with db.tx() as c:
        s1 = scan(db, c, cfg, t1)
    want_fed = 2 * sum(1 for b in FIXTURE['bars'] if b['minute'] <= 720)
    assert s1['fed'] == want_fed
    assert s1['entries'] == len(FIXTURE['as_written'])
    assert s1['entries_unlimited'] == len(FIXTURE['unlimited'])
    assert s1['closes'] == len(FIXTURE['as_written'])
    assert s1['closes_unlimited'] == len(FIXTURE['unlimited'])
    assert s1['first_session'] == FIXTURE['dates'][0]

    with db.tx() as c:
        rows = c.execute(select(episodes.c.payload)).scalars().all()
        state = db.get(c, 'futures_sweep:state')
        outbox = c.execute(select(
            futures_sweep_push_outbox.c.payload)).scalars().all()
    by_cfg = {}
    for p in rows:
        by_cfg.setdefault(p['config'], []).append(p)
    assert sorted(by_cfg) == ['as_written', 'unlimited']
    for name, ref in (('as_written', FIXTURE['as_written']),
                      ('unlimited', FIXTURE['unlimited'])):
        ps = by_cfg[name]
        assert len(ps) == len(ref)
        assert all(p['status'] == 'closed' for p in ps)
        assert sorted(p['result']['net'] for p in ps) == \
            sorted(t['net'] for t in ref)
        sub = state['per_root']['MES'][name]
        assert sub['n'] == len(ref)
        assert sub['net'] == round(sum(t['net'] for t in ref), 2)
    assert state['configs'] == [name for name, _ in CONFIGS]
    assert state['first_session'] == FIXTURE['dates'][0]
    assert state['counts']['closed'] == \
        len(FIXTURE['as_written']) + len(FIXTURE['unlimited'])
    # Alerts: ENTRY + one close alert per as-written episode, none
    # for the unlimited configuration.
    aw_ids = {p['id'] for p in by_cfg['as_written']}
    assert len(outbox) == 2 * len(FIXTURE['as_written'])
    assert all(p['episode_id'] in aw_ids for p in outbox)
    types = sorted(p['type'] for p in outbox)
    assert types.count('entry') == len(FIXTURE['as_written'])
