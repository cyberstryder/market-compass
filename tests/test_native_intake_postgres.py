"""Intake must not reserve a connection while waiting for another one."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select, func
from tests.test_smoothers_postgres_handoff import pg
from native_morning_fixtures import signal_event
from compass import native_morning_worker as worker
from compass.native_morning import store as source
from compass.native_routing import receipts
from compass.native_outbox import outbox

NOW = datetime(2026, 9, 17, 13, 35, tzinfo=timezone.utc).timestamp()


def test_concurrent_intake_finishes_with_single_connection_pool(pg):
    worker.initialize(pg)
    url = pg.engine.url
    pg.engine.dispose()
    pg.engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=2)
    events = [signal_event('burst-' + str(i), int(NOW * 1000)) for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        jobs = [pool.submit(worker.accept, pg, event, NOW) for event in events]
        assert all(job.result(timeout=10)['status'] == 'accepted' for job in jobs)
    assert worker.accept(pg, events[0], NOW + 1)['status'] == 'duplicate'
    with pg.tx() as c:
        for table in (source.signals, source.events, receipts, outbox):
            assert c.execute(select(func.count()).select_from(table)).scalar_one() == 12
        assert set(c.execute(select(outbox.c.status)).scalars()) == {'shadow'}


def test_receipt_failure_rolls_back_input_and_preview(pg, monkeypatch):
    worker.initialize(pg)
    event = signal_event('atomic-intake', int(NOW * 1000))
    original = worker.record
    def failed_receipt(*args, **kwargs):
        raise RuntimeError('synthetic receipt failure')
    monkeypatch.setattr(worker, 'record', failed_receipt)
    with pytest.raises(RuntimeError, match='synthetic receipt failure'):
        worker.accept(pg, event, NOW)
    with pg.tx() as c:
        for table in (source.signals, source.events, source.outbox, receipts, outbox):
            assert c.execute(select(func.count()).select_from(table)).scalar_one() == 0
        assert pg.get(c, 'native_morning:last_intake') is None
    monkeypatch.setattr(worker, 'record', original)
    assert worker.accept(pg, event, NOW + 1)['status'] == 'accepted'
