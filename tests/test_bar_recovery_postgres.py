"""Exercise the actual event-index lock timeout seen in production."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from contextlib import contextmanager
from types import SimpleNamespace
from sqlalchemy import text,select,func
from tests.test_smoothers_postgres_handoff import pg
from compass.providers import Collectors
from compass.store import events


def test_bar_writer_retries_real_event_index_lock_timeout(pg,monkeypatch):
    original_tx=pg.tx
    @contextmanager
    def short_transaction():
        with original_tx() as c:
            c.execute(text("SET LOCAL lock_timeout='100ms'"))
            yield c
    monkeypatch.setattr(pg,'tx',short_transaction)
    retry=Event();release=Event()
    def pause(_):
        retry.set();assert release.wait(5)
    monkeypatch.setattr('compass.providers.time.sleep',pause)
    rows=[('SPY',float(i),dict(o=1,h=2,l=1,c=2,v=10)) for i in range(501)]
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with original_tx() as c:
                pg.append_bars(c,'bar','alpaca',rows[:1])
                future=pool.submit(Collectors.bars,SimpleNamespace(db=pg),'alpaca',rows)
                assert retry.wait(5), 'writer must encounter the held unique-index row'
            release.set()
            future.result(timeout=10)
        finally:release.set()
    with pg.tx() as c:
        assert c.execute(select(func.count()).select_from(events)).scalar_one()==501
        assert pg.get(c,'latestbar:SPY')==500
        assert len(pg.get(c,'bar_window:SPY'))==501
