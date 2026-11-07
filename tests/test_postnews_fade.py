"""MNQ post-news fade (t60) forward shadow scorer (2026-11-07,
Josh): the PROVISIONAL pass of the post-news study, registration
sha 27cb1ebc4fa568523983a8b54c549442ee998bba2012e9222c2adcf6b3b8ec6d.
Calendar classification mapping, the frozen reaction/fade math
against hand-computed bars, the worker lifecycle on a sqlite store,
the store.py table registration regression, and parity against the
gauntlet's 47 scored trades."""
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass.postnews_fade import (PostnewsFade, classify_event,
                                   episodes, fade_trade, release_ts)
from compass.store import Store

DATE = '2026-11-20'


def ev(title, time='8:30 AM', impact='', country='United States',
       date=DATE):
    return {'title': title, 'date': date, 'time': time,
            'impact': impact, 'description': '', 'country': country}


# ── the documented calendar classification mapping ──────────────────

@pytest.mark.parametrize('title,cls', [
    ('Nonfarm Payrolls', 'NFP'),
    ('Employment Situation', 'NFP'),
    ('Unemployment Rate', 'NFP'),
    ('Average Hourly Earnings m/m', 'NFP'),
    ('CPI m/m', 'CPI'),
    ('Core CPI m/m', 'CPI'),
    ('Consumer Price Index', 'CPI'),
    ('PPI m/m', 'PPI'),
    ('Core Producer Price Index', 'PPI'),
    ('Retail Sales m/m', 'RETAIL'),
    ('Core Retail Sales m/m', 'RETAIL'),
    ('GDP Advance', 'GDP'),
    ('Advance GDP q/q', 'GDP'),
    ('Gross Domestic Product Advance Estimate', 'GDP'),
])
def test_classify_frozen_classes(title, cls):
    assert classify_event(ev(title)) == cls


@pytest.mark.parametrize('title', [
    'Initial Jobless Claims',        # Thursday claims: NOT the census
    'Continuing Jobless Claims',
    'ADP Nonfarm Employment Change',
    'GDP Prelim q/q',                # second estimate: not frozen
    'GDP Second Estimate',
    'GDP Final q/q',
    'ISM Manufacturing PMI',         # 10:00 ET anchor, not this cell
    'FOMC Statement',
    'Durable Goods Orders m/m',
    'JOLTS Job Openings',
    '',
])
def test_classify_exclusions(title):
    assert classify_event(ev(title)) is None


def test_classify_guards():
    # Explicitly rated below high impact: never classifies (the
    # event_opens_pause rule the news pause also applies).
    assert classify_event(ev('CPI m/m', impact='low')) is None
    assert classify_event(ev('CPI m/m', impact='medium')) is None
    assert classify_event(ev('CPI m/m', impact='high')) == 'CPI'
    # Non-US rows never classify.
    assert classify_event(ev('CPI m/m', country='Eurozone')) is None
    assert classify_event(ev('CPI m/m', country='')) == 'CPI'
    # The release clock must read as the 08:30 ET anchor (08:30
    # ET-rendered, or 07:30 — the same instant rendered in CT).
    assert classify_event(ev('CPI m/m', time='8:30 AM')) == 'CPI'
    assert classify_event(ev('CPI m/m', time='7:30 AM')) == 'CPI'
    assert classify_event(ev('CPI m/m', time='10:00 AM')) is None
    assert classify_event(ev('CPI m/m', time='2:00 PM')) is None
    assert classify_event(ev('CPI m/m', time='All Day')) is None


# ── the frozen reaction / fade math ─────────────────────────────────

def bars_around(rel, reaction_closes, exit_open, minutes_after=100):
    """1-minute bars: flat 100 before the release; the release bar
    opens at 100; closes walk to reaction_closes by +10m; flat after;
    the bar at +70m opens at exit_open."""
    bars = []
    for m in range(-30, minutes_after):
        ts = rel + m * 60
        if m < 0:
            o = h = l = c = 100.0
        elif m == 0:
            o, c = 100.0, reaction_closes[0]
            h, l = max(o, c), min(o, c)
        elif m <= 10:
            c = reaction_closes[min(m, len(reaction_closes) - 1)]
            o = reaction_closes[min(m - 1, len(reaction_closes) - 1)]
            h, l = max(o, c), min(o, c)
        elif m == 70:
            o = h = l = c = exit_open
        else:
            o = h = l = c = reaction_closes[-1]
        bars.append({'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': 1})
    return bars


def test_fade_trade_hand_computed():
    rel = release_ts(DATE)
    # Reaction +10 (100 -> 110): fade is SHORT (d=-1); exit at +70m
    # open 105. entry = 110 - 0.25; exit fill = 105 + 0.25;
    # pnl = -1*(105.25 - 109.75)*$2 - $3 = $6.00.
    bars = bars_around(rel, [101 + i for i in range(10)], 105.0)
    got = fade_trade(bars, rel, DATE)
    assert got['status'] == 'closed' and got['d'] == -1
    assert got['exit_kind'] == 'timed'
    assert got['pnl'] == 6.00 and got['pnl_stress'] == 6.00


def test_fade_trade_long_side():
    rel = release_ts(DATE)
    # Reaction -10 (100 -> 90): fade is LONG; exit open 95.
    # entry = 90 + 0.25; exit fill = 95 - 0.25;
    # pnl = +1*(94.75 - 90.25)*$2 - $3 = $6.00.
    bars = bars_around(rel, [99 - i for i in range(10)], 95.0)
    got = fade_trade(bars, rel, DATE)
    assert got['status'] == 'closed' and got['d'] == 1
    assert got['pnl'] == 6.00


def test_signless_and_untradeable():
    rel = release_ts(DATE)
    flat = bars_around(rel, [100] * 10, 100.0)
    assert fade_trade(flat, rel, DATE)['status'] == 'signless'
    late = [b for b in flat if b['ts'] >= rel + 3600]
    assert fade_trade(late, rel, DATE)['status'] == 'untradeable'
    assert fade_trade([], rel, DATE)['status'] == 'untradeable'


def test_missing_exit_bar_then_flatten_fallback():
    rel = release_ts(DATE)
    # Early-close shape (the gauntlet's 2026-04-03 case): bars end
    # at +30m. While the session may still stream: no_exit_bar.
    # Once complete: the frozen flatten fallback — exit at the last
    # bar's close (100 + reaction end 110), kind 'flat'.
    bars = bars_around(rel, [101 + i for i in range(10)], 105.0,
                       minutes_after=31)
    got = fade_trade(bars, rel, DATE)
    assert got['status'] == 'no_exit_bar'
    got = fade_trade(bars, rel, DATE, complete=True)
    assert got['status'] == 'closed' and got['exit_kind'] == 'flat'
    # entry = 110 - 0.25 = 109.75; exit fill = 110 + 0.25 = 110.25;
    # pnl = -1*(110.25 - 109.75)*$2 - $3 = -$4.00.
    assert got['pnl'] == -4.00


# ── store registration + worker lifecycle ───────────────────────────

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
        "assert 'postnews_fade_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'postnews.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def test_worker_scores_calendar_event(db):
    boot_date, event_date = '2026-11-10', '2026-11-11'
    rel = release_ts(event_date)
    rows = [[b['ts'], b['o'], b['h'], b['l'], b['c'], b['v']]
            for b in bars_around(rel, [101 + i for i in range(10)], 105.0,
                                 minutes_after=130)]
    with db.tx() as c:
        db.put(c, 'intel:economic:2026-11-09', {'high_impact': [
            {'title': 'Nonfarm Payrolls', 'date': event_date,
             'time': '8:30 AM', 'impact': 'high', 'description': '',
             'country': 'United States'},
            # Same morning, a non-frozen release: must not score.
            {'title': 'Durable Goods Orders m/m', 'date': event_date,
             'time': '8:30 AM', 'impact': 'high', 'description': '',
             'country': 'United States'},
        ]})
        db.put(c, 'bar_window:MNQ.c.0', rows)
    worker = PostnewsFade(db, cfg=None)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    boot_now = datetime.fromisoformat(boot_date).replace(
        hour=12, tzinfo=ZoneInfo('America/Chicago')).timestamp()
    worker.process(boot_now)               # stamps forward_from
    worker.process(rel + 72 * 60)          # evaluates the event
    with db.tx() as c:
        state = db.get(c, 'postnews_fade:state', {})
        eps = c.execute(select(episodes.c.payload)).scalars().all()
    assert state['forward_from'] == event_date
    assert state['provisional'] is True
    assert len(eps) == 1
    p = eps[0]
    assert p['classes'] == ['NFP'] and p['status'] == 'closed'
    assert p['pnl'] == 6.00


# ── parity: fade_trade vs the gauntlet's scored trades ──────────────

def _default_archive():
    here = Path(__file__).resolve()
    root = here.parents[3] if len(here.parents) > 3 else here.parent
    return (root / 'goals' / 'market-compass-trading-improvements'
            / 'hidden_files')


ARCHIVE = Path(os.environ.get('POSTNEWS_ARCHIVE_DIR', _default_archive()))


@pytest.mark.skipif(
    not (ARCHIVE / 'post-news-continuation-2026-11-06'
           / 'trades_MNQ_0830_fade_all_t60.json').exists(),
    reason='gauntlet archive lives in the goal workspace, not the '
           'repo; parity runs where the archive is present')
def test_parity_against_gauntlet_trades():
    import bisect

    import pandas as pd
    from compass.postnews_fade import _flat_ts
    df = pd.read_parquet(
        ARCHIVE / 'momentum-trend-sweep-2026-11-06/parq/MNQ.parquet',
        columns=['ts', 'o', 'h', 'l', 'c'])
    bars_all = [{'ts': int(r.ts), 'o': r.o, 'h': r.h, 'l': r.l, 'c': r.c}
                for r in df.itertuples()]
    ts_list = [b['ts'] for b in bars_all]
    exp = json.loads((ARCHIVE / 'post-news-continuation-2026-11-06'
                      / 'trades_MNQ_0830_fade_all_t60.json'
                      ).read_text())
    assert len(exp) == 47
    for t in exp:
        rel = t['rel_ts']
        lo = bisect.bisect_left(ts_list, rel - 3600)
        hi = bisect.bisect_right(ts_list, rel + 30 * 3600)
        sub = bars_all[lo:hi]
        complete = any(b['ts'] > _flat_ts(t['date']) for b in sub)
        got = fade_trade(sub, rel, t['date'], complete=complete)
        if got['status'] == 'no_exit_bar':   # worker retry-expiry path
            got = fade_trade(sub, rel, t['date'], complete=True)
        assert got['status'] == 'closed', t['date']
        assert got['d'] == t['d'], t['date']
        assert got['exit_kind'] == t['exit_kind'], t['date']
        assert abs(got['pnl'] - t['pnl']) < 0.01, t['date']
