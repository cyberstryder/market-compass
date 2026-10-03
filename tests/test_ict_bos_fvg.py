"""BOS + FVG detector: displacement breaks, FVG geometry, mitigation,
retrace signals, scan integration, and paper-trade gating."""
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import bos_fvg as bf
from compass.ict_common import atr, detect_fvgs, fvg_mitigated

OPEN = 1_699_992_000
SYM = 'NQ.c.0'
NOW = OPEN + 15 * 60

# Swing high 102 (idx6), bullish FVG (101.2, 102.6) formed idx9,
# BOS long at idx11, retrace touch at idx13.
RAW = [
    (100, 100.2, 99.8, 100.0),
    (100, 100.3, 99.7, 100.0), (100, 100.3, 99.7, 100.1),
    (100.1, 100.4, 99.8, 100.2), (100.2, 100.4, 99.9, 100.0),
    (100.0, 101.0, 99.9, 100.5), (100.5, 102.0, 100.4, 101.0),
    (101.0, 101.2, 100.8, 101.0), (101.0, 101.1, 99.0, 99.5),
    (99.5, 103.0, 102.6, 102.8), (102.8, 103.2, 102.7, 103.0),
    (103.0, 105.0, 102.9, 104.8), (104.8, 105.0, 103.5, 104.0),
    (104.0, 104.2, 102.0, 102.8), (102.8, 103.0, 100.5, 101.0),
]


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'bf.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def row(ts, o, h, l, c, v=1000):
    return [ts, o, h, l, c, v]


@pytest.fixture
def bars():
    return bf._bars_from_window([row(OPEN + i * 60, *r)
                                 for i, r in enumerate(RAW)])


@pytest.fixture
def atr_v(bars):
    return atr(bars, 14)


@pytest.fixture
def bos(bars):
    return [e for e in bf.detect_bos(bars, n=2) if e['direction'] == 'long'][-1]


def _cfg(**kw):
    base = {'ict_bos_fvg': True, 'ict_symbols': (SYM,)}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c):
    db.put(c, 'bar_window:' + SYM,
           [row(OPEN + i * 60, *r) for i, r in enumerate(RAW)])


# --- BOS ---

def test_detect_bos_long_with_displacement(bars):
    evs = [e for e in bf.detect_bos(bars, n=2) if e['direction'] == 'long']
    assert evs, 'expected a bullish BOS'
    assert evs[-1]['swing_price'] == pytest.approx(102.0)


def test_no_bos_without_close_beyond_swing(bars):
    flat = [dict(b, o=100.0, h=100.1, l=99.9, c=100.0) for b in bars]
    assert bf.detect_bos(flat, n=2) == []


# --- FVG ---

def test_detect_fvgs_finds_bullish_gap(bars):
    fvgs = [f for f in detect_fvgs(bars) if f['direction'] == 'long']
    assert fvgs
    f = fvgs[0]
    assert f['bottom'] == pytest.approx(101.2)
    assert f['top'] == pytest.approx(102.6)


def test_fvg_mitigated_by_wick(bars):
    f = [f for f in detect_fvgs(bars) if f['direction'] == 'long'][0]
    assert fvg_mitigated(bars, f) is True          # idx13 wicked in
    assert fvg_mitigated(bars, f, up_to_idx=11) is False  # clean at BOS


# --- signal ---

def test_detect_signal_first_touch(bars, atr_v, bos):
    sig = bf.detect_signal(bars, bos, atr_v)
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['entry'] == pytest.approx(102.8)
    assert sig['stop'] == pytest.approx(101.1 - 0.25 * atr_v)  # fresher FVG wins
    assert sig['stop'] < sig['entry'] < sig['target']
    assert sig['rr'] >= 1.5


def test_no_signal_when_fvg_mitigated_before_bos(bars, atr_v, bos):
    # Wick the FVG out before the BOS bar: no unmitigated POI remains.
    touched = [dict(b) for b in bars]
    touched[10] = dict(touched[10], l=100.0)
    assert bf.detect_signal(touched, bos, atr(touched, 14)) is None


def test_no_signal_without_retrace(bars, atr_v, bos):
    clipped = [b for b in bars if b['ts'] <= OPEN + 12 * 60]
    assert bf.detect_signal(clipped, bos, atr(clipped, 14)) is None


# --- scan integration ---

def test_scan_disabled(db):
    with db.tx() as c:
        assert bf.scan(db, c, _cfg(ict_bos_fvg=False), NOW) == \
            {'ran': False, 'reason': 'disabled'}


def test_scan_throttled(db):
    with db.tx() as c:
        _seed(db, c)
        assert bf.scan(db, c, _cfg(), NOW)['ran'] is True
        assert bf.scan(db, c, _cfg(), NOW) == \
            {'ran': False, 'reason': 'throttled'}


def test_scan_writes_snapshot_and_signal_without_paper(db):
    with db.tx() as c:
        _seed(db, c)
        out = bf.scan(db, c, _cfg(), NOW)
        assert out['ran'] is True
        snap = db.get(c, 'ict_bos_fvg:latest')
        assert snap['rows'][SYM]['direction'] == 'long'
        signals = db.recent(c, 'ict_bos_fvg_signal', limit=10)
        assert len(signals) == 1
        assert db.get(c, 'position:ict:bos_fvg:' + SYM) is None


def test_scan_submits_paper_when_both_flags_on(db):
    with db.tx() as c:
        _seed(db, c)
        bf.scan(db, c, _cfg(ict_futures_paper=True), NOW)
        pos = db.get(c, 'position:ict:bos_fvg:' + SYM)
    assert pos is not None
    assert pos['strategy'] == 'ict-bos-fvg'
    assert pos['side'] == 'long'
    assert pos['entry'] == pytest.approx(102.8)
