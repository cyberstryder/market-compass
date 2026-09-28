"""BOS + Golden Zone + VWAP detector: displacement leg, zone math, VWAP
overlap gating, retrace signals, scan integration, and paper-trade gating."""
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import bos_gz_vwap as bgv
from compass.ict_common import atr

OPEN = 1_700_000_000
SYM = 'NQ.c.0'
NOW = OPEN + 15 * 60

# Same BOS tape as the FVG fixture: swing high 102 broken at idx11,
# leg origin (swing low) 99.0 at idx8, extreme 105.0; idx14 closes
# back inside the leg's golden zone.
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
    s = Store('sqlite:///' + str(tmp_path / 'bgv.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def row(ts, o, h, l, c, v=1000):
    return [ts, o, h, l, c, v]


@pytest.fixture
def bars():
    return bgv._bars_from_window([row(OPEN + i * 60, *r)
                                  for i, r in enumerate(RAW)])


@pytest.fixture
def atr_v(bars):
    return atr(bars, 14)


@pytest.fixture
def bos(bars):
    return [e for e in bgv.detect_bos(bars, n=2)
            if e['direction'] == 'long'][-1]


def _cfg(**kw):
    base = {'ict_bos_gz_vwap': True, 'ict_symbols': (SYM,),
            'ict_bgv_vwap_min_bars': 5}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c):
    db.put(c, 'bar_window:' + SYM,
           [row(OPEN + i * 60, *r) for i, r in enumerate(RAW)])


# --- leg + zone ---

def test_displacement_leg_runs_origin_to_extreme(bars, bos):
    leg = bgv.displacement_leg(bars, bos)
    assert leg['direction'] == 'long'
    assert leg['origin'] == pytest.approx(99.0)
    assert leg['extreme'] == pytest.approx(105.0)
    assert leg['range'] == pytest.approx(6.0)


def test_golden_zone_math(bars, bos):
    leg = bgv.displacement_leg(bars, bos)
    zlo, zhi = bgv.golden_zone(leg)
    assert zlo == pytest.approx(105.0 - 0.786 * 6.0)
    assert zhi == pytest.approx(105.0 - 0.618 * 6.0)


# --- signal ---

def test_signal_with_vwap_overlap(bars, atr_v, bos):
    sig = bgv.detect_signal(bars, bos, 100.8, atr_v)
    assert sig is not None
    assert sig['direction'] == 'long'  # BOS direction: continuation
    assert sig['entry'] == pytest.approx(101.0)
    zlo, zhi = bgv.golden_zone(bgv.displacement_leg(bars, bos))
    assert zlo <= sig['entry'] <= zhi
    assert sig['stop'] == pytest.approx(zlo - 0.25 * atr_v)
    assert sig['stop'] < sig['entry'] < sig['target']
    assert sig['rr'] >= 1.5


def test_no_signal_without_vwap_overlap(bars, atr_v, bos):
    assert bgv.detect_signal(bars, bos, 120.0, atr_v) is None


def test_no_signal_without_retrace(bars, atr_v, bos):
    clipped = [b for b in bars if b['ts'] <= OPEN + 13 * 60]
    assert bgv.detect_signal(clipped, bos, 100.8, atr(clipped, 14)) is None


# --- scan integration ---

def test_scan_disabled(db):
    with db.tx() as c:
        assert bgv.scan(db, c, _cfg(ict_bos_gz_vwap=False), NOW) == \
            {'ran': False, 'reason': 'disabled'}


def test_scan_reports_no_bos(db):
    with db.tx() as c:
        flat = [row(OPEN + i * 60, 100, 100.1, 99.9, 100) for i in range(20)]
        db.put(c, 'bar_window:' + SYM, flat)
        out = bgv.scan(db, c, _cfg(), NOW)
    assert out['ran'] is True
    assert out['excluded'].get('no_bos') == 1


def test_scan_writes_snapshot_and_signal_without_paper(db):
    with db.tx() as c:
        _seed(db, c)
        out = bgv.scan(db, c, _cfg(), NOW)
        assert out['ran'] is True
        snap = db.get(c, 'ict_bos_gz_vwap:latest')
        assert snap['rows'][SYM]['direction'] == 'long'
        signals = db.recent(c, 'ict_bos_gz_vwap_signal', limit=10)
        assert len(signals) == 1
        assert db.get(c, 'position:ict:bos_gz_vwap:' + SYM) is None


def test_scan_submits_paper_when_both_flags_on(db):
    with db.tx() as c:
        _seed(db, c)
        bgv.scan(db, c, _cfg(ict_futures_paper=True), NOW)
        pos = db.get(c, 'position:ict:bos_gz_vwap:' + SYM)
    assert pos is not None
    assert pos['strategy'] == 'ict-bos-gz-vwap'
    assert pos['side'] == 'long'
    assert pos['entry'] == pytest.approx(101.0)
