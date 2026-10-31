"""Archive trigger stale-flag guard (2026-10-22 incident fix).

The trigger's DB running flag is cleared only by the worker thread's
finally block. A redeploy that kills the thread mid-copy orphans the
flag, and every later trigger bounces off "Archive worker already
running" forever. The guard now treats a run as dead when its copy
heartbeat (and its start time) are more than 900s old, clears the flag,
and starts fresh — while a genuinely live worker (fresh heartbeat)
still blocks concurrent triggers.
"""
import time

from fastapi.testclient import TestClient

from compass.config import Config
from compass.store import Store
from compass.app import create_app
CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}


def make_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'archive-trigger.db')
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.delenv('ARCHIVE_BUCKET', raising=False)
    cfg = Config()
    cfg.cron_token = CRON  # 2026-10-23: locked endpoints always require the bearer
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    # Role 'web' + seed-before-start (below) are the fix for the
    # intermittent "database is locked" failures at seed() (bit
    # 2026-10-31, CI run 41174152077 and local repro). With the default
    # role 'all', TestClient.__enter__ runs the lifespan, which spawns
    # the collectors, the engine and ~25 background loops — all writing
    # the same sqlite file through the app's own Store. Those writers
    # held the sqlite write lock past the 30s busy timeout (the app's
    # own heartbeat and survivors-seed writes failed with the same
    # error in the captured log), so the test's seed INSERT starved.
    # Role 'web' spawns only the heartbeat loop — the standard pattern
    # for TestClient tests in this suite — and the archive-trigger
    # route under test does not depend on role. Seeding before
    # __enter__ (the test_readiness_poison_health.py pattern) removes
    # the remaining race: no app writer exists at seed time.
    cfg.role = 'web'
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    return client, db


def seed(db, running, last_run=None, heartbeat_at=None):
    with db.tx() as c:
        db.put(c, 'archive:worker:running', running)
        if last_run is not None:
            db.put(c, 'archive:worker:last_run', last_run)
        if heartbeat_at is not None:
            db.put(c, 'copy-archive:heartbeat',
                   {'at': heartbeat_at, 'chunk': 'quote/2026-W41',
                    'phase': 'copy_running', 'rows_so_far': 0})


def test_fresh_worker_still_blocks(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    now = time.time()
    seed(db, True, last_run=now - 60, heartbeat_at=now - 30)
    with client:
        r = client.get('/api/admin/archive-trigger', headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is False
        assert 'already running' in body['error']


def test_stale_flag_cleared_and_worker_starts(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    now = time.time()
    # The 2026-10-22 shape: flag set hours ago, heartbeat frozen
    # with it (thread killed by a redeploy).
    seed(db, True, last_run=now - 7200, heartbeat_at=now - 7140)
    with client:
        r = client.get('/api/admin/archive-trigger', headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True, body
        assert body['started'] is True


def test_no_flag_starts_normally(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    seed(db, False)
    with client:
        r = client.get('/api/admin/archive-trigger', headers=AUTH)
        assert r.status_code == 200
        assert r.json()['ok'] is True
