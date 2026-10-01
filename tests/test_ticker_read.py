"""Tests for the mechanical ticker read (compass/ticker_read.py).

Evidence-only factor assembly: no entries, no trade structures. Tests use
synthetic bar/magnet/flow data and verify graceful degradation on missing
inputs.
"""

import time

import pytest

from compass.store import Store
from compass import ticker_read as tr
from compass.market import day


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'tr.db'))
    store.initialize()
    yield store
    store.engine.dispose()


# A fixed "now": 2026-10-01 10:00:00 America/Chicago == 15:00 UTC.
NOW = 1_790_000_000.0  # placeholder, replaced below by a real session stamp


def _session_open_ny(y, m, d):
    """09:30 ET as epoch seconds for a date (Oct 2026: ET == UTC-4)."""
    import datetime
    dt = datetime.datetime(y, m, d, 9, 30,
                           tzinfo=datetime.timezone(datetime.timedelta(hours=-4)))
    return dt.timestamp()


SESSION_OPEN = _session_open_ny(2026, 10, 1)
NOW = SESSION_OPEN + 3600  # 10:30 ET, mid-session


def minute_bars(start, closes, volumes=None, spread=0.5):
    """Build [ts,o,h,l,c,v] rows, one per minute."""
    rows = []
    for i, c in enumerate(closes):
        v = (volumes or [1000] * len(closes))[i]
        rows.append([start + i * 60, c - 0.1, c + spread, c - spread, c, v])
    return rows


def seed_bars(db, symbol, rows):
    with db.tx() as c:
        db.put(c, 'bar_window:' + symbol, rows)


def seed_quote(db, symbol, price):
    with db.tx() as c:
        db.put(c, 'quote:' + symbol,
               {'ts': NOW, 'bid': price - 0.01, 'ask': price + 0.01,
                'source': 'test', 'symbol': symbol})


# ---------------------------------------------------------------- VWAP ---

def test_vwap_volume_weighted(db):
    rows = minute_bars(SESSION_OPEN, [100, 102, 101],
                       volumes=[1000, 3000, 1000])
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    # typical price == close here (spread symmetric): (100*1 + 102*3 + 101*1)/5
    assert out['structure']['vwap'] == pytest.approx(101.4, abs=0.01)


def test_vwap_none_without_volume(db):
    rows = [[SESSION_OPEN + i * 60, 100, 101, 99, 100, None] for i in range(5)]
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    assert out['structure']['vwap'] is None
    assert any(f['factor'] == 'price_vs_vwap' and f['direction'] == 'neutral'
               for f in out['factors'])


# --------------------------------------------------------------- swings ---

def _swing_closes():
    # Peaks at index 3 (108) and 7 (106); troughs at 5 (100) and 9 (99).
    return [104, 105, 107, 108, 106, 100, 103, 106, 104, 99, 101, 100, 102]


def test_swing_detection_finds_peaks_and_troughs(db):
    closes = _swing_closes()
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    rows = [[SESSION_OPEN + i * 60, closes[i] - 0.1, highs[i], lows[i],
             closes[i], 1000] for i in range(len(closes))]
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    sh = [s['price'] for s in out['structure']['swing_highs']]
    sl = [s['price'] for s in out['structure']['swing_lows']]
    assert 109 in sh  # 108 + 1
    assert 98 in sl   # 99 - 1


def test_trend_downtrend(db):
    # Sawtooth with descending peaks (108,104,100) and troughs (96,92,88).
    closes = [102, 105, 108, 106, 103, 96, 99, 104, 102, 99, 92,
              95, 100, 98, 95, 88, 91, 94, 92, 90, 89]
    rows = minute_bars(SESSION_OPEN, closes)
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    assert out['structure']['trend'] == 'downtrend'
    assert any(f['factor'] == 'trend_structure' and f['direction'] == 'bearish'
               for f in out['factors'])


def test_trend_uptrend(db):
    # Mirror image: ascending troughs (92,96,100) and peaks (104,108,112).
    closes = [200 - c for c in
              [102, 105, 108, 106, 103, 96, 99, 104, 102, 99, 92,
               95, 100, 98, 95, 88, 91, 94, 92, 90, 89]]
    rows = minute_bars(SESSION_OPEN, closes)
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    assert out['structure']['trend'] == 'uptrend'
    assert any(f['factor'] == 'trend_structure' and f['direction'] == 'bullish'
               for f in out['factors'])


def test_trend_insufficient_data(db):
    rows = minute_bars(SESSION_OPEN, [100, 101, 100])
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    assert out['structure']['trend'] == 'insufficient_data'


# ------------------------------------------------------------------ tape ---

def test_largest_volume_bar_flags_selling(db):
    rows = minute_bars(SESSION_OPEN, [105, 104, 103, 102, 101],
                       volumes=[1000, 1000, 9000, 1000, 1000])
    # Make the big bar a down bar: open above close.
    rows[2][1], rows[2][4] = 104.0, 102.0
    rows[2][2], rows[2][3] = 104.5, 101.5
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'MU', NOW)
    lvb = out['structure']['largest_volume_bar']
    assert lvb['volume'] == 9000
    assert lvb['direction'] == 'down'
    assert lvb['vs_avg'] == pytest.approx(3.5, abs=0.1)
    assert any(f['factor'] == 'volume_bar' and f['direction'] == 'bearish'
               for f in out['factors'])


# ------------------------------------------------------------------ apex ---

def _seed_apex(db, rows):
    with db.tx() as c:
        db.put(c, 'apex_magnet:latest',
               {'at': NOW, 'rows': {str(i): r for i, r in enumerate(rows)}})


def test_apex_nearest_support_resistance(db):
    seed_quote(db, 'MU', 1050.0)
    _seed_apex(db, [
        {'symbol': 'MU', 'magnet': 1060.0, 'score': 88, 'signal': 'tested_holding',
         'role': 'resistance', 'distance_pct': 0.0095},
        {'symbol': 'MU', 'magnet': 1040.0, 'score': 65, 'signal': 'approaching',
         'role': 'support', 'distance_pct': 0.0095},
    ])
    out = tr.read(db, 'MU', NOW)
    assert out['apex']['nearest_resistance']['magnet'] == 1060.0
    assert out['apex']['nearest_support']['magnet'] == 1040.0


def test_apex_break_of_resistance_is_bearish(db):
    seed_quote(db, 'MU', 1059.0)
    _seed_apex(db, [
        {'symbol': 'MU', 'magnet': 1060.0, 'score': 88, 'signal': 'broke_through',
         'role': 'resistance', 'distance_pct': 0.001},
    ])
    out = tr.read(db, 'MU', NOW)
    assert any(f['factor'] == 'apex_magnets' and f['direction'] == 'bearish'
               for f in out['factors'])


# ------------------------------------------------------------------ gamma ---

def test_gamma_positive_regime_is_bullish(db):
    seed_quote(db, 'MU', 1051.0)
    with db.tx() as c:
        db.put(c, 'gamma_flip:MU',
               {'symbol': 'MU', 'flip': 1014.56, 'status': 'ok'})
    out = tr.read(db, 'MU', NOW)
    assert out['gamma']['regime'] == 'positive'
    assert out['gamma']['distance_pct'] == pytest.approx(0.0359, abs=0.001)
    assert any(f['factor'] == 'gamma_regime' and f['direction'] == 'bullish'
               for f in out['factors'])


def test_gamma_negative_regime_is_bearish(db):
    seed_quote(db, 'MU', 1000.0)
    with db.tx() as c:
        db.put(c, 'gamma_flip:MU',
               {'symbol': 'MU', 'flip': 1014.56, 'status': 'ok'})
    out = tr.read(db, 'MU', NOW)
    assert out['gamma']['regime'] == 'negative'
    assert any(f['factor'] == 'gamma_regime' and f['direction'] == 'bearish'
               for f in out['factors'])


# ------------------------------------------------------------------ tape ---

def _seed_tape(db, trials):
    with db.tx() as c:
        for i, t in enumerate(trials):
            db.put(c, 'tape_confirm:t%d' % i, t)


def test_tape_short_dominance_is_bearish(db):
    _seed_tape(db, [
        {'symbol': 'MU', 'side': 'short', 'confirmed': True,
         'evaluated_at': NOW, 'call_premium': 100000, 'put_premium': 900000,
         'max_score': 90},
        {'symbol': 'MU', 'side': 'short', 'confirmed': True,
         'evaluated_at': NOW, 'call_premium': 50000, 'put_premium': 400000,
         'max_score': 85},
    ])
    out = tr.read(db, 'MU', NOW)
    assert out['tape']['short_trials'] == 2
    assert out['tape']['long_trials'] == 0
    assert any(f['factor'] == 'tape_flow' and f['direction'] == 'bearish'
               for f in out['factors'])


# ------------------------------------------------------------------- gap ---

def _seed_gap(db, state):
    with db.tx() as c:
        db.put(c, 'gap_cont:MU', state)


def test_gap_qualified_up_is_bullish(db):
    _seed_gap(db, {'symbol': 'MU', 'day': day(NOW), 'gap_pct': 0.0195,
                   'gap_direction': 'up', 'qualified': True,
                   'break_direction': 'up', 'stage': 'holding'})
    out = tr.read(db, 'MU', NOW)
    assert out['gap']['on_board'] is True
    assert any(f['factor'] == 'gap' and f['direction'] == 'bullish'
               for f in out['factors'])


# ------------------------------------------------------------------- lean ---

def test_lean_scoring_labels():
    assert tr._lean([{'direction': 'bullish'}] * 3)['label'] == 'strongly_bullish'
    assert tr._lean([{'direction': 'bullish'}])['label'] == 'leaning_bullish'
    assert tr._lean([{'direction': 'neutral'}])['label'] == 'mixed'
    assert tr._lean([{'direction': 'bearish'}] * 2)['label'] == 'leaning_bearish'
    assert tr._lean([{'direction': 'bearish'}] * 4)['label'] == 'strongly_bearish'
    assert tr._lean([])['score'] == 0


def test_full_read_bearish_alignment(db):
    """A downtrend day below VWAP with negative gamma leans bearish."""
    closes = [1060, 1058, 1055, 1052, 1050, 1048, 1046, 1044, 1042, 1040]
    rows = minute_bars(SESSION_OPEN, closes)
    seed_bars(db, 'MU', rows)
    seed_quote(db, 'MU', 1040.0)
    with db.tx() as c:
        db.put(c, 'gamma_flip:MU', {'symbol': 'MU', 'flip': 1050.0})
    out = tr.read(db, 'MU', NOW)
    assert out['spot'] == 1040.0
    assert out['session']['bars'] == 10
    assert out['lean']['score'] < 0
    assert out['lean']['label'] in ('leaning_bearish', 'strongly_bearish')
    # No trade-adjacent content: the disclaimer names what is excluded, and
    # no factor note may contain trade-action language.
    assert 'no entries' in out['note']
    for fac in out['factors']:
        note = fac['note'].lower()
        for word in ('you should', 'consider buying', 'consider selling',
                     'enter at', 'place a stop', 'position size'):
            assert word not in note, fac


# ------------------------------------------------------- missing data ---

def test_missing_everything_degrades_gracefully(db):
    out = tr.read(db, 'ZZZZ', NOW)
    assert out['symbol'] == 'ZZZZ'
    assert out['spot'] is None
    assert out['session']['bars'] == 0
    assert out['structure']['trend'] == 'insufficient_data'
    assert out['apex']['has_data'] is False
    assert out['gamma']['has_data'] is False
    assert out['tape']['has_data'] is False
    assert out['gap']['on_board'] is False
    assert out['lean'] == {'score': 0, 'label': 'mixed'}
    # every factor present but neutral
    assert len(out['factors']) == 7
    assert all(f['direction'] == 'neutral' for f in out['factors'])


def test_empty_symbol_rejected(db):
    out = tr.read(db, '  ', NOW)
    assert out['error'] == 'symbol required'


def test_symbol_normalized_to_upper(db):
    rows = minute_bars(SESSION_OPEN, [100, 101])
    seed_bars(db, 'MU', rows)
    out = tr.read(db, 'mu', NOW)
    assert out['symbol'] == 'MU'
    assert out['session']['bars'] == 2
