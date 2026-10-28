"""Prune events rows ONLY where the COPY archive holds a verified R2 copy.

This is the prune half of compass.copy_archive, written separately from the
legacy compass.archive_prune (which gates on the retired archive system's
schedule frontier and must never be pointed at copy-archive data).

Gate design (Josh's rule, 2026-10-19: verify R2 has the data, THEN prune):
- A (kind, week) is prunable only when copy_archive's progress record exists
  with rows > 0 and a sha256.
- Verification happens HERE, at prune time, not at upload time: the R2 object
  is downloaded again and its sha256 recomputed; it must equal the progress
  record before a single row is deleted.
- The live row count for the week must equal the archived row count exactly.
  A mismatch means rows were written after archiving (late/backfilled data);
  the week is REFUSED and reported, never silently destroyed.
- Deletion is batched and resumable; per-week prune state lives in
  copy-archive:prune:<kind>:<week> so an interrupted run never re-verifies
  mid-delete or double-counts.
- The completion recount (deleted == expected, certifying remaining == 0
  before a week may flip to 'done') is sliced and banked the same way:
  a count timeout stalls the week visibly (action='recount_pending')
  but can never wedge it silently behind a fieldless error.
"""
import hashlib
import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from . import copy_archive as ca
from .store import events

PRUNE_PREFIX = 'copy-archive:prune:'
DAY = 86400
# Verification (streaming ~2 GB R2 hash + exact count over tens of
# millions of rows) can outlast any HTTP connection — the first live W38
# attempts died at the connection timeout twice, restarting from zero
# each time because 'verified' saved only at the very end (2026-10-19).
# prune_next therefore runs verification on a daemon thread: it waits
# inline up to PRUNE_VERIFY_INLINE_SECONDS (small weeks behave exactly
# as before), then returns action='verifying' while the thread banks
# the outcome in prune state. The chunk lock keeps concurrent calls
# from double-verifying or double-deleting; contenders get 'busy'.
_CHUNK_LOCK = threading.Lock()
_VERIFY_THREADS = {}
_VERIFY_INLINE_SECONDS = float(
    os.environ.get('PRUNE_VERIFY_INLINE_SECONDS', '120'))
# Deletion walks the primary key in probed ranges and deletes by
# explicit id list (see _prune_next_locked). _DELETE_SUB_BATCH caps one
# delete statement; _DELETE_PROBE caps one id-range probe.
_DELETE_SUB_BATCH = int(os.environ.get('PRUNE_DELETE_SUB_BATCH', '5000'))
_DELETE_PROBE = int(os.environ.get('PRUNE_DELETE_PROBE', '50000'))
_DELETE_CALL_SECONDS = float(
    os.environ.get('PRUNE_DELETE_CALL_SECONDS', '40'))
# Kinds the copy-archive pump deliberately does not walk (see run_copy_chunk).
# They can never become prunable (no progress records), which coverage shows.
PUMP_SKIPPED_KINDS = ('chain_archive', 'matrix_raw', 'vendor_flow', 'flow',
                      'alert_delivery')


def _kinds():
    return list(ca.RETENTION.keys()) + list(ca.AUDIT_KINDS)


def _week_start(period_str):
    """Timestamp of the Monday 00:00 UTC starting an ISO week label."""
    year, week = period_str.split('-W')
    dt = datetime.fromisocalendar(int(year), int(week), 1)
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _all_copy_state(db):
    with db.tx() as c:
        return db.prefix(c, 'copy-archive:')


def _progress_records(state):
    """{(kind, period): record} for real archive progress entries only."""
    out = {}
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        parts = key.split(':')
        if len(parts) != 3:  # copy-archive:<kind>:<period>
            continue
        out[(parts[1], parts[2])] = rec
    return out


def _prune_states(state):
    out = {}
    for key, rec in state.items():
        if key.startswith(PRUNE_PREFIX) and isinstance(rec, dict):
            parts = key[len(PRUNE_PREFIX):].split(':')
            if len(parts) == 2:
                out[(parts[0], parts[1])] = rec
    return out


def _set_prune_state(db, kind, period_str, value):
    with db.tx() as c:
        db.put(c, f"{PRUNE_PREFIX}{kind}:{period_str}", value)


_HASH_CHUNK = 64 * 1024 * 1024  # 64 MiB ranged reads


def _hash_object(client, bucket, key):
    """Hash an R2 object via ranged GETs with per-chunk retries.

    Failure history: a whole-object read OOM'd the request
    (2026-10-19); plain streaming then truncated mid-object on the
    2.58 GB W39 week ('863894364 read, but total bytes expected is
    2580627842'). Ranged chunks bound the blast radius — a dropped
    connection loses one 64 MiB chunk, which is retried, never the
    whole object."""
    size = int(client.head_object(Bucket=bucket, Key=key)['ContentLength'])
    hasher = hashlib.sha256()
    offset = 0
    while offset < size:
        end = min(size - 1, offset + _HASH_CHUNK - 1)
        want = end - offset + 1
        data = None
        for _attempt in range(3):
            resp = client.get_object(Bucket=bucket, Key=key,
                                     Range=f'bytes={offset}-{end}')
            body = resp['Body']
            try:
                buf = body.read()
            finally:
                body.close()
            if len(buf) == want:
                data = buf
                break
        if data is None:
            raise IOError(f'ranged read failed at {offset}-{end} of {key}')
        hasher.update(data)
        offset += want
    return hasher.hexdigest(), size


_COUNT_SLICE_SECONDS = 1 * 3600  # 168 slices per week


def _count_rows(db, kind, lo, hi):
    """Exact count as a sum of per-slice counts, each in its own
    transaction. A single COUNT over a big week (21M+ rows) outlived
    every timeout production enforces — statement_timeout first, then
    transaction_timeout, and SET LOCAL lifts of both did not take
    effect in the deployed service (2026-10-19/20). Slices were six
    hours until W42 (2026-10-26/27): its densest six-hour slice still
    outlived the 30s statement timeout and the week refused with
    verify_error, so slices are one hour (~125k rows each, seconds per
    transaction); the sum is exactly the week's count, so the gate
    loses no strictness."""
    total = 0
    t = lo
    while t < hi:
        nxt = min(hi, t + _COUNT_SLICE_SECONDS)
        with db.tx() as c:
            total += c.execute(
                select(func.count()).select_from(events).where(
                    events.c.kind == kind, events.c.ts >= t,
                    events.c.ts < nxt)).scalar() or 0
        t = nxt
    return total


# The completion recount uses one-hour slices (168 per week). It runs
# over a range the delete walk has just emptied, where the index still
# carries the week's dead entries, and a six-hour kind+ts COUNT there
# outlived the production statement timeout (2026-10-24, see
# _completion_recount).
_RECOUNT_SLICE_SECONDS = float(
    os.environ.get('PRUNE_RECOUNT_SLICE_SECONDS', '3600'))


def _bank_recount(db, kind, pstr, st, nxt, total):
    st['recount_next_ts'] = nxt
    st['recount_total'] = total
    st['updated_at'] = time.time()
    _set_prune_state(db, kind, pstr, st)


def _completion_recount(db, kind, pstr, st, ws, we, deadline):
    """Resumable exact recount of a week's live rows for the completion
    gate (deleted >= expected_rows). Returns the remaining count when
    the recount completes, or None while it is still pending.

    The recount this replaces called _count_rows once, all-or-nothing,
    AFTER the final deletions were already banked. On quote/2026-W40 a
    slice COUNT over the just-emptied range died on the statement
    timeout, the exception escaped prune_next into the route's
    fieldless error body, and every retry re-ran the recount from zero
    and banked nothing — the week wedged at 'deleting' with
    deleted == expected and, selection being oldest-week-first,
    blocked the whole prune queue behind it (2026-10-24). So the
    recount now mirrors the delete walk: one-hour slices of the SAME
    kind + ts-range predicate _count_rows uses (via _count_rows
    itself, one internal slice per call — the completed sum is exactly
    the week's live count, so the gate loses no strictness), progress
    banked in prune state (recount_next_ts / recount_total) after
    every slice, bounded by the caller's deadline. A slice that raises
    is caught here: the last banked progress stays, recount_error
    notes the failure, and the caller returns a fielded
    'recount_pending' — a recount failure must never escape prune_next
    or masquerade as the fieldless error body again. State rows banked
    before this fix carry no recount fields; missing fields mean a
    fresh recount from the week start."""
    nxt = st.get('recount_next_ts')
    total = st.get('recount_total')
    if nxt is None or total is None or not ws <= nxt <= we:
        nxt, total = ws, 0
    while nxt < we:
        if time.time() >= deadline:
            break
        slice_hi = min(we, nxt + _RECOUNT_SLICE_SECONDS)
        try:
            n = _count_rows(db, kind, nxt, slice_hi)
        except Exception as e:
            st['recount_error'] = str(e)[:300]
            break
        total += n
        nxt = slice_hi
        st.pop('recount_error', None)
        try:
            _bank_recount(db, kind, pstr, st, nxt, total)
        except Exception as e:
            st['recount_error'] = f'recount bank failed: {e}'[:300]
            break
    if nxt >= we:
        return total
    # Pending: bank where the recount stopped (covers the
    # deadline-before-first-slice and slice-failure exits; a no-op
    # rewrite when the loop already banked this exact point).
    try:
        _bank_recount(db, kind, pstr, st, nxt, total)
    except Exception:
        pass
    return None


def coverage(db, now=None):
    """Per-kind archive coverage: what fraction of archivable weeks hold a
    verified progress record, and what has already been pruned."""
    now = now or time.time()
    state = _all_copy_state(db)
    progress = _progress_records(state)
    pruned = _prune_states(state)
    out = {'kinds': {}, 'ready': True, 'at': now}
    totals = {'periods': 0, 'archived': 0, 'data_periods': 0,
              'empty_periods': 0, 'rows_archived': 0, 'bytes_archived': 0,
              'missing': 0, 'pruned_periods': 0, 'rows_pruned': 0}
    for kind in _kinds():
        periods = ca._periods_to_archive(kind, now)
        entry = {'periods': len(periods), 'archived': 0, 'data_periods': 0,
                 'empty_periods': 0, 'rows_archived': 0, 'bytes_archived': 0,
                 'missing': [], 'pruned_periods': 0, 'rows_pruned': 0,
                 'pump_skipped': kind in PUMP_SKIPPED_KINDS}
        for ws, _we in periods:
            pstr = ca._week_str(ws)
            rec = progress.get((kind, pstr))
            if not rec:
                entry['missing'].append(pstr)
                continue
            entry['archived'] += 1
            if rec.get('rows', 0) > 0:
                entry['data_periods'] += 1
                entry['rows_archived'] += rec['rows']
                entry['bytes_archived'] += rec.get('bytes', 0)
            else:
                entry['empty_periods'] += 1
            st = pruned.get((kind, pstr))
            if st and st.get('status') == 'done':
                entry['pruned_periods'] += 1
                entry['rows_pruned'] += st.get('deleted', 0)
        for k in ('periods', 'archived', 'data_periods', 'empty_periods',
                  'rows_archived', 'bytes_archived', 'pruned_periods',
                  'rows_pruned'):
            totals[k] += entry[k]
        totals['missing'] += len(entry['missing'])
        if entry['missing'] and not entry['pump_skipped']:
            out['ready'] = False
        out['kinds'][kind] = entry
    out['totals'] = totals
    return out


def verify_sample(db, client, bucket, per_kind=2):
    """Independent readback: re-download a sample of archived objects from
    R2 and recompute sha256 against the progress records."""
    state = _all_copy_state(db)
    progress = _progress_records(state)
    by_kind = {}
    for (kind, pstr), rec in progress.items():
        if rec.get('rows', 0) > 0 and rec.get('sha256'):
            by_kind.setdefault(kind, []).append((pstr, rec))
    samples, failed = [], []
    for kind in sorted(by_kind):
        items = sorted(by_kind[kind])
        picks = items[:per_kind]
        if len(items) > per_kind:
            picks += items[-per_kind:]
        seen = set()
        for pstr, rec in picks:
            if pstr in seen:
                continue
            seen.add(pstr)
            entry = {'kind': kind, 'period': pstr, 'ok': False}
            try:
                digest, size = _hash_object(
                    client, bucket, f"copy-archive/{kind}/{pstr}.jsonl.gz")
                entry['ok'] = (digest == rec['sha256']
                               and size == rec.get('bytes', size))
                if not entry['ok']:
                    entry['error'] = 'sha256_or_size_mismatch'
            except Exception as e:
                entry['error'] = str(e)[:200]
            samples.append(entry)
            if not entry['ok']:
                failed.append(entry)
    return {'ok': not failed, 'checked': len(samples), 'failed': failed,
            'samples': samples}


def prune_status(db):
    state = _all_copy_state(db)
    pruned = _prune_states(state)
    done = [(k, p, s) for (k, p), s in pruned.items()
            if s.get('status') == 'done']
    refused = [{'kind': k, 'period': p, 'reason': s.get('reason'),
                'detail': s.get('detail'), 'attempts': s.get('attempts')}
               for (k, p), s in pruned.items() if s.get('status') == 'refused']
    active = [{'kind': k, 'period': p, 'status': s.get('status'),
               'deleted': s.get('deleted', 0),
               'expected_rows': s.get('expected_rows')}
              for (k, p), s in pruned.items()
              if s.get('status') in ('verified', 'deleting', 'verifying')]
    return {'periods_done': len(done),
            'rows_deleted': sum(s.get('deleted', 0) for _k, _p, s in done),
            'refused': refused, 'active': active}


# Transient infrastructure failures (a canceled count, an R2 read blip)
# are NOT evidence about the archive's integrity, so they may be
# re-verified — up to a cap, after which the refusal sticks like any
# other. Evidence refusals (sha_mismatch, count_mismatch,
# final_count_mismatch) are sticky from the first occurrence.
RETRYABLE_REFUSALS = ('verify_error', 'r2_read_failed')
MAX_VERIFY_ATTEMPTS = 5


def _refuse(db, kind, pstr, reason, detail):
    prev = _prune_states(_all_copy_state(db)).get((kind, pstr), {})
    attempts = int(prev.get('attempts', 0)) + 1
    _set_prune_state(db, kind, pstr,
                     {'status': 'refused', 'reason': reason,
                      'detail': detail, 'attempts': attempts,
                      'at': time.time()})
    return {'ok': False, 'action': 'refused', 'kind': kind, 'period': pstr,
            'reason': reason, 'detail': detail}


def rearm_verification(db, kind, pstr):
    """Deliberately re-arm ONE cap-exhausted retryable refusal.

    Josh's decision 2026-10-26: a code path, not a state edit. The
    W38/W39 refusals were banked under pre-fix verification code (a
    full-week count that timed out before counting was sliced; a
    whole-object hash read that truncated before ranged reads), and
    with attempts >= MAX_VERIFY_ATTEMPTS the candidate filter in
    prune_next never selects them again — the weeks are wedged even
    though the code that failed them no longer exists.

    Re-arming resets ONLY the attempt counter on the named entry. The
    week goes back through the untouched prune-time gate (R2 re-hash +
    exact live count in _verify_week) before a single row is deleted,
    and a fresh refusal under current code banks against the fresh
    budget exactly like any other week. Evidence refusals
    (sha_mismatch, count_mismatch, final_count_mismatch) are NEVER
    re-armed here: they are facts about the archive, not about the
    code that happened to run. Non-refused entries are untouched."""
    st = _prune_states(_all_copy_state(db)).get((kind, pstr))
    base = {'kind': kind, 'period': pstr}
    if st is None:
        return {'ok': False, 'action': 'rearm_ignored', **base,
                'detail': 'no prune state for this kind/period'}
    if st.get('status') != 'refused':
        return {'ok': False, 'action': 'rearm_ignored', **base,
                'detail': f"status is {st.get('status')!r}, not 'refused'"}
    if st.get('reason') not in RETRYABLE_REFUSALS:
        return {'ok': False, 'action': 'rearm_ignored', **base,
                'detail': f"refusal reason {st.get('reason')!r} is an "
                          'evidence refusal and is never re-armed'}
    attempts = int(st.get('attempts', 0))
    if attempts < MAX_VERIFY_ATTEMPTS:
        return {'ok': True, 'action': 'already_eligible', **base,
                'attempts': attempts}
    new = dict(st)
    new['attempts'] = 0
    new['rearmed_at'] = time.time()
    new['rearmed_from_attempts'] = attempts
    new['rearm_count'] = int(st.get('rearm_count', 0)) + 1
    _set_prune_state(db, kind, pstr, new)
    return {'ok': True, 'action': 'rearmed', **base,
            'attempts_before': attempts, 'reason': st.get('reason')}


def _verify_week(db, client, bucket, kind, pstr, rec, ws, we, now):
    """The prune gate, unchanged: re-hash the R2 object NOW (not trusting
    the upload-time check) and require the live row count to equal the
    archived count exactly. Banks 'verified' or a sticky refusal.
    Returns True iff verified."""
    try:
        digest, size = _hash_object(
            client, bucket, f"copy-archive/{kind}/{pstr}.jsonl.gz")
    except Exception as e:
        _refuse(db, kind, pstr, 'r2_read_failed', str(e)[:300])
        return False
    if digest != rec['sha256'] or size != rec.get('bytes', -1):
        _refuse(db, kind, pstr, 'sha_mismatch',
                {'record_sha256': rec['sha256'], 'r2_sha256': digest,
                 'record_bytes': rec.get('bytes'), 'r2_bytes': size})
        return False
    live = _count_rows(db, kind, ws, we)
    if live != rec['rows']:
        _refuse(db, kind, pstr, 'count_mismatch',
                {'db_rows': live, 'archived_rows': rec['rows']})
        return False
    _set_prune_state(db, kind, pstr,
                     {'status': 'verified', 'expected_rows': rec['rows'],
                      'verified_sha256': digest, 'deleted': 0,
                      'verified_at': now})
    return True


def _verify_worker(db, client, bucket, kind, pstr, rec, ws, we, now):
    try:
        _verify_week(db, client, bucket, kind, pstr, rec, ws, we, now)
    except Exception as e:  # never leave a week stuck in 'verifying'
        try:
            _refuse(db, kind, pstr, 'verify_error', str(e)[:300])
        except Exception:
            pass
    finally:
        _VERIFY_THREADS.pop((kind, pstr), None)


def _row_at_or_after(db, id_floor):
    with db.tx() as c:
        row = c.execute(select(events.c.id, events.c.ts)
                        .where(events.c.id >= id_floor)
                        .order_by(events.c.id).limit(1)).first()
    return (row[0], row[1]) if row else None


def _first_id_with_ts(db, target_ts, lo, hi):
    """Smallest id in [lo, hi] whose ts >= target_ts, or hi+1 if none.

    Binary search over the PK; each step is one indexed single-row
    probe. Assumes ts is near-monotonic in id (insert order) — late or
    backfilled stragglers violate it locally, which the caller's
    full-pass fallback and the exact final count absorb."""
    ans = hi + 1
    l, r = lo, hi
    while l <= r:
        mid = (l + r) // 2
        row = _row_at_or_after(db, mid)
        if row is None:
            break
        rid, rts = row
        if rid > hi:
            r = mid - 1
        elif (rts or 0) >= target_ts:
            ans = rid
            r = rid - 1
        else:
            l = rid + 1
    return ans


def _id_band(db, ws, we):
    """[lo, hi) id range holding the week's rows: lo is the first id
    with ts >= ws, hi the first id with ts >= we (hi = max_id+1 when
    the week runs to the table's end)."""
    with db.tx() as c:
        lo = c.execute(select(func.min(events.c.id))).scalar()
        hi = c.execute(select(func.max(events.c.id))).scalar()
    if lo is None or hi is None:
        return (0, 0)
    return (_first_id_with_ts(db, ws, lo, hi),
            _first_id_with_ts(db, we, lo, hi))


def prune_next(db, client, bucket, now=None, row_budget=50000):
    """Verify (once) and delete one batch of the oldest prunable week.

    Returns a summary dict; 'idle' when nothing prunable remains. Refusals
    are sticky per week and never block other weeks. Verification runs on
    a background thread (see module note): a call that kicks one off
    waits inline briefly, then returns action='verifying'; a call that
    finds another chunk in flight returns action='busy'."""
    if not _CHUNK_LOCK.acquire(blocking=False):
        return {'ok': True, 'action': 'busy'}
    try:
        return _prune_next_locked(db, client, bucket, now=now,
                                  row_budget=row_budget)
    finally:
        _CHUNK_LOCK.release()


def _prune_next_locked(db, client, bucket, now=None, row_budget=50000):
    now = now or time.time()
    state = _all_copy_state(db)
    progress = _progress_records(state)
    pruned = _prune_states(state)
    candidates = []
    for (kind, pstr), rec in progress.items():
        if rec.get('rows', 0) <= 0 or not rec.get('sha256'):
            continue
        st = pruned.get((kind, pstr))
        if st and st.get('status') == 'done':
            continue
        if st and st.get('status') == 'refused' and not (
                st.get('reason') in RETRYABLE_REFUSALS
                and int(st.get('attempts', 1)) < MAX_VERIFY_ATTEMPTS):
            continue
        candidates.append((pstr, kind, rec, st))
    if not candidates:
        return {'ok': True, 'action': 'idle', **prune_status(db)}
    candidates.sort()  # oldest ISO week first
    pstr, kind, rec, st = candidates[0]
    ws = _week_start(pstr)
    we = ws + 7 * DAY

    if not st or st.get('status') in ('verifying', 'refused'):
        # 'refused' here means a retryable refusal under its attempt cap
        # (the candidate filter already excluded the sticky ones).
        thread = _VERIFY_THREADS.get((kind, pstr))
        if thread is not None and thread.is_alive():
            return {'ok': True, 'action': 'verifying',
                    'kind': kind, 'period': pstr}
        # No state yet, or an orphaned 'verifying' whose thread died
        # with a previous process: (re-)kick verification. The attempt
        # count carries over so retryable refusals can exhaust their cap.
        _set_prune_state(db, kind, pstr,
                         {'status': 'verifying', 'started_at': now,
                          'attempts': int(st.get('attempts', 0)) if st else 0,
                          'expected_rows': rec['rows']})
        thread = threading.Thread(
            target=_verify_worker,
            args=(db, client, bucket, kind, pstr, rec, ws, we, now),
            daemon=True)
        _VERIFY_THREADS[(kind, pstr)] = thread
        thread.start()
        thread.join(timeout=_VERIFY_INLINE_SECONDS)
        if thread.is_alive():
            return {'ok': True, 'action': 'verifying',
                    'kind': kind, 'period': pstr}
        st = _prune_states(_all_copy_state(db)).get((kind, pstr))
        if not st or st.get('status') != 'verified':
            return {'ok': False, 'action': 'refused', 'kind': kind,
                    'period': pstr, 'reason': (st or {}).get('reason'),
                    'detail': (st or {}).get('detail')}

    # Delete by walking the primary key. Every statement here is
    # index-true by construction — an id-range probe or a delete by
    # explicit id list — because the two previous shapes both died on
    # production's session limits (statement_timeout 30s,
    # transaction_timeout 60s, store.py): the single-transaction 50k
    # delete (kind+ts subquery, ORDER BY id) was killed and rolled back
    # on every call, and even a 5k sub-batch of the same shape hit the
    # 30s statement timeout — the (kind, symbol, ts) index cannot serve
    # a kind+ts range ordered by id, so the planner scans/sorts on a
    # 164M-row table (diagnosed 2026-10-20). The probe advances a
    # cursor banked in prune state; the cursor only ever moves past
    # rows whose fate is decided, so a call cut short by its budget or
    # deadline resumes exactly where it stopped.
    st = dict(st)
    deleted_before = st.get('deleted', 0)
    cursor = int(st.get('delete_cursor') or 0)
    # Start the walk at the week's id band, not the table's first row:
    # a cursor from 0 spends whole calls traversing older weeks' id
    # space before reaching a single matching row (observed live
    # 2026-10-21: full 40s calls deleting 0). The band is computed once
    # and banked; when it is exhausted short of the expected count the
    # walk falls back to a full pass from 0 for stragglers.
    band_hi = st.get('band_hi')
    band_done = bool(st.get('band_done'))
    if band_hi is None and not band_done and not cursor:
        band_lo, band_hi = _id_band(db, ws, we)
        st['band_lo'] = band_lo
        st['band_hi'] = band_hi
        cursor = max(0, band_lo - 1)
    n_all = 0
    deadline = time.time() + _DELETE_CALL_SECONDS
    probe_exhausted = False
    while n_all < row_budget and time.time() < deadline:
        probe_q = (select(events.c.id, events.c.kind, events.c.ts)
                   .where(events.c.id > cursor)
                   .order_by(events.c.id).limit(_DELETE_PROBE))
        if band_hi and not band_done:
            probe_q = probe_q.where(events.c.id < band_hi)
        with db.tx() as c:
            rows = c.execute(probe_q).all()
        if not rows:
            if band_hi and not band_done:
                if st['deleted'] >= st['expected_rows']:
                    break
                band_done = True
                st['band_done'] = True
                cursor = 0
                st['delete_cursor'] = 0
                _set_prune_state(db, kind, pstr, st)
                continue
            probe_exhausted = True
            break
        last_probe_id = rows[-1][0]
        matches = [r[0] for r in rows
                   if r[1] == kind and ws <= (r[2] or 0) < we]
        if not matches:
            cursor = last_probe_id
            continue
        pos = 0
        stop = False
        while pos < len(matches):
            if n_all >= row_budget or time.time() >= deadline:
                stop = True
                break
            take = min(_DELETE_SUB_BATCH, row_budget - n_all)
            chunk = matches[pos:pos + take]
            with db.tx() as c:
                n = c.execute(delete(events).where(
                    events.c.id.in_(chunk))).rowcount or 0
            n_all += n
            # Rows already gone count as decided, not as deletions —
            # either way the cursor may pass them.
            pos += n if n > 0 else len(chunk)
            cursor = matches[pos - 1] if pos else cursor
            st['deleted'] = deleted_before + n_all
            st['delete_cursor'] = cursor
            st['updated_at'] = time.time()
            st['status'] = 'deleting'
            _set_prune_state(db, kind, pstr, st)
        if pos >= len(matches):
            cursor = last_probe_id
        if stop:
            break
    st['delete_cursor'] = 0 if (probe_exhausted
                                and st['deleted'] < st['expected_rows']
                                ) else cursor
    st['updated_at'] = time.time()
    st['status'] = 'deleting'
    _set_prune_state(db, kind, pstr, st)
    if st['deleted'] >= st['expected_rows']:
        remaining = _completion_recount(db, kind, pstr, st, ws, we,
                                        deadline)
        if remaining is None:
            # Recount incomplete (deadline reached or a slice failed):
            # progress is banked in prune state and the week stays
            # 'deleting'; the driver sees a fielded pending instead of
            # the route's fieldless error body, and the next call
            # resumes the recount where this one stopped.
            return {'ok': True, 'action': 'recount_pending',
                    'kind': kind, 'period': pstr,
                    'deleted_this_call': n_all,
                    'deleted_total': st['deleted'],
                    'expected_rows': st['expected_rows'],
                    'status': st['status'],
                    'recount_next_ts': st.get('recount_next_ts'),
                    'recount_total': st.get('recount_total'),
                    'recount_error': st.get('recount_error')}
        if remaining == 0 and st['deleted'] == st['expected_rows']:
            st['status'] = 'done'
        else:
            st['status'] = 'refused'
            st['reason'] = 'final_count_mismatch'
            st['detail'] = {'deleted': st['deleted'],
                            'expected_rows': st['expected_rows'],
                            'remaining': remaining}
        _set_prune_state(db, kind, pstr, st)
    return {'ok': st['status'] != 'refused', 'action': 'pruned_batch',
            'kind': kind, 'period': pstr, 'deleted_this_call': n_all,
            'deleted_total': st['deleted'],
            'expected_rows': st['expected_rows'], 'status': st['status']}
