"""Real ownership races and atomic report publication, including PostgreSQL."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select, text

from compass.config import Config
from compass.readiness import decorate_health
from compass.setup_report_worker import ReportWorker, KEY, HEALTH, LEASE_SECONDS, run
from compass.setup_study import SetupStudy
from compass.store import leases
from test_setup_study import db
from test_smoothers_postgres_handoff import pg

NOW = 2000000000.


@pytest.fixture(params=['sqlite', 'postgres'])
def report_db(request, monkeypatch):
    monkeypatch.setattr('compass.setup_report_worker.time.time', lambda: NOW)
    return request.getfixturevalue('db' if request.param == 'sqlite' else 'pg')


def study(db, build=None):
    result = SetupStudy(db, Config(local=True))
    result.build_report = build or (lambda c, now: {'at': now, 'count': 1, 'groups': []})
    return result


def seed(db):
    with db.tx() as c:
        db.put(c, KEY, {'at': NOW-60, 'count': 99})
        db.put(c, HEALTH, dict(name='setup_reports', status='running', detail='previous',
            checked_at=NOW-40, last_complete_at=NOW-40, report_asof=NOW-60))


def test_calculation_holds_no_lease_lock_and_contender_cannot_claim_health(report_db):
    seed(report_db)
    entered, release = Event(), Event()
    def build(c, now):
        if c.dialect.name == 'postgresql':
            assert c.execute(text('SHOW transaction_read_only')).scalar_one() == 'on'
            assert c.execute(text('SHOW transaction_isolation')).scalar_one() == 'repeatable read'
        entered.set()
        assert release.wait(5)
        return {'at': now, 'count': 1}
    owner = ReportWorker(study(report_db, build))
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(owner.refresh)
        try:
            assert entered.wait(3)
            with report_db.tx() as c:
                if c.dialect.name == 'postgresql':
                    c.execute(text("SET LOCAL lock_timeout='100ms'"))
                    # A real row lock can be taken while calculation is blocked.
                    assert c.execute(select(leases.c.owner).where(leases.c.key == KEY)
                        .with_for_update(nowait=True)).scalar_one() == owner.owner
                saved = report_db.get(c, HEALTH)
                assert saved['status'] == 'refreshing'
                assert saved['last_complete_at'] == NOW-40
                assert report_db.lease(c, 'engine', 'scanner', 30)
            contender = ReportWorker(study(report_db))
            assert not contender.refresh()
            with report_db.tx() as c:
                assert report_db.get(c, HEALTH) == saved
        finally:
            release.set()
        assert task.result(timeout=3)
    with report_db.tx() as c:
        health = report_db.get(c, HEALTH)
        assert health['status'] == 'running' and health['last_complete_at'] == NOW
        assert health['report_asof'] == NOW and not health['scan_in_progress']
        assert report_db.get(c, KEY) == {'at': NOW, 'count': 1}
        assert c.execute(select(leases.c.until).where(leases.c.key == KEY)).scalar_one() == 0


def test_legacy_uncommitted_lease_is_a_bounded_retry_not_false_success(pg, monkeypatch):
    monkeypatch.setattr('compass.setup_report_worker.time.time', lambda: NOW)
    seed(pg)
    with pg.tx() as c:
        before = pg.get(c, HEALTH)
    with pg.tx() as held, ThreadPoolExecutor(max_workers=1) as pool:
        assert pg.lease(held, KEY, 'legacy-worker', LEASE_SECONDS)
        started = time.monotonic()
        assert pool.submit(ReportWorker(study(pg)).refresh).result(timeout=2) is False
        assert time.monotonic()-started < 1
        with pg.tx() as c:
            assert pg.get(c, HEALTH) == before
    with pg.tx() as c:
        c.execute(leases.update().where(leases.c.key == KEY).values(until=0))
    assert ReportWorker(study(pg)).refresh()


def test_expired_calculation_cannot_overwrite_replacement_or_release_its_lease(report_db):
    seed(report_db)
    replacement = ReportWorker(study(report_db))
    def build(c, now):
        with report_db.tx() as other:
            other.execute(leases.update().where(leases.c.key == KEY).values(until=NOW-1))
        assert replacement.claim(NOW+1)
        assert replacement.publish({'at': NOW+1, 'count': 2}, NOW)
        # Simulate a subsequent active attempt; stale cleanup cannot release it.
        with report_db.tx() as other:
            assert report_db.lease(other, KEY, replacement.owner, LEASE_SECONDS)
        return {'at': now, 'count': 1}
    old = ReportWorker(study(report_db, build))
    assert not old.refresh()
    with report_db.tx() as c:
        assert report_db.get(c, KEY) == {'at': NOW+1, 'count': 2}
        assert report_db.get(c, HEALTH)['owner'] == replacement.owner
        row = c.execute(select(leases).where(leases.c.key == KEY)).mappings().one()
        assert row['owner'] == replacement.owner and row['until'] == NOW+LEASE_SECONDS
    replacement.release()


def test_expired_lease_without_replacement_still_cannot_publish(report_db):
    seed(report_db)
    worker = ReportWorker(study(report_db))
    assert worker.claim(NOW)
    with report_db.tx() as c:
        c.execute(leases.update().where(leases.c.key == KEY).values(until=NOW-1))
    assert not worker.publish({'at': NOW, 'count': 1}, NOW)
    worker.release()
    with report_db.tx() as c:
        assert report_db.get(c, KEY)['count'] == 99
        assert report_db.get(c, HEALTH)['last_complete_at'] == NOW-40


def test_failed_publication_preserves_last_report_and_retry_recovers(report_db, monkeypatch):
    seed(report_db)
    original = report_db.put
    def put(c, key, value):
        # Fail after writing the new report, while recording its completion.
        # The report and health must roll back together.
        if key == HEALTH and value.get('status') == 'running' and value.get('report_asof') == NOW:
            raise RuntimeError('private details must not reach health')
        return original(c, key, value)
    monkeypatch.setattr(report_db, 'put', put)
    assert not ReportWorker(study(report_db)).refresh()
    with report_db.tx() as c:
        assert report_db.get(c, KEY) == {'at': NOW-60, 'count': 99}
        health = report_db.get(c, HEALTH)
        assert health['status'] == 'error' and health['error'] == 'RuntimeError'
        assert health['last_complete_at'] == NOW-40
        assert not health['scan_in_progress']
    monkeypatch.setattr(report_db, 'put', original)
    assert ReportWorker(study(report_db)).refresh()
    with report_db.tx() as c:
        assert report_db.get(c, KEY)['at'] == NOW
        assert report_db.get(c, HEALTH)['error'] is None


def test_shutdown_releases_ownership_and_late_result_is_discarded(report_db):
    seed(report_db)
    entered, release, finished = Event(), Event(), Event()
    def build(c, now):
        entered.set()
        try:
            assert release.wait(5)
            return {'at': now, 'count': 1}
        finally:
            finished.set()
    async def exercise():
        task = asyncio.create_task(run(study(report_db, build)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with report_db.tx() as c:
                assert c.execute(select(leases.c.until).where(leases.c.key == KEY)).scalar_one() == 0
            replacement = ReportWorker(study(report_db, lambda c, now: {'at': NOW+1, 'count': 2}))
            assert await asyncio.to_thread(replacement.refresh)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)
    asyncio.run(exercise())  # Also drains the original calculation thread.
    with report_db.tx() as c:
        assert report_db.get(c, KEY) == {'at': NOW+1, 'count': 2}
        assert report_db.get(c, HEALTH)['report_asof'] == NOW+1


def test_existing_fresh_report_skips_calculation_without_claiming_new_completion(report_db):
    seed(report_db)
    assert ReportWorker(study(report_db)).refresh()
    with report_db.tx() as c:
        before = report_db.get(c, HEALTH)
    def forbidden(c, now):
        pytest.fail('A current report must not be recalculated by a contender')
    assert not ReportWorker(study(report_db, forbidden)).refresh()
    with report_db.tx() as c:
        assert report_db.get(c, HEALTH) == before


def test_refresh_attempts_do_not_hide_old_report_or_error():
    health = dict(name='setup_reports', status='refreshing', detail='Working',
        checked_at=NOW, report_asof=NOW-91)
    workers = {'worker:engine': {'at': NOW}}
    assert decorate_health([health], workers, {}, NOW)[0]['status'] == 'stale'
    health['status'] = 'error'
    assert decorate_health([health], workers, {}, NOW)[0]['status'] == 'error'


def test_cancelled_worker_never_claims_or_publishes(report_db):
    seed(report_db)
    worker = ReportWorker(study(report_db))
    worker.stopped.set()
    assert not worker.refresh()
    with report_db.tx() as c:
        assert report_db.get(c, HEALTH)['checked_at'] == NOW-40
        assert report_db.get(c, KEY)['count'] == 99
