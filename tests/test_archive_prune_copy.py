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

    def head_object(self, Bucket, Key):
        return {'ContentLength': len(self.objects[Key])}

    def get_object(self, Bucket, Key, Range=None):
        data = self.objects[Key]
        if Range:
            start, end = Range[len('bytes='):].split('-')
            data = data[int(start):int(end) + 1]
        return {'Body': io.BytesIO(data)}

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


def test_prune_deletes_in_sub_batches_within_one_call(db, s3, monkeypatch):
    """Regression (2026-10-20): production's single-transaction 50k
    delete outlived the 60s session transaction_timeout and rolled
    back forever (verified weeks stuck at deleted=0). Deletion now runs
    in sub-batch transactions; one call must span several of them and
    bank the cumulative progress."""
    monkeypatch.setattr(apc, '_DELETE_SUB_BATCH', 2)
    ts = NOW - 400 * DAY
    for i in range(5):
        add_event(db, 'bar', ts + i * 60)
    archive_week(db, s3, 'bar', ts, rows=5)
    r1 = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=3)
    assert r1['deleted_this_call'] == 3 and r1['status'] == 'deleting'
    active = apc.prune_status(db)['active']
    assert len(active) == 1 and active[0]['deleted'] == 3  # banked mid-call
    r2 = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r2['deleted_this_call'] == 2 and r2['status'] == 'done'
    assert count_kind(db, 'bar') == 0


def test_prune_walks_the_weeks_id_band_not_the_whole_table(db, s3):
    """The delete walk starts at the week's id band (binary-searched
    from timestamps) instead of id 0, and leaves other weeks' rows —
    before and after the band — untouched."""
    old = NOW - 500 * DAY
    for i in range(3):
        add_event(db, 'bar', old + i * 60)  # an older week, lower ids
    ts = NOW - 400 * DAY
    for i in range(4):
        add_event(db, 'bar', ts + i * 60)   # the archived week
    add_event(db, 'bar', NOW - DAY)         # newer week, higher ids
    pstr = archive_week(db, s3, 'bar', ts, rows=4)
    r = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r['deleted_this_call'] == 4 and r['status'] == 'done'
    assert count_kind(db, 'bar') == 4  # 3 old + 1 new survive
    states = apc._prune_states(apc._all_copy_state(db))
    assert states[('bar', pstr)].get('band_lo') is not None


def test_prune_band_straggler_is_caught_by_full_pass_fallback(db, s3):
    """A row carrying an in-week ts at an id ABOVE the band (a late
    backfill) is missed by the band walk; the fallback full pass must
    still find it so the week completes instead of refusing."""
    ts = NOW - 400 * DAY
    for i in range(3):
        add_event(db, 'bar', ts + i * 60)
    for i in range(3):
        add_event(db, 'bar', NOW - DAY + i * 60)  # newer rows push ids up
    add_event(db, 'bar', ts + 30)  # straggler: in-week ts, highest id
    archive_week(db, s3, 'bar', ts, rows=4)
    r = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r['deleted_this_call'] == 4 and r['status'] == 'done'
    assert count_kind(db, 'bar') == 3


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


def test_hash_object_retries_truncated_ranged_reads():
    """W39 live (2026-10-20): the R2 stream truncated at 863 MB of
    2.58 GB and the whole verify died. Ranged chunks must retry a
    short read and still produce the exact digest."""
    body = b'x' * 1000 + b'y' * 1000
    flaky = FakeS3()
    flaky.objects['k'] = body
    real_get = flaky.get_object
    calls = {'n': 0}

    def truncated_first(Bucket, Key, Range=None):
        calls['n'] += 1
        out = real_get(Bucket, Key, Range=Range)
        if calls['n'] == 1:
            return {'Body': io.BytesIO(out['Body'].read()[:-10])}
        return out

    flaky.get_object = truncated_first
    digest, size = apc._hash_object(flaky, BUCKET, 'k')
    assert size == len(body)
    assert digest == hashlib.sha256(body).hexdigest()
    assert calls['n'] == 2


def test_count_slice_default_is_one_hour():
    """W42 (2026-10-26/27): the densest six-hour verify slice outlived
    the 30s statement timeout and the week refused with verify_error.
    The production slice is one hour, like the completion recount's."""
    assert apc._COUNT_SLICE_SECONDS == 3600


def test_count_rows_slices_sum_exactly(db, monkeypatch):
    """The week count is a sum of slice counts; boundary rows must
    be counted exactly once."""
    monkeypatch.setattr(apc, '_COUNT_SLICE_SECONDS', 3600)
    lo = 1759000000.0
    hi = lo + 3 * 3600
    for ts in (lo, lo + 3599, lo + 3600, hi - 1, hi, lo - 1):
        add_event(db, 'bar', ts)
    assert apc._count_rows(db, 'bar', lo, hi) == 4
    assert apc._count_rows(db, 'bar', lo, lo + 3600) == 2


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


def test_rearm_rearms_cap_exhausted_retryable_refusal(db, s3, monkeypatch):
    """A retryable refusal that exhausted its attempt cap (the W38/W39
    wedge: refusals banked under since-fixed verify code) becomes
    eligible again ONLY through the deliberate re-arm, and then still
    has to pass the full gate before anything is deleted."""
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    pstr = archive_week(db, s3, 'bar', ts, rows=1)
    real_count = apc._count_rows

    def always_fail(db_, kind, lo, hi):
        raise RuntimeError('database is gone')

    monkeypatch.setattr(apc, '_count_rows', always_fail)
    for _ in range(apc.MAX_VERIFY_ATTEMPTS):
        r = apc.prune_next(db, s3, BUCKET, now=NOW)
        assert r['ok'] is False and r['reason'] == 'verify_error'
    assert apc.prune_next(db, s3, BUCKET, now=NOW)['action'] == 'idle'
    refused = apc.prune_status(db)['refused']
    assert refused[0]['attempts'] == apc.MAX_VERIFY_ATTEMPTS

    r = apc.rearm_verification(db, 'bar', pstr)
    assert r['ok'] is True and r['action'] == 'rearmed'
    assert r['attempts_before'] == apc.MAX_VERIFY_ATTEMPTS
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['status'] == 'refused' and st['attempts'] == 0
    assert count_kind(db, 'bar') == 1  # re-arm itself deletes nothing

    monkeypatch.setattr(apc, '_count_rows', real_count)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['deleted_this_call'] == 1 and r['status'] == 'done'
    assert count_kind(db, 'bar') == 0


def test_rearm_after_rearm_refusal_banks_against_fresh_budget(db, s3,
                                                            monkeypatch):
    """A re-armed week that refuses again under current code is not
    wedged open: the fresh refusal increments from zero."""
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    pstr = archive_week(db, s3, 'bar', ts, rows=1)

    def always_fail(db_, kind, lo, hi):
        raise RuntimeError('still gone')

    monkeypatch.setattr(apc, '_count_rows', always_fail)
    for _ in range(apc.MAX_VERIFY_ATTEMPTS):
        apc.prune_next(db, s3, BUCKET, now=NOW)
    assert apc.rearm_verification(db, 'bar', pstr)['action'] == 'rearmed'
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['reason'] == 'verify_error'
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['status'] == 'refused' and st['attempts'] == 1


def test_rearm_never_rearms_evidence_refusal(db, s3):
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    pstr = archive_week(db, s3, 'bar', ts, rows=1)
    s3.objects[f'copy-archive/bar/{pstr}.jsonl.gz'] = b'tampered'
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['reason'] == 'sha_mismatch'
    r = apc.rearm_verification(db, 'bar', pstr)
    assert r['ok'] is False and r['action'] == 'rearm_ignored'
    assert apc.prune_next(db, s3, BUCKET, now=NOW)['action'] == 'idle'
    assert count_kind(db, 'bar') == 1


def test_rearm_ignores_non_refused_and_unknown(db, s3):
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    pstr = archive_week(db, s3, 'bar', ts, rows=1)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['status'] == 'done'
    r = apc.rearm_verification(db, 'bar', pstr)
    assert r['ok'] is False and r['action'] == 'rearm_ignored'
    r = apc.rearm_verification(db, 'bar', '1999-W01')
    assert r['ok'] is False and r['action'] == 'rearm_ignored'


def test_rearm_already_eligible_is_a_noop(db, s3, monkeypatch):
    ts = NOW - 400 * DAY
    add_event(db, 'bar', ts)
    pstr = archive_week(db, s3, 'bar', ts, rows=1)

    def fail_once_factory():
        real_count = apc._count_rows
        calls = {'n': 0}

        def flaky(db_, kind, lo, hi):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('one blip')
            return real_count(db_, kind, lo, hi)
        return flaky

    monkeypatch.setattr(apc, '_count_rows', fail_once_factory())
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['reason'] == 'verify_error'
    r = apc.rearm_verification(db, 'bar', pstr)
    assert r['ok'] is True and r['action'] == 'already_eligible'
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['attempts'] == 1  # unchanged: it was never wedged


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


def seed_deleting(db, kind, pstr, deleted, expected, **extra):
    """Bank a mid-deletion prune state directly (verification already
    passed in an earlier life), as the pre-fix code left W40."""
    st = {'status': 'deleting', 'deleted': deleted,
          'expected_rows': expected, 'band_done': True,
          'delete_cursor': 0}
    st.update(extra)
    with db.tx() as c:
        db.put(c, f'copy-archive:prune:{kind}:{pstr}', st)
    return st


def test_completion_recount_failure_banks_progress_and_resumes(
        db, s3, monkeypatch):
    """W40 live (2026-10-24): the completion recount's COUNT died on
    the statement timeout, the exception escaped prune_next into the
    route's fieldless error body, and every retry re-ran the recount
    from zero and banked nothing — the week wedged at 'deleting' with
    deleted == expected, blocking the whole queue. A slice failure
    must now surface as a fielded recount_pending with progress
    banked, and the next call must resume from the banked cursor."""
    ts = NOW - 400 * DAY
    pstr = archive_week(db, s3, 'bar', ts, rows=5)
    ws = apc._week_start(pstr)
    seed_deleting(db, 'bar', pstr, deleted=5, expected=5)  # rows gone
    real_count = apc._count_rows
    calls = {'n': 0, 'fail': True}

    def flaky_count(db_, kind, lo, hi):
        calls['n'] += 1
        if calls['fail'] and calls['n'] == 3:
            raise RuntimeError(
                'canceling statement due to statement timeout')
        return real_count(db_, kind, lo, hi)

    monkeypatch.setattr(apc, '_count_rows', flaky_count)
    r1 = apc.prune_next(db, s3, BUCKET, now=NOW)  # must not raise
    assert r1['ok'] is True and r1['action'] == 'recount_pending'
    assert r1['kind'] == 'bar' and r1['period'] == pstr
    assert r1['status'] == 'deleting'
    assert 'statement timeout' in r1['recount_error']
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['status'] == 'deleting'
    assert st['recount_next_ts'] == ws + 2 * 3600  # two slices banked
    assert st['recount_total'] == 0
    assert 'statement timeout' in st['recount_error']
    calls['fail'] = False
    r2 = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r2['action'] == 'pruned_batch' and r2['status'] == 'done'
    assert r2['deleted_this_call'] == 0
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['status'] == 'done'
    assert apc.prune_status(db)['rows_deleted'] == 5


def test_completion_recount_with_remaining_rows_refuses(db, s3):
    """The gate's strictness is unchanged: a completed recount that
    finds live rows banks the final_count_mismatch refusal with the
    same detail shape as before the fix. The seeded delete_cursor
    sits past the surviving rows' ids (a straggler geometry the walk
    has already passed), so the recount is the instrument that
    catches them."""
    ts = NOW - 400 * DAY
    pstr = archive_week(db, s3, 'bar', ts, rows=5)
    ws = apc._week_start(pstr)
    add_event(db, 'bar', ws + 60)
    add_event(db, 'bar', ws + 3600 + 60)  # second recount slice
    seed_deleting(db, 'bar', pstr, deleted=5, expected=5,
                  delete_cursor=10 ** 9)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['status'] == 'refused'
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['reason'] == 'final_count_mismatch'
    assert st['detail'] == {'deleted': 5, 'expected_rows': 5,
                            'remaining': 2}
    assert count_kind(db, 'bar') == 2  # a refusal never deletes


def test_completion_recount_with_deleted_over_expected_refuses(db, s3):
    """deleted may overshoot expected when rows landed between verify
    and deletion; a completed recount of 0 remaining must still
    refuse rather than bless the week."""
    ts = NOW - 400 * DAY
    pstr = archive_week(db, s3, 'bar', ts, rows=5)
    seed_deleting(db, 'bar', pstr, deleted=6, expected=5)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['ok'] is False and r['status'] == 'refused'
    st = apc._prune_states(apc._all_copy_state(db))[('bar', pstr)]
    assert st['reason'] == 'final_count_mismatch'
    assert st['detail'] == {'deleted': 6, 'expected_rows': 5,
                            'remaining': 0}


def test_pre_fix_w40_state_completes_via_recount(db, s3):
    """W40's exact live shape (2026-10-24): prune state banked by the
    pre-fix code — status 'deleting', deleted == expected_rows, band
    fields present, NO recount fields — with zero live rows left. The
    week must complete through the new recount path in one call."""
    ts = NOW - 400 * DAY
    pstr = archive_week(db, s3, 'bar', ts, rows=5)
    seed_deleting(db, 'bar', pstr, deleted=5, expected=5,
                  band_lo=1, band_hi=100, band_done=False)
    r = apc.prune_next(db, s3, BUCKET, now=NOW)
    assert r['status'] == 'done' and r['deleted_this_call'] == 0
    status = apc.prune_status(db)
    assert status['periods_done'] == 1 and status['rows_deleted'] == 5


def test_band_timeout_falls_back_to_full_pass(db, s3, monkeypatch):
    """Regression (2026-10-28): the synchronous _id_band never returned
    on the production table, wedging every prune call before a single
    row was deleted. The band is now resolved on a budgeted thread;
    on timeout the week is marked band_failed and the delete walk
    proceeds as a banked full pass in the SAME call."""
    import threading as _t
    monkeypatch.setattr(apc, '_BAND_INLINE_SECONDS', 0.2)
    monkeypatch.setattr(apc, '_BAND_RESULTS', {})
    monkeypatch.setattr(apc, '_BAND_THREADS', {})
    gate = _t.Event()
    monkeypatch.setattr(apc, '_id_band',
                        lambda db_, ws, we: (gate.wait(30), (0, 0))[1])
    ts = NOW - 400 * DAY
    for i in range(4):
        add_event(db, 'bar', ts + i * 60)
    add_event(db, 'bar', NOW - DAY)  # different week: untouchable
    pstr = archive_week(db, s3, 'bar', ts, rows=4)
    r = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r['deleted_this_call'] == 4 and r['status'] == 'done'
    assert count_kind(db, 'bar') == 1
    states = apc._prune_states(apc._all_copy_state(db))
    assert states[('bar', pstr)].get('band_failed') is True
    gate.set()


def test_band_result_adopted_without_recompute(db, s3, monkeypatch):
    """A band landed by an earlier thread is adopted from _BAND_RESULTS;
    _id_band must not run again."""
    monkeypatch.setattr(apc, '_BAND_THREADS', {})
    def explode(db_, ws, we):
        raise AssertionError('_id_band must not be called')
    monkeypatch.setattr(apc, '_id_band', explode)
    old = NOW - 500 * DAY
    for i in range(3):
        add_event(db, 'bar', old + i * 60)   # ids 1-3, older week
    ts = NOW - 400 * DAY
    for i in range(4):
        add_event(db, 'bar', ts + i * 60)    # ids 4-7, archived week
    add_event(db, 'bar', NOW - DAY)          # id 8, newer week
    pstr = archive_week(db, s3, 'bar', ts, rows=4)
    monkeypatch.setattr(apc, '_BAND_RESULTS', {('bar', pstr): (4, 8)})
    r = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r['deleted_this_call'] == 4 and r['status'] == 'done'
    assert count_kind(db, 'bar') == 4
    states = apc._prune_states(apc._all_copy_state(db))
    assert states[('bar', pstr)]['band_lo'] == 4
    assert states[('bar', pstr)]['band_hi'] == 8


def test_nonmatching_probe_banks_cursor(db, s3, monkeypatch):
    """A probe that finds no matches must bank its cursor advance, so
    a call cut short mid-traversal resumes past decided id space
    instead of re-probing it (previously only deletions banked)."""
    monkeypatch.setattr(apc, '_DELETE_PROBE', 2)
    writes = []
    orig = apc._set_prune_state
    def rec(db_, kind, pstr, value):
        writes.append((value.get('delete_cursor'), value.get('deleted')))
        return orig(db_, kind, pstr, value)
    monkeypatch.setattr(apc, '_set_prune_state', rec)
    ts = NOW - 400 * DAY
    for i in range(2):
        add_event(db, 'quote', ts + i * 60)  # ids 1-2: in-week ts, wrong kind
    for i in range(2):
        add_event(db, 'bar', ts + i * 60)    # ids 3-4: the archived week
    archive_week(db, s3, 'bar', ts, rows=2)
    r = apc.prune_next(db, s3, BUCKET, now=NOW, row_budget=50)
    assert r['deleted_this_call'] == 2 and r['status'] == 'done'
    first_delete = next(i for i, (_, d) in enumerate(writes) if d)
    assert (2, 0) in writes[:first_delete]  # banked before any deletion
