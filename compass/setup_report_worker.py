"""Short ownership transactions around a read-only research report calculation."""
import asyncio
import logging
import threading
import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from .diagnostics import database_error
from .store import leases

KEY = 'setup_study:report'
HEALTH = 'health:setup_reports'
LEASE_SECONDS = 120
LOG = logging.getLogger('uvicorn.error')


def short_lock_wait(c):
    if c.dialect.name == 'postgresql':
        c.execute(text("SET LOCAL lock_timeout = '100ms'"))


def lock_busy(error):
    return getattr(getattr(error, 'orig', None), 'sqlstate', None) == '55P03'


def health(db, c, status, detail, **extra):
    previous = db.get(c, HEALTH, {})
    db.put(c, HEALTH, {**previous, 'name': 'setup_reports', 'status': status,
        'detail': detail, 'checked_at': time.time(), **extra})


class ReportWorker:
    def __init__(self, study):
        self.study, self.db = study, study.db
        # Each calculation has a new token, including retries in one process.
        self.owner = uuid.uuid4().hex
        self.stopped = threading.Event()

    def claim(self, now):
        try:
            with self.db.tx() as c:
                short_lock_wait(c)
                if self.stopped.is_set():
                    return False
                previous = self.db.get(c, KEY, {})
                if 0 <= now-previous.get('at', 0) < 30:
                    return False
                if not self.db.lease(c, KEY, self.owner, LEASE_SECONDS):
                    return False
                health(self.db, c, 'refreshing', 'Research report calculation in progress',
                    scan_in_progress=True, started_at=time.time(), owner=self.owner,
                    report_asof=previous.get('at'), error=None)
            return True
        except DBAPIError as error:
            if not lock_busy(error):
                raise
            # An older worker may still hold the row during deployment. A
            # contender must not overwrite that worker's health or wait 5s.
            return False

    def owns(self, c):
        return c.execute(select(leases.c.owner).where(leases.c.key == KEY,
            leases.c.owner == self.owner, leases.c.until > time.time())
            .with_for_update(nowait=True)).first() is not None

    def release(self):
        try:
            with self.db.tx() as c:
                short_lock_wait(c)
                c.execute(leases.update().where(leases.c.key == KEY,
                    leases.c.owner == self.owner).values(until=0))
        except (DBAPIError, RuntimeError) as error:
            if not lock_busy(error) and not self.db._closing:
                LOG.warning('Research report lease cleanup: %s', database_error(error))
            # The bounded lease also expires after a crash or failed cleanup.

    def publish(self, report, started):
        with self.db.tx() as c:
            short_lock_wait(c)
            if self.stopped.is_set() or not self.owns(c):
                return False
            previous = self.db.get(c, KEY, {})
            if previous.get('at', 0) > report['at']:
                return False
            self.db.put(c, KEY, report)
            health(self.db, c, 'running', 'Research report calculation and publication completed',
                scan_in_progress=False, last_complete_at=time.time(), report_asof=report['at'],
                cycle_seconds=round(time.time()-started, 2), owner=self.owner, error=None)
            c.execute(leases.update().where(leases.c.key == KEY,
                leases.c.owner == self.owner).values(until=0))
        LOG.info('Futures market research: model=%s cohorts=%s trials=%s',
            report.get('version'), len(report.get('groups', [])), report.get('count'))
        return True

    def failed(self, error):
        if self.stopped.is_set():
            return
        LOG.warning('Research report refresh failed: %s', database_error(error))
        try:
            with self.db.tx() as c:
                short_lock_wait(c)
                if self.owns(c):
                    health(self.db, c, 'error', 'Report refresh failed; saved report retained; retry scheduled',
                        scan_in_progress=False, error=database_error(error), owner=self.owner)
        except (DBAPIError, RuntimeError):
            LOG.warning('Cannot record research report failure; completion health will become stale')

    def refresh(self, now=None):
        started = time.time()
        now = started if now is None else now
        try:
            if not self.study.cfg.setup_study:
                self.db.health('setup_reports', 'disabled', 'Research reports disabled by configuration')
                return False
            if not self.claim(now):
                return False
            if self.stopped.is_set():
                return False
            # The lease is committed before calculation. No lease/state row
            # locks are held while the aggregates are read and calculated.
            with self.db.tx() as c:
                if c.dialect.name == 'postgresql':
                    c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                report = self.study.build_report(c, now)
            return self.publish(report, started)
        except Exception as error:
            self.failed(error)
            return False
        finally:
            self.release()


async def run(study):
    while True:
        worker = ReportWorker(study)
        try:
            published = await asyncio.to_thread(worker.refresh)
        except asyncio.CancelledError:
            worker.stopped.set()
            # An in-flight read may finish later; its token and stop flag
            # prevent publication after the replacement worker takes over.
            await asyncio.to_thread(worker.release)
            raise
        await asyncio.sleep(30 if published or not study.cfg.setup_study else 5)
