"""Real PostgreSQL contention between source revisions and checkpoint work."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import select
from tests.test_smoothers_postgres_handoff import pg
from compass.projects import ingest
from compass.strategy_tracking import ledger, tick


def test_checkpoint_worker_skips_source_locked_rows_and_recovers(pg):
    now = 14000
    older = dict(id='older', symbol='SPY', source_ts=10000, entry=100.,
                 side='long', checkpoints=[])
    newer = {**older, 'id': 'newer', 'source_ts': 10001}
    with pg.tx() as c:
        for row in (older, newer):
            ingest(pg, c, 'morning', row, 10002)
        pg.put(c, 'strategy_tracking:bootstrap_cursor', 'complete')
    locked, release = Event(), Event()
    revision = {**newer, 'status': 'observed_60m'}

    def source_page():
        with pg.tx() as c:
            ingest(pg, c, 'morning', revision, now)
            locked.set()
            assert release.wait(10)
            ingest(pg, c, 'morning', {**older, 'status': 'observed_60m'}, now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(source_page)
        assert locked.wait(5)
        worker = pool.submit(tick, pg, 'worker', now)
        try:
            # Must finish while source_page holds its ledger lock; the old
            # implementation waits and then deadlocks when source_page proceeds.
            worker.result(timeout=5)
            with pg.tx() as c:
                rows = {r['source_id']: r for r in c.execute(select(ledger)).mappings()}
                assert rows['older']['measurements']['complete'] is True
                assert rows['newer']['measurements'] == {}
                assert pg.get(c, 'health:strategy_tracking')['status'] == 'available'
        finally:
            release.set()
        writer.result(timeout=5)
    tick(pg, 'worker', now + 5)
    with pg.tx() as c:
        rows = list(c.execute(select(ledger)).mappings())
        assert all(r['measurements']['complete'] for r in rows)
        assert all(r['latest']['status'] == 'observed_60m' for r in rows)
        assert all('status' not in r['frozen'] for r in rows)
        assert all(r['measurements']['5']['reason'] ==
                   'no_retained_quote_in_checkpoint_window' for r in rows)
        before = copy.deepcopy([r['measurements'] for r in rows])
    tick(pg, 'worker', now + 10)
    with pg.tx() as c:
        assert [r['measurements'] for r in c.execute(select(ledger)).mappings()] == before
