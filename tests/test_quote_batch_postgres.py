"""Exercise the production stream/recovery quote-writer lock order."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from types import SimpleNamespace
from sqlalchemy import select, func
from tests.test_smoothers_postgres_handoff import pg
from compass.providers import Collectors
from compass.store import events


def test_reversed_concurrent_batches_finish_and_keep_all_samples(pg,monkeypatch):
    locked,attempted,second_locked=Event(),Event(),Event()
    first_name=[]
    original=pg.put_quote
    def put(c,key,value):
        first=current_thread().name==first_name[0]
        if not first:attempted.set()
        result=original(c,key,value)
        if first and key=='quote:AAA':
            locked.set()
            assert attempted.wait(5)
            # With the old reversed lock order the second writer locks BBB,
            # then waits on AAA; this writer then deadlocks trying BBB.
            second_locked.wait(.2)
        elif not first:second_locked.set()
        return result
    monkeypatch.setattr(pg,'put_quote',put)
    def batch(first):
        if first:first_name.append(current_thread().name)
        quotes=[(s,{'ts':10 if first else 11,'bid':1,'ask':1.1},True) for s in ('AAA','BBB')]
        Collectors.quote_batch(SimpleNamespace(db=pg),'massive',quotes if first else quotes[::-1])
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(batch,True)
        assert locked.wait(5)
        two=pool.submit(batch,False)
        one.result(timeout=5);two.result(timeout=5)
    with pg.tx() as c:
        assert pg.get(c,'quote:AAA')['ts']==pg.get(c,'quote:BBB')['ts']==11
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='quote')).scalar_one()==4
