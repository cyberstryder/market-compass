"""Focused tests for compass/archive_prune.py. Uses throwaway SQLite files only."""
import time
import pytest
from sqlalchemy import select, func, insert
from compass.store import Store, events, tm_studies
from compass import archive_prune

NOW = 1759000000.0
DAY = 86400


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'prune.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def add_event(db, kind, ts, symbol='SPY', source='test'):
    with db.tx() as c:
        assert db.append(c, kind, source, symbol + str(ts), ts, {'v': 1})


def max_id(db):
    with db.tx() as c:
        return c.execute(select(func.max(events.c.id))).scalar() or 0


def set_frontier(db, frontier):
    with db.tx() as c:
        db.put(c, 'archive:schedule-v1', dict(frontier=frontier, updated_at=NOW))


def count_kind(db, kind):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(events)
            .where(events.c.kind == kind)).scalar()


def add_study(db, id, assessed_at, status):
    with db.tx() as c:
        c.execute(insert(tm_studies).values(id=id, day='2026-09-01', vendor_id='v' + id,
            version='v1', symbol='SPY', first_seen=assessed_at, assessed_at=assessed_at,
            origin='test', status=status, score_status='scored', direction='call',
            age_bucket='fresh', dte_bucket='0dte', payload={}))


def test_dry_run_is_default_and_deletes_nothing(db):
    add_event(db, 'quote', NOW - 30 * DAY)
    set_frontier(db, max_id(db))
    work = archive_prune.plan(db, NOW)
    quotes = next(c for c in work['categories'] if c['category'] == 'market_quotes')
    assert quotes['eligible_rows'] == 1
    result = archive_prune.execute(db, work)
    assert result['dry_run'] is True and result['deleted_rows'] == 0
    assert count_kind(db, 'quote') == 1


def test_confirm_deletes_only_archived_and_expired(db):
    old_archived = NOW - 30 * DAY
    old_unarchived = NOW - 31 * DAY
    recent = NOW - 1 * DAY
    add_event(db, 'quote', old_archived)
    set_frontier(db, max_id(db))  # only the first row has a verified S3 copy
    add_event(db, 'quote', old_unarchived)
    add_event(db, 'quote', recent)
    work = archive_prune.plan(db, NOW)
    quotes = next(c for c in work['categories'] if c['category'] == 'market_quotes')
    assert quotes['eligible_rows'] == 1 and quotes['held_rows'] == 1  # old-but-unarchived held back
    result = archive_prune.execute(db, work, confirm=True)
    assert result['dry_run'] is False and result['deleted_rows'] == 1
    assert count_kind(db, 'quote') == 2  # unarchived old + recent survive


def test_no_archive_state_refuses_raw_evidence(db):
    add_event(db, 'quote', NOW - 60 * DAY)
    add_event(db, 'chain_archive', NOW - 60 * DAY)
    work = archive_prune.plan(db, NOW)
    for name in ('market_quotes', 'chain_snapshots'):
        item = next(c for c in work['categories'] if c['category'] == name)
        assert item['eligible_rows'] == 0 and item['guard'] == 'refused_no_archive'
    result = archive_prune.execute(db, work, confirm=True)
    assert result['deleted_rows'] == 0
    assert count_kind(db, 'quote') == 1 and count_kind(db, 'chain_archive') == 1


def test_global_recency_floor_overrides_short_retention(db):
    policy = {'tiny': dict(kinds=('quote',), retention_days=1, requires_archive=False, basis='test')}
    add_event(db, 'quote', NOW - 3 * DAY)  # older than retention, younger than floor
    work = archive_prune.plan(db, NOW, event_policy=policy)
    assert work['categories'][0]['eligible_rows'] == 0
    result = archive_prune.execute(db, work, confirm=True)
    assert result['deleted_rows'] == 0 and count_kind(db, 'quote') == 1


def test_pending_studies_never_pruned_but_finished_old_ones_are(db):
    add_event(db, 'quote', NOW - 400 * DAY)
    set_frontier(db, max_id(db))
    add_study(db, 'pending-old', NOW - 401 * DAY, 'pending')
    add_study(db, 'done-old', NOW - 401 * DAY, 'complete')
    add_study(db, 'done-recent', NOW - 10 * DAY, 'complete')
    work = archive_prune.plan(db, NOW)
    studies = next(c for c in work['categories'] if c['category'] == 'tm_flow_studies')
    assert studies['eligible_rows'] == 1  # only the finished, old one
    result = archive_prune.execute(db, work, confirm=True)
    # The 400-day-old quote is also eligible (archived + past quote retention).
    assert result['deleted_rows'] == 2
    with db.tx() as c:
        remaining = {r[0] for r in c.execute(select(tm_studies.c.id)).all()}
    assert remaining == {'pending-old', 'done-recent'}
    assert count_kind(db, 'quote') == 0


def test_deletion_log_accounts_every_row(db):
    for i in range(7):
        add_event(db, 'quote', NOW - 30 * DAY - i)
    set_frontier(db, max_id(db))
    work = archive_prune.plan(db, NOW)
    result = archive_prune.execute(db, work, confirm=True, batch=3)
    assert result['deleted_rows'] == 7
    assert sum(e['rows_deleted'] for e in result['log']) == 7
    assert all(e['category'] == 'market_quotes' for e in result['log'])
    assert count_kind(db, 'quote') == 0


def test_unknown_kinds_are_never_touched(db):
    add_event(db, 'mystery_kind', NOW - 400 * DAY)
    set_frontier(db, max_id(db))
    work = archive_prune.plan(db, NOW)
    assert work['categories'] and all('mystery_kind' not in c.get('kinds', []) for c in work['categories'])
    result = archive_prune.execute(db, work, confirm=True)
    assert result['deleted_rows'] == 0
    assert count_kind(db, 'mystery_kind') == 1


def test_per_run_row_cap_is_respected(db):
    for i in range(10):
        add_event(db, 'quote', NOW - 30 * DAY - i)
    set_frontier(db, max_id(db))
    work = archive_prune.plan(db, NOW)
    result = archive_prune.execute(db, work, confirm=True, max_rows=4)
    assert result['deleted_rows'] == 4
    assert count_kind(db, 'quote') == 6
