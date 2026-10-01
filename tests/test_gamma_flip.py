import pytest
from compass.gamma_flip import compute_flip
from compass.swing_ideas import swings  # noqa: F401 -- registers schema before Store.initialize()


def strikes(pairs):
    return [{"strike": s, "gex": g} for s, g in pairs]


def test_flip_interpolated_crossing():
    out = compute_flip(strikes([(100, 10), (101, 10), (102, -30), (103, -5), (104, 2)]))
    assert out["status"] == "ok"
    # cum: 10, 20, -10 -> crossing between 101 and 102: 101 + (20/30)*1
    assert out["flip"] == pytest.approx(101.6667, abs=1e-3)
    assert out["mixed_signs"] is True


def test_flip_exact_strike_crossing():
    out = compute_flip(strikes([(100, 10), (101, -10), (102, -5), (103, 3), (104, -2)]))
    assert out["status"] == "ok"
    assert out["flip"] == pytest.approx(101.0)


def test_flip_single_signed_positive_reports_no_flip():
    out = compute_flip(strikes([(100, 5), (101, 8), (102, 3), (103, 12), (104, 1)]))
    assert out["flip"] is None
    assert out["status"] == "single-signed"
    assert out["mixed_signs"] is False


def test_flip_single_signed_negative_reports_no_flip():
    out = compute_flip(strikes([(100, -5), (101, -8), (102, -3), (103, -12), (104, -1)]))
    assert out["flip"] is None
    assert out["status"] == "single-signed"


def test_flip_mixed_without_crossing_reports_no_flip():
    out = compute_flip(strikes([(100, 5), (101, 4), (102, -1), (103, -1), (104, 3)]))
    assert out["flip"] is None
    assert out["status"] == "no-crossing"
    assert out["mixed_signs"] is True


def test_flip_insufficient_strikes():
    out = compute_flip(strikes([(100, 10), (101, -30), (102, 5)]))
    assert out["flip"] is None
    assert out["status"] == "insufficient"


def test_flip_regime_above_and_below():
    pairs = [(100, 10), (101, 10), (102, -30), (103, -5), (104, 2)]
    above = compute_flip(strikes(pairs), spot=103.0)
    assert above["regime"] == "above_flip"
    assert above["distance_pct"] == pytest.approx(1.311, abs=1e-2)
    below = compute_flip(strikes(pairs), spot=100.0)
    assert below["regime"] == "below_flip"
    assert below["distance_pct"] < 0


def test_flip_ignores_zero_and_none_gex():
    rows = strikes([(100, 10), (101, 10), (102, -30), (103, -5), (104, 2)])
    rows.append({"strike": 105, "gex": 0})
    rows.append({"strike": 106, "gex": None})
    rows.append({"strike": 99})  # no gex key at all
    out = compute_flip(rows)
    assert out["status"] == "ok"
    assert out["flip"] == pytest.approx(101.6667, abs=1e-3)
    assert out["strikes"] == 5


def test_flip_unsorted_input():
    rows = strikes([(104, 2), (100, 10), (102, -30), (103, -5), (101, 10)])
    out = compute_flip(rows)
    assert out["flip"] == pytest.approx(101.6667, abs=1e-3)


def test_futures_gex_symbols_join_matrix_rotation(tmp_path):
    from compass.config import Config
    from compass.store import Store
    from compass.vendor_schedule import matrix_target

    db = Store('sqlite:///' + str(tmp_path / 'gex.db'))
    db.initialize()
    try:
        cfg = Config(local=True, stocks=('SPY', 'QQQ', 'IWM'))
        now = 1_700_000_000.0
        seen = set()
        with db.tx() as c:
            for i in range(5):
                symbol = matrix_target(db, c, cfg, now + i * 61, i)
                assert symbol is not None
                seen.add(symbol)
                db.put(c, 'matrix_job:' + symbol, dict(attempted_at=now + i * 61))
        # NQ/ES are polled for futures GEX even though they are not watchlist names.
        assert {'NQ', 'ES'} <= seen
    finally:
        db.engine.dispose()
