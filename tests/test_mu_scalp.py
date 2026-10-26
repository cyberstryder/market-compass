"""Tests for MU scalp detectors (gap fade + spike fade).

Fixtures seed the REAL production keyspace: bars live in
bar_window:<SYM> as [ts, o, h, l, c, v, vw] rows (providers._bar_chunk).
The pre-fix detectors read db.prefix('bar:<SYM>:') — a keyspace no module
writes — and the old fixtures seeded those phantom rows by hand, which is
why the suite never caught the bug. The phantom-keyspace tests below pin
both directions: real keyspace fires, phantom-only does not.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from compass.store import Store
from compass import mu_scalp

ET = ZoneInfo('America/New_York')


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'muscalp.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def et_ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


def bar_row(ts, o, h, l, c, v=1000):
    """One bar_window row, exactly the shape providers writes."""
    return [ts, o, h, l, c, v, None]


def seed_prior_session(db, c, close=1000.0, symbol='MU'):
    """Prior session (Tue 2026-10-06) RTH bars in bar_window, last close
    `close` at 15:59 ET, plus an after-hours bar that must NOT be used as
    the prior close (validated rule: last bar inside [09:30, 16:00) ET)."""
    rows = [
        bar_row(et_ts(2026, 10, 6, 9, 30), close, close, close, close),
        bar_row(et_ts(2026, 10, 6, 12, 0), close, close, close, close),
        bar_row(et_ts(2026, 10, 6, 15, 59), close, close, close, close),
        # After-hours print at a different price: outside the session
        # window, so the gap must still be measured against `close`.
        bar_row(et_ts(2026, 10, 6, 19, 0), 1050.0, 1050.0, 1050.0, 1050.0),
    ]
    db.put(c, 'bar_window:' + symbol, rows)


def test_gap_fade_detects_large_gap(db):
    """Gap >2% vs prior session close (bar_window) triggers a fade."""
    now = et_ts(2026, 10, 7, 8, 45)
    with db.tx() as c:
        seed_prior_session(db, c, close=1000.0)
        db.put(c, 'quote:MU', {'bid': 1030, 'ask': 1031, 'ts': now})
        sig = mu_scalp.check_gap_fade(db, c, now)
    # 3% gap up → short the fade
    assert sig is not None
    assert sig['direction'] == 'short'
    assert sig['gap_pct'] == pytest.approx(3.0)


def test_gap_fade_detects_large_gap_down(db):
    """Gap <-2% triggers a long fade."""
    now = et_ts(2026, 10, 7, 8, 45)
    with db.tx() as c:
        seed_prior_session(db, c, close=1000.0)
        db.put(c, 'quote:MU', {'bid': 969, 'ask': 970, 'ts': now})
        sig = mu_scalp.check_gap_fade(db, c, now)
    assert sig is not None
    assert sig['direction'] == 'long'
    assert sig['gap_pct'] == pytest.approx(-3.1)


def test_gap_fade_ignores_small_gap(db):
    """Gap <2% does not trigger."""
    now = et_ts(2026, 10, 7, 8, 45)
    with db.tx() as c:
        seed_prior_session(db, c, close=1000.0)
        db.put(c, 'quote:MU', {'bid': 1010, 'ask': 1011, 'ts': now})
        sig = mu_scalp.check_gap_fade(db, c, now)
    assert sig is None  # 1% gap < 2% threshold


def test_gap_fade_no_prior_session_bars(db):
    """No bars in the prior session window → no signal (never fabricate)."""
    now = et_ts(2026, 10, 7, 8, 45)
    with db.tx() as c:
        # Only a premarket bar from today — no prior session data.
        db.put(c, 'bar_window:MU', [
            bar_row(et_ts(2026, 10, 7, 8, 30), 1030.0, 1030.0, 1030.0, 1030.0),
        ])
        db.put(c, 'quote:MU', {'bid': 1030, 'ask': 1031, 'ts': now})
        sig = mu_scalp.check_gap_fade(db, c, now)
    assert sig is None


def test_gap_fade_ignores_phantom_keyspace(db):
    """Regression pin: rows in the old phantom 'bar:MU:...' keyspace (and
    nothing in bar_window) must NOT produce a signal. The pre-fix code
    fired on exactly this seed."""
    now = et_ts(2026, 10, 7, 8, 45)
    with db.tx() as c:
        db.put(c, 'bar:MU:1d:2026-10-06', {
            'ts': et_ts(2026, 10, 6, 16, 0),
            'payload': {'o': 1000, 'h': 1010, 'l': 990, 'c': 1000},
        })
        db.put(c, 'quote:MU', {'bid': 1030, 'ask': 1031, 'ts': now})
        sig = mu_scalp.check_gap_fade(db, c, now)
    assert sig is None


def seed_spike_session(db, c, symbol='MU'):
    """Session bars for Wed 2026-10-07: flat 09:30-09:44 at 1000, then a
    5-bar 0.8% rally 09:45-09:49 (the spike under test)."""
    rows = []
    for minute in range(30, 45):
        rows.append(bar_row(et_ts(2026, 10, 7, 9, minute),
                            1000.0, 1000.5, 999.5, 1000.0))
    for i in range(5):
        price = 1000 * (1 + 0.008 * i / 4)
        rows.append(bar_row(et_ts(2026, 10, 7, 9, 45 + i),
                            price, price * 1.001, price * 0.999, price))
    db.put(c, 'bar_window:' + symbol, rows)


def test_spike_fade_detects_up_spike(db):
    """≥0.7% spike over 5 min on bar_window 1-min bars triggers a fade."""
    now = et_ts(2026, 10, 7, 9, 50)
    with db.tx() as c:
        seed_spike_session(db, c)
        sig = mu_scalp.check_spike_fade(db, c, now)
    assert sig is not None
    assert sig['direction'] == 'short'  # fade the up-spike
    assert sig['spike_pct'] == pytest.approx(0.8, abs=0.1)


def test_spike_fade_ignores_phantom_keyspace(db):
    """Regression pin: the same spike seeded ONLY as old-style phantom
    'bar:MU:1m:...' rows must NOT fire (the pre-fix code fired on it)."""
    now = et_ts(2026, 10, 7, 9, 50)
    with db.tx() as c:
        for i in range(5):
            price = 1000 * (1 + 0.008 * i / 4)
            ts = et_ts(2026, 10, 7, 9, 45 + i)
            db.put(c, 'bar:MU:1m:%d' % int(ts), {
                'ts': ts,
                'payload': {'o': price, 'h': price * 1.001,
                            'l': price * 0.999, 'c': price},
            })
        sig = mu_scalp.check_spike_fade(db, c, now)
    assert sig is None


def test_publish_alert_creates_0dte_category(db):
    """Published alerts have options_0dte category for paper tracking."""
    sig = {
        'detector': 'mu_gap_fade', 'symbol': 'MU', 'direction': 'short',
        'entry': 1030, 'stop': 1040, 'target': 1000,
        'gap_pct': 3.0, 'signal': 'gap_fade', 'ts': 2000,
    }
    with db.tx() as c:
        key = mu_scalp.publish_alert(db, c, sig, 2000)
        assert key is not None
        alert = db.get(c, 'alert:' + key)
        assert alert['publication']['category'] == 'options_0dte'
