import httpx
from sqlalchemy import select
from test_projects import db
from compass.native_outbox import queue,outbox,deliver_one
from compass.smoothers_handoff import rollback

NOW=1790400000
PRIMARY='https://discord.com/api/webhooks/123/private'
SHARED='https://discord.com/api/webhooks/456/shared'


def queued(db):
    with db.tx() as c:
        db.put(c,'native:ownership:smoothers',dict(owner='compass',previous_sender_paused=True,
            accepted_at=NOW-1,effective_from=NOW-10,epoch='test'))
        queue(db,c,'smoothers','roster',{'content':'new weekly roster'},NOW,event_time=NOW,cohort_time=NOW)


def test_primary_network_failure_does_not_block_shared_and_no_replay(db):
    queued(db);calls=[]
    def handler(request):
        calls.append(str(request.url))
        if '/123/' in str(request.url):raise httpx.ReadTimeout('unknown',request=request)
        return httpx.Response(200,json={'id':'4567'})
    hooks={'smoothers':PRIMARY,'smoothers_shared':SHARED}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,hooks,True,NOW+1)
        assert deliver_one(db,client,hooks,True,NOW+2)
        assert not deliver_one(db,client,hooks,True,NOW+3)
    with db.tx() as c:
        states=dict(c.execute(select(outbox.c.program,outbox.c.status)).all())
        assert states=={'smoothers':'ambiguous','smoothers_shared':'delivered'}
    assert len(calls)==2


def test_shared_rate_limit_does_not_repeat_primary(db):
    queued(db);calls=[]
    def handler(request):
        calls.append(str(request.url))
        if '/456/' in str(request.url) and len(calls)==2:return httpx.Response(429,json={'retry_after':2})
        return httpx.Response(200,json={'id':'789'})
    hooks={'smoothers':PRIMARY,'smoothers_shared':SHARED}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,hooks,True,NOW+1)
        assert deliver_one(db,client,hooks,True,NOW+2)
        assert not deliver_one(db,client,hooks,True,NOW+3)
        assert deliver_one(db,client,hooks,True,NOW+5)
    assert sum('/123/' in s for s in calls)==1


def test_same_destination_is_not_duplicated_and_shadow_never_copied(db):
    queued(db)
    with db.tx() as c:
        queue(db,c,'smoothers','old',{'content':'old'},NOW,event_time=NOW-100,cohort_time=NOW-100)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'id':'11'}))) as client:
        assert deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':PRIMARY},True,NOW+1)
        assert not deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':SHARED},True,NOW+2)
    with db.tx() as c:assert not c.execute(select(outbox).where(outbox.c.program=='smoothers_shared')).first()


def test_rollback_cancels_pending_shared_copy(db):
    queued(db)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'id':'11'}))) as client:
        assert deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':SHARED},True,NOW+1)
        rollback(db,NOW+2)
        assert not deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':SHARED},True,NOW+3)
    with db.tx() as c:
        assert c.execute(select(outbox.c.status).where(outbox.c.program=='smoothers_shared')).scalar_one()=='cancelled'


def test_changed_shared_destination_cannot_receive_old_copy(db):
    queued(db)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'id':'11'}))) as client:
        assert deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':SHARED},True,NOW+1)
        assert not deliver_one(db,client,{'smoothers':PRIMARY,'smoothers_shared':SHARED+'changed'},True,NOW+2)
    with db.tx() as c:assert c.execute(select(outbox.c.status).where(outbox.c.program=='smoothers_shared')).scalar_one()=='suppressed'


def test_readiness_reports_missing_evidence_without_returns(db):
    from compass.strategy_readiness import report
    with db.tx() as c:
        result=report(db,c,NOW)
    assert result['weekday']['candidates']==0
    assert not result['weekday']['alerts_enabled']
    assert result['smoothers']['blockers']
    assert not result['futures']['returns_reviewed']
    assert result['spy']['day'] is None


def test_shared_entry_receipt_allows_shared_exit_without_orphan_primary_exit(db):
    with db.tx() as c:
        db.put(c,'native:ownership:smoothers',dict(owner='compass',previous_sender_paused=True,
            accepted_at=NOW-1,effective_from=NOW-10,epoch='test'))
        publication=dict(id='weekly1',status='native_option_entry',contract={'symbol':'QQQ261002C00500000'},
            track='swing',quote=dict(bid=2,ask=2.1,ts=NOW),expires_at=NOW+120)
        queue(db,c,'smoothers','entry',{'content':'entry'},NOW,event_time=NOW,cohort_time=NOW,publication=publication)
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if '/123/' in str(request.url):raise httpx.ReadTimeout('unknown',request=request)
        return httpx.Response(200,json={'id':'4567'})
    hooks={'smoothers':PRIMARY,'smoothers_shared':SHARED}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert deliver_one(db,client,hooks,True,NOW+1)
        assert deliver_one(db,client,hooks,True,NOW+2)
        with db.tx() as c:
            queue(db,c,'smoothers','exit',{'content':'exit'},NOW+3,event_time=NOW+3,cohort_time=NOW,
                publication={**publication,'status':'native_option_exit','exit_reason':'target','outcome':'unresolved'})
        assert not deliver_one(db,client,hooks,True,NOW+4)
        assert deliver_one(db,client,hooks,True,NOW+5)
    with db.tx() as c:
        rows={(r.program,r.event_key):r.status for r in c.execute(select(outbox))}
    assert rows[('smoothers','exit')]=='suppressed'
    assert rows[('smoothers_shared','exit')]=='delivered'
    assert sum('/123/' in x for x in calls)==1


def test_shared_entry_retry_cannot_use_stale_quote(db):
    with db.tx() as c:
        db.put(c,'native:ownership:smoothers',dict(owner='compass',previous_sender_paused=True,
            accepted_at=NOW-1,effective_from=NOW-10,epoch='test'))
        queue(db,c,'smoothers','entry',{'content':'entry'},NOW,event_time=NOW,cohort_time=NOW,
            publication=dict(id='stale-shared',status='native_option_entry',contract={'symbol':'QQQ261002C00500000'},
                track='swing',quote=dict(bid=2,ask=2.1,ts=NOW),expires_at=NOW+120))
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if '/456/' in str(request.url):return httpx.Response(429,json={'retry_after':20})
        return httpx.Response(200,json={'id':'4567'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hooks={'smoothers':PRIMARY,'smoothers_shared':SHARED}
        assert deliver_one(db,client,hooks,True,NOW+1)
        assert deliver_one(db,client,hooks,True,NOW+2)
        assert not deliver_one(db,client,hooks,True,NOW+23)
    with db.tx() as c:
        shared=c.execute(select(outbox).where(outbox.c.program=='smoothers_shared')).mappings().one()
        assert shared['status']=='suppressed'
        assert shared['delivery']['error']=='Stale or future option quote'
    assert len(calls)==2
