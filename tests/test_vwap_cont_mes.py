"""MES VWAP-continuation forward shadow scorer (2026-11-07, Josh):
the frozen vwapc_z1.5_h60_long cell — signal state machine against
hand-computed bars, the one-position dedupe, episode lifecycle +
forward gating on a sqlite store, the store.py table registration
regression (the bb1adfa lesson), and full-panel parity against the
momentum/trend sweep's scored trade list (registration sha
8bdc46537ce6c12b15482142e8c621ddac8812a2adab6d98c8a409804a15ae9f)."""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.futures_shadow_common import ct_parts
from compass.store import Store
from compass.vwap_cont_mes import VwapContFeed, VwapContMes
from compass.vwap_cont_mes import episodes

CT = ZoneInfo('America/Chicago')
DAY = '2026-11-09'    # a Monday


def ts_of(day, tod):
    base = datetime.fromisoformat(day).replace(tzinfo=CT).timestamp()
    return int(base) + tod * 60


def bar(day, tod, o, h, l, c, v=10):
    return {'ts': ts_of(day, tod), 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def warm_feed():
    """A feed whose ATR is exactly 20.0 (15 identical history
    sessions: high 110 / low 90 / close 100 -> TR 20)."""
    feed = VwapContFeed()
    hist = [{'sdate': f'2026-10-{d:02d}', 'o': 100.0, 'h': 110.0,
             'l': 90.0, 'close': 100.0, 'c1545': 100.0,
             'flat_fallback': False, 'bars': 1000, 'partial': False,
             'first_ts': 0, 'last_ts': 0} for d in range(5, 20)]
    feed.seed_history(hist)
    assert feed.atr == 20.0
    return feed


def feed_rth_open(feed):
    """30 flat RTH bars (tod 510-539) at close 100 -> VWAP 100."""
    for tod in range(510, 540):
        feed.feed(bar(DAY, tod, 100, 100, 100, 100))


def test_signal_fill_exit_hand_computed():
    # atr_t = 80 -> z scale max(0.2*80, 1) = 16 -> z = (c-vwap)/4.
    feed = warm_feed()
    feed_rth_open(feed)
    feed.feed(bar(DAY, 540, 100, 100, 100, 100))          # z 0
    feed.feed(bar(DAY, 541, 100, 107, 100, 107))          # z ~1.69: arm
    assert feed.armed
    feed.feed(bar(DAY, 542, 107, 107, 106, 107))          # no touch
    assert feed.pending is None
    # VWAP is now ~100.66; band = vwap + 0.1*20 ~ 102.66. A low of
    # 101.5 touches the band, close 103 holds above VWAP: signal.
    feed.feed(bar(DAY, 543, 103, 103, 101.5, 103))
    assert feed.pending is not None
    feed.feed(bar(DAY, 544, 103.5, 104, 103, 103.5))      # fill at open
    t = feed.open_trade
    assert t is not None and t['entry_px'] == 103.5
    for tod in range(545, 605):
        feed.feed(bar(DAY, tod, 105, 105, 105, 105))
    assert feed.open_trade is None and len(feed.trades) == 1
    tr = feed.trades[0]
    assert tr['exit_reason'] == 'time'
    # (105 - 103.5)/0.25 - 2 = 4 ticks -> 4*1.25 - 3 = $2.00
    assert tr['pnl'] == 2.00
    # guard-kept: entry = signal close 103 + 1 tick; (105-103.25)/0.25
    # = 7 ticks -> 7*1.25 - 3 = $5.75
    assert tr['pnl_gk'] == 5.75


def test_close_through_vwap_disarms():
    feed = warm_feed()
    feed_rth_open(feed)
    feed.feed(bar(DAY, 540, 100, 100, 100, 100))
    feed.feed(bar(DAY, 541, 100, 107, 100, 107))          # arm
    assert feed.armed
    feed.feed(bar(DAY, 542, 100, 100, 99, 99))            # close < VWAP
    assert not feed.armed
    # A band touch now (unarmed) must not signal.
    feed.feed(bar(DAY, 543, 100, 100, 99.5, 100.5))
    assert feed.pending is None


def test_signal_while_open_is_dropped():
    feed = warm_feed()
    feed_rth_open(feed)
    feed.feed(bar(DAY, 540, 100, 100, 100, 100))
    feed.feed(bar(DAY, 541, 100, 107, 100, 107))
    feed.feed(bar(DAY, 543, 103, 103, 101.5, 103))
    feed.feed(bar(DAY, 544, 103.5, 104, 103, 103.5))      # trade open
    # Re-arm and touch again while the first trade is still open.
    # VWAP is ~101.8 by now, so the band reaches ~103.8: a low of
    # 103 touches it with the close holding above VWAP.
    feed.feed(bar(DAY, 545, 103.5, 112, 103.5, 112))      # z high: arm
    feed.feed(bar(DAY, 546, 112, 112, 103, 111))          # band touch
    assert feed.dropped == 1
    assert len(feed.trades) == 0 and feed.open_trade is not None


def test_flatten_bar_caps_the_hold():
    feed = warm_feed()
    feed_rth_open(feed)
    # Signal late in the window: arm at 895, touch at 896, fill 897;
    # the 15:45 bar (tod 945) arrives before fill+60 (957).
    for tod in range(540, 895):
        feed.feed(bar(DAY, tod, 100, 100, 100, 100))
    feed.feed(bar(DAY, 895, 100, 107, 100, 107))
    feed.feed(bar(DAY, 896, 103, 103, 101.5, 103))
    feed.feed(bar(DAY, 897, 103.5, 104, 103, 103.5))
    for tod in range(898, 945):
        feed.feed(bar(DAY, tod, 104, 104, 104, 104))
    feed.feed(bar(DAY, 945, 106, 106, 106, 106))          # flatten bar
    assert len(feed.trades) == 1
    tr = feed.trades[0]
    assert tr['exit_reason'] == 'flatten' and tr['exit_px'] == 106
    # (106 - 103.5)/0.25 - 2 = 8 ticks -> 8*1.25 - 3 = $7.00
    assert tr['pnl'] == 7.00


def test_episodes_table_created_without_module_import(tmp_path):
    """Production import order (regression, the bb1adfa lesson): a
    fresh interpreter that never imports vwap_cont_mes must still
    get the table from compass.store alone."""
    import subprocess
    import sys
    root = Path(__file__).resolve().parent.parent
    code = (
        "from compass.store import Store\n"
        "from sqlalchemy import inspect\n"
        f"s = Store('sqlite:///{tmp_path}/fresh.db')\n"
        "s.initialize()\n"
        "names = inspect(s.engine).get_table_names()\n"
        "assert 'vwap_cont_mes_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'vwap.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def _session_rows(day):
    """The hand-computed signal session of
    test_signal_fill_exit_hand_computed, as bar_window rows."""
    rows = []

    def add(tod, o, h, l, c):
        b = bar(day, tod, o, h, l, c)
        rows.append([b['ts'], b['o'], b['h'], b['l'], b['c'], b['v']])

    for tod in range(510, 540):
        add(tod, 100, 100, 100, 100)
    add(540, 100, 100, 100, 100)
    add(541, 100, 107, 100, 107)
    add(542, 107, 107, 106, 107)
    add(543, 103, 103, 101.5, 103)
    add(544, 103.5, 104, 103, 103.5)
    for tod in range(545, 606):
        add(tod, 105, 105, 105, 105)
    return rows


def test_worker_forward_gate_and_state(db):
    """With forward_from = the boot session (2026-11-09), a full
    signal IN that session produces no episode; the identical
    session on the next day scores, and the state summary is
    written."""
    feed_state = {'forward_from': DAY, 'boot_sdate': DAY,
                  'dropped_total': 0, 'feed': warm_feed().to_state(),
                  'last_ts': None}
    with db.tx() as c:
        db.put(c, 'vwap_cont_mes:feed', feed_state)
        db.put(c, 'bar_window:MES.c.0', _session_rows(DAY))
    worker = VwapContMes(db, cfg=None)
    worker.process(ts_of(DAY, 700))
    with db.tx() as c:
        eps = c.execute(select(episodes.c.payload)).scalars().all()
    assert eps == []                        # boot session: gated out
    with db.tx() as c:
        db.put(c, 'bar_window:MES.c.0', _session_rows('2026-11-10'))
    worker.process(ts_of('2026-11-10', 700))
    with db.tx() as c:
        state = db.get(c, 'vwap_cont_mes:state', {})
        eps = c.execute(select(episodes.c.payload)).scalars().all()
    assert len(eps) == 1
    p = eps[0]
    assert p['status'] == 'closed' and p['pnl'] == 2.00
    assert p['pnl_gk'] == 5.75
    assert state['counts'] == {'created': 1, 'closed': 1, 'open': 0}
    assert state['forward_from'] == DAY


# ── parity: the streaming feed vs the sweep's scored trade list ─────

def _default_archive():
    here = Path(__file__).resolve()
    root = here.parents[3] if len(here.parents) > 3 else here.parent
    return (root / 'goals' / 'market-compass-trading-improvements'
            / 'hidden_files' / 'momentum-trend-sweep-2026-11-06')


ARCHIVE = Path(os.environ.get('VWAP_ARCHIVE_DIR', _default_archive()))


@pytest.mark.skipif(
    not (ARCHIVE / 'parq/MES.parquet').exists(),
    reason='gauntlet archive lives in the goal workspace, not the '
           'repo; parity runs where the archive is present')
def test_parity_against_sweep_trade_list():
    import numpy as np
    import pandas as pd
    df = pd.read_parquet(ARCHIVE / 'parq/MES.parquet')
    df = df[~df['degraded']].reset_index(drop=True)
    # sim.load's Panama back-adjustment, verbatim.
    roll = (df['instrument_id'] != df['instrument_id'].shift()).values.copy()
    roll[0] = False
    jump = pd.Series(df['o'].values
                     - df['c'].shift().values).where(roll, 0.0).values
    adj = np.cumsum(jump[::-1])[::-1] - jump
    for col in ['o', 'h', 'l', 'c']:
        df[col] = df[col].values + adj
    exp = pd.read_pickle(
        ARCHIVE / 'results/trades_MES_vwapc_z1.5_h60_long.pkl')
    feed = VwapContFeed()
    last_ts = None
    for r in df.itertuples():
        feed.feed({'ts': int(r.ts), 'o': r.o, 'h': r.h, 'l': r.l,
                   'c': r.c, 'v': r.v})
        last_ts = int(r.ts)
    # Panel-end flush: the gauntlet's last trade exits at its
    # flatten cap on the panel's final bar; in production the next
    # session's first bar closes it at the same price (the roll
    # close). Feed one next-session bar to trigger that close.
    ts = last_ts + 3600
    while ct_parts(ts)[1] == ct_parts(last_ts)[1]:
        ts += 3600
    last_close = float(df.c.iloc[-1])
    feed.feed({'ts': ts, 'o': last_close, 'h': last_close,
               'l': last_close, 'c': last_close, 'v': 0})
    exp_keys = {(r.sdate, int(r.tod)): r.pnl for r in exp.itertuples()}
    got_keys = {(t['signal_sdate'], ct_parts(t['entry_ts'])[0]): t['pnl']
                for t in feed.trades}
    assert len(exp_keys) == 65
    assert got_keys.keys() == exp_keys.keys()
    for k, pnl in exp_keys.items():
        assert abs(got_keys[k] - pnl) < 0.01, k
