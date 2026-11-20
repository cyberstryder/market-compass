"""Pin the retention cutoffs Josh approved on 2026-11-19 for the
disk-release deep prune: quotes 7d (already), bars 365d -> 180d, audit
kinds 365d -> 180d. Everything else is unchanged by that approval —
chains 30d, vendor (matrix_raw) 90d, flow 180d, mapping 365d — so a
future edit that widens or narrows any of these fails here first.
"""
from compass import copy_archive as ca


def test_approved_retention_values():
    assert ca._retention_days('quote') == 7
    assert ca._retention_days('bar') == 180
    for kind in ca.AUDIT_KINDS:
        assert ca._retention_days(kind) == 180, kind


def test_unchanged_retention_values():
    assert ca._retention_days('chain_archive') == 30
    assert ca._retention_days('matrix_raw') == 90
    assert ca._retention_days('exposure') == 90
    assert ca._retention_days('vendor_flow') == 180
    assert ca._retention_days('flow') == 180
    assert ca._retention_days('option_trade') == 180
    assert ca._retention_days('mapping') == 365
    assert ca._retention_days('alert_delivery') == 30


def test_unknown_kind_still_unretained():
    assert ca._retention_days('no_such_kind') is None
