import pytest
from compass.archive_schedule import cycle,KEY
from compass.store import Store,events
from test_archive_object import Objects


def insert(db,i):
    with db.tx() as c:c.execute(events.insert().values(id=i,key=str(i),kind='quote',source='fixture',symbol='X',ts=float(i),received=float(i),payload={'n':i}))


def test_restart_catchup_tail_and_late_commit_repair(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'source'));db.initialize();objects=Objects()
    for i in (1,4,5):insert(db,i)
    first=cycle(db,objects,'private',width=2,chunks=1)
    assert first['frontier']==2
    second=cycle(db,objects,'private',width=2,chunks=2)
    assert second['frontier']==4 and second['tail_receipt']['last_id']==5
    insert(db,2)  # A transaction commits behind the saved export frontier.
    for _ in range(3):result=cycle(db,objects,'private',width=2,chunks=1)
    assert result['repairs']==1 and not result['cleanup_enabled']
    with db.tx() as c:record=db.get(c,KEY+':range:0')
    assert record['receipt']['rows']==2 and record['replaces_receipt']
    db.engine.dispose()


def test_failed_upload_does_not_advance_checkpoint(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'source'));db.initialize();insert(db,2)
    class Failed(Objects):
        def put_object(self,**kwargs):raise RuntimeError('network failed')
    with pytest.raises(RuntimeError):cycle(db,Failed(),'private',width=2,chunks=1)
    with db.tx() as c:assert db.get(c,KEY) is None
    result=cycle(db,Objects(),'private',width=2,chunks=1)
    assert result['frontier']==2
    db.engine.dispose()


def test_remote_corruption_is_replaced_before_audit_advances(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'source'));db.initialize();insert(db,2)
    objects=Objects();cycle(db,objects,'private',width=2,chunks=1)
    with db.tx() as c:old=db.get(c,KEY+':range:0')['receipt']['object_key']
    objects.data[old]=b'corrupt'
    result=cycle(db,objects,'private',width=2,chunks=1)
    assert result['repairs']==1 and result['audit_passes']==1
    db.engine.dispose()


def test_audit_runs_before_catchup_can_exhaust_time_budget(tmp_path,monkeypatch):
    import compass.archive_schedule as module
    db=Store('sqlite:///'+str(tmp_path/'source'));db.initialize();insert(db,2)
    objects=Objects();cycle(db,objects,'private',width=2,chunks=1)
    insert(db,1);insert(db,4)
    real=module.run;calls=[]
    def tracked(*args,**kwargs):
        calls.append(args[3]);return real(*args,**kwargs)
    monkeypatch.setattr(module,'run',tracked)
    result=cycle(db,objects,'private',width=2,chunks=1)
    assert calls==[0,2]  # Repair the old range before copying the new range.
    assert result['audits_this_run']==1 and result['repairs']==1
    db.engine.dispose()
