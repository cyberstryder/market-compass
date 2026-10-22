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


def make_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'archive-trigger.db')
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.delenv('ARCHIVE_BUCKET', raising=False)
    cfg = Config()
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
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
    try:
        now = time.time()
        seed(db, True, last_run=now - 60, heartbeat_at=now - 30)
        r = client.get('/api/admin/archive-trigger')
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is False
        assert 'already running' in body['error']
    finally:
        client.__exit__(None, None, None)


def test_stale_flag_cleared_and_worker_starts(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        now = time.time()
        # The 2026-10-22 shape: flag set hours ago, heartbeat frozen
        # with it (thread killed by a redeploy).
        seed(db, True, last_run=now - 7200, heartbeat_at=now - 7140)
        r = client.get('/api/admin/archive-trigger')
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True, body
        assert body['started'] is True
    finally:
        client.__exit__(None, None, None)


def test_no_flag_starts_normally(tmp_path, monkeypatch):
    client, db = make_client(tmp_path, monkeypatch)
    try:
        seed(db, False)
        r = client.get('/api/admin/archive-trigger')
        assert r.status_code == 200
        assert r.json()['ok'] is True
    finally:
        client.__exit__(None, None, None)
