from types import SimpleNamespace
import pytest
from compass.store import Store
from compass import smt_divergence as smt

OPEN = 1_700_000_000  # arbitrary fixed ts
A, B = 'NQ.c.0', 'ES.c.0'
PAIR = A + ':' + B


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'smt.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def row(ts, o, h, l, c):
    return [ts, o, h, l, c, 1000]


def bars(rows):
    return smt._bars_from_window(rows)


def a_takes_high(start=OPEN):
    """NQ takes its prior high 100 (wick to 100.6 at i3)."""
    return bars([
        row(start + 0 * 60, 99.0, 99.2, 98.9, 99.1),
        row(start + 1 * 60, 99.1, 99.4, 99.0, 99.3),
        row(start + 2 * 60, 99.3, 99.6, 99.2, 99.5),
        row(start + 3 * 60, 99.5, 100.6, 99.4, 100.4),   # takeout
        row(start + 4 * 60, 100.4, 100.1, 100.0, 100.2),
    ])


def b_fails_high_with_reversal(start=OPEN):
    """ES never takes 200; bearish reversal bar at i3 (ts >= takeout ts)."""
    return bars([
        row(start + 0 * 60, 198.0, 198.2, 197.8, 198.1),
        row(start + 1 * 60, 198.1, 198.5, 198.0, 198.4),
        row(start + 2 * 60, 199.5, 199.8, 198.9, 199.0),  # bearish but too early
        row(start + 3 * 60, 199.0, 199.2, 198.7, 198.8),  # reversal
    ])


PRIOR_A = {'high': 100.0, 'low': 95.0}
PRIOR_B = {'high': 200.0, 'low': 195.0}


def liq_levels():
    return [
        {'symbol': A, 'session': 'asia', 'high': 100.0, 'low': 95.0,
         'high_ts': OPEN, 'low_ts': OPEN, 'complete': True},
        {'symbol': B, 'session': 'asia', 'high': 200.0, 'low': 195.0,
         'high_ts': OPEN, 'low_ts': OPEN, 'complete': True},
    ]


def _cfg(**kw):
    base = {'ict_smt_divergence': True, 'ict_smt_pairs': (PAIR,)}
    base.update(kw)
    return SimpleNamespace(**base)


def _seed(db, c, bars_a, bars_b, levels=True):
    db.put(c, 'bar_window:' + A, [[b['ts'], b['o'], b['h'], b['l'], b['c'], 1000]
                                  for b in bars_a] if bars_a else [])
    db.put(c, 'bar_window:' + B, [[b['ts'], b['o'], b['h'], b['l'], b['c'], 1000]
                                  for b in bars_b] if bars_b else [])
    if levels:
        db.put(c, 'ict_session_liquidity:latest',
               {'at': OPEN, 'levels': liq_levels()})


# --- detect_divergence ---

def test_bearish_divergence_a_takes_b_fails():
    events = smt.detect_divergence(a_takes_high(), b_fails_high_with_reversal(),
                                   PRIOR_A, PRIOR_B, label_a=A, label_b=B)
    assert len(events) == 1
    ev = events[0]
    assert ev['direction'] == 'short'
    assert ev['confirming_leg'] == A
    assert ev['divergent_leg'] == B
    assert ev['level_kind'] == 'prior_session_high'
    assert ev['signal_ts'] == OPEN + 3 * 60
    assert ev['entry'] == pytest.approx(198.8)
    assert ev['stop'] == pytest.approx(199.2 + 0.25)
    assert ev['target'] == pytest.approx(195.0)  # divergent leg's prior low
    assert ev['rr'] > 0


def test_both_legs_take_no_divergence():
    b_takes = bars([
        row(OPEN + 0 * 60, 198.0, 198.2, 197.8, 198.1),
        row(OPEN + 1 * 60, 198.1, 198.5, 198.0, 198.4),
        row(OPEN + 2 * 60, 199.5, 200.6, 198.9, 200.2),  # takes 200 too
        row(OPEN + 3 * 60, 200.2, 200.4, 199.8, 200.0),
    ])
    events = smt.detect_divergence(a_takes_high(), b_takes,
                                   PRIOR_A, PRIOR_B, label_a=A, label_b=B)
    assert events == []


def test_neither_takes_no_divergence():
    a_flat = bars([row(OPEN + i * 60, 99.0, 99.4, 98.8, 99.2) for i in range(5)])
    b_flat = bars([row(OPEN + i * 60, 198.0, 198.6, 197.8, 198.2) for i in range(5)])
    assert smt.detect_divergence(a_flat, b_flat, PRIOR_A, PRIOR_B) == []


def test_no_reversal_bar_no_divergence():
    b_no_rev = bars([
        row(OPEN + 0 * 60, 198.0, 198.2, 197.8, 198.1),
        row(OPEN + 1 * 60, 198.1, 198.5, 198.0, 198.4),
        row(OPEN + 2 * 60, 199.0, 199.8, 198.9, 199.5),  # bullish
        row(OPEN + 3 * 60, 199.5, 199.7, 199.2, 199.6),  # bullish
    ])
    events = smt.detect_divergence(a_takes_high(), b_no_rev,
                                   PRIOR_A, PRIOR_B, label_a=A, label_b=B)
    assert events == []


def test_bullish_mirror_a_takes_low():
    bars_a = bars([
        row(OPEN + 0 * 60, 96.0, 96.2, 95.8, 95.9),
        row(OPEN + 1 * 60, 95.9, 96.0, 95.5, 95.6),
        row(OPEN + 2 * 60, 95.6, 95.7, 94.5, 95.0),   # takes prior low 95
        row(OPEN + 3 * 60, 95.0, 95.3, 94.8, 95.2),
    ])
    bars_b = bars([
        row(OPEN + 0 * 60, 196.0, 196.2, 195.8, 195.9),
        row(OPEN + 1 * 60, 195.9, 196.1, 195.6, 195.7),
        row(OPEN + 2 * 60, 195.7, 196.0, 195.5, 195.9),  # bullish
        row(OPEN + 3 * 60, 195.9, 196.2, 195.8, 196.1),  # bullish reversal
    ])
    events = smt.detect_divergence(bars_a, bars_b, PRIOR_A, PRIOR_B,
                                   label_a=A, label_b=B)
    assert len(events) == 1
    ev = events[0]
    assert ev['direction'] == 'long'
    assert ev['divergent_leg'] == B
    assert ev['level_kind'] == 'prior_session_low'
    assert ev['entry'] == pytest.approx(196.1)
    assert ev['stop'] == pytest.approx(195.8 - 0.25)
    assert ev['target'] == pytest.approx(200.0)  # divergent leg's prior high


def test_symmetric_b_takes_a_fails():
    bars_a = bars([
        row(OPEN + 0 * 60, 98.0, 98.3, 97.8, 98.1),
        row(OPEN + 1 * 60, 98.1, 98.6, 98.0, 98.5),
        row(OPEN + 2 * 60, 98.5, 98.8, 98.3, 98.6),
        row(OPEN + 3 * 60, 99.2, 99.5, 98.9, 99.0),   # bearish reversal
    ])
    bars_b = bars([
        row(OPEN + 0 * 60, 198.0, 198.3, 197.8, 198.2),
        row(OPEN + 1 * 60, 198.2, 198.6, 198.1, 198.5),
        row(OPEN + 2 * 60, 198.5, 200.6, 198.4, 200.2),  # takes 200
        row(OPEN + 3 * 60, 200.2, 200.4, 199.8, 200.0),
    ])
    events = smt.detect_divergence(bars_a, bars_b, PRIOR_A, PRIOR_B,
                                   label_a=A, label_b=B)
    assert len(events) == 1
    assert events[0]['direction'] == 'short'
    assert events[0]['divergent_leg'] == A
    assert events[0]['confirming_leg'] == B


def test_stale_takeout_no_divergence():
    bars_a = ([{'ts': OPEN, 'o': 99.0, 'h': 100.6, 'l': 98.9, 'c': 99.5}] +
              [{'ts': OPEN + i * 60, 'o': 99.3, 'h': 99.4, 'l': 99.2, 'c': 99.3}
               for i in range(1, 25)])
    bars_b = ([{'ts': OPEN + i * 60, 'o': 198.5, 'h': 198.8, 'l': 198.2, 'c': 198.6}
               for i in range(24)] +
              [{'ts': OPEN + 24 * 60, 'o': 199.0, 'h': 199.2, 'l': 198.7,
                'c': 198.8}])  # bearish reversal, but takeout is 24 bars old
    events = smt.detect_divergence(bars_a, bars_b, PRIOR_A, PRIOR_B,
                                   label_a=A, label_b=B)
    assert events == []


def test_empty_bars_no_divergence():
    assert smt.detect_divergence([], b_fails_high_with_reversal(),
                                 PRIOR_A, PRIOR_B) == []
    assert smt.detect_divergence(a_takes_high(), [], PRIOR_A, PRIOR_B) == []


# --- scan integration ---

def test_scan_emits_divergence_and_snapshot(db):
    with db.tx() as c:
        _seed(db, c, a_takes_high(), b_fails_high_with_reversal())
        result = smt.scan(db, c, _cfg(), OPEN + 3600)
    assert result['ran'] is True
    assert result['signals'] == 1
    with db.tx() as c:
        latest = db.get(c, 'ict_smt_divergence:latest')
        row_ = latest['rows'][PAIR]
        assert row_['concept'] == 'smt_divergence'
        assert row_['direction'] == 'short'
        assert row_['divergent_leg'] == B
        sigs = db.recent(c, 'ict_smt_signal', limit=10)
        assert len(sigs) == 1
        assert sigs[0]['payload']['pair'] == PAIR


def test_scan_idempotent_signal(db):
    with db.tx() as c:
        _seed(db, c, a_takes_high(), b_fails_high_with_reversal())
        smt.scan(db, c, _cfg(), OPEN)
        db.put(c, 'ict_smt_divergence:scanned_at', 0)
        result = smt.scan(db, c, _cfg(), OPEN + 200)
        assert result['signals'] == 0
        assert len(db.recent(c, 'ict_smt_signal', limit=10)) == 1


def test_scan_one_leg_missing_bars(db):
    with db.tx() as c:
        _seed(db, c, a_takes_high(), [])
        result = smt.scan(db, c, _cfg(), OPEN)
    assert result['ran'] is True
    assert result['signals'] == 0
    assert result['excluded'] == {'no_bars': 1}
    with db.tx() as c:
        assert db.get(c, 'ict_smt_divergence:latest')['rows'][PAIR]['excluded'] == 'no_bars'


def test_scan_missing_levels_graceful(db):
    with db.tx() as c:
        _seed(db, c, a_takes_high(), b_fails_high_with_reversal(), levels=False)
        result = smt.scan(db, c, _cfg(), OPEN)
    assert result['ran'] is True
    assert result['excluded'] == {'no_levels': 1}


def test_scan_no_divergence_excluded(db):
    a_flat = bars([row(OPEN + i * 60, 99.0, 99.4, 98.8, 99.2) for i in range(5)])
    b_flat = bars([row(OPEN + i * 60, 198.0, 198.6, 197.8, 198.2) for i in range(5)])
    with db.tx() as c:
        _seed(db, c, a_flat, b_flat)
        result = smt.scan(db, c, _cfg(), OPEN)
    assert result['ran'] is True
    assert result['excluded'] == {'no_divergence': 1}


def test_scan_bad_pair_and_disabled(db):
    with db.tx() as c:
        assert smt.scan(db, c, SimpleNamespace(), OPEN) == \
            {'ran': False, 'reason': 'disabled'}
        _seed(db, c, a_takes_high(), b_fails_high_with_reversal())
        result = smt.scan(db, c, _cfg(ict_smt_pairs=('NOPE',)), OPEN)
        assert result['ran'] is True
        assert result['excluded'] == {'bad_pair': 1}
        assert smt.scan(db, c, _cfg(), OPEN + 10) == \
            {'ran': False, 'reason': 'throttled'}


def test_display_payload(db):
    with db.tx() as c:
        _seed(db, c, a_takes_high(), b_fails_high_with_reversal())
        smt.scan(db, c, _cfg(), OPEN)
        out = smt.display(db, c, OPEN)
    assert out['rows'][0]['direction'] == 'short'
    assert out['rows'][0]['pair'] == PAIR
    assert len(out['signals']) == 1
    assert out['note'].startswith('Research evidence only')
