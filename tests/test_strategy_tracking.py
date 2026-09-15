import copy
import pytest
from sqlalchemy import select
from compass.store import Store, events
from compass.projects import ingest
from compass.strategy_tracking import ledger, tick, snapshot, measure, comparison

@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tracking.db'))
    db.initialize()
    yield db
    db.engine.dispose()

def signal(ts=10000):
    return {'id':'signal-1','symbol':'SPY','source_ts':ts,'entry':100.,'side':'long',
        'strategy':'Morning Algo FAST_OPEN','version':'1.3','stream':'live',
        'original':{'settings':{'orb_bars':5}},'checkpoints':[]}

def quote(c, db, ts, received=None):
    db.append(c,'quote','alpaca','SPY',ts,{'ts':ts,'bid':100.,'ask':102.,'bid_size':1,'ask_size':1})
    c.execute(events.update().where(events.c.kind=='quote',events.c.ts==ts).values(received=received or ts))

def test_checkpoint_replay_restart_and_missing_not_losses(db):
    with db.tx() as c:
        ingest(db,c,'morning',signal(),10001)
        quote(c,db,10301)
    tick(db,'worker',10600)
    with db.tx() as c:
        r=c.execute(select(ledger)).mappings().one()
        assert r['measurements']['5']['status']=='observed'
        assert r['measurements']['5']['return_pct']==pytest.approx(1.)
        assert '15' not in r['measurements']
    tick(db,'worker',14000)
    with db.tx() as c:
        r=c.execute(select(ledger)).mappings().one()
        assert r['measurements']['15']['status']=='not_observed'
        assert r['measurements']['complete'] is True
        assert len(db.recent(c,'alert'))==0
        assert db.prefix(c,'position:')=={}
        assert snapshot(db,c)['cutover_ready'] is False

def test_source_revisions_frozen_entry_and_cross_project_identity(db):
    row=signal()
    with db.tx() as c:
        ingest(db,c,'morning',row,10001)
        assert not ingest(db,c,'morning',row,10002)
        row=copy.deepcopy(row);row['entry']=105
        ingest(db,c,'morning',row,10003)
        ingest(db,c,'smoothers',row,10003)
        r=c.execute(select(ledger).where(ledger.c.project=='morning')).mappings().one()
        assert r['frozen']['entry']==100
        assert comparison(r,5)['status']=='source_entry_changed'
        assert len(db.recent(c,'strategy_revision'))==3

def test_backfill_and_stale_quotes_cannot_create_live_checkpoint(db):
    with db.tx() as c:
        ingest(db,c,'morning',signal(),10001)
        quote(c,db,10301,10400)
        r=c.execute(select(ledger)).mappings().one()
        assert measure(c,r,5,10400)['status']=='not_observed'
        quote(c,db,10302,10302)
        late={**r,'registered_at':10301}
        assert measure(c,late,5,10400)['reason']=='registered_after_checkpoint'
        assert measure(c,r,5,10305) is None

def test_reconciliation_keeps_price_basis_and_missing_source_explicit(db):
    row=signal()
    with db.tx() as c:
        ingest(db,c,'morning',row,10001);quote(c,db,10301)
    tick(db,'worker',10311)
    with db.tx() as c:
        r=c.execute(select(ledger)).mappings().one()
        assert comparison(r,5)['status']=='source_checkpoint_missing'
        row['checkpoints']=[{'horizon_min':5,'observed_at_ms':10300000,'continuous':True,'price':100.5}]
        ingest(db,c,'morning',row,10312)
        r=c.execute(select(ledger)).mappings().one()
        result=comparison(r,5)
        assert result['status']=='paired_different_price_basis'
        assert result['return_difference_pp']==pytest.approx(.5)
        row['checkpoints'][0]['continuous']=False
        ingest(db,c,'morning',row,10313)
        r=c.execute(select(ledger)).mappings().one()
        assert comparison(r,5)['status']=='source_timing_or_coverage_mismatch'

def test_bootstrap_does_not_rewind_live_revision(db):
    from compass.strategy_tracking import register
    old=signal();new=copy.deepcopy(old);new['status']='observed_60m'
    with db.tx() as c:
        register(db,c,'morning',new,10001)
        register(db,c,'morning',old,10002,refresh=False)
        r=c.execute(select(ledger)).mappings().one()
        assert r['latest']['status']=='observed_60m'
        assert len(db.recent(c,'strategy_revision'))==1
