"""Pending-population cleanup (2026-11-04).

The two remaining full-prefix readers of the pending population move
to server-side predicates, and terminal pendings gain a retention
prune:

- Scanner.retry_options prefix()-hauled both pending prefixes on
  every scan and skipped every non-'waiting' row on sight. It now
  reads Store.waiting_pending_rows (status filtered server-side,
  full payloads -- it is the lifecycle writer), in the same key
  order the old merge produced.
- daily_results prefix()-hauled the research population and applied
  its session window in Python. It now reads
  Store.pendings_created_between (created_at window server-side).
- compass.pending_prune.run_pass deletes terminal rows older than
  14 days in banked batches behind a watermark; 'waiting' rows are
  never touched.

The structural pins monkeypatch Store.prefix to raise on the pending
prefixes: both fail on the pre-cleanup code, where the readers haul.
"""
from datetime import datetime

import pytest
from sqlalchemy import update

from compass.config import Config
from compass.store import Store, state
from compass.market import NY, CT, session
from compass.scanner import Scanner
from compass.engine import Engine
# Importing the app registers every table-defining module before
# Store.initialize() runs create_all (the lazy-import table lesson).
from compass.app import create_app  # noqa: F401

NOW = datetime(2026, 9, 14, 10, 15, tzinfo=NY).timestamp()
DAY = 86400


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'pending_cleanup.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def signal(i, symbol='SPY'):
    return {'id': f'sig-{i}', 'symbol': symbol, 'side': 'long',
            'invalidation': 99, 'signal_price': 100,
            'signal_time': NOW - 10,
            'strategy': 'compass-scanner-v2:session_sweep_reclaim',
            'context': {'atr14': 1}}


def seed(db, key, value, updated=None):
    with db.tx() as c:
        db.put(c, key, value)
        if updated is not None:
            c.execute(update(state).where(state.c.key == key)
                      .values(updated=updated))


def research(i, status='waiting', created=NOW - 10, expires=NOW + 1790):
    return {'signal': signal(i), 'created_at': created,
            'expires_at': expires, 'status': status, 'research_only': True}


def scanner_pending(i, status='waiting', created=NOW - 10, expires=NOW + 100):
    return {'signal': signal(i), 'created_at': created,
            'expires_at': expires, 'status': status}


def old_merge(db, c):
    return {**db.prefix(c, 'pending_options:'),
            **db.prefix(c, 'pending_research_options:')}


def test_waiting_pending_rows_matches_old_merge(db):
    seed(db, 'pending_options:a', scanner_pending(1))
    seed(db, 'pending_options:b', scanner_pending(2, status='entered'))
    seed(db, 'pending_options:c', scanner_pending(3, expires=NOW - 5))
    seed(db, 'pending_research_options:d', research(4))
    seed(db, 'pending_research_options:e', research(5, status='expired'))
    seed(db, 'pending_research_options:f', research(6, expires=NOW - 5))
    with db.tx() as c:
        merged = old_merge(db, c)
        expected = [(k, v) for k, v in merged.items()
                    if v.get('status') == 'waiting']
        got = list(db.waiting_pending_rows(c).items())
    # Same rows, same order, same payloads -- including the two
    # closed-window waiting rows retry_options exists to expire.
    assert got == expected
    assert [k for k, _ in got] == ['pending_options:a', 'pending_options:c',
                                   'pending_research_options:d',
                                   'pending_research_options:f']


def _seed_retry_population(db):
    # Paper off: non-research waiting rows pause; research rows with a
    # closed window expire; a live research row without a fresh quote
    # records a waiting diagnostic. Terminal rows must be ignored.
    seed(db, 'pending_options:p1', scanner_pending(1))
    seed(db, 'pending_options:p2', scanner_pending(2, status='entered'))
    seed(db, 'pending_research_options:r1', research(3, expires=NOW - 5))
    seed(db, 'pending_research_options:r2', research(4))
    seed(db, 'pending_research_options:r3', research(5, status='observed'))


def _run_retry(db):
    cfg = Config(local=True, paper_trading=False)
    with db.tx() as c:
        Scanner(db, cfg).retry_options(c, {}, NOW, Engine(db, cfg))
    with db.tx() as c:
        return {**db.prefix(c, 'pending_options:'),
                **db.prefix(c, 'pending_research_options:')}


def test_retry_options_identical_under_old_and_new_reads(db, tmp_path,
                                                         monkeypatch):
    _seed_retry_population(db)
    other = Store('sqlite:///' + str(tmp_path / 'other.db'))
    other.initialize()
    _seed_retry_population(other)
    # Store A runs the post-cleanup scanner against the OLD read.
    monkeypatch.setattr(
        Store, 'waiting_pending_rows',
        lambda self, c: {k: v for k, v in old_merge(self, c).items()
                         if v.get('status') == 'waiting'})
    after_old_read = _run_retry(db)
    monkeypatch.undo()
    after_new_read = _run_retry(other)
    other.engine.dispose()
    assert after_old_read == after_new_read
    # And the transitions really happened (not a vacuous comparison):
    assert after_new_read['pending_options:p1']['status'] == 'paused'
    assert after_new_read['pending_research_options:r1']['status'] == 'expired'
    assert after_new_read['pending_research_options:r2']['status'] == 'waiting'
    assert after_new_read['pending_options:p2']['status'] == 'entered'
    assert after_new_read['pending_research_options:r3']['status'] == 'observed'


def test_retry_options_does_not_haul_pending_prefixes(db, monkeypatch):
    _seed_retry_population(db)
    original = Store.prefix

    def guarded(self, c, prefix):
        if prefix.startswith('pending'):
            raise AssertionError('full pending haul: ' + prefix)
        return original(self, c, prefix)

    monkeypatch.setattr(Store, 'prefix', guarded)
    cfg = Config(local=True, paper_trading=False)
    with db.tx() as c:
        Scanner(db, cfg).retry_options(c, {}, NOW, Engine(db, cfg))
    monkeypatch.undo()
    with db.tx() as c:
        after = {**db.prefix(c, 'pending_options:'),
                 **db.prefix(c, 'pending_research_options:')}
    assert after['pending_options:p1']['status'] == 'paused'
    assert after['pending_research_options:r1']['status'] == 'expired'


def test_pendings_created_between_matches_old_filter(db):
    seed(db, 'pending_research_options:a', research(1, created=NOW - 100))
    seed(db, 'pending_research_options:b', research(2, created=NOW - 5000))
    seed(db, 'pending_research_options:c', research(3, created=NOW + 5000))
    no_created = research(4)
    no_created.pop('created_at')
    seed(db, 'pending_research_options:d', no_created)
    seed(db, 'pending_options:e', scanner_pending(5, created=NOW - 100))
    with db.tx() as c:
        expected = [p for p in db.prefix(c, 'pending_research_options:').values()
                    if NOW - 1000 <= p.get('created_at', 0) < NOW + 1000]
        got = db.pendings_created_between(
            c, 'pending_research_options:', NOW - 1000, NOW + 1000)
    assert got == expected
    assert len(got) == 1 and got[0]['signal']['id'] == 'sig-1'


from test_setup_reporting import report_db  # noqa: E402


def test_daily_results_does_not_haul_pending_prefix(db, report_db,
                                                    monkeypatch):
    from compass.daily_results import build
    from compass.native_morning_worker import initialize
    initialize(report_db)
    open_ = session('2026-09-21')[0]
    now = datetime(2026, 9, 21, 16, 25, tzinfo=CT).timestamp()
    seed(report_db, 'pending_research_options:today',
         {'signal': signal(1), 'created_at': open_ + 60,
          'expires_at': open_ + 1800, 'status': 'observed',
          'research_only': True,
          'last_selection': {'reason': 'No eligible contract',
                             'rejections': [{'reason': 'spread'}]}})
    seed(report_db, 'pending_research_options:old',
         {'signal': signal(2), 'created_at': open_ - 30 * DAY,
          'expires_at': open_ - 29 * DAY, 'status': 'expired',
          'research_only': True,
          'last_selection': {'reason': 'Stale', 'rejections': []}})
    original = Store.prefix

    def guarded(self, c, prefix):
        if prefix.startswith('pending'):
            raise AssertionError('full pending haul: ' + prefix)
        return original(self, c, prefix)

    monkeypatch.setattr(Store, 'prefix', guarded)
    cfg = Config(local=True, paper_trading=True)
    with report_db.tx() as c:
        r = build(report_db, c, cfg, now, '2026-09-21')
    assert r['zero_dte_selection']['total'] == 1
    assert r['zero_dte_selection']['states'] == {'observed': 1}
    assert r['zero_dte_selection']['last_attempt_reasons'] == {
        'No eligible contract': 1}


# --- the prune -------------------------------------------------------

OLD = NOW - 20 * DAY
RECENT = NOW - 2 * DAY
TERMINAL = ('expired', 'observed', 'entered', 'routed_to_book_path',
            'paused')


def _seed_prune_population(db):
    for i, status in enumerate(TERMINAL):
        seed(db, f'pending_options:t-old-{status}',
             scanner_pending(100 + i, status=status, created=OLD),
             updated=OLD + 60)
        seed(db, f'pending_research_options:t-old-{status}',
             research(200 + i, status=status, created=OLD),
             updated=OLD + 60)
        seed(db, f'pending_options:t-new-{status}',
             scanner_pending(300 + i, status=status, created=RECENT),
             updated=RECENT + 60)
    # Waiting rows are untouchable at any age.
    seed(db, 'pending_options:w-old', scanner_pending(400, created=OLD),
         updated=OLD + 60)
    seed(db, 'pending_research_options:w-old', research(401, created=OLD),
         updated=OLD + 60)
    # Terminal + old last-write but created inside retention: the
    # created_at guard keeps it.
    seed(db, 'pending_options:t-young-created',
         scanner_pending(402, status='expired', created=RECENT),
         updated=OLD + 60)
    # Terminal + old, no created_at at all: the last-write clock rules.
    bare = scanner_pending(403, status='expired')
    bare.pop('created_at')
    seed(db, 'pending_options:t-no-created', bare, updated=OLD + 60)


def _keys(db):
    with db.tx() as c:
        return sorted({**db.prefix(c, 'pending_options:'),
                       **db.prefix(c, 'pending_research_options:')})


def test_prune_deletes_only_terminal_old(db):
    from compass import pending_prune
    _seed_prune_population(db)
    report = pending_prune.run_pass(db, NOW, budget_s=3600)
    assert report['complete'] is True
    assert report['prunable_before'] == 11
    assert report['deleted'] == 11
    assert report['prunable_after'] == 0
    survivors = _keys(db)
    assert 'pending_options:w-old' in survivors
    assert 'pending_research_options:w-old' in survivors
    assert 'pending_options:t-young-created' in survivors
    assert 'pending_options:t-no-created' not in survivors
    for status in TERMINAL:
        assert f'pending_options:t-old-{status}' not in survivors
        assert f'pending_research_options:t-old-{status}' not in survivors
        assert f'pending_options:t-new-{status}' in survivors
    with db.tx() as c:
        mark = db.get(c, pending_prune.WATERMARK, {})
    assert mark['status'] == 'complete'
    assert mark['last_completed_at'] == NOW
    assert mark['deleted_total'] == 11


def test_prune_batches_bank_progress_and_resume(db):
    from compass import pending_prune
    _seed_prune_population(db)
    first = pending_prune.run_pass(db, NOW, batch=2, max_batches=1,
                                   budget_s=3600)
    assert first['complete'] is False and first['deleted'] == 2
    with db.tx() as c:
        mark = db.get(c, pending_prune.WATERMARK, {})
    assert mark['status'] == 'in_progress'
    assert 'last_completed_at' not in mark
    assert mark['deleted_total'] == 2
    second = pending_prune.run_pass(db, NOW, batch=2, budget_s=3600)
    assert second['complete'] is True and second['deleted'] == 9
    # A pass inside 24h of the completed sweep is a no-op.
    third = pending_prune.run_pass(db, NOW + 3600, batch=2, budget_s=3600)
    assert third.get('skipped') == 'sweep_completed_recently'
    with db.tx() as c:
        mark = db.get(c, pending_prune.WATERMARK, {})
    assert mark['deleted_total'] == 11


def test_live_waiting_set_identical_across_prune(db):
    from compass import pending_prune
    _seed_prune_population(db)
    with db.tx() as c:
        live_before = db.waiting_pendings(c, NOW)
        rows_before = db.waiting_pending_rows(c)
    pending_prune.run_pass(db, NOW, budget_s=3600)
    with db.tx() as c:
        assert db.waiting_pendings(c, NOW) == live_before
        assert db.waiting_pending_rows(c) == rows_before


from tests.test_smoothers_postgres_handoff import pg  # noqa: E402,F401


def test_pending_cleanup_reads_on_postgres(pg):
    """Dialect proof: the JSONB status predicate, the created_at
    float cast, the retention predicate's coalesce, and the batched
    delete must select/delete identically on Postgres (production's
    dialect). Runs only where the pg fixture's isolated service
    exists (CI); skipped on a bare dev box."""
    cutoff = NOW - 14 * DAY
    seed(pg, 'pending_options:pg-wait', scanner_pending(1), updated=NOW)
    seed(pg, 'pending_research_options:pg-wait',
         research(2, created=NOW - 100), updated=NOW)
    seed(pg, 'pending_options:pg-old',
         scanner_pending(3, status='expired', created=OLD), updated=OLD + 60)
    seed(pg, 'pending_options:pg-new',
         scanner_pending(4, status='expired', created=RECENT),
         updated=RECENT + 60)
    with pg.tx() as c:
        rows = pg.waiting_pending_rows(c)
        assert sorted(rows) == ['pending_options:pg-wait',
                                'pending_research_options:pg-wait']
        window = pg.pendings_created_between(
            c, 'pending_research_options:', NOW - 1000, NOW + 1000)
        assert [p['signal']['id'] for p in window] == ['sig-2']
        pop = pg.pending_population(c, cutoff)
        assert pop['pending_options:']['expired']['prunable'] == 1
        deleted = pg.prune_terminal_pendings_batch(c, cutoff, 500)
    assert deleted == ['pending_options:pg-old']
    with pg.tx() as c:
        assert pg.get(c, 'pending_options:pg-old') is None
        assert pg.get(c, 'pending_options:pg-new') is not None
        assert pg.get(c, 'pending_options:pg-wait') is not None
