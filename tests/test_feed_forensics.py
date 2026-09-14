from types import SimpleNamespace
from compass.feed_forensics import quote_reason, clock_fields, futures_gaps
from compass.setup_study import SetupStudy
from compass.engine import spec
from test_setup_study import db, cfg, NOW, quote, signal, rows
from test_quote_coverage import archive


def test_quote_failures_keep_receipt_and_source_clocks_separate():
    q=quote()
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW+1))=='usable'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW+6))=='stale_or_invalid_at_receipt'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW,received=NOW-1))=='future_at_receipt'
    assert quote_reason(SimpleNamespace(payload=q,ts=NOW+1,received=NOW+2))=='timestamp_mismatch'


def test_clock_fields_omit_market_payloads_and_credentials():
    result=clock_fields({'api_key':'secret','data':{'spot':100,'snapshotTime':'2026-09-14T20:00:00Z'},
        'freshness':{'stale':True,'refreshSeconds':300,'credentials':'secret'},'cached':True})
    assert result=={'data.snapshotTime':'2026-09-14T20:00:00Z','freshness.stale':True,
        'freshness.refreshSeconds':300,'cached':True}


def test_gap_report_separates_late_arrivals_and_preserves_outcome(db,cfg):
    study=SetupStudy(db,cfg)
    with db.tx() as c:
        db.put(c,'quote:MESZ6@1',quote())
        study.start(c,signal(),NOW,spec('MESZ6@1'))
        study.tick(c,NOW+20)
        before=rows(c)
        archive(db,c,NOW+10,symbol='MESZ6@1',received=NOW+30)
        result=futures_gaps(db,c,NOW+40)
        row=result['rows'][0]
        assert row['stored_after_finish']==1 and row['available_by_finish']=={}
        assert row['first_next_quote']['reason']=='stale_or_invalid_at_receipt'
        assert rows(c)==before and rows(c)[0]['status']=='unresolved'


def test_closed_session_history_is_separate_from_live_readiness(db,cfg):
    from compass.feed_forensics import input_readiness
    from compass.market import CT
    from datetime import datetime
    now=datetime(2026,9,14,16,10,tzinfo=CT).timestamp()
    cfg.watchlist=('TGT','SBUX')
    with db.tx() as c:
        for symbol in ('TGT','SBUX','YMZ6@1','MYMZ6@2'):
            end=now-600
            db.put(c,'quote:'+symbol,quote(end))
            db.put(c,'bar_window:'+symbol,[[end-60*i,100,102,99,101,10] for i in range(30,0,-1)])
        db.put(c,'secondary:data_status',dict(at=now,rows=[dict(symbol='TGT',projects=['smoothers'],
            daily_ready=True,daily_through='2026-09-11',collection_enabled=True)]))
        result=input_readiness(db,c,cfg,now)
        assert not result['stocks_market_open']
        assert result['stock_current_quotes']==0
        assert all(r['last_30_complete'] for r in result['stock_focus'])
        assert result['smoother_daily_ready']==1
        assert all(not r['market_open'] and not r['quote_current'] for r in result['dow'])
        assert all(r['last_30_complete'] for r in result['dow'])
        assert all(r['htf15_bias'] is None for r in result['dow'])


def test_history_gaps_are_explicit_and_missing_quotes_not_ready(db,cfg):
    from compass.feed_forensics import input_readiness
    cfg.watchlist=('TGT','SBUX')
    with db.tx() as c:
        db.put(c,'bar_window:TGT',[[NOW-60*i,100,102,99,101,10] for i in range(31,0,-1) if i!=5])
        result=input_readiness(db,c,cfg,NOW)
        tgt=next(r for r in result['stock_focus'] if r['symbol']=='TGT')
        assert not tgt['last_30_complete'] and tgt['missing_recent_minutes']==1
        assert not tgt['quote_current']
        assert all(r['history_status']=='missing' for r in result['dow'])


def test_next_session_daily_history_requires_after_close_receipt(db,cfg):
    from compass.feed_forensics import input_readiness
    from compass.swing_signals import sessions_between
    from compass.market import session
    from compass.store import events
    from sqlalchemy import update
    dates=sessions_between('2026-06-01','2026-09-14')
    end=session('2026-09-14')[1]
    cfg.watchlist=('TGT',)
    with db.tx() as c:
        for i,date in enumerate(dates):
            c.execute(db.insert(events).values(key='daily-'+date,kind='daily',source='fixture',symbol='TGT',
                ts=session(date)[0],received=end-60,payload=dict(o=100+i,h=102+i,l=99+i,c=101+i,v=100)))
        db.put(c,'secondary:data_status',dict(at=end,rows=[dict(symbol='TGT',projects=['smoothers'],
            daily_ready=True,daily_through='2026-09-11',collection_enabled=True)]))
        first=input_readiness(db,c,cfg,end+120)
        assert first['next_stock_session']=='2026-09-15'
        assert first['smoother_next_session_ready']==0
        c.execute(update(events).where(events.c.key=='daily-2026-09-14').values(received=end+60))
        assert input_readiness(db,c,cfg,end+120)['smoother_next_session_ready']==1
