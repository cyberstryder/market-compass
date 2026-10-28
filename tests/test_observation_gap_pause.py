"""Observation-gap fix package (2026-10-26, Josh-approved "a and b").

- Fix 3: recorded_path judges a late-stored quote by its receipt stamp
  (socket_read_at / recovery_fetched_at), not only by storage time.
- Fix 1 (scoped): a hole pauses a current-version trade instead of
  terminating it; the resuming quote resolves it on its natural reason,
  carrying data_gap=True (excluded from P&L, Josh's 2026-10-06 rule).
- Fix 4 helpers: dropped-batch window math behind the streams' new
  degraded health.
- Instrumentation: tolerance derivation (median basis + bound applied).
"""
import pytest

from compass.config import Config
from compass.store import Store, events
from compass.quote_path import recorded_path
from compass.gap_tolerance import tolerance_detail, scaled_tolerance
from compass.option_stream import drops_in_window

T = 1_700_000_000.0


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'gapfix.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _quote(ts, bid=2.0, ask=2.05, **extra):
    return dict(ts=ts, bid=bid, ask=ask, bid_size=5, ask_size=5, **extra)


def _emit(db, c, symbol, quote, received):
    db.append(c, 'quote', 'massive', symbol, quote['ts'], quote)
    c.execute(events.update().where(events.c.symbol == symbol,
                                    events.c.ts == quote['ts']).values(received=received))


# ---------------------------------------------------------------- fix 3

def test_socket_read_plus1_stored_plus9_is_usable(db):
    # Claude's named test (desk Task 005): fresh at receipt, stale at
    # storage -> usable under fix 3, rejected under the old invariant.
    with db.tx() as c:
        _emit(db, c, 'SPY', _quote(T, socket_read_at=T + 1), received=T + 9)
        path, truncated, check = recorded_path(db, c, 'SPY', T - 1, T + 10)
    assert len(path) == 1 and not truncated
    assert path[0]['recorded_at'] == T + 1
    assert check['late_storage'] == 1 and check['late_storage_accepted'] == 1
    assert check['stale_or_invalid_when_recorded'] == 0


def test_recovery_fetched_at_is_a_receipt_stamp(db):
    # Ruling (verdict correction D): recovery_fetched_at names the same
    # kind of receipt as socket_read_at and is treated identically.
    with db.tx() as c:
        _emit(db, c, 'SPY', _quote(T, recovery_fetched_at=T + 1), received=T + 9)
        path, _, check = recorded_path(db, c, 'SPY', T - 1, T + 10)
    assert len(path) == 1 and path[0]['recorded_at'] == T + 1
    assert check['late_storage_accepted'] == 1


def test_stale_even_at_receipt_stays_rejected(db):
    with db.tx() as c:
        _emit(db, c, 'SPY', _quote(T, socket_read_at=T + 9), received=T + 10)
        path, _, check = recorded_path(db, c, 'SPY', T - 1, T + 11)
    assert path == []
    assert check['stale_or_invalid_when_recorded'] == 1
    assert check['disqualified'] == [dict(ts=T, bucket='invalid_quote')]


def test_received_bound_still_gates_the_replay(db):
    # Correction D: the events.received <= until bound stays — a row is
    # visible only once stored, however fresh its receipt was.
    with db.tx() as c:
        _emit(db, c, 'SPY', _quote(T, socket_read_at=T + 1), received=T + 30)
        path, _, check = recorded_path(db, c, 'SPY', T - 1, T + 10)
        assert path == [] and check['read_rows'] == 0
        path, _, check = recorded_path(db, c, 'SPY', T - 1, T + 31)
    assert len(path) == 1 and check['late_storage_accepted'] == 1


def test_fresh_at_storage_unchanged_and_mismatch_timestamped(db):
    with db.tx() as c:
        _emit(db, c, 'SPY', _quote(T), received=T + .1)
        bad = _quote(T + 50)
        db.append(c, 'quote', 'massive', 'SPY', T + 60, bad)  # payload ts != row ts
        c.execute(events.update().where(events.c.symbol == 'SPY',
                                        events.c.ts == T + 60).values(received=T + 60.1))
        path, _, check = recorded_path(db, c, 'SPY', T - 1, T + 61)
    assert len(path) == 1 and path[0]['recorded_at'] == T + .1
    assert check['timestamp_mismatches'] == 1
    assert check['disqualified'] == [dict(ts=T + 60, bucket='timestamp_mismatch')]


# ------------------------------------------------------- tolerance detail

def test_tolerance_detail_reports_basis():
    d = tolerance_detail([10, 20])
    assert d['basis'] == 'insufficient_samples' and d['tolerance'] == 15 and d['median'] is None
    d = tolerance_detail([2, 2, 3, 2])
    assert d['basis'] == 'floor' and d['tolerance'] == 15 and d['median'] == 2
    d = tolerance_detail([60, 60, 60])
    assert d['basis'] == 'scaled' and d['tolerance'] == 180 and d['median'] == 60
    d = tolerance_detail([200, 200, 200])
    assert d['basis'] == 'cap' and d['tolerance'] == 300
    for intervals in ([], [10, 20], [2, 2, 3, 2], [60, 60, 60], [200, 200, 200]):
        assert tolerance_detail(intervals)['tolerance'] == scaled_tolerance(intervals)


# ------------------------------------------------------------ fix 4 window

def test_drops_in_window_counts_recent_only():
    ev = [(100.0, 'quotes', 10), (350.0, 'trades', 5), (1000.0, 'quotes', 7)]
    assert drops_in_window(ev, 400.0) == (2, 15)   # 300s and 50s old: inside
    assert drops_in_window(ev, 401.0) == (1, 5)    # the 300s-old drop aged out
    assert drops_in_window([], 400.0) == (0, 0)
    assert drops_in_window(ev, 50.0) == (0, 0)     # future events never count


# ------------------------------------------- fix 1: setup_study replay path

@pytest.fixture
def study_cfg():
    return Config(local=True, stocks=('SPY',), futures=('MES.c.0',), extra_futures=(),
                  databento='', alpaca_key='', alpaca_secret='', matrix='', massive='', discord='')


def _enabled_trial(t0, **kw):
    from test_gap_fixes import _trial
    from compass import observation_recovery as recovery
    p = _trial(t0, **kw)
    recovery.enable(p)  # current collection version: gap_policy on
    return p


def test_trial_midlife_hole_resumes_and_resolves_on_natural_exit(db, study_cfg):
    # The task's replay-style proof: a 60s mid-life hole that resumes
    # must resolve on the natural exit (target), NOT as observation_gap,
    # carrying data_gap=True and the hole in gap_seconds_total.
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0
    from test_gap_fixes import _insert_trial, _sq
    study = SetupStudy(db, study_cfg)
    p = _enabled_trial(T0, quote_gaps=[2, 2, 2, 2])  # proven cadence: tol 15s
    with db.tx() as c:
        _insert_trial(db, c, p)
        study.observe(c, p, _sq(T0 + 60, bid=2.25, ask=2.30), T0 + 60)
    assert p['status'] == 'closed'
    assert p['exit_reason'] == 'target'
    assert p['data_gap'] is True
    assert p['gap_seconds_total'] == 60


def test_trial_open_hole_pauses_then_resume_resolves(db, study_cfg):
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0
    from test_gap_fixes import _insert_trial, _sq
    study = SetupStudy(db, study_cfg)
    p = _enabled_trial(T0, quote_gaps=[2, 2, 2, 2])
    with db.tx() as c:
        _insert_trial(db, c, p)
        # No usable quote at +20: hole detected, defer grace starts.
        study.observe(c, p, _sq(T0), T0 + 20)
        assert p['status'] == 'open' and p.get('gap_pending')
        # Still nothing at +300: past grace the trial PAUSES, not dies.
        study.observe(c, p, _sq(T0), T0 + 300)
        assert p['status'] == 'open'
        assert p['data_gap'] is True
        assert p['measurement_paused_from'] == T0
        assert 'exit_reason' not in p
        # A quote finally lands at +360, through the target: natural exit.
        study.observe(c, p, _sq(T0 + 360, bid=2.25, ask=2.30), T0 + 360)
    assert p['status'] == 'closed' and p['exit_reason'] == 'target'
    assert p['data_gap'] is True and p['gap_seconds_total'] == 360


def test_legacy_trial_still_terminates_on_first_hole(db, study_cfg):
    # No gap_policy (legacy collection version): behavior is unchanged —
    # the first proven hole is still fatal, per the versioning split.
    from compass.setup_study import SetupStudy
    from test_setup_study import NOW as T0
    from test_gap_fixes import _trial, _insert_trial, _sq
    study = SetupStudy(db, study_cfg)
    p = _trial(T0, quote_gaps=[2, 2, 2, 2])
    with db.tx() as c:
        _insert_trial(db, c, p)
        study.observe(c, p, _sq(T0 + 60, bid=2.25, ask=2.30), T0 + 60)
    assert p['status'] == 'unresolved' and p['exit_reason'] == 'observation_gap'


def test_gap_paused_trial_is_terminalized_at_flatten(db, study_cfg):
    # Flatten backstop (2026-10-27): a recorded-quotes-v2 trial paused on
    # a hole whose path never fills must not hang open past its flatten;
    # tick() terminates it as session_flatten, priced from the freshest
    # quote on record, with data_gap accounting intact.
    from sqlalchemy import select
    from compass.setup_study import SetupStudy, trials
    from test_setup_study import NOW as T0
    from test_gap_fixes import _insert_trial, _sq
    study = SetupStudy(db, study_cfg)
    p = _enabled_trial(T0, observation_model='recorded-quotes-v2',
                       quote_gaps=[2, 2, 2, 2], flatten_at=T0 + 900)
    p['data_gap'] = True
    p['data_gap_since'] = T0
    with db.tx() as c:
        _insert_trial(db, c, p)
        db.put(c, 'quote:' + p['symbol'], _sq(T0 + 900, bid=1.50, ask=1.55))
        study.tick(c, T0 + 960)
        saved = c.execute(select(trials.c.payload)
                          .where(trials.c.id == p['id'])).scalar_one()
    assert saved['status'] == 'closed'
    assert saved['exit_reason'] == 'session_flatten'
    assert saved['exit'] == 1.50
    assert saved['data_gap'] is True
