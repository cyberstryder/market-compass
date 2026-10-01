import pytest
from compass.store import Store
from compass.market import session, day
from compass import breakouts as bo

DAY = '2026-09-25'
SESS_OPEN, SESS_CLOSE = session(DAY)
NOW = SESS_CLOSE + 3600


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'brk.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class Cfg:
    breakouts = True
    stocks = ['BRK', 'TRI']


def B(o, h, l, c, v=1_000_000):
    return {'o': o, 'h': h, 'l': l, 'c': c, 'v': v}


def seed_daily(db, c, symbol, bars, start_ts=SESS_OPEN):
    n = len(bars)
    for i, b in enumerate(bars):
        db.append(c, 'daily', 'test', symbol, start_ts - (n - 1 - i) * 86400, dict(b))


# --- range breaks ---

def flat_range(n, lo=95.0, hi=100.0, v=1_000_000):
    return [B(97, hi, lo, 97, v) for _ in range(n)]


def test_range_break_up_confirmed():
    bars = [dict(b, ts=float(i)) for i, b in enumerate(flat_range(20))]
    bars.append(dict(B(100, 102, 99, 101, 2_000_000), ts=20.0))
    brk = bo.detect_range_break(bars, 20)
    assert brk['direction'] == 'up' and brk['confirmation'] == 'confirmed'
    assert brk['volume_ratio'] == 2.0 and brk['level'] == 100.0


def test_range_break_close_only_and_down():
    bars = [dict(b, ts=float(i)) for i, b in enumerate(flat_range(20))]
    # Wick through 100 but close back inside -> no break (close-only).
    bars.append(dict(B(97, 102, 96, 97, 2_000_000), ts=20.0))
    assert bo.detect_range_break(bars, 20) is None
    bars[-1] = dict(B(96, 97, 93, 94, 500_000), ts=20.0)
    brk = bo.detect_range_break(bars, 20)
    assert brk['direction'] == 'down' and brk['confirmation'] == 'unconfirmed_break'


def test_range_break_flavors_need_data():
    bars = [dict(b, ts=float(i)) for i, b in enumerate(flat_range(20))]
    assert bo.detect_range_break(bars, 50) is None  # not enough history


# --- triangles ---

def coil(sym=True, asc=False, desc=False):
    """15 bars: contracting ranges with the requested structure."""
    bars = []
    for i in range(15):
        block = i // 5
        hi = 110 - block * 2
        lo = 90 + block * 2
        if asc:
            hi = 110  # flat highs
        if desc:
            lo = 90  # flat lows
        bars.append(dict(B(100, hi, lo, 100), ts=float(i)))
    return bars


def test_triangle_symmetrical():
    found = bo.detect_triangle(coil())
    assert found and found[0] == 'triangle_sym'


def test_triangle_ascending_descending():
    assert bo.detect_triangle(coil(asc=True))[0] == 'triangle_asc'
    assert bo.detect_triangle(coil(desc=True))[0] == 'triangle_desc'


def test_triangle_rejects_non_contracting():
    bars = [dict(B(100, 110, 90, 100), ts=float(i)) for i in range(15)]
    assert bo.detect_triangle(bars) is None
    assert bo.detect_triangle(coil()[:10]) is None


def test_triangle_state_firing_and_forming():
    flavor, upper, lower = bo.detect_triangle(coil())
    fire = dict(B(100, 108, 104, 107), ts=15.0)
    state, direction = bo.triangle_state((flavor, upper, lower), fire)
    assert (state, direction) == ('firing', 'up')
    inside = dict(B(100, 103, 101, 102), ts=15.0)
    assert bo.triangle_state((flavor, upper, lower), inside)[0] == 'forming'


def test_forward_marks_and_excursion():
    bars = [dict(B(100 + i, 100 + i + 3, 100 + i - 1, 100 + i), ts=float(i)) for i in range(25)]
    marks = bo.forward_marks(bars, 0, 'up')
    assert marks['d5'] == pytest.approx(0.05) and marks['d20'] == pytest.approx(0.20)
    assert marks['mfe'] >= marks['d20'] and marks['mae'] <= 0


# --- scan end to end ---

def test_scan_range_break_event(db):
    with db.tx() as c:
        bars = flat_range(24) + [B(100, 102, 99, 101, 2_000_000)]
        seed_daily(db, c, 'BRK', bars)
        result = bo.scan(db, c, Cfg(), NOW)
    assert result['ran'] is True and result['new_events'] >= 1
    with db.tx() as c:
        eid = 'brk:BRK:%s:range20:up' % DAY
        event = db.get(c, 'breakout:' + eid)
        assert event['confirmation'] == 'confirmed' and event['status'] == 'fresh'
        # Idempotent re-run.
        db.put(c, 'breakouts:scanned_day', None)
        assert bo.scan(db, c, Cfg(), NOW + 10)['new_events'] == 0


def test_scan_triangle_fires_then_fails(db):
    with db.tx() as c:
        bars = coil()  # 15 coil bars, last ts = SESS_OPEN
        fire = B(100, 108, 104, 107, 3_000_000)
        seed_daily(db, c, 'TRI', bars + [fire])
        result = bo.scan(db, c, Cfg(), NOW)
    assert result['new_events'] == 1
    with db.tx() as c:
        eid = 'brk:TRI:%s:triangle_sym:up' % DAY
        event = db.get(c, 'breakout:' + eid)
        assert event['status'] == 'firing'
        # Next session (Monday 2026-09-28): close back inside the frozen
        # triangle -> failed.
        db.append(c, 'daily', 'test', 'TRI', SESS_OPEN + 3 * 86400,
                  B(100, 104, 102, 103))
        db.put(c, 'breakouts:scanned_day', None)
        bo.scan(db, c, Cfg(), NOW + 3 * 86400)
        event = db.get(c, 'breakout:' + eid)
        assert event['status'] == 'failed_triangle'


def test_scan_skips_before_close(db):
    with db.tx() as c:
        assert bo.scan(db, c, Cfg(), SESS_CLOSE - 3600)['reason'] == 'not_after_close'


def test_display_board(db):
    with db.tx() as c:
        db.put(c, 'triangle_state:AAA',
               {'symbol': 'AAA', 'day': DAY, 'flavor': 'triangle_sym',
                'state': 'forming', 'evaluated_at': NOW})
        db.put(c, 'breakout:e1',
               {'id': 'e1', 'symbol': 'AAA', 'day': DAY, 'pattern': 'range20',
                'direction': 'up', 'confirmation': 'confirmed', 'status': 'fresh',
                'marks': {'d20': 0.05}})
        out = bo.display(db, c, NOW)
    assert len(out['forming']) == 1 and len(out['fresh']) == 1
    assert out['weekly_outcomes'][0]['mean_d20'] == pytest.approx(0.05)
    assert out['note'].startswith('Research evidence only')


def _trap_bars():
    # 20 flat sessions (95-100), then a break up to 101, then back inside to 97.
    bars = [dict(b, ts=float(i), day='2026-09-%02d' % (i + 1))
            for i, b in enumerate(flat_range(20))]
    bars.append(dict(B(100, 102, 99, 101, 2_000_000), ts=20.0, day='2026-09-21'))
    bars.append(dict(B(101, 101, 96, 97, 1_500_000), ts=21.0, day='2026-09-22'))
    return bars


def test_detect_trap_bull():
    bars = _trap_bars()
    trap = bo.detect_trap(bars)
    assert trap is not None
    assert trap['kind'] == 'bull_trap'
    assert trap['direction'] == 'down'  # fade direction
    assert trap['target'] == 95.0  # opposite range edge
    assert trap['break_day'] == '2026-09-21'
    assert trap['trap_day'] == '2026-09-22'


def test_detect_trap_bear():
    bars = [dict(b, ts=float(i), day='2026-09-%02d' % (i + 1))
            for i, b in enumerate(flat_range(20))]
    bars.append(dict(B(96, 97, 93, 94, 2_000_000), ts=20.0, day='2026-09-21'))
    bars.append(dict(B(94, 98, 93, 97, 1_500_000), ts=21.0, day='2026-09-22'))
    trap = bo.detect_trap(bars)
    assert trap is not None
    assert trap['kind'] == 'bear_trap'
    assert trap['direction'] == 'up'
    assert trap['target'] == 100.0


def test_detect_trap_no_false_positive():
    # Break up and holds above range -> no trap.
    bars = [dict(b, ts=float(i), day='2026-09-%02d' % (i + 1))
            for i, b in enumerate(flat_range(20))]
    bars.append(dict(B(100, 102, 99, 101, 2_000_000), ts=20.0, day='2026-09-21'))
    bars.append(dict(B(101, 103, 100, 102, 1_500_000), ts=21.0, day='2026-09-22'))
    bars.append(dict(B(102, 104, 101, 103, 1_500_000), ts=22.0, day='2026-09-23'))
    assert bo.detect_trap(bars) is None


def test_detect_trap_needs_data():
    bars = [dict(b, ts=float(i)) for i, b in enumerate(flat_range(10))]
    assert bo.detect_trap(bars) is None


def test_scan_creates_trap_event(db):
    bars = _trap_bars()
    with db.tx() as c:
        seed_daily(db, c, 'BRK', bars)
        out = bo.scan(db, c, Cfg(), NOW)
    assert out['new_events'] >= 1
    with db.tx() as c:
        events = [e for k, e in db.prefix(c, 'breakout:').items()
                  if isinstance(e, dict) and e.get('kind') in ('bull_trap', 'bear_trap')]
    assert len(events) == 1
    assert events[0]['direction'] == 'down'
    assert events[0]['target'] == 95.0
    assert events[0]['status'] == 'fresh'


def test_display_includes_traps(db):
    with db.tx() as c:
        db.put(c, 'breakout:t1',
               {'id': 't1', 'symbol': 'TRP', 'day': DAY, 'pattern': 'bull_trap',
                'kind': 'bull_trap', 'direction': 'down', 'target': 95.0,
                'break_day': DAY, 'status': 'fresh',
                'marks': {'d20': 0.03}})
        out = bo.display(db, c, NOW)
    assert len(out['traps']) == 1
    assert out['traps'][0]['target'] == 95.0
    # traps also feed the weekly outcome table
    assert any(r['pattern'] == 'bull_trap' for r in out['weekly_outcomes'])
