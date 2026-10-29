"""Tests for the Close Momentum detector (first-30 → last-45, YM shadow)."""
from datetime import datetime, timezone

import pytest

from compass.close_momentum import (
    signal, trade_levels, daily_atr, scan, FALLBACK_POINTS, _session_ts,
)
from compass.config import Config
from compass.store import Store
from compass import ict_paper


# Wed 2026-09-30, a regular CBOT session. 15:00 CT = 20:00 UTC (CDT).
T1500 = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc).timestamp()
T0830 = T1500 - 6.5 * 3600
SYM = 'YM.c.0'


def _day(o830, o900, o1500, start_px=None):
    """1-min bars 08:00 → 15:00 CT. Opens pinned at 08:30, 09:00, 15:00."""
    bars = []
    px = start_px if start_px is not None else o830
    t = T0830 - 30 * 60
    while t <= T1500:
        if t == T0830:
            px = o830
        elif t == T0830 + 1800:
            px = o900
        elif t == T1500:
            px = o1500
        bars.append({'ts': t, 'o': px, 'h': px + 2, 'l': px - 2, 'c': px + 1})
        t += 60
    return bars


def test_session_ts_is_ct_wall_clock():
    assert _session_ts(T1500 + 120, 15, 0) == T1500
    assert _session_ts(T1500 + 120, 8, 30) == T0830


def test_signal_long_when_first30_and_day_agree():
    bars = _day(46000, 46050, 46200)
    s = signal(bars, T1500 + 30)
    assert s and s['direction'] == 'long' and s['entry'] == 46200


def test_signal_short_when_both_down():
    s = signal(_day(46000, 45950, 45800), T1500 + 30)
    assert s and s['direction'] == 'short'


def test_no_signal_when_first30_and_day_disagree():
    assert signal(_day(46000, 46050, 45800), T1500 + 30) is None
    assert signal(_day(46000, 45950, 46300), T1500 + 30) is None


def test_no_signal_on_flat_first30():
    assert signal(_day(46000, 46000, 46300), T1500 + 30) is None


def test_outside_window_is_silent():
    bars = _day(46000, 46050, 46200)
    assert signal(bars, T1500 - 60) is None          # 14:59
    assert signal(bars, T1500 + 5 * 60) is None      # 15:05
    assert signal(bars, T1500 + 4 * 60 + 59) is not None


def test_missing_anchor_bar_is_no_signal():
    bars = [b for b in _day(46000, 46050, 46200) if b['ts'] != T0830]
    assert signal(bars, T1500 + 30) is None


def test_trade_levels_from_daily_atr():
    sig = signal(_day(46000, 46050, 46200), T1500 + 30)
    lv = trade_levels(sig, 600.0, 'YM')
    assert lv['stop'] == 46200 - 150 and lv['target'] == 46200 + 300
    assert lv['stop_basis'] == 'daily_atr'
    lv_s = trade_levels({**sig, 'direction': 'short'}, 600.0, 'YM')
    assert lv_s['stop'] == 46200 + 150 and lv_s['target'] == 46200 - 300


def test_trade_levels_fallback_points():
    sig = signal(_day(46000, 46050, 46200), T1500 + 30)
    lv = trade_levels(sig, None, 'YM')
    assert lv['stop_basis'] == 'fallback_points'
    assert lv['risk_pts'] == FALLBACK_POINTS['YM'][0]
    assert trade_levels(sig, None, 'ZZZ') is None


def test_registered_in_paper_registries():
    assert ict_paper.STRATEGY_TAGS['close_momentum'] == 'ict-close-momentum'
    assert ict_paper.DETECTOR_FLAGS['close_momentum'] == 'ict_close_momentum'
    assert ict_paper.DETECTOR_SYMBOL_ALLOWLIST['close_momentum'] == {'YM', 'MYM'}
    assert 'close_momentum' in ict_paper.PAPER_ONLY_DETECTORS
    assert not ict_paper.symbol_allowed('close_momentum', 'MNQ.c.0')
    assert not ict_paper.symbol_allowed('close_momentum', 'NQ.c.0')


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'cm.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_close_momentum = True
    c.ict_close_momentum_symbols = (SYM,)
    return c


def _seed(db, bars, quote_px, now):
    with db.tx() as c:
        db.put(c, 'bar_window:' + SYM,
               [(b['ts'], b['o'], b['h'], b['l'], b['c'], 10) for b in bars])
        db.put(c, 'quote:' + SYM, {'ts': now, 'bid': quote_px - 1,
                                   'ask': quote_px, 'bid_size': 10,
                                   'ask_size': 10, 'source': 'fixture'})
        for i in range(20):
            ts = T0830 - (20 - i) * 86400
            db.append(c, 'daily', 'futures', SYM, ts,
                      {'o': 46000, 'h': 46300, 'l': 45700, 'c': 46100, 'v': 1})


def test_daily_atr_from_store(db):
    _seed(db, _day(46000, 46050, 46200), 46200, T1500 + 30)
    with db.tx() as c:
        assert daily_atr(db, c, SYM) == pytest.approx(600.0)


def test_scan_submits_once_per_day(db, cfg):
    now = T1500 + 30
    _seed(db, _day(46000, 46050, 46200), 46200, now)
    with db.tx() as c:
        r = scan(db, c, cfg, now)
        assert r['ran'] and r['fired'] == 1, r
        pos = db.get(c, 'position:ict:close_momentum:%s:t1' % SYM, {})
        assert pos.get('status') == 'open' and pos.get('side') == 'long'
        assert pos['stop'] == pytest.approx(46200 - 150)
        # Second pass inside the window: nothing new.
        r2 = scan(db, c, cfg, now + 60)
        assert r2['ran'] and r2['fired'] == 0
        assert len(db.recent(c, 'ict_close_momentum_signal', limit=10)) == 1


def test_scan_disabled_and_outside_window(db, cfg):
    _seed(db, _day(46000, 46050, 46200), 46200, T1500 + 30)
    with db.tx() as c:
        cfg.ict_close_momentum = False
        assert scan(db, c, cfg, T1500 + 30)['reason'] == 'disabled'
        cfg.ict_close_momentum = True
        assert scan(db, c, cfg, T1500 - 600)['reason'] == 'outside_window'


def test_scan_records_no_signal_day(db, cfg):
    now = T1500 + 30
    _seed(db, _day(46000, 46050, 45800), 45800, now)
    with db.tx() as c:
        r = scan(db, c, cfg, now)
        assert r['fired'] == 0
        seen = db.get(c, 'ict_close_momentum:fired:%s:2026-09-30' % SYM)
        assert seen and seen.get('no_signal') is True
