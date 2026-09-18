"""PostgreSQL lease release and shutdown behavior under real blocked queries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.test_smoothers_postgres_handoff import pg


def test_session_limits_and_schema_are_preserved(pg):
    with pg.tx() as c:
        assert c.execute(text('SHOW statement_timeout')).scalar_one() == '30s'
        assert c.execute(text('SHOW lock_timeout')).scalar_one() == '5s'
        assert c.execute(text('SHOW idle_in_transaction_session_timeout')).scalar_one() == '30s'
        assert c.execute(text('SELECT current_schema()')).scalar_one().startswith('smoothers_test_')
        if pg.engine.dialect.server_version_info >= (17,):
            assert c.execute(text('SHOW transaction_timeout')).scalar_one() == '1min'


def test_timeout_rolls_back_lease_and_next_owner_can_scan(pg):
    with pytest.raises(DBAPIError):
        with pg.tx() as c:
            assert pg.lease(c,'engine','stalled',30)
            c.execute(text("SET LOCAL statement_timeout='50ms'"))
            c.execute(text('SELECT pg_sleep(1)'))
    with pg.tx() as c:
        assert pg.lease(c,'engine','replacement',30)


def test_lock_wait_is_bounded_and_connection_recovers(pg):
    with pg.tx() as held:
        pg.put(held,'locked',1)
    with pg.tx() as held, ThreadPoolExecutor(max_workers=1) as pool:
        pg.locked_get(held,'locked',0)
        def wait():
            with pg.tx() as c:
                c.execute(text("SET LOCAL lock_timeout='50ms'"))
                pg.put(c,'locked',2)
        with pytest.raises(DBAPIError):pool.submit(wait).result(timeout=2)
    with pg.tx() as c:assert pg.get(c,'locked') == 1


def test_shutdown_cancels_owned_query_and_drains_connection(pg):
    entered=Event(); backend=[]
    def slow():
        try:
            with pg.tx() as c:
                backend.append(c.execute(text("SELECT pg_backend_pid()")).scalar_one())
                entered.set()
                c.execute(text('SELECT pg_sleep(30)'))
        except (DBAPIError,RuntimeError):pass
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(slow)
        assert entered.wait(2)
        import time
        deadline=time.monotonic()+2
        while True:
            with pg.tx() as c:
                sleeping=c.execute(text("SELECT wait_event='PgSleep' FROM pg_stat_activity WHERE pid=:pid"),{'pid':backend[0]}).scalar_one()
            if sleeping:break
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert pg.shutdown(timeout=3)
        pending.result(timeout=2)
    with pytest.raises(RuntimeError,match='shutting down'):
        with pg.tx():pass


def test_report_lease_does_not_hold_engine_lease(pg):
    from compass.setup_study import SetupStudy
    from compass.config import Config
    entered=Event();release=Event()
    study=SetupStudy(pg,Config(local=True))
    def report(c,now):
        entered.set();assert release.wait(3)
    study.refresh_report=report
    with ThreadPoolExecutor(max_workers=1) as pool:
        report_job=pool.submit(study.report_tick)
        assert entered.wait(2)
        try:
            with pg.tx() as c:
                c.execute(text("SET LOCAL lock_timeout='100ms'"))
                assert pg.lease(c,'engine','live-scanner',30)
        finally:release.set()
        report_job.result(timeout=2)
