import gzip
import json
import pytest
from sqlalchemy import select, func
from compass.store import Store, events
from compass.archive import export, verify, encode
from compass.storage_health import assess, snapshot
from compass.config import Config

@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'source.db'));db.initialize()
    with db.tx() as c:
        for i in range(5):db.append(c,'quote','fixture','SPY',1000+i,{'ts':1000+i,'bid':100+i,'ask':101+i,'unknown':None,'unicode':'✓'})
    yield db
    db.engine.dispose()

def test_archive_round_trip_bounds_continuation_and_no_delete(db,tmp_path):
    a=export(db,tmp_path/'first.gz',max_rows=3)
    assert a['count']==3 and a['last_id']==3 and a['may_have_more']
    assert not a['durable_copy_verified'] and not a['cross_chunk_completeness_verified']
    b=export(db,tmp_path/'second.gz',after_id=a['last_id'])
    assert b['count']==2 and b['last_id']==5
    assert verify(tmp_path/'first.gz')['verified']
    with gzip.open(tmp_path/'first.gz','rt') as f:rows=[json.loads(line) for line in f]
    assert rows[1]['payload']['unicode']=='✓'
    with db.tx() as c: assert c.execute(select(func.count()).select_from(events)).scalar_one()==5
    with pytest.raises(ValueError,match='already exists'):export(db,tmp_path/'first.gz')

def test_archive_rejects_corruption_and_false_continuation(db,tmp_path):
    export(db,tmp_path/'source.gz')
    with gzip.open(tmp_path/'source.gz','rt') as f:original=[json.loads(line) for line in f]
    for field,value in [('last_id',99),('sha256','wrong')]:
        rows=[dict(x) for x in original];rows[-1][field]=value
        with gzip.open(tmp_path/'bad.gz','wt') as f:
            for row in rows:f.write(encode(row).decode())
        with pytest.raises(ValueError):verify(tmp_path/'bad.gz')

def test_thresholds_growth_unknown_capacity_and_no_false_free_space():
    now=90000
    r=assess(44e9,1e9,50,[{'at':now-3600,'bytes':43e9}],now)
    assert r['status']=='high' and r['budget_fraction']==.9
    assert r['growth_bytes_per_hour']==pytest.approx(2e9) and r['hours_to_budget_at_observed_rate']==pytest.approx(2.5)
    assert r['filesystem_free_bytes'] is None
    assert assess(49e9,0,50,[],now)['status']=='critical'
    assert assess(1e9,None,0,[],now)['status']=='capacity_unknown'
    assert assess(2e9,0,50,[{'at':now-10,'bytes':1}],now)['growth_bytes_per_hour'] is None

def test_storage_report_stale_and_budget_validation(db):
    with db.tx() as c:
        db.put(c,'storage:report',{'at':1000,'status':'below_threshold'})
        assert snapshot(db,c,2000)['status']=='stale'
    with pytest.raises(ValueError,match='storage'):Config(local=True,storage_budget_gb=float('nan')).validate()


def test_isolated_restore_preserves_every_event_and_rejects_existing_destination(tmp_path):
    from compass.archive import restore_isolated
    from compass.store import Store
    db=Store('sqlite:///'+str(tmp_path/'source.sqlite'));db.initialize()
    with db.tx() as c:
        db.append(c,'quote','fixture','ABC',123,{'ts':123,'bid':10,'ask':11})
    archive=tmp_path/'events.gz';export(db,archive)
    destination=tmp_path/'restored.sqlite'
    result=restore_isolated(archive,destination)
    assert result['restore_verified'] and result['rows']==1 and not result['durable_copy_verified']
    with pytest.raises(ValueError,match='already exists'):restore_isolated(archive,destination)
    db.engine.dispose()
