"""ICC detector: resampling, bias, indication/grab/continuation/flip,
target selection, scan integration, paper gating."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import icc
from compass import ict_paper
from compass.ict_common import fractal_swings

NY = ZoneInfo("America/New_York")
SYM = 'NQ.c.0'
T0 = datetime(2026, 9, 14, 0, 0, tzinfo=NY)


def q(o, h, l, c, v=500):
    return {'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def expand(qbars, start):
    out = []
    dt = start
    for b in qbars:
        base = len(out)
        for i in range(15):
            frac = i / 14.0
            p = b['o'] + (b['c'] - b['o']) * frac
            out.append({'ts': int((dt + timedelta(minutes=i)).timestamp()),
                        'o': p, 'h': p + 0.03, 'l': p - 0.03, 'c': p,
                        'v': b['v'] // 15})
        out[base]['o'] = b['o']
        out[-1]['c'] = b['c']
        him = max(range(base, len(out)), key=lambda i: out[i]['h'])
        lom = max(range(base, len(out)), key=lambda i: -out[i]['l'])
        out[him]['h'] = b['h']
        out[lom]['l'] = b['l']
        dt += timedelta(minutes=15)
    return out, dt


def build_long():
    """Full long scenario; returns (1-min bars, indication level L)."""
    qs = []
    p = 90.0
    for _ in range(2):  # two 4H waves -> 4H bias long
        for _ in range(64):
            o = p
            c = p + 0.15
            qs.append(q(o, c + 0.06, o - 0.05, c))
            p = c
        for _ in range(32):
            o = p
            c = p - 0.12
            qs.append(q(o, o + 0.05, c - 0.06, c))
            p = c
    top = p
    for k in range(48):
        o = top - 0.6
        c = top - 0.4 if k % 2 == 0 else top - 0.8
        qs.append(q(o, top - 0.2, top - 1.0, c))
    m1, dt = expand(qs, T0)
    L = max(b['h'] for b in icc.resample(m1, 60))
    qs2 = []
    qs2.append(q(L - 1.0, L + 2.5, L - 1.2, L + 1.7))  # indication spike
    qs2.append(q(L + 1.7, L + 1.9, L + 0.9, L + 1.3))
    qs2.append(q(L + 1.3, L + 1.5, L + 0.5, L + 0.9))
    qs2.append(q(L + 0.9, L + 1.1, L + 0.3, L + 0.5))
    ind_1h_hi = L + 2.5
    qs2.append(q(L + 0.5, L + 0.7, L - 0.2, L + 0.3))  # grab: touches L
    qs2.append(q(L + 0.3, L + 0.5, L - 0.4, L))
    qs2.append(q(L, L + 0.2, L - 0.7, L - 0.3))
    qs2.append(q(L - 0.3, L - 0.1, L - 1.0, L - 0.5))
    qs2.append(q(L - 0.5, L, L - 0.8, L - 0.3))  # continuation
    qs2.append(q(L - 0.3, L + 0.1, L - 0.6, L - 0.1))
    qs2.append(q(L - 0.1, ind_1h_hi + 0.8, L - 0.3, L + 2.7))  # 15m flip
    qs2.append(q(L + 2.7, L + 2.9, L - 0.2, L + 0.1))
    for _ in range(8):
        qs2.append(q(L + 0.1, L + 0.3, L - 0.3, L))
    m1b, _ = expand(qs2, dt)
    return m1 + m1b, L


def h1bar(ts, o, h, l, c):
    return {'ts': ts, 'o': o, 'h': h, 'l': l, 'c': c, 'v': 100}


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'icc.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _cfg(**kw):
    base = {'ict_icc': True, 'ict_symbols': (SYM,), 'ict_icc_lookback_1h': 20}
    base.update(kw)
    return SimpleNamespace(**base)


# --- resample ---

def test_resample_buckets():
    m1, _ = expand([q(100, 101, 99, 100.5), q(100.5, 102, 100, 101.5)],
                   T0)
    h1 = icc.resample(m1, 60)
    assert len(h1) == 1
    assert h1[0]['o'] == pytest.approx(100.0)
    assert h1[0]['h'] == pytest.approx(102.0)
    assert h1[0]['l'] == pytest.approx(99.0)
    assert h1[0]['c'] == pytest.approx(101.5)
    m15 = icc.resample(m1, 15)
    assert len(m15) == 2


# --- bias ---

def _h4_trend():
    vals = [(100, 101, 99, 100), (100, 102, 99, 101),
            (101, 104, 100, 103), (103, 103, 100, 101),
            (101, 102, 98, 99), (99, 101, 98, 100),
            (100, 103, 99, 102), (102, 106, 101, 105),
            (105, 105, 102, 103), (103, 104, 100, 101),
            (101, 103, 100, 102), (102, 104, 101, 103)]
    return [h1bar(i, o, h, l, c) for i, (o, h, l, c) in enumerate(vals)]


def test_htf_bias_long_and_short():
    h4 = _h4_trend()
    assert icc.htf_bias(h4) == 'long'
    inv = [dict(b, o=200 - b['o'], h=200 - b['l'], l=200 - b['h'],
                c=200 - b['c']) for b in h4]
    assert icc.htf_bias(inv) == 'short'


def test_htf_bias_none_when_mixed():
    h4 = [h1bar(i, 100, 101, 99, 100 + (0.5 if i % 2 else -0.5))
          for i in range(12)]
    assert icc.htf_bias(h4) is None


# --- indication / grab / continuation ---

def test_detect_indication_long():
    h1 = [h1bar(i, 100, 101, 99, 100) for i in range(30)]
    h1.append(h1bar(30, 100, 103, 99.5, 102.5))
    idx, direction, level = icc.detect_indication(h1, lookback=20)
    assert (idx, direction) == (30, 'long')
    assert level == pytest.approx(101.0)


def test_detect_indication_short():
    h1 = [h1bar(i, 100, 101, 99, 100) for i in range(30)]
    h1.append(h1bar(30, 100, 100.5, 96, 97))
    idx, direction, level = icc.detect_indication(h1, lookback=20)
    assert (idx, direction) == (30, 'short')
    assert level == pytest.approx(99.0)


def test_detect_indication_none_without_new_extreme():
    h1 = [h1bar(i, 100, 101, 99, 100) for i in range(40)]
    assert icc.detect_indication(h1, lookback=20) == (None, None, None)


def test_grab_is_first_touch_only():
    h1 = [h1bar(i, 102, 103, 101.5, 102) for i in range(10)]
    h1[5] = h1bar(5, 102, 104, 101.5, 103.5)   # indication
    h1[7] = h1bar(7, 103, 103.5, 100.8, 101.5)  # first touch of 101
    h1[9] = h1bar(9, 101.5, 102, 100.9, 101.2)  # second touch
    assert icc.detect_grab(h1, 5, 101.0, 'long') == 7


def test_continuation_close_through_level():
    h1 = [h1bar(i, 102, 103, 101.5, 102) for i in range(10)]
    h1[5] = h1bar(5, 102, 104, 101.5, 103.5)
    h1[6] = h1bar(6, 103, 103.5, 100.8, 101.2)  # grab touch
    h1[7] = h1bar(7, 101.2, 101.5, 100.6, 100.9)  # still correcting
    h1[8] = h1bar(8, 100.9, 102, 100.8, 101.8)  # back through 101
    assert icc.detect_continuation(h1, 6, 101.0, 'long') == 8
    assert icc.detect_continuation(h1, 6, 101.0, 'short') == 7


# --- micro flip ---

def _m15_trend(n=12, start=100.0, step=0.4):
    return [h1bar(i, start + i * step, start + i * step + 0.2,
                  start + i * step - 0.2, start + i * step + 0.1)
            for i in range(n)]


def test_micro_flip_long():
    m15 = _m15_trend(6, 100.0, 0.3)
    # pullback forms a swing high, then a close above it
    m15 += [h1bar(6, 101.8, 102.0, 101.0, 101.2),
            h1bar(7, 101.2, 101.4, 100.6, 100.8),
            h1bar(8, 100.8, 101.0, 100.2, 100.4),
            h1bar(9, 100.4, 102.5, 100.3, 102.3)]
    bar, entry = icc.micro_flip(m15, after_ts=-1, direction='long')
    assert bar is not None and entry == pytest.approx(102.3)


def test_micro_flip_none_without_break():
    m15 = _m15_trend(10, 100.0, 0.1)
    bar, _ = icc.micro_flip(m15, after_ts=-1, direction='long')
    assert bar is None


# --- target selection ---

def _h1_with_swings():
    # swing highs at idx 5 (110) and 10 (108); swing low at 15 (90)
    bars = [h1bar(i, 100, 101, 99, 100) for i in range(25)]
    bars[5] = h1bar(5, 109, 110, 108, 109)
    bars[10] = h1bar(10, 107, 108, 106, 107)
    bars[15] = h1bar(15, 91, 92, 90, 91)
    return bars


def test_pick_target_nearest_unswept():
    h1 = _h1_with_swings()
    tgt, kind = icc.pick_target(h1, 105.0, 'long', 1.0, ind_idx=20)
    assert (tgt, kind) == (108.0, 'liquidity')


def test_pick_target_skips_swept():
    h1 = _h1_with_swings()
    h1[20] = h1bar(20, 107, 109, 106, 108)  # sweeps the 108 level
    tgt, kind = icc.pick_target(h1, 105.0, 'long', 1.0, ind_idx=21)
    # 108 is swept; the sweeping bar's own high (109) is now the level
    assert (tgt, kind) == (109.0, 'liquidity')


def test_pick_target_eqh_no_trade():
    h1 = _h1_with_swings()
    h1[10] = h1bar(10, 109, 110.05, 108, 109)  # ~equal to the 110 high
    tgt, kind = icc.pick_target(h1, 105.0, 'long', 2.0, ind_idx=20)
    assert (tgt, kind) == (None, 'eqh_eql')


def test_pick_target_ignores_post_indication_swings():
    h1 = _h1_with_swings()
    tgt, kind = icc.pick_target(h1, 105.0, 'long', 1.0, ind_idx=4)
    assert (tgt, kind) == (None, 'no_level')


# --- end-to-end ---

def test_detect_signal_long():
    bars, L = build_long()
    sig = icc.detect_signal(bars, lookback=20)
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['htf_bias'] == 'long'
    # level = max high of the 20-bar indication window; L = max over the
    # whole pre-spike history, so they agree within the window's range
    assert abs(sig['indication_level'] - L) < 0.5
    assert sig['grab_ts'] > sig['indication_ts']
    assert sig['signal_ts'] > sig['grab_ts']
    assert sig['stop'] < sig['entry'] < sig['target']
    assert sig['stop'] == pytest.approx(sig['correction_extreme'])
    assert sig['rr'] == pytest.approx(2.0)  # no pre-indication liquidity -> 2R


def test_detect_signal_none_on_chop():
    m1, _ = expand([q(100, 101, 99, 100)] * 400, T0)
    assert icc.detect_signal(m1, lookback=20) is None


# --- scan integration ---

def _seed(db, c, bars):
    db.put(c, 'bar_window:' + SYM,
           [[b['ts'], b['o'], b['h'], b['l'], b['c'], b['v']] for b in bars])


def test_scan_signal_and_dedupe(db):
    with db.tx() as c:
        bars, _ = build_long()
        _seed(db, c, bars)
        now = bars[-1]['ts'] + 60
        res = icc.scan(db, c, _cfg(), now)
        assert res['ran'] is True and res['signals'] == 1
        row = db.get(c, 'icc:latest')['rows'][SYM]
        assert row['direction'] == 'long'
        sigs = db.recent(c, 'icc_signal', limit=5)
        assert len(sigs) == 1
        res2 = icc.scan(db, c, _cfg(), now + 400)
        assert res2['signals'] == 0
        assert res2['excluded'].get('already_signaled') == 1


def test_scan_disabled(db):
    with db.tx() as c:
        res = icc.scan(db, c, _cfg(ict_icc=False), 1_699_992_000)
        assert res == {'ran': False, 'reason': 'disabled'}


# --- paper gating ---

def _sig():
    return {'direction': 'long', 'entry': 108.0, 'stop': 104.0,
            'target': 116.0, 'signal_ts': 1_699_992_000, 'rr': 2.0}


def test_paper_dual_gate(db):
    with db.tx() as c:
        r = ict_paper.submit(db, c, _cfg(ict_icc=False), 1_699_992_000,
                             'icc', SYM, _sig())
        assert r == {'submitted': False, 'reason': 'paper_disabled'}
        r = ict_paper.submit(db, c, _cfg(), 1_699_992_000,
                             'icc', SYM, _sig())
        assert r['reason'] == 'paper_disabled'


def test_paper_submit_swing_track(db):
    with db.tx() as c:
        cfg = _cfg(ict_futures_paper=True)
        r = ict_paper.submit(db, c, cfg, 1_699_992_000, 'icc', SYM, _sig(),
                             track='swing', flatten_at=1_800_000_000)
        assert r['submitted'] is True
        trade = db.get(c, 'trade:' + r['trade_id'])
        assert trade['track'] == 'swing'  # exits() skips session flatten
        assert trade['flatten_at'] == 1_800_000_000
        assert trade['strategy'] == 'yt-icc'
