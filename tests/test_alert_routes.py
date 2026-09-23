import asyncio
import json
import time
import httpx
import pytest
from sqlalchemy import select
from compass.alert_routes import route_for, manifest, destination
from compass.alerts import DeliveryWorker, outbox_status
from compass.config import Config
from compass.store import Store, events, discord_jobs

URL = 'https://discord.com/api/webhooks/123/fixture'
SPY = 'https://discord.com/api/webhooks/456/spy-fixture'


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'routes.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def add(db, category='intraday', status='setup_triggered', **extra):
    with db.tx() as c:
        db.append(c, 'alert', 'fixture', 'SPY', time.time(),
                  {'alert_category': category, 'status': 'notification_test', 'delivery_route': category, 'expires_at': time.time()+900, **extra})
        return c.execute(select(events).order_by(events.c.id.desc()).limit(1)).mappings().one()['id']


@pytest.mark.parametrize('category,status,expected', [
    ('morning', 'project_observation', 'research'),
    ('smoothers', 'project_observation', 'research'),
    ('futures', 'project_observation', 'futures'),
    ('futures', 'secondary_review', 'research'),
    ('options_0dte', 'setup_result', 'research'),
    ('swing_ideas', 'swing_idea_new', 'swing'),
    ('unusual_options', 'setup_triggered', 'unusual_options'),
    ('spy_morning', 'spy_chart_prompt', 'spy_morning'),
])
def test_family_routing(category, status, expected):
    assert route_for({'symbol': 'SPY', 'payload': {'alert_category': category, 'status': status}}) == expected


def test_failed_route_does_not_block_others_and_migration_does_not_replay(db):
    old = add(db)
    with db.tx() as c: db.put(c, 'outbox:discord', old)
    blocked = add(db, 'intraday')
    good = add(db, 'spy_morning', status='skipped', reason='test')
    worker = DeliveryWorker(db, Config(local=True, discord=URL, discord_routes={'spy_morning': SPY}))
    sent = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(200, json={'id': '123', 'channel_id': '789'})
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(429, json={'retry_after': 30}) if '/123/' in str(request.url) else httpx.Response(200, json={'id': '888'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.tick(client)
            await worker.tick(client)  # No immediate retry or replay of the successful route.
    asyncio.run(run())
    assert len(sent) == 2
    assert all('Event #'+str(old)+' ' not in body['content'] for _, body in sent)
    with db.tx() as c:
        jobs = list(c.execute(select(discord_jobs).order_by(discord_jobs.c.event_id)).mappings())
        assert [(j['event_id'], j['status']) for j in jobs] == [(blocked, 'pending'), (good, 'sent')]
        state = outbox_status(db, c, time.time())
        assert state['pending'] == 1
        assert next(r for r in state['routes'] if r['route'] == 'spy_morning')['pending'] == 0
        assert db.get(c, 'outbox:discord') < blocked
    assert 'fixture' not in json.dumps(state)


def test_dedicated_only_start_and_missing_invalid_routes_hold_without_fallback(db):
    add(db, 'spy_morning', status='skipped')
    add(db, 'futures', status='skipped')
    add(db, 'intraday', status='skipped')
    cfg = Config(local=True, discord='', discord_fallback=False,
                 discord_routes={'spy_morning': SPY, 'intraday': 'https://attacker.invalid/secret'})
    worker = DeliveryWorker(db, cfg); seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'id': '123'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.tick(client)
    asyncio.run(run())
    assert len(seen) == 2 and all('/456/' in url for url in seen)
    with db.tx() as c:
        assert outbox_status(db, c, time.time())['pending'] == 2
    assert destination(cfg, 'intraday')[1] == 'invalid'
    assert destination(cfg, 'futures')[1] == 'awaiting_channel'


def test_restart_rotation_retains_receipts_and_route_order(db):
    first = add(db, 'spy_morning', status='skipped', reason='brief stand-in')
    second = add(db, 'spy_morning', status='skipped', reason='chart stand-in')
    cfg = Config(local=True, discord=URL)
    worker = DeliveryWorker(db, cfg); sent = []
    def handler(request):
        if request.method == 'GET': return httpx.Response(200, json={'id': '123'})
        sent.append(json.loads(request.content)['content'])
        return httpx.Response(200, json={'id': str(900+len(sent))})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.tick(client)
            with db.tx() as c:
                assert outbox_status(db, c, time.time())['pending'] == 1
                # Simulate lease expiry between process deployments.
                from compass.store import leases
                c.execute(leases.delete())
            restarted = DeliveryWorker(db, Config(local=True, discord=URL, discord_routes={'spy_morning': SPY}))
            await restarted.tick(client)
            await restarted.tick(client)
    asyncio.run(run())
    assert len(sent) == 2 and f'Event #{first}' in sent[0] and f'Event #{second}' in sent[1]
    with db.tx() as c: assert outbox_status(db, c, time.time())['pending'] == 0


def test_route_test_keeps_test_identity_and_no_native_ownership_change(db):
    row = {'symbol': 'SYSTEM', 'payload': {'status': 'notification_test', 'delivery_route': 'spy_morning'}}
    assert route_for(row) == 'spy_morning'
    assert len(manifest(Config(local=True))) == 12
    with db.tx() as c: assert not db.prefix(c, 'native:ownership:')


def test_verification_recovers_without_sending_a_synthetic_alert(db):
    worker = DeliveryWorker(db, Config(local=True, discord=URL))
    calls = []
    def handler(request):
        calls.append(request.method)
        assert request.method == 'GET'
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={'id': '123', 'channel_id': '789'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.tick(client, 100)
            await worker.tick(client, 131)
    asyncio.run(run())
    with db.tx() as c:
        assert db.get(c, 'health:discord:spy_morning')['status'] == 'connected'
        assert db.get(c, 'health:discord:spy_morning')['destination']['channel_id'] == '789'
        assert outbox_status(db, c, 131)['pending'] == 0
    assert calls == ['GET', 'GET']


def test_inactive_routes_do_not_hide_real_queued_or_owned_delivery_blocks(db):
    cfg=Config(local=True,discord='',discord_fallback=False)
    worker=DeliveryWorker(db,cfg)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:pytest.fail('No network expected'))) as client:
            await worker.tick(client)
            with db.tx() as c:
                assert db.get(c,'health:discord:options_leaps')['status']=='inactive'
                assert db.get(c,'health:discord:smoothers')['status']=='externally_managed'
                db.put(c,'native:ownership:smoothers',{'owner':'compass'})
            add(db,'options_leaps')
            await worker.tick(client)
            with db.tx() as c:
                assert db.get(c,'health:discord:options_leaps')['status']=='not_configured'
                assert db.get(c,'health:discord:smoothers')['status']=='not_configured'
    asyncio.run(run())
