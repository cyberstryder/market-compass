"""fresh() future-skew tolerance (2026-11-02).

Production evidence: Alpaca-sourced underlying quotes arrive stamped
~3s ahead of the engine clock (median +3s, tail +13s over the sampled
RTH hour). The old -1s future bound made those quotes fail as not
fresh at any stale bound, nullifying the 60s decision-time standard.
These tests pin the new semantics: bounded future skew passes, the
stale side is exactly as strict as before, corrupt far-future stamps
still fail, and structural validity is still demanded.
"""
from compass.market import QUOTE_FUTURE_SKEW_TOLERANCE_S, fresh

NOW = 1_000_000.0


def quote(ts_offset, **overrides):
    q = {"ts": NOW + ts_offset, "bid": 10.0, "ask": 10.05,
         "bid_size": 100, "ask_size": 100}
    q.update(overrides)
    return q


def test_quote_stamped_at_now_is_fresh():
    assert fresh(quote(0), NOW)


def test_small_future_skew_passes():
    # The production median (+3s) and observed tail samples pass at the
    # default 5s bound and at the 60s decision-time bound alike: the
    # stale bound is not what governs the future side.
    for offset in (3, 10, 13):
        assert fresh(quote(offset), NOW)
        assert fresh(quote(offset), NOW, age=60)


def test_future_skew_boundary():
    tol = QUOTE_FUTURE_SKEW_TOLERANCE_S
    assert fresh(quote(tol), NOW)
    assert not fresh(quote(tol + 1), NOW)
    assert not fresh(quote(tol + 0.5), NOW, age=60)


def test_far_future_stamp_is_corrupt_and_fails():
    assert not fresh(quote(3600), NOW)
    assert not fresh(quote(3600), NOW, age=600)


def test_stale_side_unchanged_at_default_bound():
    assert fresh(quote(-5), NOW)
    assert not fresh(quote(-6), NOW)
    assert not fresh(quote(-5.5), NOW)


def test_stale_side_unchanged_at_decision_bound():
    assert fresh(quote(-60), NOW, age=60)
    assert not fresh(quote(-61), NOW, age=60)


def test_structural_validity_still_required_within_skew():
    assert not fresh(quote(3, bid=10.05, ask=10.0), NOW)
    assert not fresh(quote(3, bid=0), NOW)
    assert not fresh(quote(3, bid_size=0), NOW)
    assert not fresh(quote(3, ask_size=0), NOW)
    assert not fresh(quote(3, ts=None), NOW)
    assert not fresh(None, NOW)
    assert not fresh({}, NOW)
