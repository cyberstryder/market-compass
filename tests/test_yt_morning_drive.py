"""Morning-drive fade detector: drive math, trigger, scan integration, paper gating."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import morning_drive as md
from compass import ict_paper

NY = ZoneInfo("America/New_York")
SYM = 'NQ.c.0'
# Monday 2026-09-14; tests use consecutive weekdays from here.
D0 = datetime(2026, 9, 14).date()


def rth_ts(d, minute_idx):
    base = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
    return int((base + timedelta(minutes=minute_idx)).timestamp())


def row(ts, o, h, l, c, v=1000):
    return [ts, o, h, l, c, v]


def flat_day(d, o=100.0, rng=4.0):
    """390 1-min RTH bars, range exactly `rng`, no drive."""
    return [row(rth_ts(d, i), o, o + rng / 2, o - rng / 2, o)
            for i in range(390)]


def drive_day(d):
    """Unhealthy up-drive: 100 -> 110 by 09:44, then a fade with a
    momentum-reclaim trigger at 10:00 (bar 30)."""
    rows = []
    for i in range(15):  # 09:30-09:44 ramp
        o = 100.0 + i * (10.0 / 14)
        rows.append(row(rth_ts(d, i), o, o + 0.5, o - 0.5, o))
    for i in range(15, 28):  # drift down to ~107
        o = 109.5 - (i - 15) * 0.2
        rows.append(row(rth_ts(d, i), o, o + 0.4, o - 0.4, o))
    # bars 28,29 set up the 2-bar extreme; bar 30 triggers
    rows.append(row(rth_ts(d, 28), 107.0, 107.4, 106.5, 106.9))
    rows.append(row(rth_ts(d, 29), 106.9, 107.2, 106.3, 106.6))
    rows.append(row(rth_ts(d, 30), 106.6, 106.8, 105.8, 106.0))  # 10:00 trigger
    for i in range(31, 60):  # rest of the window, keep fading
        o = 106.0 - (i - 30) * 0.1
        rows.append(row(rth_ts(d, i), o, o + 0.3, o - 0.3, o))
    return rows


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'md.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _cfg(**kw):
    base = {'ict_morning_drive': True, 'ict_symbols': (SYM,)}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c, rows):
    db.put(c, 'bar_window:' + SYM, rows)


def _fourteen_flat_plus_drive(db, c):
    rows = []
    d = D0
    added = 0
    while added < 14:
        if d.weekday() < 5:
            rows.extend(flat_day(d))
            added += 1
        d += timedelta(days=1)
    drive_d = d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday())
    rows.extend(drive_day(drive_d))
    _seed(db, c, rows)
    return drive_d.isoformat()


# --- drive math ---

def test_avg_rth_range_needs_history(db):
    with db.tx() as c:
        assert md.avg_rth_range([], today='2026-09-14') is None


def test_drive_state_unhealthy(db):
    with db.tx() as c:
        today = _fourteen_flat_plus_drive(db, c)
        bars = md._bars_from_recent(
            [{'ts': r[0], 'payload': {'o': r[1], 'h': r[2], 'l': r[3],
                                      'c': r[4], 'v': r[5]}} for r in
             db.get(c, 'bar_window:' + SYM)])
        avg = md.avg_rth_range(bars, today=today)
        assert avg == pytest.approx(4.0)
        drive = md.drive_state(bars, today, avg)
        assert drive['direction'] == 'up'
        assert drive['drive_range'] == pytest.approx(10.5)
        assert drive['unhealthy'] is True


def test_small_drive_not_unhealthy(db):
    with db.tx() as c:
        rows = []
        d = D0
        added = 0
        while added < 14:
            if d.weekday() < 5:
                rows.extend(flat_day(d, rng=4.0))
                added += 1
            d += timedelta(days=1)
        # drive of only 1.0 vs avg 4.0 -> healthy
        dd = d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday())
        rows.extend([row(rth_ts(dd, i), 100.0, 100.5, 99.5, 100.0)
                     for i in range(60)])
        _seed(db, c, rows)
        bars = md._bars_from_recent(
            [{'ts': r[0], 'payload': {'o': r[1], 'h': r[2], 'l': r[3],
                                      'c': r[4], 'v': r[5]}} for r in rows])
        avg = md.avg_rth_range(bars, today=dd.isoformat())
        drive = md.drive_state(bars, dd.isoformat(), avg)
        assert drive['unhealthy'] is False


# --- trigger ---

def test_trigger_fires_in_window(db):
    with db.tx() as c:
        today = _fourteen_flat_plus_drive(db, c)
        bars = md._bars_from_recent(
            [{'ts': r[0], 'payload': {'o': r[1], 'h': r[2], 'l': r[3],
                                      'c': r[4], 'v': r[5]}} for r in
             db.get(c, 'bar_window:' + SYM)])
        avg = md.avg_rth_range(bars, today=today)
        drive = md.drive_state(bars, today, avg)
        idx, direction = md.detect_trigger(bars, today, drive)
        assert direction == 'short'
        trig = md._rth_bars(bars)[today][idx]
        assert trig['c'] == pytest.approx(106.0)


# --- scan integration ---

def test_find_trigger_uses_asof_drive_no_lookahead(db):
    """A new extreme printed AFTER the trigger must not move the drive
    extreme / stop. find_trigger evaluates the drive as of the trigger bar."""
    with db.tx() as c:
        today = _fourteen_flat_plus_drive(db, c)
        bars = md._bars_from_recent(
            [{'ts': r[0], 'payload': {'o': r[1], 'h': r[2], 'l': r[3],
                                      'c': r[4], 'v': r[5]}} for r in
             db.get(c, 'bar_window:' + SYM)])
        sess = md._rth_bars(bars)[today]
        # spike to a new high well after the trigger bar
        late = dict(sess[45])
        late['h'] = 115.0
        late['c'] = 112.0
        sess2 = sess[:45] + [late] + sess[46:]
        avg = md.avg_rth_range(bars, today=today)
        idx, direction, drive, _ = md.find_trigger(sess2, avg)
        assert direction == 'short'
        assert drive['extreme'] == pytest.approx(110.5)  # pre-spike extreme
        sig = md.build_signal(sess2, idx, direction, drive)
        assert sig['stop'] == pytest.approx(110.5)


def test_scan_signal_and_levels(db):
    with db.tx() as c:
        today = _fourteen_flat_plus_drive(db, c)
        now = rth_ts(datetime.fromisoformat(today).date(), 61) + 1
        res = md.scan(db, c, _cfg(), now)
        assert res['ran'] is True and res['signals'] == 1
        latest = db.get(c, 'morning_drive:latest')
        sig = latest['rows'][SYM]
        assert sig['direction'] == 'short'
        assert sig['entry'] == pytest.approx(106.0)
        assert sig['stop'] == pytest.approx(110.5)  # session extreme
        assert sig['target'] == pytest.approx(106.0 - 2 * 4.5)  # 2R
        assert sig['rr'] == pytest.approx(2.0)
        flat = datetime.fromtimestamp(sig['flatten_at'], NY)
        assert (flat.hour, flat.minute) == (12, 0)  # 12:00 ET time stop
        sigs = db.recent(c, 'morning_drive_signal', limit=5)
        assert len(sigs) == 1


def test_scan_second_run_same_day_held(db):
    with db.tx() as c:
        today = _fourteen_flat_plus_drive(db, c)
        now = rth_ts(datetime.fromisoformat(today).date(), 61) + 1
        assert md.scan(db, c, _cfg(), now)['signals'] == 1
        res2 = md.scan(db, c, _cfg(), now + 130)
        assert res2['signals'] == 0
        assert res2['excluded'].get('already_traded_today') == 1


def test_scan_disabled(db):
    with db.tx() as c:
        res = md.scan(db, c, _cfg(ict_morning_drive=False), 1_700_000_000)
        assert res == {'ran': False, 'reason': 'disabled'}


def test_scan_healthy_drive_excluded(db):
    with db.tx() as c:
        rows = []
        d = D0
        added = 0
        while added < 14:
            if d.weekday() < 5:
                rows.extend(flat_day(d, rng=4.0))
                added += 1
            d += timedelta(days=1)
        dd = d if d.weekday() < 5 else d + timedelta(days=7 - d.weekday())
        rows.extend([row(rth_ts(dd, i), 100.0, 100.5, 99.5, 100.0)
                     for i in range(60)])
        _seed(db, c, rows)
        now = rth_ts(dd, 61)
        res = md.scan(db, c, _cfg(), now)
        assert res['excluded'].get('drive_too_small') == 1


# --- paper gating ---

def _sig():
    return {'direction': 'short', 'entry': 106.0, 'stop': 110.0,
            'target': 98.0, 'signal_ts': 1_700_000_000, 'rr': 2.0}


def test_paper_dual_gate(db):
    with db.tx() as c:
        # both flags off -> held
        r = ict_paper.submit(db, c, _cfg(ict_morning_drive=False), 1_700_000_000,
                             'morning_drive', SYM, _sig())
        assert r == {'submitted': False, 'reason': 'paper_disabled'}
        # detector flag on but paper flag off -> still held
        r = ict_paper.submit(db, c, _cfg(), 1_700_000_000,
                             'morning_drive', SYM, _sig())
        assert r['reason'] == 'paper_disabled'


def test_paper_submit_time_stop(db):
    with db.tx() as c:
        cfg = _cfg(ict_futures_paper=True)
        r = ict_paper.submit(db, c, cfg, 1_700_000_000, 'morning_drive', SYM,
                             _sig(), flatten_at=1_700_010_000)
        assert r['submitted'] is True
        trade = db.get(c, 'trade:' + r['trade_id'])
        assert trade['flatten_at'] == 1_700_010_000
        assert trade['strategy'] == 'yt-morning-drive'
        assert 'track' not in trade
