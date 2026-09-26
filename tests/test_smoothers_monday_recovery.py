"""Regressions for late historical closures and temporary minute/quote gaps."""
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import select
from test_projects import db
from compass.native_config import effective
from compass.native_smoothers import VERSION, weekly, save, monitor, refresh_entry_quotes
from compass.projects import records

NOW=datetime(2026,9,21,14,30,tzinfo=timezone.utc).timestamp()


def test_source_late_closure_replaces_incomplete_baseline_without_double_count(db):
    config={'alltime':[dict(ticker='DEMO',wins=2,losses=0)],'owned_since':NOW-7*86400}
    with db.tx() as c:
        for week,wins,losses,status in [('2026-09-14',2,0,'LOSS'),('2026-09-21',2,1,'WIN')]:
            p=dict(ticker='DEMO',monday_date=week,alltime_wins_at_entry=wins,alltime_losses_at_entry=losses,status=status)
            c.execute(records.insert().values(project='smoothers',key=week,source_id=week,symbol='DEMO',source_ts=NOW,first_seen=NOW,updated=NOW,revision=week,payload={'original':p},context={}))
        save(db,c,dict(id='old',ticker='DEMO',week='2026-09-14',created_at=NOW-7*86400+1,status='LOSS'))
        save(db,c,dict(id='current',ticker='DEMO',week='2026-09-21',created_at=NOW,status='WIN'))
        result=effective(db,c,config,'2026-09-21')
        assert result['alltime']==[dict(ticker='DEMO',wins=2,losses=1)]
        assert result['statistics_anchors']=={'DEMO':'2026-09-14'}
        # Source data after the accepted sender boundary cannot alter history.
        from compass.native_config import KEY
        db.put(c,'native:ownership:smoothers',{'effective_week':'2026-09-21'})
        assert effective(db,c,config,'2026-09-28')['alltime']==[dict(ticker='DEMO',wins=3,losses=1)]
    assert config['alltime'][0]['losses']==0


def test_monitor_refetches_gap_older_than_overlap_then_clears_flag(db):
    start=NOW
    class Data:
        calls=[]
        repaired=False
        def bar_batch(self,symbols,frame,begin,end):
            self.calls.append(begin)
            stamps=list(range(int(begin),int(end),60))
            if not self.repaired:stamps.remove(int(start+60))
            return {'DEMO':pd.DataFrame(dict(open=100,high=101,low=99,close=100),index=pd.to_datetime(stamps,unit='s',utc=True))}
    data=Data()
    with db.tx() as c:
        db.put(c,VERSION+':week:2026-09-21',{'sessions':[dict(date='2026-09-21',open=start-3600,close=start+3600)]})
        save(db,c,dict(id='x',week='2026-09-21',ticker='DEMO',status='OPEN',direction='CALL',target_price=105,model_entry_time=start))
    monitor(db,data,start+615)
    with db.tx() as c:
        p=c.execute(select(weekly.c.payload)).scalar_one()
        assert p['coverage_missing'] and p['coverage_missing_since']==start+60
    data.repaired=True
    monitor(db,data,start+675)
    assert data.calls[-1]==start+60
    with db.tx() as c:
        p=c.execute(select(weekly.c.payload)).scalar_one()
        assert not p['coverage_missing'] and p['coverage_missing_since'] is None


def test_final_quotes_preserve_selection_observation_and_fail_closed():
    p=dict(id='x',config={'backtest_wr':.75},contract={'symbol':'DEMO260925C00100000','strike':100,'expiration':'2026-09-25'},
           direction='CALL',entry_price=100,target_price=102,target_atr_mult=.25,stats_at_entry={'wins':2,'losses':1},
           quote={'status':'available','midpoint':1},entry_premium=1,est_return_pct=10)
    class Data:
        def quotes(self,contracts):return {contracts[0]['symbol']:{'status':'available','midpoint':2,'bid':1.9,'ask':2.1,'quote_at_ms':NOW*1000}}
    refresh_entry_quotes([p],Data(),NOW)
    assert p['entry_premium']==2 and p['selection_option_observation']['entry_premium']==1
    class Failed:
        def quotes(self,contracts):raise TimeoutError()
    refresh_entry_quotes([p],Failed(),NOW+1)
    assert p['entry_premium'] is None and p['est_return_pct'] is None
    assert p['selection_option_observation']['entry_premium']==1
    assert p['quality_option_adjustment']==-7.5
