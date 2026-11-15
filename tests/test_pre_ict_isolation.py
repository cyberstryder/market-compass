"""Per-unit transaction isolation for the pre-ICT scans (2026-11-15).

The pre-ICT scans used to share ONE transaction per tick. On Fri
2026-11-13 that transaction crossed the Postgres transaction timeout
on effectively every tick for ~5 hours, and every timeout rolled back
ALL of the unit's writes — zero scanner opportunities or selection
diagnostics committed 07:57-12:47 CT — while separately-transacted
segments (ICT, mu_scalp, levels) kept recording. A transaction
timeout TERMINATES the connection, so no try/except inside the shared
transaction can save the other scans; isolation has to be at
transaction boundaries.

Each pre-ICT scan now runs in its own transaction via
Engine._pre_ict_unit. These tests pin: a unit that raises mid-write
(1) does not stop the units before or after it — their writes commit,
(2) loses only its own writes (rolled back), and (3) leaves a
full-shape health row (name/status/detail/checked_at — the
2026-10-30 contract) per unit plus the aggregate pre_ict_error row.
"""
import time

import pytest

from compass.store import Store
from compass.config import Config
from compass.engine import Engine

NOW = time.time()


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "t.db"))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg(tmp_path):
    return Config(local=True, role="all", db="sqlite:///" + str(tmp_path / "t.db"),
                  stocks=("SPY",), futures=(), alpaca_key="", alpaca_secret="", massive="",
                  databento="", matrix="", discord="", mu_scalp=False, setup_study=False,
                  scanner=True, option_ideas=False, swing_ideas=False, day_trading_board=False,
                  apex_magnet=False, tape_confirmed=True, flow_pulse=False, zero_dte_paper=False,
                  darkpool=False, iv_rank=False, squeeze=False, gap_continuation=False, breakouts=False)


def assert_wellformed(err, name):
    assert err['name'] == name
    assert err['status'] == 'error'
    assert err['detail'] and err['detail'] == err['error'][:300]
    assert err['checked_at'] == err['at']


def _patch_units(engine, db, monkeypatch):
    """Swings (before) and tape (after) commit probes; the scanner
    writes a probe and then raises, simulating a mid-unit timeout."""
    monkeypatch.setattr(engine, 'swings',
                        lambda c, now: db.put(c, "probe:swings", {"at": now}))

    def scan_boom(c, now, eng):
        db.put(c, "probe:scanner", {"at": now})
        raise RuntimeError('transaction timeout (simulated)')

    monkeypatch.setattr(engine.scanner, 'scan', scan_boom)
    from compass import tape_confirmed

    def tape_ok(db_, c, cfg, now):
        db_.put(c, "probe:tape", {"at": now})
        return {'ran': True, 'evaluated': 3, 'confirmed': 1}

    monkeypatch.setattr(tape_confirmed, 'scan', tape_ok)


def test_failing_unit_rolls_back_alone_and_others_commit(db, cfg, monkeypatch):
    engine = Engine(db, cfg)
    _patch_units(engine, db, monkeypatch)
    summaries = engine._pre_ict_scans(NOW)  # must not raise
    with db.tx() as c:
        # Units before and after the failure committed their writes.
        assert db.get(c, "probe:swings") == {"at": NOW}
        assert db.get(c, "probe:tape") == {"at": NOW}
        # The failing unit's own write rolled back.
        assert db.get(c, "probe:scanner") is None
        # Per-unit health row, full shape, naming the failed unit.
        err = db.get(c, 'health:pre_ict_scanner_error')
        assert err and 'transaction timeout' in err['error']
        assert err['unit'] == 'scanner'
        assert_wellformed(err, 'pre_ict_scanner_error')
        # The aggregate row existing readers assert on is still written.
        agg = db.get(c, 'health:pre_ict_error')
        assert agg and 'transaction timeout' in agg['error']
        assert_wellformed(agg, 'pre_ict_error')
    # The surviving unit's summary flows to the post-ICT health writes.
    assert summaries['tape_summary'] == {'ran': True, 'evaluated': 3, 'confirmed': 1}


def test_tick_completes_with_failing_scanner_unit(db, cfg, monkeypatch):
    """Tick level: the scanner unit dies mid-write; the tick still
    completes (worker:engine written), the later tape unit commits,
    and the scanner's partial write is gone."""
    engine = Engine(db, cfg)
    _patch_units(engine, db, monkeypatch)
    engine.tick(NOW)  # must not raise
    with db.tx() as c:
        assert db.get(c, "probe:tape") == {"at": NOW}
        assert db.get(c, "probe:scanner") is None
        assert db.get(c, "worker:engine")
        err = db.get(c, 'health:pre_ict_scanner_error')
        assert err and err['unit'] == 'scanner'
        assert_wellformed(err, 'pre_ict_scanner_error')


def test_lease_lost_in_a_unit_aborts_remaining_units(db, cfg, monkeypatch):
    """Lease loss is NOT contained by the per-unit boundary: it aborts
    the rest of the tick (a second engine may own the lease). Units
    that already committed keep their writes."""
    engine = Engine(db, cfg)
    _patch_units(engine, db, monkeypatch)
    original = db.lease
    state = {"n": 0}

    def lease(c, name, owner, seconds=30):
        if name == "engine":
            state["n"] += 1
            if state["n"] >= 3:  # swings=1, earnings=2, scanner=3 -> lost
                return False
        return original(c, name, owner, seconds)

    monkeypatch.setattr(db, "lease", lease)
    with pytest.raises(Engine.LeaseLost):
        engine._pre_ict_scans(NOW)
    with db.tx() as c:
        assert db.get(c, "probe:swings") == {"at": NOW}  # committed before the loss
        assert db.get(c, "probe:tape") is None  # never ran
