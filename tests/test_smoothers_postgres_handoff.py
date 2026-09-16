"""Exercise real PostgreSQL transaction boundaries; no production connections."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor,TimeoutError
from threading import Event
import httpx
import pytest
from sqlalchemy import create_engine,text,select
from sqlalchemy.engine import make_url
from compass.store import Store
from compass.native_outbox import queue,outbox,deliver_one
from compass.smoothers_handoff import OWNER,rollback


@pytest.fixture
def pg():
    url=os.getenv('SMOOTHERS_TEST_DATABASE_URL')
    if not url:pytest.skip('Isolated PostgreSQL test service is provided in CI')
    parsed=make_url(url)
    if parsed.database!='compass_test' or parsed.host not in ('localhost','127.0.0.1','postgres'):
        raise ValueError('Only the dedicated local/CI compass_test database is permitted')
    schema='smoothers_test_'+uuid.uuid4().hex
    admin=create_engine(parsed)
    with admin.begin() as c:c.execute(text('CREATE SCHEMA '+schema))
    scoped=parsed.update_query_dict({'options':'-csearch_path='+schema})
    db=Store(scoped.render_as_string(hide_password=False));db.initialize()
    with db.tx() as c:db.put(c,OWNER,dict(owner='compass',previous_sender_paused=True,accepted_at=1,epoch='test',effective_from=1))
    try:yield db
    finally:
        db.engine.dispose()
        with admin.begin() as c:c.execute(text('DROP SCHEMA '+schema+' CASCADE'))
        admin.dispose()


def test_postgres_queue_commit_and_rollback_are_serialized(pg):
    queued=Event();release=Event()
    def enqueue():
        with pg.tx() as c:
            queue(pg,c,'smoothers','atomic',{'content':'test'},2,event_time=2,cohort_time=2)
            queued.set();assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        q=pool.submit(enqueue);assert queued.wait(5)
        stop=pool.submit(rollback,pg,3)
        try:
            with pytest.raises(TimeoutError):stop.result(timeout=.2)
        finally:release.set()
        q.result(timeout=5);stop.result(timeout=5)
    with pg.tx() as c:
        assert c.execute(select(outbox.c.status)).scalar_one()=='cancelled'
        assert pg.get(c,OWNER)['owner']=='original'


def test_postgres_rollback_during_http_preserves_uncertain_receipt(pg):
    sending=Event();release=Event()
    with pg.tx() as c:queue(pg,c,'smoothers','in-flight',{'content':'test'},2,event_time=2,cohort_time=2)
    def handler(request):
        sending.set();assert release.wait(5)
        return httpx.Response(200,json={'id':'123456789'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client,ThreadPoolExecutor(max_workers=1) as pool:
        send=pool.submit(deliver_one,pg,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},True,3)
        try:
            assert sending.wait(5)
            result=rollback(pg,4)
            assert result['unknown_delivery']==1
        finally:release.set()
        assert send.result(timeout=5)
    with pg.tx() as c:
        row=c.execute(select(outbox)).mappings().one()
        assert row['status']=='ambiguous'
        assert row['delivery']['message_id']=='123456789'
        assert row['delivery']['error']=='handoff_rollback_requires_delivery_reconciliation'
