import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
import pytest
from compass.config import Config
from compass.daily_history import RECOVERY_CACHE, completed_rows, current_context
from compass.market import day
from compass.providers import Collectors
from compass.secondary import daily_context as secondary_context
from compass.secondary_data import coverage, refresh
from compass.swing_signals import daily_context, stored_daily_context
from compass.swing_ideas import SwingIdeas
from test_scanner import db
from test_swing_ideas import NOW, history, stamp
from tests.test_smoothers_postgres_handoff import pg


def archive(db, symbol='SPY', rows=None):
    rows=history() if rows is None else rows
    with db.tx() as c:
        for row in rows:
            db.append(c,'daily','alpaca',symbol,row['ts'],row['payload'])
    return rows


@pytest.mark.parametrize('database',('db','pg'))
def test_revisions_and_forming_day_cannot_hide_completed_history(request,database):
    db=request.getfixturevalue(database)
    rows=archive(db)
    with db.tx() as c:
        # Four revisions of every completed session crowd out the raw-row limit.
        for row in rows:
            for revision in range(1,5):
                db.append(c,'daily','alpaca','SPY',row['ts'],{**row['payload'],'v':1000+revision})
        old=daily_context(db.recent(c,'daily','SPY',limit=180),NOW)
        assert old['status']=='warming_up'
        # Today's partial bar is never eligible, regardless of revision count.
        for revision in range(200):
            db.append(c,'daily','alpaca','SPY',stamp(day(NOW),0,0),
                dict(o=9000,h=9999,l=1,c=9900,v=revision))
        selected=completed_rows(db,c,'SPY',NOW)
        fixed=stored_daily_context(db,c,'SPY',NOW)
        assert fixed['status']=='ready' and fixed['through']=='2026-09-11'
        assert fixed['close']==daily_context(rows,NOW)['close']
        assert len(selected)==len({day(r['ts']) for r in selected})
        assert fixed['history']['archived_rows']==5*len(selected)
        assert all(r['payload']['v']==1004 for r in selected)
        assert secondary_context(db,c,'SPY',NOW)['status']=='ready'


def test_latest_invalid_revision_blocks_without_falling_back(db):
    rows=archive(db)
    last=[r for r in rows if day(r['ts'])<day(NOW)][-1]
    invalid=dict(last,id=10000,payload={**last['payload'],'h':0})
    assert daily_context(rows+[invalid],NOW)['history']['invalid_sessions']==['2026-09-11']
    with db.tx() as c:
        db.append(c,'daily','alpaca','SPY',invalid['ts'],invalid['payload'])
        result=stored_daily_context(db,c,'SPY',NOW)
        assert result['status']=='warming_up'
        assert result['history']['status']=='invalid_bars'
        assert secondary_context(db,c,'SPY',NOW)['status']=='missing'


def test_short_history_requires_completed_source_evidence(db):
    rows=[r for r in history() if day(r['ts'])<day(NOW)][-30:]
    archive(db,rows=rows)
    with db.tx() as c:
        assert stored_daily_context(db,c,'SPY',NOW)['history']['status']=='insufficient_history'
        proof=dict(day=day(NOW),status='error',error='TimeoutError')
        db.put(c,'daily_history_recovery:SPY',proof)
        assert stored_daily_context(db,c,'SPY',NOW)['history']['status']=='collection_failed'
        db.put(c,'daily_history_recovery:SPY',dict(proof,status='complete',error=None,
            requested_start_day='2026-05-17',requested_end_day='2026-09-13',
            sessions=30,first_session=day(rows[0]['ts'])))
        result=stored_daily_context(db,c,'SPY',NOW)
        assert result['history']['status']=='source_history_short'
        assert result['status']=='warming_up' and 'returned 30 sessions' in result['reason']
    # An interior hole stays a gap even after a successful source request.
    archive(db,'GAPPED',[r for r in history() if day(r['ts'])!='2026-09-10'])
    with db.tx() as c:
        db.put(c,'daily_history_recovery:GAPPED',db.get(c,'daily_history_recovery:SPY'))
        assert stored_daily_context(db,c,'GAPPED',NOW)['history']['status']=='missing_sessions'


@pytest.mark.parametrize('failure',[None,'timeout','pagination','invalid'])
def test_recovery_commits_real_bars_refreshes_stale_cache_and_records_failures(db,monkeypatch,failure):
    cfg=Config(local=True,stocks=('SPY',),discovery=False)
    source=[r for r in history() if day(r['ts'])<day(NOW)][-80:]
    with db.tx() as c:
        db.put(c,'swing_daily:SPY',dict(day=day(NOW),computed_at=NOW,status='warming_up',reason='old cache'))
    monkeypatch.setattr('compass.secondary_data.time',SimpleNamespace(time=lambda:NOW))
    monkeypatch.setattr('compass.secondary_data.plan',lambda collector,now:dict(quotes=[],minute=[],daily=['SPY']))
    async def get(url,headers,params):
        assert params['timeframe']=='1Day' and params['adjustment']=='split'
        if failure=='timeout':raise TimeoutError('private transport detail')
        if failure=='invalid':source[-1]['payload']['h']=0
        return dict(bars={'SPY':[dict(t=datetime.fromtimestamp(r['ts'],timezone.utc).isoformat(),
            **r['payload']) for r in source]},next_page_token='more' if failure=='pagination' else None)
    async def run():
        collector=Collectors(db,cfg);collector.get=get
        try:await refresh(collector)
        finally:await collector.close()
    asyncio.run(run())
    with db.tx() as c:
        # Recovery never takes ownership of the scanner's cache row.
        assert db.get(c,'swing_daily:SPY')['reason']=='old cache'
        result=db.get(c,RECOVERY_CACHE+'SPY')
        proof=db.get(c,'daily_history_recovery:SPY')
        if failure:
            assert proof['status']=='error' and 'private' not in str(proof)
            assert result['status']=='warming_up' and result['history']['status']=='collection_failed'
            assert not db.recent(c,'daily','SPY')
        else:
            assert proof['status']=='complete' and proof['sessions']==80
            assert result['status']=='ready' and result['through']=='2026-09-11'
            assert coverage(db,c,cfg,NOW)['rows'][0]['daily_ready']
        SwingIdeas(db,cfg).discover(c,NOW+1)
        assert db.get(c,'swing_daily:SPY')==result
        assert not db.recent(c,'alert') and not db.recent(c,'swing_candidate')


def test_new_recovery_receipt_invalidates_fresh_scanner_and_collector_caches(db):
    cfg=Config(local=True,stocks=('SPY',),discovery=False)
    rows=archive(db)
    with db.tx() as c:
        cached=stored_daily_context(db,c,'SPY',NOW)
        db.put(c,'swing_daily:SPY',cached)
        db.put(c,RECOVERY_CACHE+'SPY',cached)
        last=[r for r in rows if day(r['ts'])<day(NOW)][-1]
        db.append(c,'daily','alpaca','SPY',last['ts'],{**last['payload'],'h':0})
        proof=dict(day=day(NOW),status='complete',at=NOW+1)
        db.put(c,'daily_history_recovery:SPY',proof)
        # An invalid revision cannot remain ready for the five-minute TTL.
        SwingIdeas(db,cfg).discover(c,NOW+2)
        result=db.get(c,'swing_daily:SPY')
        assert result['status']=='warming_up'
        assert result['history']['status']=='invalid_bars'
        assert result['history']['recovery']==proof
        assert not coverage(db,c,cfg,NOW+2)['rows'][0]['daily_ready']


def test_recovery_committing_during_bar_query_does_not_validate_old_snapshot(db,monkeypatch):
    archive(db)
    with db.tx() as c:
        old_rows=completed_rows(db,c,'SPY',NOW)
        proof=dict(day=day(NOW),status='complete',at=NOW+1)
        def concurrent_recovery(db,c,symbol,now):
            # The bar query already saw its snapshot when a recovery committed.
            db.put(c,'daily_history_recovery:SPY',proof)
            return old_rows
        monkeypatch.setattr('compass.daily_history.completed_rows',concurrent_recovery)
        result=stored_daily_context(db,c,'SPY',NOW+1)
        assert not current_context(result,NOW+2,proof)


def test_postgres_collector_refresh_completes_while_scanner_holds_cache_lock(pg,monkeypatch):
    cfg=Config(local=True,stocks=('ZZZ','AAA'),discovery=False)
    for symbol in cfg.watch_symbols:archive(pg,symbol)
    scanner_locked=Event();release_scanner=Event()
    put=pg.put
    def pause_scanner(c,key,value):
        result=put(c,key,value)
        if key=='swing_daily:ZZZ':
            scanner_locked.set()
            assert release_scanner.wait(10)
        return result
    monkeypatch.setattr(pg,'put',pause_scanner)
    def scan():
        with pg.tx() as c:SwingIdeas(pg,cfg).discover(c,NOW)
    def collect():
        with pg.tx() as c:return coverage(pg,c,cfg,NOW,refreshed=cfg.watch_symbols)
    with ThreadPoolExecutor(max_workers=2) as pool:
        scanner=pool.submit(scan)
        try:
            assert scanner_locked.wait(5)
            collector=pool.submit(collect)
            # Must finish before the scanner releases its uncommitted row.
            result=collector.result(timeout=3)
            assert all(row['daily_ready'] for row in result['rows'] if row['symbol'] in cfg.watch_symbols)
        finally:release_scanner.set()
        scanner.result(timeout=5)
    with pg.tx() as c:
        for symbol in cfg.watch_symbols:
            assert pg.get(c,'swing_daily:'+symbol)['status']=='ready'
            assert pg.get(c,RECOVERY_CACHE+symbol)['status']=='ready'
