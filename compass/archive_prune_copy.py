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
"""
import hashlib
import time
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from . import copy_archive as ca
from .store import events

PRUNE_PREFIX = 'copy-archive:prune:'
DAY = 86400
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


def _hash_object(client, bucket, key):
    """Stream an R2 object through sha256 — never load it whole. Archived
    quote weeks are ~1.5 GB; the first live verify attempt read one into
    memory and the request died without a response (2026-10-19)."""
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp['Body']
    hasher = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = body.read(8 * 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    finally:
        body.close()
    return hasher.hexdigest(), total


def _count_rows(db, kind, lo, hi):
    with db.tx() as c:
        return c.execute(select(func.count()).select_from(events).where(
            events.c.kind == kind, events.c.ts >= lo,
            events.c.ts < hi)).scalar() or 0


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
                'detail': s.get('detail')}
               for (k, p), s in pruned.items() if s.get('status') == 'refused']
    active = [{'kind': k, 'period': p, 'status': s.get('status'),
               'deleted': s.get('deleted', 0),
               'expected_rows': s.get('expected_rows')}
              for (k, p), s in pruned.items()
              if s.get('status') in ('verified', 'deleting')]
    return {'periods_done': len(done),
            'rows_deleted': sum(s.get('deleted', 0) for _k, _p, s in done),
            'refused': refused, 'active': active}


def _refuse(db, kind, pstr, reason, detail):
    _set_prune_state(db, kind, pstr,
                     {'status': 'refused', 'reason': reason,
                      'detail': detail, 'at': time.time()})
    return {'ok': False, 'action': 'refused', 'kind': kind, 'period': pstr,
            'reason': reason, 'detail': detail}


def prune_next(db, client, bucket, now=None, row_budget=50000):
    """Verify (once) and delete one batch of the oldest prunable week.

    Returns a summary dict; 'idle' when nothing prunable remains. Refusals
    are sticky per week and never block other weeks."""
    now = now or time.time()
    state = _all_copy_state(db)
    progress = _progress_records(state)
    pruned = _prune_states(state)
    candidates = []
    for (kind, pstr), rec in progress.items():
        if rec.get('rows', 0) <= 0 or not rec.get('sha256'):
            continue
        st = pruned.get((kind, pstr))
        if st and st.get('status') in ('done', 'refused'):
            continue
        candidates.append((pstr, kind, rec, st))
    if not candidates:
        return {'ok': True, 'action': 'idle', **prune_status(db)}
    candidates.sort()  # oldest ISO week first
    pstr, kind, rec, st = candidates[0]
    ws = _week_start(pstr)
    we = ws + 7 * DAY

    if not st:
        # Verify against R2 NOW — not trusting the upload-time check.
        try:
            digest, size = _hash_object(
                client, bucket, f"copy-archive/{kind}/{pstr}.jsonl.gz")
        except Exception as e:
            return _refuse(db, kind, pstr, 'r2_read_failed', str(e)[:300])
        if digest != rec['sha256'] or size != rec.get('bytes', -1):
            return _refuse(db, kind, pstr, 'sha_mismatch',
                           {'record_sha256': rec['sha256'],
                            'r2_sha256': digest,
                            'record_bytes': rec.get('bytes'),
                            'r2_bytes': size})
        live = _count_rows(db, kind, ws, we)
        if live != rec['rows']:
            return _refuse(db, kind, pstr, 'count_mismatch',
                           {'db_rows': live, 'archived_rows': rec['rows']})
        st = {'status': 'verified', 'expected_rows': rec['rows'],
              'verified_sha256': digest, 'deleted': 0, 'verified_at': now}
        _set_prune_state(db, kind, pstr, st)

    with db.tx() as c:
        sub = (select(events.c.id)
               .where(events.c.kind == kind, events.c.ts >= ws,
                      events.c.ts < we)
               .order_by(events.c.id).limit(row_budget))
        n = c.execute(delete(events).where(events.c.id.in_(sub))).rowcount or 0
    st = dict(st)
    st['deleted'] = st.get('deleted', 0) + n
    st['updated_at'] = time.time()
    if st['deleted'] >= st['expected_rows']:
        remaining = _count_rows(db, kind, ws, we)
        if remaining == 0 and st['deleted'] == st['expected_rows']:
            st['status'] = 'done'
        else:
            st['status'] = 'refused'
            st['reason'] = 'final_count_mismatch'
            st['detail'] = {'deleted': st['deleted'],
                            'expected_rows': st['expected_rows'],
                            'remaining': remaining}
    else:
        st['status'] = 'deleting'
    _set_prune_state(db, kind, pstr, st)
    return {'ok': st['status'] != 'refused', 'action': 'pruned_batch',
            'kind': kind, 'period': pstr, 'deleted_this_call': n,
            'deleted_total': st['deleted'],
            'expected_rows': st['expected_rows'], 'status': st['status']}
