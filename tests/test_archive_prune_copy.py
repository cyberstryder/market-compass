"""Tests for compass/archive_prune_copy.py — the R2-verified prune gate.

Throwaway SQLite + in-memory fake S3 only. The refusals are the point:
nothing may be deleted unless the R2 object re-hashes to the archive
progress record AND the live row count matches it exactly.
"""
import hashlib
import io

import pytest
from sqlalchemy import func, select

from compass import archive_prune_copy as apc
from compass import copy_archive as ca
from compass.store import Store, events

NOW = 1759000000.0  # 2025-09-27
DAY = 86400
BUCKET = 'test-bucket'


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body.read() if hasattr(Body, 'read') else Body


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'prune-copy.db'))
    store.initialize()
    yield store
    store.engine.dispose()


@pytest.fixture
def s3():
    return FakeS3()


def add_event(db, kind, ts):
    with db.tx() as c:
        assert db.append(c, kind, 'test', kind + str(ts), ts, {'v': 1})


def count_kind(db, kind):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(events)
                         .where(events.c.kind == kind)).scalar()


def archive_week(db, s3, kind, any_ts, rows, body=None):
    """Write a copy-archive progress record + matching R2 object for the
    week containing any_ts. Returns the period label."""
    pstr = ca._week_str(any_ts)
    body = body if body is not None else f'archived-{kind}-{pstr}'.encode()
    s3.objects[f'copy-archive/{kind}/{pstr}.jsonl.gz'] = body
    with db.tx() as c:
        db.put(c, f'copy-archive:{kind}:{pstr}',
               {'rows': rows, 'bytes': len(body),
                'sha256': hashlib.sha256(body).hexdigest(), 'at': NOW})
    return pstr


def test_prune_deletes_in_batches_after_reverification(db, s3):
    ts = NOW - 400 * DAY
    for i in range(5):
        add_event(db, 'bar', ts + i * 60)
    add_event(db, 'bar', NOW - DAY)  # different week: untouchable
    archive_week(db, s3, 'bar', ts, rows=5)
    r1 = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=2)
    assert r1['deleted_this_call'] == 2 and r1['status'] == 'deleting'
    r2 = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=2)
    assert r2['deleted_this_call'] == 2
    r3 = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=2)
    assert r3['deleted_this_call'] == 1 and r3['status'] == 'done'
    assert count_kind(db, 'bar') == 1  # only the recent-week row survives
    status = apc.prune_status(db)
    assert status['periods_done'] == 1 and status['rows_deleted'] == 5


def test_prune_refuses_when_r2_object_was_tampered(db, s3):
    ts = NOW - 400 * DAY
    for i in range(3):
        add_event(db, 'bar', ts + i * 60)
    pstr = archive_week(db, s3, 'bar', ts, rows=3)
    s3.objects[f'copy-archive/bar/{pstr}.jsonl.gz'] = b'tampered-bytes'
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['reason'] == 'sha_mismatch'
    assert count_kind(db, 'bar') == 3
    # Refusal is sticky: the week is reported, not retried or skipped past.
    assert apc.prune_status(db)['refused'][0]['reason'] == 'sha_mismatch'


def test_prune_refuses_when_rows_were_written_after_archive(db, s3):
    ts = NOW - 400 * DAY
    for i in range(4):
        add_event(db, 'bar', ts + i * 60)
    archive_week(db, s3, 'bar', ts, rows=3)  # archive saw 3; a 4th landed late
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['reason'] == 'count_mismatch'
    assert r['detail'] == {'db_rows': 4, 'archived_rows': 3}
    assert count_kind(db, 'bar') == 4


def test_no_progress_record_means_no_prune(db, s3):
    add_event(db, 'bar', NOW - 400 * DAY)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['action'] == 'idle'
    assert count_kind(db, 'bar') == 1


def test_slow_verify_returns_verifying_then_deletes(db, s3, monkeypatch):
    """Live W38 (2026-10-19): hash+count outlasted the HTTP connection
    twice and restarted from zero. A verify that exceeds the inline
    budget must bank its result in state instead of dying with the
    request; the next call then deletes without re-verifying."""
    import time as _time
    ts = NOW - 400 * DAY
    for i in range(3):
        add_event(db, 'bar', ts + i * 60)
    archive_week(db, s3, 'bar', ts, rows=3)
    real_hash = apc._hash_object

    def slow_hash(client, bucket, key):
        _time.sleep(1.0)
        return real_hash(client, bucket, key)

    monkeypatch.setattr(apc, '_hash_object', slow_hash)
    monkeypatch.setattr(apc, '_VERIFY_INLINE_SECONDS', 0.2)
    r1 = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r1['action'] == 'verifying'
    assert count_kind(db, 'bar') == 3  # nothing deleted pre-verification
    deadline = _time.time() + 10
    while apc._VERIFY_THREADS and _time.time() < deadline:
        _time.sleep(0.05)
    assert not apc._VERIFY_THREADS
    states = apc._prune_states(apc._all_copy_state(db))
    pstr = ca._week_str(ts)
    assert states[('bar', pstr)]['status'] == 'verified'
    r2 = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r2['deleted_this_call'] == 3 and r2['status'] == 'done'
    assert count_kind(db, 'bar') == 0


def test_transient_verify_error_is_retried_then_recovers(db, s3, monkeypatch):
    """W38 live (2026-10-19): the exact count was canceled by Postgres
    statement_timeout and banked as a sticky refusal. A transient
    verify_error is not evidence about the archive — it must retry."""
    ts = NOW - 400 * DAY
    for i in range(3):
        add_event(db, 'bar', ts + i * 60)
    archive_week(db, s3, 'bar', ts, rows=3)
    real_count = apc._count_rows
    calls = {'n': 0}

    def flaky_count(db_, kind, lo, hi):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('canceling statement due to statement timeout')
        return real_count(db_, kind, lo, hi)

    monkeypatch.setattr(apc, '_count_rows', flaky_count)
    r1 = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r1['ok'] is False and r1['reason'] == 'verify_error'
    assert count_kind(db, 'bar') == 3  # nothing deleted
    r2 = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r2['deleted_this_call'] == 3 and r2['status'] == 'done'
    assert count_kind(db, 'bar') == 0


def test_transient_refusal_sticks_after_attempt_cap(db, s3, monkeypatch):
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    archive_week(db, s3, 'bar', ts, rows=1)

    def always_fail(db_, kind, lo, hi):
        raise RuntimeError('database is gone')

    monkeypatch.setattr(apc, '_count_rows', always_fail)
    for _ in range(apc.MAX_VERIFY_ATTEMPTS):
        r = apc.prune_next(db, s3, BUCKET, now=NOW)
        assert r['ok'] is False and r['reason'] == 'verify_error'
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['action'] == 'idle'  # cap reached: sticky, no more retries
    assert count_kind(db, 'bar') == 1


def test_concurrent_chunk_returns_busy(db, s3):
    assert apc._CHUNK_LOCK.acquire(blocking=False)
    try:
        r = apc.prune_next(db, s3, BUCKET, now=NOW)
    finally:
        apc._CHUNK_LOCK.release()
    assert r['action'] == 'busy'


def test_verify_sample_detects_tamper(db, s3):
    ts = NOW - 400 * DAY
    archive_week(db, s3, 'bar', ts, rows=3)
    good = apc.verify_sample(db, s3, BUCKET)
    assert good['ok'] is True and good['checked'] == 1
    pstr = ca._week_str(ts)
    s3.objects[f'copy-archive/bar/{pstr}.jsonl.gz'] = b'corrupted'
    bad = apc.verify_sample(db, s3, BUCKET)
    assert bad['ok'] is False and bad['failed'][0]['period'] == pstr


def test_coverage_reports_progress_and_gaps(db, s3):
    periods = ca._periods_to_archive('bar', NOW)
    assert periods
    ws, _we = periods[0]
    pstr = ca._week_str(ws)
    body = b'x' * 10
    with db.tx() as c:
        db.put(c, f'copy-archive:bar:{pstr}',
               {'rows': 3, 'bytes': 10,
                'sha256': hashlib.sha256(body).hexdigest(), 'at': NOW})
    cov = apc.coverage(db, now=NOW)
    bar = cov['kinds']['bar']
    assert bar['archived'] == 1 and bar['data_periods'] == 1
    assert bar['rows_archived'] == 3
    assert len(bar['missing']) == bar['periods'] - 1
    assert cov['ready'] is False  # other kinds have no coverage yet
