"""Rumers Box detector: box math, continuation/reversal, confirmation,
trigger-time VWAP, scan integration, paper gating."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import rumers_box as rb
from compass import ict_paper
from compass.ict_common import session_vwap

NY = ZoneInfo("America/New_York")
SYM = 'NQ.c.0'
YMSYM = 'YM.c.0'
D0 = datetime(2026, 9, 14).date()  # Monday


def rth_ts(d, i):
    base = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
    return int((base + timedelta(minutes=i)).timestamp())


def row(ts, o, h, l, c, v=100):
    return [ts, o, h, l, c, v]


def bar_dicts(rows):
    return [{'ts': r[0], 'o': r[1], 'h': r[2], 'l': r[3], 'c': r[4], 'v': r[5]}
            for r in rows]


def prior_day(d, hi=101.0, lo=99.0, mid=100.0):
    """Complete RTH session -> box (hi, lo)."""
    return [row(rth_ts(d, i), mid, hi, lo, mid) for i in range(390)]


def inside_bars(d, n, price=100.0, v=100):
    return [row(rth_ts(d, i), price, price + 0.5, price - 0.5, price, v)
            for i in range(n)]


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'rb.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _cfg(**kw):
    base = {'ict_rumers_box': True, 'ict_symbols': (SYM,),
            'ict_rb_vwap_min_bars': 5}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c, sym, rows):
    db.put(c, 'bar_window:' + sym, rows)


def _bars(db, c, sym):
    return bar_dicts(db.get(c, 'bar_window:' + sym))


def _prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# --- box ---

def test_prior_box(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        rows = prior_day(d0) + inside_bars(d1, 30)
        box = rb.prior_box(bar_dicts(rows), d1.isoformat())
        assert box[0] == pytest.approx(101.0)
        assert box[1] == pytest.approx(99.0)
        assert box[2] == d0.isoformat()


def test_prior_box_none_without_history(db):
    with db.tx() as c:
        d1 = D0 + timedelta(days=1)
        assert rb.prior_box(bar_dicts(inside_bars(d1, 30)),
                            d1.isoformat()) is None


# --- continuation ---

def _continuation_day(d, direction='long', trig_vol=5000):
    """Two consecutive closes beyond the box edge at bars 21/22."""
    rows = inside_bars(d, 21)
    if direction == 'long':
        rows.append(row(rth_ts(d, 21), 100.0, 101.6, 99.8, 101.4, 100))
        rows.append(row(rth_ts(d, 22), 101.4, 102.6, 101.0, 102.0, trig_vol))
        for i in range(23, 30):
            rows.append(row(rth_ts(d, i), 102.0, 102.5, 101.5, 102.2, 100))
    else:
        rows.append(row(rth_ts(d, 21), 100.0, 100.2, 98.4, 98.6, 100))
        rows.append(row(rth_ts(d, 22), 98.6, 99.0, 97.4, 98.0, trig_vol))
        for i in range(23, 30):
            rows.append(row(rth_ts(d, i), 98.0, 98.5, 97.5, 97.8, 100))
    return rows


def test_continuation_long(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        sig = rb.detect_signal(
            bar_dicts(prior_day(d0) + _continuation_day(d1)),
            101.0, 99.0, d1.isoformat(), vwap_min_bars=5)
        assert sig is not None
        assert (sig['setup'], sig['direction']) == ('continuation', 'long')
        assert sig['entry'] == pytest.approx(102.0)
        assert sig['vol_ok'] is True
        assert sig['stop'] < 101.0  # beyond the broken edge
        assert sig['target'] == pytest.approx(sig['entry'] + 2 * sig['risk_pts'])
        assert sig['target_kind'] == 'rr'
        assert sig['rr'] == pytest.approx(2.0)


def test_continuation_short(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        sig = rb.detect_signal(
            bar_dicts(prior_day(d0) + _continuation_day(d1, 'short')),
            101.0, 99.0, d1.isoformat(), vwap_min_bars=5)
        assert sig is not None
        assert (sig['setup'], sig['direction']) == ('continuation', 'short')
        assert sig['entry'] == pytest.approx(98.0)
        assert sig['stop'] > 99.0


# --- reversal ---

def _reversal_day(d, direction='short', box_hi=101.0, box_lo=99.0,
                  trig_vol=5000):
    mid = (box_hi + box_lo) / 2.0
    rows = inside_bars(d, 10, price=mid)
    if direction == 'short':  # spike above box_hi, close back inside
        rows.append(row(rth_ts(d, 10), mid, box_hi + 2.0, mid - 0.2,
                        box_hi - 1.0, trig_vol))
        for i in range(11, 30):
            rows.append(row(rth_ts(d, i), box_hi - 1.0, box_hi - 0.5,
                            box_hi - 1.5, box_hi - 1.2, 100))
    else:  # spike below box_lo, close back inside
        rows.append(row(rth_ts(d, 10), mid, mid + 0.2, box_lo - 2.0,
                        box_lo + 1.0, trig_vol))
        for i in range(11, 30):
            rows.append(row(rth_ts(d, i), box_lo + 1.0, box_lo + 1.5,
                            box_lo + 0.5, box_lo + 1.2, 100))
    return rows


def _wide_box_day(d0, d1, direction='short', width=7.0):
    """Box of `width` centered on 101.5 with a reversal day sized so the
    opposite edge lands between 1.5R and 2R."""
    box_hi = 101.5 + width / 2.0
    box_lo = 101.5 - width / 2.0
    return (prior_day(d0, hi=box_hi, lo=box_lo, mid=101.5),
            _reversal_day(d1, direction, box_hi, box_lo), box_hi, box_lo)


def test_reversal_short(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        prior, day, box_hi, box_lo = _wide_box_day(d0, d1, 'short', width=6.0)
        sig = rb.detect_signal(bar_dicts(prior + day), box_hi, box_lo,
                               d1.isoformat(), vwap_min_bars=5,
                               stop_buf_atr_frac=0.0)
        assert sig is not None
        assert (sig['setup'], sig['direction']) == ('reversal', 'short')
        assert sig['entry'] == pytest.approx(box_hi - 1.0)
        assert sig['stop'] == pytest.approx(box_hi + 2.0)  # spike extreme
        assert sig['target'] == pytest.approx(box_lo)
        assert sig['target_kind'] == 'opposite_edge'


def test_reversal_long(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        prior, day, box_hi, box_lo = _wide_box_day(d0, d1, 'long', width=6.0)
        sig = rb.detect_signal(bar_dicts(prior + day), box_hi, box_lo,
                               d1.isoformat(), vwap_min_bars=5,
                               stop_buf_atr_frac=0.0)
        assert sig is not None
        assert (sig['setup'], sig['direction']) == ('reversal', 'long')
        assert sig['stop'] == pytest.approx(box_lo - 2.0)
        assert sig['target'] == pytest.approx(box_hi)
        assert sig['target_kind'] == 'opposite_edge'


def test_reversal_takes_opposite_edge_target(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        # wide box: opposite edge sits between 1.5R and 2R -> closer target
        prior, day, box_hi, box_lo = _wide_box_day(d0, d1, 'short', width=6.0)
        sig = rb.detect_signal(bar_dicts(prior + day), box_hi, box_lo,
                               d1.isoformat(), vwap_min_bars=5,
                               stop_buf_atr_frac=0.0)
        assert sig is not None
        assert sig['target_kind'] == 'opposite_edge'
        assert sig['target'] == pytest.approx(box_lo)
        assert sig['rr'] == pytest.approx(5.0 / 3.0)


def test_reversal_skipped_when_target_too_close(db):
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        # narrow box: opposite edge (102.5) sits inside 1.5R -> no trade
        rows = inside_bars(d1, 10, price=103.0)
        rows.append(row(rth_ts(d1, 10), 103.0, 105.0, 102.8, 103.8, 5000))
        sig = rb.detect_signal(
            bar_dicts(prior_day(d0, hi=104.0, lo=102.5, mid=103.0) + rows),
            104.0, 102.5, d1.isoformat(), vwap_min_bars=5)
        assert sig is None


# --- confirmation ---

def test_vwap_only_confirmation(db):
    """Low volume but price on the right side of VWAP still confirms."""
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        sig = rb.detect_signal(
            bar_dicts(prior_day(d0) + _continuation_day(d1, trig_vol=100)),
            101.0, 99.0, d1.isoformat(), vwap_min_bars=5)
        assert sig is not None
        assert sig['vol_ok'] is False
        assert sig['vwap_ok'] is True


def test_no_confirmation_no_signal(db):
    """Low volume and VWAP unavailable -> no trade."""
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        sig = rb.detect_signal(
            bar_dicts(prior_day(d0) + _continuation_day(d1, trig_vol=100)),
            101.0, 99.0, d1.isoformat(), vwap_min_bars=1000)
        assert sig is None


def test_vwap_computed_asof_trigger(db):
    """Bars printed after the trigger must not move the VWAP used for
    confirmation."""
    with db.tx() as c:
        d0, d1 = D0, D0 + timedelta(days=1)
        rows = prior_day(d0) + _continuation_day(d1)
        bars = bar_dicts(rows)
        sig = rb.detect_signal(bars, 101.0, 99.0, d1.isoformat(),
                               vwap_min_bars=5)
        assert sig is not None
        known = [b for b in bars if b['ts'] <= sig['signal_ts']]
        expected = session_vwap(known, sig['signal_ts'], min_bars=5)
        assert sig['vwap'] == pytest.approx(expected)
        # the full-day VWAP (with the post-trigger rally) differs
        full = session_vwap(bars, bars[-1]['ts'], min_bars=5)
        assert full != pytest.approx(expected)


# --- scan integration ---

def _seed_continuation(db, c, sym):
    d0 = _prev_weekday(D0 + timedelta(days=1))
    d1 = D0 + timedelta(days=1)
    _seed(db, c, sym, prior_day(d0) + _continuation_day(d1))
    return d1


def test_scan_signal_and_dedupe(db):
    with db.tx() as c:
        d1 = _seed_continuation(db, c, SYM)
        now = rth_ts(d1, 31)
        res = rb.scan(db, c, _cfg(), now)
        assert res['ran'] is True and res['signals'] == 1
        latest = db.get(c, 'rumers_box:latest')
        row_ = latest['rows'][SYM]
        assert row_['setup'] == 'continuation'
        assert row_['box_hi'] == pytest.approx(101.0)
        sigs = db.recent(c, 'rumers_box_signal', limit=5)
        assert len(sigs) == 1
        res2 = rb.scan(db, c, _cfg(), now + 200)
        assert res2['signals'] == 0
        assert res2['excluded'].get('already_traded_today') == 1


def test_scan_disabled(db):
    with db.tx() as c:
        res = rb.scan(db, c, _cfg(ict_rumers_box=False), 1_699_992_000)
        assert res == {'ran': False, 'reason': 'disabled'}


def test_scan_ym_supported(db):
    with db.tx() as c:
        d1 = _seed_continuation(db, c, YMSYM)
        now = rth_ts(d1, 31)
        res = rb.scan(db, c, _cfg(ict_symbols=(YMSYM,)), now)
        assert res['signals'] == 1
        assert db.get(c, 'rumers_box:latest')['rows'][YMSYM]['direction'] \
            == 'long'


def test_scan_no_setup_excluded(db):
    with db.tx() as c:
        d0 = _prev_weekday(D0 + timedelta(days=1))
        d1 = D0 + timedelta(days=1)
        _seed(db, c, SYM, prior_day(d0) + inside_bars(d1, 30))
        res = rb.scan(db, c, _cfg(), rth_ts(d1, 31))
        assert res['excluded'].get('no_setup') == 1


# --- paper gating ---

def _sig():
    return {'direction': 'long', 'entry': 102.0, 'stop': 100.5,
            'target': 105.0, 'signal_ts': 1_699_992_000, 'rr': 2.0}


def test_paper_dual_gate(db):
    with db.tx() as c:
        r = ict_paper.submit(db, c, _cfg(ict_rumers_box=False), 1_699_992_000,
                             'rumers_box', SYM, _sig())
        assert r == {'submitted': False, 'reason': 'paper_disabled'}
        r = ict_paper.submit(db, c, _cfg(), 1_699_992_000,
                             'rumers_box', SYM, _sig())
        assert r['reason'] == 'paper_disabled'


def test_paper_submit_tag(db):
    with db.tx() as c:
        cfg = _cfg(ict_futures_paper=True)
        r = ict_paper.submit(db, c, cfg, 1_699_992_000,
                             'rumers_box', SYM, _sig())
        assert r['submitted'] is True
        trade = db.get(c, 'trade:' + r['trade_id'])
        assert trade['strategy'] == 'yt-rumers-box'
