"""Tests for compass/gamma_levels.py (MPhinance-style dealer levels)."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.gamma_levels import compute_levels, describe, MIN_STRIKES


def _strikes():
    # Signed net GEX: positive high (call wall), negative low (put wall),
    # mixed signs so the flip exists.
    return [
        {"strike": 740, "gex": -50.0},
        {"strike": 742, "gex": -120.0},   # put wall (most negative)
        {"strike": 745, "gex": -30.0},
        {"strike": 746, "gex": 40.0},
        {"strike": 748, "gex": 90.0},
        {"strike": 750, "gex": 150.0},    # concentration + call wall (max positive)
        {"strike": 751, "gex": 110.0},
    ]


def test_levels_ok():
    out = compute_levels(_strikes(), spot=747.0)
    assert out["status"] == "ok"
    levels = out["levels"]
    assert levels["call_wall"] == 750.0
    assert levels["put_wall"] == 742.0
    assert levels["concentration"] == 750.0
    assert levels["neutral_gamma"] is not None  # flip from mixed signs
    assert out["mixed_signs"] is True
    assert out["position"] == "between_walls"
    assert out["regime"] in {"above_flip", "below_flip", "at_flip"}


def test_position_above_call_wall():
    out = compute_levels(_strikes(), spot=755.0)
    assert out["position"] == "above_call_wall"


def test_position_below_put_wall():
    out = compute_levels(_strikes(), spot=738.0)
    assert out["position"] == "below_put_wall"


def test_insufficient_strikes():
    out = compute_levels([{"strike": 750, "gex": 10.0}])
    assert out["status"] == "insufficient"
    assert out["levels"] is None


def test_single_signed_no_walls_assigned():
    rows = [{"strike": 740 + i, "gex": 10.0 + i} for i in range(MIN_STRIKES + 1)]
    out = compute_levels(rows, spot=745.0)
    assert out["status"] == "ok"
    assert out["mixed_signs"] is False
    # All positive: call wall exists, no put wall, no flip-based neutral
    assert out["levels"]["call_wall"] is not None
    assert out["levels"]["put_wall"] is None
    assert out["levels"]["neutral_gamma"] is None
    assert "regime" not in out


def test_zero_gex_rows_ignored():
    rows = _strikes() + [{"strike": 760, "gex": 0}, {"strike": 761, "gex": None}]
    out = compute_levels(rows)
    assert out["strikes"] == len(_strikes())


def test_describe():
    out = compute_levels(_strikes(), spot=747.0)
    text = describe(out)
    assert "Call wall 750" in text
    assert "Put wall 742" in text
    assert "Neutral" in text
    assert describe({}) == "gamma levels unavailable"
    assert describe(None) == "gamma levels unavailable"


def test_evidence_only_flag():
    out = compute_levels(_strikes())
    assert out["evidence_only"] == "regime context; never an entry trigger"
