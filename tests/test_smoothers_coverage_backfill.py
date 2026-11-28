"""Coverage-gap backfill at week-end finalization (2026-11-27).

The target monitor flags coverage_missing when its in-week bar pull has
holes, and finalization used to take that flag at face value: the pick
closed UNRESOLVED even when the missing minutes were later retrievable.
The monitor's source is Alpaca SIP *history* (data.bar_batch), so the
closure now re-pulls the pick's bars ONCE before finalizing: a complete
record resolves on the evidence (WIN on a touch, LOSS otherwise); a
record that still has gaps — or a provider error — stays UNRESOLVED,
and a pick that was never coverage-missing is never re-pulled.
"""
import pandas as pd
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass.native_smoothers import VERSION, weekly, save, monitor
from compass.native_outbox import outbox as native_outbox
from compass.smoothers_handoff import sessions

WEEK = '2026-09-14'
CAL = sessions(WEEK)
CLOSE = CAL[-1]['close']
ENTRY = CAL[0]['open'] + 3600
CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
            'strike': 102, 'expiration': '2026-09-18'}


def _q(bid, ask, at_s):
    return dict(status='available', bid=bid, ask=ask,
                quote_at_ms=int(at_s * 1000), quote_age_seconds=1,
                contract=CONTRACT)


def _pick(**over):
    p = dict(id='x', week=WEEK, ticker='DEMO', status='OPEN',
             direction='CALL', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, created_at=ENTRY,
             last_seen_at=ENTRY + 3600, last_underlying=100.0,
             coverage_missing=True,
             coverage_missing_since=ENTRY + 7200,
             target_error='empty_completed_bars',
             entry_premium=2.0, est_return_pct=10.0,
             contract=dict(CONTRACT), quote=_q(1.9, 2.0, ENTRY))
    p.update(over)
    return p


def _seed(db, pick):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        save(db, c, pick)


def _stored(db, pid='x'):
    with db.tx() as c:
        return c.execute(select(weekly.c.payload)
                         .where(weekly.c.id == pid)).scalar_one()


def _outbox_keys(db):
    with db.tx() as c:
        return list(c.execute(select(native_outbox.c.event_key)).scalars())


def _bars(touch_at=None, drop=()):
    """Full-week 1-min bars from ENTRY: flat 100/101/99/99 (no touch of
    the 102 CALL target) unless touch_at names a stamp whose high
    reaches 103."""
    stamps = []
    for s in CAL:
        stamps.extend(range(int(max(ENTRY, s['open'])), int(s['close']), 60))
    stamps = [t for t in stamps if t not in drop]
    high = [103.0 if t == touch_at else 101.0 for t in stamps]
    return pd.DataFrame(
        dict(open=100.0, high=high, low=99.0, close=99.0),
        index=pd.to_datetime(stamps, unit='s', utc=True))


class _Provider:
    def __init__(self, bars=None, error=None):
        self.bars = bars
        self.error = error
        self.bar_calls = []

    def bar_batch(self, symbols, frame, begin, end):
        self.bar_calls.append((symbols, frame, begin, end))
        if self.error is not None:
            raise self.error
        return {'DEMO': self.bars}

    def quote(self, contract):
        return _q(0.4, 0.5, CLOSE + 300)


def test_backfill_complete_record_without_touch_resolves_loss(db):
    _seed(db, _pick())
    provider = _Provider(bars=_bars())
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'LOSS'
    assert saved['coverage_missing'] is False
    assert saved['coverage_missing_since'] is None
    assert saved['last_seen_at'] == CLOSE - 60
    assert saved['exit_underlying'] == 99.0
    assert len(provider.bar_calls) == 1
    assert 'x:close' in _outbox_keys(db)


def test_backfill_complete_record_with_touch_resolves_win(db):
    _seed(db, _pick())
    touch = int(max(ENTRY, CAL[1]['open'])) + 3600
    provider = _Provider(bars=_bars(touch_at=touch))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'WIN'
    assert saved['coverage_missing'] is False
    assert saved['exit_underlying'] == 102.0
    assert saved['resolution_time'] == touch
    assert saved['touch_bar']['high'] == 103.0
    assert len(provider.bar_calls) == 1
    keys = _outbox_keys(db)
    assert 'x:target' in keys and 'x:close' not in keys


def test_backfill_still_gapped_stays_unresolved(db):
    _seed(db, _pick())
    missing = int(max(ENTRY, CAL[2]['open'])) + 3600
    provider = _Provider(bars=_bars(drop={missing}))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'UNRESOLVED'
    assert saved['coverage_missing'] is True
    assert len(provider.bar_calls) == 1
    assert 'x:close' in _outbox_keys(db)


def test_backfill_provider_error_stays_unresolved(db):
    _seed(db, _pick())
    provider = _Provider(error=RuntimeError('provider down'))
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'UNRESOLVED'
    assert saved['coverage_missing'] is True
    assert len(provider.bar_calls) == 1


def test_no_backfill_for_pick_that_was_never_coverage_missing(db):
    _seed(db, _pick(coverage_missing=False, coverage_missing_since=None,
                    target_error=None, last_seen_at=CLOSE - 60,
                    last_underlying=99.0))
    provider = _Provider(bars=_bars())
    monitor(db, provider, CLOSE + 600)
    saved = _stored(db)
    assert saved['status'] == 'LOSS'
    assert provider.bar_calls == []
