"""DB maintenance endpoint repair (2026-10-30).

Three defects, three pins:

1. Connection credentials. The job's raw psycopg connection must be
   built from the app's own parsed engine URL. The first version passed
   str(engine.url) — which MASKS the password as '***' — and got
   'password authentication failed' (Oct 12, cce5a06); the Oct 13 fix
   re-read DATABASE_URL and hand-edited its scheme string, parsing the
   secret a second, divergent way. _maintenance_connect_kwargs derives
   host/port/user/password/dbname from the already-parsed URL, so the
   raw connection authenticates with exactly what the engine uses.

2. Stale running flag. The flag is cleared only by the worker thread's
   finally; the 2026-10-30 13:24Z run was killed mid-index by that
   morning's redeploys and reported running:true for the rest of the
   day, blocking every re-trigger. The thread now heartbeats, and both
   the trigger and the log view treat a flag with a stale (or absent)
   heartbeat as dead — the archive trigger's 2026-10-22 pattern.

3. Auth alignment. POST + log GET are cron-token gated like the other
   admin cron endpoints (CRON_TOKEN_PATHS), so the job can be triggered
   and monitored without a human console session.
"""
import time

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from compass.app import (CRON_TOKEN_ENDPOINTS, CRON_TOKEN_PATHS,
                         _maintenance_connect_kwargs, create_app)
from compass.config import Config
from compass.store import Store

CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
POSTGRES_URL = ('postgresql://postgres:p%40ss%3Aw%23rd'
                '@db.internal:5433/railway')
DECODED_PASSWORD = 'p@ss:w#rd'  # percent-decoded exactly as the engine sees it


# ---------- connection construction ----------

def test_connect_kwargs_match_the_app_parsed_url():
    kwargs = _maintenance_connect_kwargs(make_url(POSTGRES_URL))
    assert kwargs == {'host': 'db.internal', 'port': 5433,
                      'user': 'postgres', 'password': DECODED_PASSWORD,
                      'dbname': 'railway'}


def test_connect_kwargs_never_the_masked_rendering():
    # str(url) renders the password as '***' (the Oct 12 bug); the
    # kwargs must carry the real decoded value, not the rendering.
    url = make_url(POSTGRES_URL)
    assert '***' in str(url)
    assert _maintenance_connect_kwargs(url)['password'] == DECODED_PASSWORD


def test_connect_kwargs_omits_absent_parts():
    kwargs = _maintenance_connect_kwargs(
        make_url('postgresql://postgres:pw@db.internal/railway'))
    assert 'port' not in kwargs  # psycopg default applies
    assert kwargs['host'] == 'db.internal'


def test_connect_kwargs_rejects_non_postgres():
    with pytest.raises(ValueError):
        _maintenance_connect_kwargs(make_url('sqlite:///compass.db'))


# ---------- auth alignment ----------

def test_maintenance_paths_are_cron_token_locked():
    for path in ('/api/admin/db-maintenance', '/api/admin/db-maintenance-log'):
        assert path in CRON_TOKEN_PATHS
        assert ('GET', path) in CRON_TOKEN_ENDPOINTS
        assert ('POST', path) in CRON_TOKEN_ENDPOINTS


# ---------- endpoint behaviour ----------

def make_client(tmp_path, monkeypatch, token=CRON):
    db_path = str(tmp_path / 'maint.db')
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
    db = Store(cfg.db)
    db.initialize()
    app = create_app(cfg)
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    return client, db, app


def seed_state(db, **rows):
    with db.tx() as c:
        for key, value in rows.items():
            db.put(c, 'db:maintenance:' + key, value)


def read_state(db, key):
    with db.tx() as c:
        return db.get(c, 'db:maintenance:' + key, None)


def test_endpoints_reject_missing_and_wrong_token(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        assert client.post('/api/admin/db-maintenance').status_code == 401
        assert client.get('/api/admin/db-maintenance-log').status_code == 401
        bad = {'Authorization': 'Bearer not-the-token'}
        assert client.post('/api/admin/db-maintenance',
                           headers=bad).status_code == 401
        assert client.get('/api/admin/db-maintenance-log',
                          headers=bad).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_endpoints_503_when_token_unconfigured(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch, token=None)
    try:
        assert client.post('/api/admin/db-maintenance').status_code == 503
        assert client.get('/api/admin/db-maintenance-log').status_code == 503
    finally:
        client.__exit__(None, None, None)


def test_fresh_run_still_blocks_trigger(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        now = time.time()
        seed_state(db, running=True, started_at=now - 30, heartbeat=now - 10)
        r = client.post('/api/admin/db-maintenance', headers=AUTH)
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
        body = client.get('/api/admin/db-maintenance-log',
                          headers=AUTH).json()
        assert body['running'] is False
        assert body['running_flag'] is True
        # Live: fresh heartbeat -> running.
        seed_state(db, heartbeat=now)
        body = client.get('/api/admin/db-maintenance-log',
                          headers=AUTH).json()
        assert body['running'] is True
        # Legacy fossil shape (pre-heartbeat rows have no timestamps).
        with db.tx() as c:
            db.delete(c, 'db:maintenance:started_at')
            db.delete(c, 'db:maintenance:heartbeat')
        body = client.get('/api/admin/db-maintenance-log',
                          headers=AUTH).json()
        assert body['running'] is False
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
        if 'pg_indexes' in self.conn.last_sql:
            return (1,) if self.conn.index_exists else None
        if 'reloptions' in self.conn.last_sql:
            return ('{fillfactor=70,autovacuum_vacuum_scale_factor=0.01,'
                    'autovacuum_vacuum_cost_limit=1000}',)
        return None


class FakeConn:
    def __init__(self, index_exists):
        self.index_exists = index_exists
        self.executed = []
        self.last_sql = ''
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def run_job_to_completion(client, db, monkeypatch, index_exists):
    """POST the trigger against a fake psycopg and wait out the thread."""
    conn = FakeConn(index_exists)
    calls = {}

    def fake_connect(**kwargs):
        calls.update(kwargs)
        return conn

    monkeypatch.setattr(psycopg, 'connect', fake_connect)
    r = client.post('/api/admin/db-maintenance', headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()['started'] is True
    deadline = time.time() + 10
    while time.time() < deadline:
        if read_state(db, 'running') is False and \
                read_state(db, 'last_result') is not None:
            break
        time.sleep(0.05)
    else:
        raise AssertionError('maintenance thread did not finish')
    return conn, calls


def point_app_engine_at_postgres(app):
    # The app's Store keeps serving state from its sqlite pool (the
    # pool is built at construction); only .url — the value the
    # maintenance job reads — is swapped for a postgres URL.
    app.state.db.engine.url = make_url(POSTGRES_URL)


def test_stale_fossil_trigger_runs_job_with_engine_credentials(
        tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        point_app_engine_at_postgres(app)
        # The exact 2026-10-30 fossil: flag set by a killed thread, an
        # auth-shaped last_error, no heartbeat keys at all.
        seed_state(db, running=True,
                   last_error='password authentication failed (fossil)')
        conn, calls = run_job_to_completion(client, db, monkeypatch,
                                            index_exists=False)
        # Credentials: derived from the app engine URL, decoded.
        assert calls['autocommit'] is True
        assert calls['host'] == 'db.internal'
        assert calls['port'] == 5433
        assert calls['user'] == 'postgres'
        assert calls['password'] == DECODED_PASSWORD
        assert calls['dbname'] == 'railway'
        # Job steps, in order, unchanged.
        sql = conn.executed
        assert sql[0] == 'VACUUM (ANALYZE) events;'
        assert any('pg_indexes' in s for s in sql)
        assert any('CREATE INDEX CONCURRENTLY' in s for s in sql)
        assert any('ALTER TABLE state SET (fillfactor=70' in s for s in sql)
        assert any('reloptions' in s for s in sql)
        assert sql[-1] == 'VACUUM (ANALYZE) state;'
        # State: completed clean, fossil error cleared at start and
        # never replaced, flag cleared by the thread itself.
        assert read_state(db, 'last_result')['ok'] is True
        assert read_state(db, 'last_error') is None
        log = read_state(db, 'log')
        assert any('VACUUM complete' in line for line in log)
        assert any('Index creation complete' in line for line in log)
        assert any('state reloptions now:' in line for line in log)
        assert any('DB maintenance complete' in line for line in log)
    finally:
        client.__exit__(None, None, None)


def test_existing_index_is_skipped_not_rebuilt(tmp_path, monkeypatch):
    client, db, app = make_client(tmp_path, monkeypatch)
    try:
        point_app_engine_at_postgres(app)
        conn, calls = run_job_to_completion(client, db, monkeypatch,
                                            index_exists=True)
        assert not any('CREATE INDEX CONCURRENTLY' in s
                       for s in conn.executed)
        log = read_state(db, 'log')
        assert any('already exists, skipping' in line for line in log)
        assert read_state(db, 'last_result')['ok'] is True
    finally:
        client.__exit__(None, None, None)
