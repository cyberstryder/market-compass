import asyncio
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from compass.config import Config
from compass.market import day, session
from compass.projects import ingest
from compass.providers import Collectors
from compass.secondary import (Secondary, VERSION, assess, daily_context, pending, reviews,
    snapshot, technical_context)
from compass.secondary_data import coverage, plan, refresh
from compass.store import Store, identity, leases
from compass.swing_signals import sessions_between
from compass.universe import connected_symbols, data_symbols

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    result = Store('sqlite:///' + str(tmp_path / 'recovery.db'))
    result.initialize()
    yield result
    result.engine.dispose()


def source(now=NOW, **kw):
    return dict(id='review-1', symbol='SPY', source_ts=now, strategy='Original strategy',
        status='tracking', side='long', entry=100, target=120, original={}, **kw)


def quote(now=NOW):
    return dict(ts=now, bid=100, ask=100.02, bid_size=10, ask_size=10)


def seed(db, now=NOW, symbol='SPY'):
    with db.tx() as c:
        db.put(c, 'quote:' + symbol, quote(now))
        db.put(c, 'scanner_features:' + symbol, dict(status='ready', asof=now-1, atr14=2,
            htf15_bias=1, vwap=99, ema9=100, ema21=99))


def enqueue(db, worker, project='morning', row=None):
    worker.tick(NOW-1)
    with db.tx() as c:
        ingest(db, c, project, row or source(), NOW)
    worker.tick(NOW)


def daily_rows(db, symbol='BKNG', omit=None):
    dates = sessions_between('2026-05-20', '2026-09-11')[-70:]
    with db.tx() as c:
        for i, d in enumerate(dates):
            if d == omit:
                continue
            db.append(c, 'daily', 'alpaca', symbol, session(d)[0],
                dict(o=100+i, h=102+i, l=99+i, c=101+i, v=10000), 'day:'+symbol+d)
    return dates


def minute_rows(db, symbol='BKNG'):
    with db.tx() as c:
        db.put(c, 'bar_window:'+symbol,
            [[NOW-60*i, 100, 102, 99, 101, 1000, 100.5] for i in range(40, 0, -1)])


def test_missing_inputs_retry_then_freeze_one_decision_with_current_anchor(db):
    worker = Secondary(db, Config(local=True))
    enqueue(db, worker)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one() == 0
        assert c.execute(select(pending.c.deadline)).scalar_one() == NOW+60
        assert not db.recent(c, 'alert')
    seed(db, NOW+4)
    worker.tick(NOW+4)
    worker.tick(NOW+6)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row['verdict'] == 'supported' and row['decided_at'] == NOW+4
        assert row['decision']['readiness_wait_seconds'] == 4
        assert row['decision']['readiness_attempts'] == 2
        assert row['measurements']['anchor_ts'] == NOW+4
        assert row['measurements']['horizons']['15']['due'] == NOW+4+900
        assert len(db.recent(c, 'alert')) == 1
        assert not c.execute(select(pending)).first()
        assert not db.prefix(c, 'position:')


def test_expired_window_is_never_recovered_as_a_timely_trade(db):
    worker = Secondary(db, Config(local=True))
    enqueue(db, worker)
    worker.tick(NOW+61)
    with db.tx() as c:
        frozen = dict(c.execute(select(reviews)).mappings().one())
        assert frozen['verdict'] == 'insufficient_data'
        assert not frozen['decision']['timely'] and not db.recent(c, 'alert')
        assert 'deadline' in frozen['decision']['reasons'][0]
    seed(db, NOW+62)
    worker.tick(NOW+62)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row['inputs'] == frozen['inputs'] and row['decision'] == frozen['decision']
        assert row['measurements']['state'] == 'unmeasurable'


def test_pending_survives_restart_without_new_original_clock(db):
    worker = Secondary(db, Config(local=True))
    enqueue(db, worker)
    with db.tx() as c:
        c.execute(leases.update().where(leases.c.key == 'secondary').values(until=0))
    restarted = Secondary(db, Config(local=True))
    seed(db, NOW+32)
    restarted.tick(NOW+32)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row['source_ts'] == NOW and row['decision']['latency_seconds'] == 32
        assert len(db.recent(c, 'alert')) == 1


def test_source_outcome_updates_do_not_extend_a_pending_deadline(db):
    worker = Secondary(db, Config(local=True))
    enqueue(db, worker)
    with db.tx() as c:
        row = source()
        row['outcome'] = {'checkpoint': 'new observation'}
        ingest(db, c, 'morning', row, NOW+50)
    worker.tick(NOW+50)
    with db.tx() as c:
        waiting = c.execute(select(pending)).mappings().one()
        assert waiting['deadline'] == NOW+60 and waiting['first_seen'] == NOW
        assert waiting['outcome']['outcome'] == {'checkpoint': 'new observation'}
    worker.tick(NOW+61)
    with db.tx() as c:
        assert c.execute(select(reviews.c.verdict)).scalar_one() == 'insufficient_data'


def test_old_version_receives_only_outcome_updates_never_a_new_review(db):
    worker = Secondary(db, Config(local=True))
    seed(db)
    enqueue(db, worker)
    with db.tx() as c:
        row = dict(c.execute(select(reviews)).mappings().one())
        legacy_id = identity('secondary-context-v1', 'morning', row['source_key'])
        c.execute(reviews.update().values(id=legacy_id, version='secondary-context-v1'))
        old_inputs = copy.deepcopy(row['inputs'])
        updated = source()
        updated['status'] = 'observed_60m'
        ingest(db, c, 'morning', updated, NOW+61)
        worker.review(c, row['candidate'], {}, NOW+62)
    worker.tick(NOW+62)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert row['id'] == legacy_id and row['inputs'] == old_inputs
        assert row['source_outcome']['status'] == 'observed_60m'
        assert len(db.recent(c, 'alert')) == 1


def test_missing_daily_history_is_not_a_directional_disagreement(db):
    worker = Secondary(db, Config(local=True))
    seed(db)
    weekly = {**source(), 'status': 'open', 'original': {'target_atr_mult': .3}}
    enqueue(db, worker, 'smoothers', weekly)
    with db.tx() as c:
        waiting = c.execute(select(pending)).mappings().one()
        assert waiting['missing'] == ['Completed daily price history is unavailable; direction cannot be compared']
    worker.tick(NOW+61)
    with db.tx() as c:
        row = c.execute(select(reviews)).mappings().one()
        assert not any('daily' in x for x in row['decision']['cautions'])
        assert row['decision']['data_checks']['daily']['status'] == 'missing'


def test_connected_coverage_collects_bkng_without_expanding_scanner_entries(db):
    cfg = Config(local=True, stocks=('SPY',), watchlist=('SPY',))
    with db.tx() as c:
        ingest(db, c, 'smoothers', {**source(), 'symbol': 'BKNG'}, NOW)
        assert data_symbols(db, c, cfg, NOW) == ('SPY', 'BKNG')
        assert connected_symbols(db, c, NOW) == ('BKNG',)
        assert cfg.watch_symbols == ('SPY',)
    daily_rows(db)
    minute_rows(db)
    with db.tx() as c:
        assert db.get(c, 'scanner_features:BKNG') is None
        assert technical_context(db, c, 'BKNG', NOW)['status'] == 'ready'
        result = coverage(db, c, cfg, NOW)['rows'][0]
        assert result['collection_enabled'] and result['daily_ready'] and result['minute_ready']
        assert not db.recent(c, 'alert')


def test_daily_history_uses_completed_exchange_sessions_and_rejects_a_gap(db):
    dates = daily_rows(db)
    assert '2026-09-07' not in dates  # Labor Day is not a missing session.
    with db.tx() as c:
        ready = daily_context(db, c, 'BKNG', NOW)
        assert ready['status'] == 'ready' and ready['through'] == '2026-09-11'
        db.append(c, 'daily', 'test', 'BKNG', session(day(NOW))[0],
            dict(o=1, h=1000, l=1, c=1, v=1), 'forming-today')
        assert daily_context(db, c, 'BKNG', NOW) == ready
    daily_rows(db, 'GAPPED', omit='2026-09-10')
    with db.tx() as c:
        assert daily_context(db, c, 'GAPPED', NOW)['status'] == 'missing'


def test_recovery_requests_are_bounded_and_prioritize_waiting_candidates(db):
    cfg = Config(local=True, stocks=('SPY',))
    worker = Secondary(db, cfg)
    enqueue(db, worker)
    collector = SimpleNamespace(db=db, cfg=cfg)
    first = plan(collector, NOW)
    assert first['quotes'] == ['SPY'] and first['minute'] == ['SPY']
    assert plan(collector, NOW+2)['minute'] == []
    assert plan(collector, NOW+60)['minute'] == ['SPY']


def test_rest_quote_recovery_keeps_source_clock_and_cannot_rewind_stream(db, monkeypatch):
    cfg = Config(local=True, stocks=('SPY',))
    worker = Secondary(db, cfg)
    enqueue(db, worker)
    collector = Collectors(db, cfg)
    calls = []
    async def get(url, headers=None, params=None):
        calls.append((url, params))
        if url.endswith('/quotes/latest'):
            # Stream advances while the REST request is in flight.
            with db.tx() as c:
                db.put_quote(c, 'quote:SPY', quote(NOW+3))
            stamp = datetime.fromtimestamp(NOW-20, timezone.utc).isoformat()
            return {'quotes': {'SPY': dict(t=stamp, bp=90, ap=91, bs=10, **{'as': 10})}}
        return {'bars': {}, 'next_page_token': None}
    collector.get = get
    monkeypatch.setattr('compass.secondary_data.time', SimpleNamespace(time=lambda: NOW+3))
    async def run():
        try:
            await refresh(collector)
        finally:
            await collector.close()
    asyncio.run(run())
    with db.tx() as c:
        assert db.get(c, 'quote:SPY')['ts'] == NOW+3
        assert db.get(c, 'quote:SPY')['bid'] == 100
        assert not c.execute(select(reviews)).first()
    assert len(calls) == 3 and calls[0][1]['feed'] == cfg.feed
    assert {p.get('timeframe') for _,p in calls[1:]} == {'1Day','1Min'}


def test_stale_readiness_never_claims_current_coverage(db):
    cfg = Config(local=True, stocks=('SPY',))
    with db.tx() as c:
        ingest(db, c, 'smoothers', source(), NOW)
        db.put(c, 'secondary:data_status', dict(at=NOW-46, rows=[dict(symbol='SPY',
            projects=['smoothers'], collection_enabled=True, daily_ready=True,
            minute_ready=True, minute_asof=NOW-1)]))
        db.put(c, 'quote:SPY', quote(NOW))
        assert not snapshot(db, c, NOW)['data_readiness']['rows'][0]['ready']

