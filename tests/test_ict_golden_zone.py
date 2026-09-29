"""Golden Zone + VWAP detector: leg detection, zone math, VWAP confluence,
retrace signals, scan integration, and paper-trade gating."""
from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import golden_zone as gz
from compass import ict_paper

OPEN = 1_700_000_000
SYM = 'NQ.c.0'
NOW = OPEN + 16 * 60

# Bullish impulsive leg 98 -> 110, then a fade back into the zone.
RAW = [
    (100, 100.2, 99.8, 100), (100, 100.2, 99.8, 100),
    (100, 100.2, 99.8, 100), (100, 100.3, 99.9, 100.1),
    (100.1, 100.3, 99.9, 100.2), (100.2, 100.4, 98.0, 99.0),
    (99.0, 101.0, 98.9, 100.5), (100.5, 103.0, 100.4, 102.5),
    (102.5, 105.0, 102.4, 104.5), (104.5, 107.0, 104.4, 106.5),
    (106.5, 110.0, 106.4, 109.5), (109.5, 109.7, 107.0, 107.5),
    (107.5, 107.7, 105.0, 105.5), (105.5, 105.7, 102.5, 103.0),
    (103.0, 103.2, 102.0, 102.5), (102.5, 102.7, 101.5, 102.0),
]


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'gz.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def row(ts, o, h, l, c, v=1000):
    return [ts, o, h, l, c, v]


@pytest.fixture
def bars():
    return gz._bars_from_window([row(OPEN + i * 60, *r)
                                 for i, r in enumerate(RAW)])


def _cfg(**kw):
    base = {'ict_golden_zone': True, 'ict_symbols': (SYM,),
            'ict_gz_vwap_min_bars': 5}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c):
    db.put(c, 'bar_window:' + SYM,
           [row(OPEN + i * 60, *r) for i, r in enumerate(RAW)])


# --- leg detection ---

def test_detect_legs_finds_impulsive_leg(bars):
    legs = gz.detect_legs(bars)
    assert legs, 'expected at least one leg'
    leg = legs[-1]
    assert leg['direction'] == 'bullish'
    assert leg['origin'] == pytest.approx(98.0)
    assert leg['extreme'] == pytest.approx(110.0)
    assert leg['range'] == pytest.approx(12.0)


def test_no_leg_without_impulse(bars):
    flat = [dict(b, o=100.0, h=100.1, l=99.9, c=100.0) for b in bars]
    assert gz.detect_legs(flat) == []


# --- zone math ---

def test_golden_zone_math(bars):
    leg = gz.detect_legs(bars)[-1]
    zlo, zhi = gz.golden_zone(leg)
    assert zlo == pytest.approx(110.0 - 0.786 * 12.0)
    assert zhi == pytest.approx(110.0 - 0.618 * 12.0)


def test_vwap_confluence_ok_inside_and_outside(bars):
    from compass.ict_common import atr
    a = atr(bars, 14)
    zlo, zhi = gz.golden_zone(gz.detect_legs(bars)[-1])
    assert gz.vwap_confluence_ok((zlo, zhi), (zlo + zhi) / 2, a)
    assert not gz.vwap_confluence_ok((zlo, zhi), zhi + 10 * a, a)


# --- signal ---

def test_detect_signal_fades_leg_toward_origin(bars):
    from compass.ict_common import atr, session_vwap
    a = atr(bars, 14)
    leg = gz.detect_legs(bars)[-1]
    vwap = session_vwap(bars, NOW, min_bars=5)
    sig = gz.detect_signal(bars, leg, vwap, a)
    assert sig is not None
    assert sig['direction'] == 'short'  # fade the bullish leg
    assert sig['entry'] == pytest.approx(102.0)
    zlo, zhi = gz.golden_zone(leg)
    assert zlo <= sig['entry'] <= zhi
    assert sig['stop'] == pytest.approx(zhi + 0.25 * a)
    assert sig['stop'] > sig['entry']
    assert sig['target'] < sig['entry']
    assert sig['rr'] >= 1.5
    assert sig['rr'] == pytest.approx(sig['reward_pts'] / sig['risk_pts'])


def test_no_signal_without_retrace(bars):
    from compass.ict_common import atr, session_vwap
    a = atr(bars, 14)
    leg = gz.detect_legs(bars)[-1]
    vwap = session_vwap(bars, NOW, min_bars=5)
    clipped = [b for b in bars if b['ts'] <= OPEN + 12 * 60]  # before the fade
    assert gz.detect_signal(clipped, leg, vwap, a) is None


def test_no_signal_without_vwap_confluence(bars):
    from compass.ict_common import atr
    a = atr(bars, 14)
    leg = gz.detect_legs(bars)[-1]
    assert gz.detect_signal(bars, leg, 500.0, a) is None


# --- scan integration ---

def test_scan_disabled(db):
    with db.tx() as c:
        out = gz.scan(db, c, _cfg(ict_golden_zone=False), NOW)
    assert out == {'ran': False, 'reason': 'disabled'}


def test_scan_throttled(db):
    with db.tx() as c:
        _seed(db, c)
        assert gz.scan(db, c, _cfg(), NOW)['ran'] is True
        out = gz.scan(db, c, _cfg(), NOW)
    assert out == {'ran': False, 'reason': 'throttled'}


def test_scan_writes_snapshot_and_signal_without_paper(db):
    with db.tx() as c:
        _seed(db, c)
        out = gz.scan(db, c, _cfg(ict_gz_ema_cross=False), NOW)
        assert out['ran'] is True
        snap = db.get(c, 'ict_golden_zone:latest')
        assert snap['rows'][SYM]['direction'] == 'short'
        signals = db.recent(c, 'ict_golden_zone_signal', limit=10)
        assert len(signals) == 1
        assert signals[0]['payload']['entry'] == pytest.approx(102.0)
        # paper flag off: evidence only, no position
        assert db.get(c, 'position:ict:golden_zone:' + SYM) is None


def test_scan_submits_paper_when_both_flags_on(db):
    cfg = _cfg(ict_futures_paper=True, ict_gz_ema_cross=False)
    with db.tx() as c:
        _seed(db, c)
        gz.scan(db, c, cfg, NOW)
        pos = db.get(c, 'position:ict:golden_zone:' + SYM)
    assert pos is not None
    assert pos['strategy'] == 'ict-golden-zone'
    assert pos['side'] == 'short'
    assert pos['entry'] == pytest.approx(102.0)
    assert pos['stop'] > pos['entry'] > pos['target']


def test_scan_no_paper_when_detector_flag_off_but_paper_on(db):
    cfg = _cfg(ict_golden_zone=False, ict_futures_paper=True)
    with db.tx() as c:
        out = gz.scan(db, c, cfg, NOW)
    assert out['reason'] == 'disabled'
    with db.tx() as c:
        assert db.get(c, 'position:ict:golden_zone:' + SYM) is None


def test_display_note_mentions_paper(db):
    with db.tx() as c:
        _seed(db, c)
        gz.scan(db, c, _cfg(), NOW)
        payload = gz.display(db, c, NOW)
    assert 'ICT_FUTURES_PAPER_ENABLED' in payload['note']
    assert 'No live orders' in payload['note']


# --- EMA-cross confirmation ---

def _mkbars(closes):
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        bars.append({'ts': OPEN + i * 60, 'o': o, 'h': max(o, c) + 0.1,
                     'l': min(o, c) - 0.1, 'c': c, 'v': 100})
    return bars


def test_ema_cross_ok_bullish_within_lookback():
    bars = _mkbars([10.0] * 15 + [10.5, 11.0, 11.5, 12.0, 12.5])
    assert gz.ema_cross_ok(bars, 'long', fast=3, slow=5) is True
    assert gz.ema_cross_ok(bars, 'short', fast=3, slow=5) is False


def test_ema_cross_ok_bearish_within_lookback():
    bars = _mkbars([12.0] * 15 + [11.5, 11.0, 10.5, 10.0, 9.5])
    assert gz.ema_cross_ok(bars, 'short', fast=3, slow=5) is True
    assert gz.ema_cross_ok(bars, 'long', fast=3, slow=5) is False


def test_ema_cross_ok_rejects_stale_cross():
    closes = [10.0] * 5 + [10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14, 14.5, 15] \
        + [15.0] * 8  # cross happened ~13 bars before the end
    bars = _mkbars(closes)
    assert gz.ema_cross_ok(bars, 'long', fast=3, slow=5, lookback=5) is False
    assert gz.ema_cross_ok(bars, 'long', fast=3, slow=5,
                           lookback=20) is True


def test_ema_cross_ok_flat_and_short_series():
    assert gz.ema_cross_ok(_mkbars([10.0] * 20), 'long',
                           fast=3, slow=5) is False
    assert gz.ema_cross_ok(_mkbars([10, 11, 12, 13]), 'long',
                           fast=3, slow=5) is False  # < slow + 1 bars
    assert gz.ema_cross_ok(_mkbars([10.0] * 20), 'long',
                           fast=5, slow=5) is False  # fast >= slow


def _trend_reversal_bars():
    """40 bars: climb 100 -> 113.5, flat top, then a steep drop into the
    golden zone. The 3/5 bearish EMA cross lands inside the last 5 bars."""
    closes = [100.0] * 10
    closes += [100 + i * 0.9 for i in range(1, 16)]
    closes += [113.4] * 10
    closes += [111.5, 109.6, 107.7, 105.8, 103.9]
    return _mkbars(closes)


def test_detect_signal_ema_gate_passes_with_fresh_cross():
    from compass.ict_common import atr
    bars = _trend_reversal_bars()
    a = atr(bars, 14)
    leg = gz.detect_legs(bars)[-1]
    assert leg['direction'] == 'bullish'
    zlo, zhi = gz.golden_zone(leg)
    sig = gz.detect_signal(bars, leg, (zlo + zhi) / 2, a,
                           require_ema_cross=True,
                           ema_fast=3, ema_slow=5, ema_cross_lookback=5)
    assert sig is not None
    assert sig['direction'] == 'short'
    assert sig['ema_cross'] is True
    assert zlo <= sig['entry'] <= zhi


def test_detect_signal_ema_gate_blocks_without_cross(bars):
    from compass.ict_common import atr, session_vwap
    a = atr(bars, 14)
    leg = gz.detect_legs(bars)[-1]
    vwap = session_vwap(bars, NOW, min_bars=5)
    # 16 bars can't confirm a 9/21 cross: gated signal is suppressed...
    assert gz.detect_signal(bars, leg, vwap, a,
                            require_ema_cross=True) is None
    # ...while the ungated path keeps the pre-existing behavior.
    sig = gz.detect_signal(bars, leg, vwap, a, require_ema_cross=False)
    assert sig is not None
    assert sig['ema_cross'] is False


def test_scan_excludes_no_ema_cross_by_default(db):
    with db.tx() as c:
        _seed(db, c)
        out = gz.scan(db, c, _cfg(), NOW)
    assert out['ran'] is True
    assert out['signals'] == 0
    with db.tx() as c:
        snap = db.get(c, 'ict_golden_zone:latest')
        assert snap['rows'][SYM]['excluded'] == 'no_ema_cross'
        assert snap['excluded'].get('no_ema_cross') == 1
        assert db.recent(c, 'ict_golden_zone_signal', limit=10) == []
    assert snap['params']['ema_cross_required'] is True
    assert snap['params']['ema_fast'] == 9
    assert snap['params']['ema_slow'] == 21


def test_scan_ema_gate_kill_switch(db):
    cfg = _cfg(ict_gz_ema_cross=False)
    with db.tx() as c:
        _seed(db, c)
        out = gz.scan(db, c, cfg, NOW)
        assert out['signals'] == 1
        snap = db.get(c, 'ict_golden_zone:latest')
        assert snap['rows'][SYM]['direction'] == 'short'
        assert snap['params']['ema_cross_required'] is False
