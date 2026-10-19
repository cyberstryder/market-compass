"""Archive pump empty-tail probe (2026-10-19).

The week-by-week walk spent 45+ hourly runs marking 0-row alert_delivery
weeks one chunk at a time, with ~25 audit kinds (~2,300 mostly-empty
weeks) queued behind it. _fast_forward_empty_tail probes each kind's
oldest archivable event once and bulk-marks every older week done —
those weeks are empty by definition.
"""
import time

import pytest

from compass.store import Store
from compass.copy_archive import (
    DAY, _fast_forward_empty_tail, _oldest_archivable_ts,
    _periods_to_archive, _week_range, _week_str)

NOW = time.time()
OLD_TS = NOW - 400 * DAY      # archivable for a 365d-retention audit kind
RECENT_TS = NOW - 10 * DAY    # inside retention: not archivable, ignored


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'arch.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _seed(db):
    with db.tx() as c:
        db.append(c, 'signal', 'test', 'SPY', OLD_TS, {'x': 1}, key='sig-old')
        db.append(c, 'signal', 'test', 'SPY', RECENT_TS, {'x': 2}, key='sig-new')


def test_oldest_archivable_ts(db):
    _seed(db)
    with db.tx() as c:
        pass
    oldest = _oldest_archivable_ts(db, 'signal', 1704067200, NOW - 365 * DAY)
    assert oldest == OLD_TS
    assert _oldest_archivable_ts(db, 'research', 1704067200, NOW - 365 * DAY) is None


def test_fast_forward_marks_only_provably_empty_tail(db):
    _seed(db)
    periods = _periods_to_archive('signal', NOW)
    assert len(periods) > 60  # 2024 floor to the 365d cutoff, weekly
    marked = _fast_forward_empty_tail(db, 'signal', periods, NOW)
    oldest_ws, _ = _week_range(OLD_TS)
    expected = len([p for p in periods if p[0] < oldest_ws])
    assert expected > 50  # the bulk of the walk, cleared in one call
    assert marked == expected
    with db.tx() as c:
        # The oldest period (2024-W01) is marked...
        assert db.get(c, f"copy-archive:signal:{_week_str(periods[-1][0])}")
        # ...but the week that actually holds data is NOT — it must still
        # be exported and hash-verified by the normal path.
        assert db.get(c, f"copy-archive:signal:{_week_str(oldest_ws)}") is None
    # Idempotent: a second probe marks nothing new.
    assert _fast_forward_empty_tail(db, 'signal', periods, NOW) == 0


def test_fast_forward_no_data_marks_everything(db):
    periods = _periods_to_archive('research', NOW)
    marked = _fast_forward_empty_tail(db, 'research', periods, NOW)
    assert marked == len(periods)
