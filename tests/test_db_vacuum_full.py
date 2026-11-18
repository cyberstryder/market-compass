"""DB vacuum-full endpoint (2026-11-18, Josh's approved disk release).

A sibling of db-maintenance, deliberately separate: VACUUM FULL rewrites
the events table under an ACCESS EXCLUSIVE lock, so it gets its own
trigger, its own state keys (db:vacuum-full:*), and its own log view —
it can never collide with the db-maintenance job's flag, heartbeat, or
log. The job does exactly three statements on a raw autocommit psycopg
connection built from the app's parsed engine URL: measure
pg_total_relation_size('events'), VACUUM (FULL, ANALYZE) events, measure
again; last_result carries both sizes. Auth is the cron token, same as
db-maintenance (CRON_TOKEN_PATHS).
"""
import time

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from compass.app import CRON_TOKEN_ENDPOINTS, CRON_TOKEN_PATHS, create_app
from compass.config import Config
from compass.store import Store

CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
POSTGRES_URL = ('postgresql://postgres:p%40ss%3Aw%23rd'
                '@db.internal:5433/railway')
DECODED_PASSWORD = 'p@ss:w#rd'  # percent-decoded exactly as the engine sees it
BEFORE_BYTES = 306_169_495_552
AFTER_BYTES = 41_000_000_000


# ---------- auth alignment ----------

def test_vacuum_full_paths_are_cron_token_locked():
    for path in ('/api/admin/db-vacuum-full', '/api/admin/db-vacuum-full-log'):
        assert path in CRON_TOKEN_PATHS
        assert ('GET', path) in CRON_TOKEN_ENDPOINTS
        assert ('POST', path) in CRON_TOKEN_ENDPOINTS


# ---------- endpoint behaviour ----------

def make_client(tmp_path, monkeypatch, token=CRON):
    db_path = str(tmp_path / 'vacfull.db')
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    if token is None:
        monkeypatch.delenv('COMPASS_CRON_TOKEN', raising=False)
    else:
        monkeypatch.setenv('COMPASS_CRON_TOKEN', token)
    cfg = Config()
    cfg.cron_token = token
    cfg.db = 'sqlite:///' + db_path
    cfg.password = 'test-password-12345678'
    cfg.local = True
    # Role 'web', same as test_db_maintenance: startup spawns only the
    # heartbeat loop, so handler-spawned job threads are the only writers
    # and the sqlite file never starves on seed-time locks.
    cfg.role = 'web'
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client, db, app


def seed_state(db, **rows):
    with db.tx() as c:
        for key, value in rows.items():
            db.put(c, 'db:vacuum-full:' + key, value)


def read_state(db, key):
    with db.tx() as c:
        return db.get(c, 'db:vacuum-full:' + key, None)


def test_endpoints_reject_missing_and_wrong_token(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        assert client.post('/api/admin/db-vacuum-full').status_code == 401
        assert client.get('/api/admin/db-vacuum-full-log').status_code == 401
        bad = {'Authorization': 'Bearer not-the-token'}
        assert client.post('/api/admin/db-vacuum-full',
                           headers=bad).status_code == 401
        assert client.get('/api/admin/db-vacuum-full-log',
                          headers=bad).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_endpoints_503_when_token_unconfigured(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch, token=None)
    try:
        assert client.post('/api/admin/db-vacuum-full').status_code == 503
        assert client.get('/api/admin/db-vacuum-full-log').status_code == 503
    finally:
        client.__exit__(None, None, None)


def test_fresh_run_still_blocks_trigger(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        now = time.time()
        seed_state(db, running=True, started_at=now - 30, heartbeat=now - 10)
        r = client.post('/api/admin/db-vacuum-full', headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is False
        assert 'already running' in body['error']
    finally:
        client.__exit__(None, None, None)


def test_log_reports_effective_running(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        now = time.time()
        # Fossil: flag set, heartbeat long dead -> not running.
        seed_state(db, running=True, started_at=now - 7200,
                   heartbeat=now - 7140)
        body = client.get('/api/admin/db-vacuum-full-log',
                          headers=AUTH).json()
        assert body['running'] is False
        assert body['running_flag'] is True
        # Live: fresh heartbeat -> running.
        seed_state(db, heartbeat=now)
        body = client.get('/api/admin/db-vacuum-full-log',
                          headers=AUTH).json()
        assert body['running'] is True
    finally:
        client.__exit__(None, None, None)


def test_vacuum_full_state_is_isolated_from_db_maintenance(
        tmp_path, monkeypatch):
    # A live db-maintenance run must not block (or be blocked by) the
    # vacuum-full job: separate flags, separate heartbeats.
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        now = time.time()
        with db.tx() as c:
            db.put(c, 'db:maintenance:running', True)
            db.put(c, 'db:maintenance:started_at', now - 30)
            db.put(c, 'db:maintenance:heartbeat', now - 10)
        body = client.get('/api/admin/db-vacuum-full-log',
                          headers=AUTH).json()
        assert body['running'] is False
        assert body['running_flag'] is False
    finally:
        client.__exit__(None, None, None)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.conn.last_sql = ' '.join(sql.split())
        self.conn.executed.append(self.conn.last_sql)

    def fetchone(self):
        if 'pg_total_relation_size' in self.conn.last_sql:
            return (self.conn.sizes.pop(0),)
        return None


class FakeConn:
    def __init__(self, sizes):
        self.sizes = list(sizes)
        self.executed = []
        self.last_sql = ''
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def test_job_measures_vacuums_and_records_sizes(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        # The app's Store keeps serving state from its sqlite pool; only
        # .url — the value the job reads for its raw connection — is
        # swapped for a postgres URL (the test_db_maintenance pattern).
        app.state.db.engine.url = make_url(POSTGRES_URL)
        conn = FakeConn([BEFORE_BYTES, AFTER_BYTES])
        calls = {}

        def fake_connect(**kwargs):
            calls.update(kwargs)
            return conn

        monkeypatch.setattr(psycopg, 'connect', fake_connect)
        r = client.post('/api/admin/db-vacuum-full', headers=AUTH)
        assert r.status_code == 200, r.text
        assert r.json()['started'] is True
        deadline = time.time() + 10
        while time.time() < deadline:
            if read_state(db, 'running') is False and \
                    read_state(db, 'last_result') is not None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError('vacuum-full thread did not finish')
        # Credentials: derived from the app engine URL, decoded.
        assert calls['autocommit'] is True
        assert calls['host'] == 'db.internal'
        assert calls['port'] == 5433
        assert calls['user'] == 'postgres'
        assert calls['password'] == DECODED_PASSWORD
        assert calls['dbname'] == 'railway'
        # Exactly three statements, in order: size, VACUUM FULL, size.
        # No other table, no other statement.
        assert conn.executed == [
            "SELECT pg_total_relation_size('events');",
            'VACUUM (FULL, ANALYZE) events;',
            "SELECT pg_total_relation_size('events');",
        ]
        assert conn.closed is True
        # State: sizes recorded, flag cleared by the thread itself.
        result = read_state(db, 'last_result')
        assert result['ok'] is True
        assert result['before_bytes'] == BEFORE_BYTES
        assert result['after_bytes'] == AFTER_BYTES
        assert read_state(db, 'last_error') is None
        log = read_state(db, 'log')
        assert any('VACUUM FULL complete' in line for line in log)
        assert any(f'events size after: {AFTER_BYTES} bytes' in line
                   for line in log)
        # The log view serves the same result.
        body = client.get('/api/admin/db-vacuum-full-log',
                          headers=AUTH).json()
        assert body['running'] is False
        assert body['last_result']['before_bytes'] == BEFORE_BYTES
        assert body['last_result']['after_bytes'] == AFTER_BYTES
    finally:
        client.__exit__(None, None, None)
