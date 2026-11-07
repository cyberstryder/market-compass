"""Post-shock next-session fade forward shadow scorer (2026-11-07,
Josh): the variance screen's pass, registration sha
a5949fb5755ebdb84c8d8f01cf1e1915d107a68ac303ddf8ba431d712de8a9c8.
Percentile arithmetic, the frozen shock/fade math on hand-computed
sessions (warm-up silence, zero-direction, both registered lines),
the store.py table registration regression, and full parity against
the gauntlet's daily panel + scored trade lists (stdlib only)."""
import csv
import json
import os
from pathlib import Path

import pytest

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.futures_shadow_common import percentile_linear
from compass.shock_fade import ROOTS, ShockFade, ShockLedger
from compass.store import Store


def test_percentile_linear_hand_values():
    w = [float(i) for i in range(1, 101)]          # 1..100
    assert percentile_linear(w, 95) == pytest.approx(95.05)
    assert percentile_linear(w, 97.5) == pytest.approx(97.525)
    assert percentile_linear([2.0] * 252, 97.5) == 2.0
    assert percentile_linear([5.0], 95) == 5.0
    w = [0.0, 10.0]
    assert percentile_linear(w, 25) == pytest.approx(2.5)


def sess(day, o, h, l, c1545):
    return {'sdate': f'2026-01-{day:02d}' if day <= 28 else f'2026-02-{day - 28:02d}',
            'o': o, 'h': h, 'l': l, 'close': c1545, 'c1545': c1545,
            'flat_fallback': False, 'bars': 1000, 'partial': False,
            'first_ts': 0, 'last_ts': 0}


def flat_sessions(n, start_day=1):
    """n sessions with TR exactly 2.0 (h 101 / l 99 / marks 100)."""
    return [sess(start_day + i, 100, 101, 99, 100) for i in range(n)]


def test_shock_fires_both_lines_hand_computed():
    led = ShockLedger('MNQ')
    for s in flat_sessions(260):
        assert led.add_session(s) == []
    # Shock: TR = max(110-99, |110-100|, |99-100|) = 11 >= thr 2.0,
    # direction +5 (mark 105 vs 100) -> fade SHORT on both lines.
    shock = sess(261 - 260 + 28 + 1, 100, 110, 99, 105)
    # (day arithmetic: session #261 -> 2026-02-... keep explicit:)
    shock['sdate'] = '2026-10-05'
    assert led.add_session(shock) == []      # entry is next session
    entry = sess(1, 104, 106, 100, 101)
    entry['sdate'] = '2026-10-06'
    events = led.add_session(entry)
    exits = [e for e in events if e['type'] == 'exit']
    entries = [e for e in events if e['type'] == 'entry']
    assert {e['line'] for e in entries} == {'pooled_p975', 'mnq_p950'}
    assert len(exits) == 2
    for e in exits:
        assert e['status'] == 'closed' and e['dir'] == -1
        # pnl = -1*(101-104)*$2 - (2*$0.50 + $3) = 6 - 4 = $2.00
        assert e['pnl'] == 2.00
        # stress = 6 - (6*$0.50 + $3) = $0.00
        assert e['pnl_stress'] == 0.00


def test_warmup_silence_and_zero_direction():
    led = ShockLedger('MNQ')
    for s in flat_sessions(100):
        led.add_session(s)
    # A huge TR inside the warm-up (< 252 TRs): no events at all.
    big = sess(101 - 100 + 28, 100, 130, 99, 120)
    big['sdate'] = '2026-05-01'
    assert led.add_session(big) == []
    assert led.pending == {}
    # Zero-direction shock at full depth: counted, no event.
    led2 = ShockLedger('MNQ')
    for s in flat_sessions(260):
        led2.add_session(s)
    z = sess(1, 100, 115, 85, 100)           # TR 30, mark unchanged
    z['sdate'] = '2026-10-05'
    assert led2.add_session(z) == []
    # The constant-mark warm sessions are themselves zero-direction
    # at full depth (TR ties the percentile, direction is zero), so
    # the counter includes them; the point is no event ever pends.
    assert led2.zero_dir >= 1 and led2.pending == {}


def test_pooled_line_only_for_non_mnq_root():
    led = ShockLedger('MES')
    for s in flat_sessions(260):
        led.add_session(s)
    shock = sess(1, 100, 110, 99, 105)
    shock['sdate'] = '2026-10-05'
    led.add_session(shock)
    assert set(led.pending) == {'pooled_p975'}
    entry = sess(2, 104, 106, 100, 101)
    entry['sdate'] = '2026-10-06'
    exits = [e for e in led.add_session(entry) if e['type'] == 'exit']
    assert len(exits) == 1 and exits[0]['line'] == 'pooled_p975'
    # MES: pnl = -1*(101-104)*$5 - (2*$1.25 + $3) = 15 - 5.50 = $9.50
    assert exits[0]['pnl'] == 9.50


def test_episodes_table_created_without_module_import(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    code = (
        "from compass.store import Store\n"
        "from sqlalchemy import inspect\n"
        f"s = Store('sqlite:///{tmp_path}/fresh.db')\n"
        "s.initialize()\n"
        "names = inspect(s.engine).get_table_names()\n"
        "assert 'shock_fade_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]


def test_worker_smoke_empty_store(tmp_path):
    """Seeding against an empty events store must not throw; the
    state summary names every root's (zero) TR depth."""
    store = Store('sqlite:///' + str(tmp_path / 'shock.db'))
    store.initialize()
    worker = ShockFade(store, cfg=None)
    now = 1794000000.0
    for _ in range(len(ROOTS) + 1):
        worker.process(now)
    with store.tx() as c:
        state = store.get(c, 'shock_fade:state', {})
    assert state['counts']['created'] == 0
    assert set(state['tr_depth']) == set(ROOTS)
    assert all(d == 0 for d in state['tr_depth'].values())
    store.engine.dispose()


# ── parity: the ledger vs the gauntlet panel + scored lists ────────

def _default_archive():
    here = Path(__file__).resolve()
    root = here.parents[3] if len(here.parents) > 3 else here.parent
    return (root / 'goals' / 'market-compass-trading-improvements'
            / 'hidden_files')


ARCHIVE = Path(os.environ.get('SHOCK_ARCHIVE_DIR', _default_archive()))


def _load_daily(root):
    alias = ROOTS[root][0]
    p = (ARCHIVE / 'hy3-donchian-gauntlet-2026-10-31' / 'daily'
         / f'daily_{alias}.csv')
    out = []
    with open(p) as f:
        for row in csv.DictReader(f):
            out.append({'sdate': row['date'], 'o': float(row['open']),
                        'h': float(row['high']), 'l': float(row['low']),
                        'close': float(row['close']),
                        'c1545': float(row['close_1545']),
                        'flat_fallback': row['flat_fallback'] == 'True',
                        'bars': int(row['n_bars']), 'partial': False,
                        'first_ts': 0, 'last_ts': 0})
    return out


@pytest.mark.skipif(
    not (ARCHIVE / 'variance-screen-2026-11-06/results'
           / 'trades_ALL_shock_p975_fade1.json').exists(),
    reason='gauntlet archive lives in the goal workspace, not the '
           'repo; parity runs where the archive is present')
def test_parity_against_gauntlet_lists():
    exp_pooled = json.loads(
        (ARCHIVE / 'variance-screen-2026-11-06/results'
         / 'trades_ALL_shock_p975_fade1.json').read_text())
    exp_byroot = json.loads(
        (ARCHIVE / 'variance-screen-2026-11-06/results'
         / 'trades_byroot_shock_p950_fade1.json').read_text())
    exp_mnq = [r for r in exp_byroot if r['root'] == 'MNQ']
    got_pooled, got_mnq = {}, {}
    for root in ROOTS:
        led = ShockLedger(root)
        for s in _load_daily(root):
            for e in led.add_session(s):
                if e['type'] != 'exit' or e['status'] != 'closed':
                    continue
                rec = {'root': root, 'entry_date': e['entry_sdate'],
                       'dir': e['dir'], 'entry_px': e['entry_px'],
                       'exit_px': e['exit_px'], 'pnl': e['pnl'],
                       'pnl_s': e['pnl_stress']}
                if e['line'] == 'pooled_p975':
                    got_pooled[(root, e['entry_sdate'])] = rec
                if e['line'] == 'mnq_p950':
                    got_mnq[(root, e['entry_sdate'])] = rec
    for exp_list, got in ((exp_pooled, got_pooled), (exp_mnq, got_mnq)):
        exp_map = {(r['root'], r['entry_date']): r for r in exp_list}
        assert got.keys() == exp_map.keys()
        for k, r in exp_map.items():
            g = got[k]
            assert g['dir'] == r['dir'] and g['pnl'] == r['pnl']
            assert g['pnl_s'] == r['pnl_s']
            assert abs(g['entry_px'] - r['entry_px']) < 1e-6
            assert abs(g['exit_px'] - r['exit_px']) < 1e-6
    assert len(got_pooled) == 476 and len(got_mnq) == 113
