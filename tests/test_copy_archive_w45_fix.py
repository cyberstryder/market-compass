"""W45 COPY fix pins (2026-11-16).

Diagnosis (w45-quote-copy-diagnosis-2026-11-15): _export_month_locked set
'30min' transaction/statement timeouts on its own export session, and
quote/2026-W45's single-transaction COPY died at exactly 1,800.0s in every
attempt; the finally block's unguarded reset then raised "the connection
is lost" on the dead connection, REPLACING the real TransactionTimeout
in every run record.

These tests pin the three fix parts:
(a) the export session carries NO timeout cap ('0' = disabled), and the
    pool-default reset still runs at the end;
(b) a failing finally-block cleanup (rollback AND reset) never masks the
    primary COPY error;
(c) the pg_stat_activity wait sampler returns shaped samples and never
    raises; a heartbeat tick during COPY records samples in the
    heartbeat payload and the durable wait-samples key.

COPY is Postgres-only, so the raw DBAPI connection is faked; the state
side runs against a real sqlite Store.
"""
import io
import time

import pytest

from compass import copy_archive as ca
from compass.store import Store


class TransactionTimeout(Exception):
    """Stand-in for psycopg.errors.TransactionTimeout."""


class FakeCopy:
    def __init__(self, chunks, error=None):
        self._chunks = chunks
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for ch in self._chunks:
            yield ch
        if self._error is not None:
            raise self._error


class FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.statements = []
        self._pid_row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.statements.append(sql)
        if self.raw.fail_reset and ("'60s'" in sql or "'30s'" in sql):
            raise RuntimeError('the connection is lost')
        if 'pg_backend_pid' in sql:
            self._pid_row = (4242,)

    def fetchone(self):
        return self._pid_row

    def copy(self, sql):
        return FakeCopy(self.raw.chunks, self.raw.copy_error)


class FakeRaw:
    def __init__(self, chunks=(), copy_error=None, fail_reset=False,
                 fail_rollback=False):
        self.chunks = list(chunks)
        self.copy_error = copy_error
        self.fail_reset = fail_reset
        self.fail_rollback = fail_rollback
        self.commits = 0
        self.rollbacks = 0
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.fail_rollback:
            raise RuntimeError('the connection is lost')


class _DriverConn:
    def __init__(self, raw):
        self.driver_connection = raw


class FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class FakeConn:
    """Serves both the export connection (raw driver_connection) and the
    sampler's short query connection (execute -> pg_stat_activity row)."""

    def __init__(self, raw, sample_row):
        self.connection = _DriverConn(raw)
        self._sample_row = sample_row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt, params=None):
        return FakeResult(self._sample_row)


class FakeEngine:
    def __init__(self, raw, sample_row=('active', 'IO', 'DataFileRead')):
        self.raw = raw
        self.sample_row = sample_row

    def connect(self):
        return FakeConn(self.raw, self.sample_row)


class FakeDB:
    """tx/get/put/delete delegate to a real sqlite Store; engine is fake."""

    def __init__(self, store, raw, sample_row=('active', 'IO', 'DataFileRead')):
        self._store = store
        self.engine = FakeEngine(raw, sample_row)

    def tx(self):
        return self._store.tx()

    def get(self, c, key, default=None):
        return self._store.get(c, key, default)

    def put(self, c, key, value):
        return self._store.put(c, key, value)

    def delete(self, c, key):
        return self._store.delete(c, key)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body.read()

    def get_object(self, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}


@pytest.fixture
def store(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'w45.db'))
    s.initialize()
    yield s
    s.engine.dispose()


WEEK_START = 1762041600  # 2026-W45-ish epoch; exact value irrelevant here
WEEK_END = WEEK_START + 7 * 86400
PERIOD = '2026-W45'
PROGRESS = f'copy-archive:quote:{PERIOD}'


def test_export_session_has_no_timeout_cap(store):
    raw = FakeRaw(chunks=[b'{"a":1}\n', b'{"a":2}\n'])
    db = FakeDB(store, raw)
    rows, nbytes = ca._export_month_locked(
        db, FakeS3(), 'bkt', 'quote', WEEK_START, WEEK_END, PERIOD, PROGRESS)
    assert rows == 2 and nbytes > 0
    stmts = raw.cursor_obj.statements
    # (a) caps disabled on the export session; the 30-min cap is gone.
    assert "SET SESSION transaction_timeout = '0'" in stmts
    assert "SET SESSION statement_timeout = '0'" in stmts
    assert not any("'30min'" in s for s in stmts)
    # ...and the pool defaults are still restored before returning the
    # connection (reset values unchanged from before the fix).
    assert "SET SESSION transaction_timeout = '60s'" in stmts
    assert "SET SESSION statement_timeout = '30s'" in stmts
    with store.tx() as c:
        rec = store.get(c, PROGRESS)
    assert rec and rec['rows'] == 2


def test_finally_failure_never_masks_primary_error(store):
    primary = TransactionTimeout(
        'terminating connection due to transaction timeout')
    raw = FakeRaw(chunks=[b'{"a":1}\n'], copy_error=primary,
                  fail_reset=True, fail_rollback=True)
    db = FakeDB(store, raw)
    with pytest.raises(TransactionTimeout) as excinfo:
        ca._export_month_locked(
            db, FakeS3(), 'bkt', 'quote', WEEK_START, WEEK_END,
            PERIOD, PROGRESS)
    # The surfaced error is the COPY's own, not the cleanup's
    # "the connection is lost".
    assert excinfo.value is primary
    # The cleanup was still attempted (rollback + reset) despite failing.
    assert raw.rollbacks >= 1
    assert any("'60s'" in s for s in raw.cursor_obj.statements)


def test_sample_copy_waits_shapes_and_never_raises(store):
    raw = FakeRaw()
    db = FakeDB(store, raw, sample_row=('active', 'IO', 'DataFileRead'))
    s = ca._sample_copy_waits(db, 4242, 12345)
    assert s['state'] == 'active'
    assert s['wait_event_type'] == 'IO'
    assert s['wait_event'] == 'DataFileRead'
    assert s['rows_so_far'] == 12345
    # Backend gone -> explicit 'gone' sample, not an exception.
    db_gone = FakeDB(store, raw, sample_row=None)
    s2 = ca._sample_copy_waits(db_gone, 4242, 7)
    assert s2['state'] == 'gone' and s2['wait_event'] is None
    # No pid -> no sample.
    assert ca._sample_copy_waits(db, None, 0) is None

    class BoomEngine:
        def connect(self):
            raise RuntimeError('pool exhausted')

    class BoomDB:
        engine = BoomEngine()

    # Engine failure -> None, never propagates.
    assert ca._sample_copy_waits(BoomDB(), 4242, 0) is None


class _TickingTime:
    """time stand-in: every call jumps 31s so the first COPY chunk trips
    the >30s heartbeat branch immediately."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        self.t += 31.0
        return self.t


def test_heartbeat_tick_records_wait_samples(store, monkeypatch):
    monkeypatch.setattr(ca, 'time', _TickingTime())
    raw = FakeRaw(chunks=[b'{"a":1}\n'])
    db = FakeDB(store, raw, sample_row=('active', 'IO', 'DataFileRead'))
    rows, _ = ca._export_month_locked(
        db, FakeS3(), 'bkt', 'quote', WEEK_START, WEEK_END, PERIOD, PROGRESS)
    assert rows == 1
    with store.tx() as c:
        hb = store.get(c, 'copy-archive:heartbeat')
        durable = store.get(c, f'copy-archive:wait-samples:quote:{PERIOD}')
    samples = durable['samples']
    assert len(samples) >= 1
    assert samples[0]['wait_event_type'] == 'IO'
    assert samples[0]['wait_event'] == 'DataFileRead'
    assert samples[0]['rows_so_far'] == 1
    # The live heartbeat carries the same samples for archive-log polling.
    assert hb['wait_samples'][-1]['wait_event'] == 'DataFileRead'
    assert time.time() > 0  # real clock untouched outside ca
