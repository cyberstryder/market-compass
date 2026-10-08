"""Tests for MU scalp detectors (gap fade + spike fade)."""
import pytest
from compass.store import Store
from compass import mu_scalp


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'muscalp.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def test_gap_fade_detects_large_gap(db):
    """Gap >2% triggers a fade signal."""
    with db.tx() as c:
        # Mock prior close via bar, premarket via quote
        db.put(c, 'bar:MU:1d:2026-10-07', {
            'ts': 1000, 'payload': {'o': 1000, 'h': 1010, 'l': 990, 'c': 1000}
        })
        db.put(c, 'quote:MU', {'bid': 1030, 'ask': 1031, 'ts': 2000})
        sig = mu_scalp.check_gap_fade(db, c, 2000)
    # 3% gap up → short the fade
    assert sig is not None
    assert sig['direction'] == 'short'
    assert sig['gap_pct'] == pytest.approx(3.0)


def test_gap_fade_ignores_small_gap(db):
    """Gap <2% does not trigger."""
    with db.tx() as c:
        db.put(c, 'bar:MU:1d:2026-10-07', {
            'ts': 1000, 'payload': {'o': 1000, 'h': 1010, 'l': 990, 'c': 1000}
        })
        db.put(c, 'quote:MU', {'bid': 1010, 'ask': 1011, 'ts': 2000})
        sig = mu_scalp.check_gap_fade(db, c, 2000)
    assert sig is None  # 1% gap < 2% threshold


def test_spike_fade_detects_up_spike(db):
    """≥0.7% spike over 5 min triggers a fade signal."""
    with db.tx() as c:
        base_ts = 10000
        # 5 bars, 0.8% rally
        for i in range(5):
            price = 1000 * (1 + 0.008 * i / 4)
            db.put(c, 'bar:MU:1m:%d' % (base_ts + i * 60), {
                'ts': base_ts + i * 60,
                'payload': {'o': price, 'h': price * 1.001, 'l': price * 0.999, 'c': price}
            })
        sig = mu_scalp.check_spike_fade(db, c, base_ts + 5 * 60)
    assert sig is not None
    assert sig['direction'] == 'short'  # fade the up-spike
    assert sig['spike_pct'] == pytest.approx(0.8, abs=0.1)


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
