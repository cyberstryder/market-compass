"""Decline-event persistence for ICT futures scans (2026-10-26).

Fire-rate audit fix 1 (+ scoped fix 3): submit() declines used to be
invisible — aoi_fade / bos_gz_vwap overwrote a single health row with
the latest reason, and every other detector discarded the result.
These tests pin the new behavior:

- a declined submission produces exactly one persisted 'ict_decline'
  event with the gate's detail fields (repeat scans of the same
  signal dedupe to that one event);
- a successful submission produces no decline event;
- a failure inside the recorder never raises and never changes the
  submission outcome.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import aoi_fade, ict_paper
from compass.ict_declines import record_decline, record_scan_eval

CT = ZoneInfo('America/Chicago')
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=CT).timestamp()  # Mon noon CT
SYM = 'MCL.v.0'


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'declines.db'))
    s.initialize()
    yield s
    s.engine.dispose()


def _events(db, kind):
    with db.tx() as c:
        return db.recent(c, kind, limit=50)


# ---------------------------------------------------------------------------
# Recorder unit tests (real Store: the dedupe key semantics are the point)
# ---------------------------------------------------------------------------

def test_decline_produces_exactly_one_event_with_details(db):
    res = {'submitted': False, 'reason': 'rr_too_low', 'rr': 1.2,
           'min_rr': 1.5, 'risk_pts': 2.0, 'reward_pts': 2.4}
    sig = {'direction': 'long', 'entry': 101.0, 'stop': 99.0,
           'target': 102.0, 'signal_ts': NOW - 3600, 'level_kind': 'fvg'}
    with db.tx() as c:
        assert record_decline(db, c, NOW, 'aoi_fade', SYM, res, sig) is True
    # The same signal re-scanned and declined again collapses to one row.
    with db.tx() as c:
        assert record_decline(db, c, NOW + 120, 'aoi_fade', SYM,
                              res, sig) is False
    rows = _events(db, 'ict_decline')
    assert len(rows) == 1
    p = rows[0]['payload']
    assert rows[0]['symbol'] == SYM
    assert p['detector'] == 'aoi_fade'
    assert p['reason'] == 'rr_too_low'
    assert p['details'] == {'rr': 1.2, 'min_rr': 1.5, 'risk_pts': 2.0,
                            'reward_pts': 2.4}
    assert p['direction'] == 'long'
    assert p['entry'] == 101.0 and p['stop'] == 99.0
    assert p['target'] == 102.0
    assert p['signal_ts'] == NOW - 3600
    assert p['level_kind'] == 'fvg'


def test_decline_without_signal_ts_dedupes_by_levels_and_reason(db):
    res = {'submitted': False, 'reason': 'cooldown', 'exited_at': NOW - 60}
    sig = {'direction': 'short', 'entry': 50.0, 'stop': 51.0,
           'target': 49.0}
    with db.tx() as c:
        assert record_decline(db, c, NOW, 'turtle_soup', SYM, res, sig)
        assert not record_decline(db, c, NOW + 120, 'turtle_soup', SYM,
                                  res, sig)
        # A different reason for the same signal is a distinct event.
        other = {'submitted': False, 'reason': 'existing_position'}
        assert record_decline(db, c, NOW, 'turtle_soup', SYM, other, sig)
    rows = _events(db, 'ict_decline')
    assert len(rows) == 2
    assert {r['payload']['reason'] for r in rows} == {
        'cooldown', 'existing_position'}


def test_decline_details_keep_event_title_and_drop_non_scalars(db):
    res = {'submitted': False, 'reason': 'news_pause', 'event': 'CPI',
           'blob': {'not': 'json-scalar'}}
    with db.tx() as c:
        assert record_decline(db, c, NOW, 'aoi_fade', SYM, res,
                              {'signal_ts': NOW - 60})
    rows = _events(db, 'ict_decline')
    assert len(rows) == 1
    assert rows[0]['payload']['details'] == {'event': 'CPI'}


def test_success_and_empty_result_produce_no_event(db):
    with db.tx() as c:
        assert record_decline(db, c, NOW, 'aoi_fade', SYM,
                              {'submitted': True, 'trades': ['t1']},
                              {'signal_ts': NOW}) is False
        assert record_decline(db, c, NOW, 'aoi_fade', SYM, None) is False
        assert record_decline(db, c, NOW, 'aoi_fade', SYM, {}) is False
    assert _events(db, 'ict_decline') == []


def test_scan_eval_event_records_counts(db):
    with db.tx() as c:
        assert record_scan_eval(db, c, NOW, 'aoi_fade', SYM, zones=3,
                                signals=2, submitted=1, declined=1)
    rows = _events(db, 'ict_scan_eval')
    assert len(rows) == 1
    p = rows[0]['payload']
    assert p['detector'] == 'aoi_fade' and p['zones'] == 3
    assert p['signals'] == 2 and p['submitted'] == 1 and p['declined'] == 1


class _ExplodingDB:
    """Store stand-in whose append always fails (recorder isolation)."""

    def append(self, *a, **k):
        raise RuntimeError('events table gone')


def test_recorder_failure_never_raises():
    bad = _ExplodingDB()
    res = {'submitted': False, 'reason': 'rr_too_low', 'rr': 1.0}
    assert record_decline(bad, 'c', NOW, 'aoi_fade', SYM, res,
                          {'signal_ts': NOW}) is False
    assert record_scan_eval(bad, 'c', NOW, 'aoi_fade', SYM,
                            signals=1) is False


# ---------------------------------------------------------------------------
# Call-site integration: aoi_fade.scan wiring
# Detector math is patched at its seams; submit() is patched at the
# ict_paper module (scan imports it inline at call time). The recorder
# and the Store are real.
# ---------------------------------------------------------------------------

_ZONE = {'top': 101.0, 'bottom': 100.0, 'bias': 'long', 'zone_kind': 'fvg'}


class _Cfg:
    ict_aoi_fade = True
    ict_symbols = (SYM,)
    ict_futures_paper = True


def _seed_scan(monkeypatch, submit_result):
    """Patch aoi_fade's seams so scan() evaluates exactly one candidate."""
    day_start = datetime(2026, 10, 26, 0, 0, tzinfo=CT).timestamp()
    bars = [{'ts': day_start - 20 * 3600 + i * 60, 'o': 100.5,
             'h': 100.6, 'l': 100.4, 'c': 100.5, 'v': 10}
            for i in range(20 * 60 + 12 * 60)]
    monkeypatch.setattr(aoi_fade, 'ict_bars', lambda db, c, s: bars)
    monkeypatch.setattr(aoi_fade, 'build_zones',
                        lambda b, now: [dict(_ZONE)])
    fired = {'done': False}

    def fake_check(bar, zones, atr):
        if fired['done']:
            return None
        fired['done'] = True
        return {'direction': 'long', 'zone': dict(_ZONE),
                'entry': 101.0, 'stop': 99.0, 'atr': 1.0}

    monkeypatch.setattr(aoi_fade, 'check_signal', fake_check)
    monkeypatch.setattr(ict_paper, 'submit',
                        lambda *a, **k: submit_result)


def test_scan_persists_decline_event_once(monkeypatch, db):
    _seed_scan(monkeypatch, {'submitted': False, 'reason': 'rr_too_low',
                             'rr': 1.2, 'min_rr': 1.5, 'risk_pts': 2.0,
                             'reward_pts': 2.4})
    with db.tx() as c:
        result = aoi_fade.scan(db, c, _Cfg(), NOW)
    assert result == {'ran': True, 'fired': 0}
    rows = _events(db, 'ict_decline')
    assert len(rows) == 1
    p = rows[0]['payload']
    assert p['detector'] == 'aoi_fade' and p['symbol'] == SYM
    assert p['reason'] == 'rr_too_low'
    assert p['details']['rr'] == 1.2
    assert p['entry'] == 101.0 and p['target'] == 102.0
    # Scan-eval summary landed too, and the health overwrite is gone.
    evals = _events(db, 'ict_scan_eval')
    assert len(evals) == 1
    assert evals[0]['payload']['signals'] == 1
    assert evals[0]['payload']['declined'] == 1
    with db.tx() as c:
        assert db.get(c, 'health:ict_aoi_fade_declines') is None
        latest = db.get(c, 'ict_aoi_fade:latest', {})
    assert latest['signals'] == 1 and latest['declined'] == 1


def test_scan_success_produces_no_decline_event(monkeypatch, db):
    _seed_scan(monkeypatch, {'submitted': True, 'trades': ['t-t1']})
    with db.tx() as c:
        result = aoi_fade.scan(db, c, _Cfg(), NOW)
    assert result == {'ran': True, 'fired': 1}
    assert _events(db, 'ict_decline') == []
    signals = _events(db, 'ict_aoi_fade_signal')
    assert len(signals) == 1
    evals = _events(db, 'ict_scan_eval')
    assert len(evals) == 1
    assert evals[0]['payload']['submitted'] == 1
    assert evals[0]['payload']['declined'] == 0


def test_scan_recorder_failure_does_not_change_outcome(monkeypatch, db):
    _seed_scan(monkeypatch, {'submitted': False, 'reason': 'paper_disabled'})
    orig_append = Store.append

    def exploding(self, c, kind, *a, **k):
        if kind == 'ict_decline':
            raise RuntimeError('events table gone')
        return orig_append(self, c, kind, *a, **k)

    monkeypatch.setattr(Store, 'append', exploding)
    with db.tx() as c:
        result = aoi_fade.scan(db, c, _Cfg(), NOW)
    # The decline outcome is unchanged: scan ran, nothing fired, no raise.
    assert result == {'ran': True, 'fired': 0}
    assert _events(db, 'ict_decline') == []
    # The scan-eval recorder (a different kind) was unaffected.
    assert len(_events(db, 'ict_scan_eval')) == 1
