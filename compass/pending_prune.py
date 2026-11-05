"""Terminal-pending retention pass (2026-11-04 cleanup).

Pending selection rows are never deleted by their lifecycle: they
transition waiting -> expired / observed / entered /
routed_to_book_path / paused and stay, carrying their full signal +
last_selection payloads. The population dates to 2026-09 and grows by
thousands of rows per session; it starved the chain pending lane once
(2026-11-04 lane repair), and every remaining full-prefix reader paid
for it daily until the readers moved to server-side predicates.

Retention rule (Josh's cleanup direction, 2026-11-04): terminal rows
older than RETENTION_DAYS are deleted; 'waiting' rows are never
touched at any age. Consumer audit behind the bound: daily_results
reads only the current session's research pendings, the option
selection audit reads the last 24 hours, scanner_shadow reads a
pending by a fresh signal id at record creation, and every
selection-path reader filters to 'waiting' server-side -- no consumer
needs terminal rows anywhere near 14 days old.

Mechanics follow the archive-prune wedge lesson: fixed-size batches,
each in its own transaction, progress banked to the
'maintenance:pending_prune' watermark row after every batch, and a
per-pass batch cap plus wall-clock budget so a pass is never an
unbounded phase. A pass that hits a cap leaves last_completed_at
unstamped and the Collector lane resumes it minutes later; once a
full sweep completes, later passes are no-ops for 24 hours. Every
pass that runs also mirrors its report into an append-only
'pending_prune' event, so the record is readable through the
cron-token events export.
"""
import time

from .store import PENDING_PREFIXES  # noqa: F401  (re-exported for callers)

RETENTION_DAYS = 14
BATCH = 500
MAX_BATCHES = 240
PASS_BUDGET_S = 120
WATERMARK = 'maintenance:pending_prune'


def _prunable_total(population):
    return sum(s['prunable'] for prefix in population.values()
               for s in prefix.values())


def run_pass(db, now, batch=BATCH, max_batches=MAX_BATCHES,
             budget_s=PASS_BUDGET_S):
    """One retention pass. Returns a report dict; a pass inside 24h
    of a completed sweep returns {'skipped': ...} without touching
    the population."""
    cutoff = now - RETENTION_DAYS * 86400
    with db.tx() as c:
        mark = db.get(c, WATERMARK, {}) or {}
    if mark.get('last_completed_at') and now - mark['last_completed_at'] < 86400:
        return {'skipped': 'sweep_completed_recently', 'watermark': mark}
    with db.tx() as c:
        before = db.pending_population(c, cutoff)
    deleted = 0
    batches = 0
    exhausted = False
    started = time.time()
    while batches < max_batches:
        with db.tx() as c:
            keys = db.prune_terminal_pendings_batch(c, cutoff, batch)
        if not keys:
            exhausted = True
            break
        deleted += len(keys)
        batches += 1
        # Bank progress after the batch commits, so an interrupted
        # pass never loses the count and never re-deletes.
        mark = {**mark, 'last_run_at': now, 'cutoff': cutoff,
                'retention_days': RETENTION_DAYS,
                'deleted_total': mark.get('deleted_total', 0) + len(keys),
                'last_pass_deleted': deleted,
                'population_before': before, 'status': 'in_progress'}
        with db.tx() as c:
            db.put(c, WATERMARK, mark)
        if len(keys) < batch:
            exhausted = True
            break
        if time.time() - started > budget_s:
            break
    with db.tx() as c:
        after = db.pending_population(c, cutoff)
    mark = {**mark, 'last_run_at': now, 'cutoff': cutoff,
            'retention_days': RETENTION_DAYS,
            'last_pass_deleted': deleted, 'population_before': before,
            'population_after': after,
            'status': 'complete' if exhausted else 'in_progress'}
    if exhausted:
        mark['last_completed_at'] = now
    report = {'deleted': deleted, 'batches': batches,
              'complete': exhausted, 'cutoff': cutoff,
              'retention_days': RETENTION_DAYS,
              'prunable_before': _prunable_total(before),
              'prunable_after': _prunable_total(after),
              'population_before': before, 'population_after': after}
    with db.tx() as c:
        db.put(c, WATERMARK, mark)
        db.append(c, 'pending_prune', 'collector', 'pending', now, report,
                  'pending-prune:' + str(int(now)))
    return report
