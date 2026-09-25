from compass.readiness import decorate_health
from compass.store import Store


def test_completed_bar_cannot_rewind_the_shared_futures_stream_health(tmp_path):
    db = Store("sqlite:///" + str(tmp_path / "health.db"))
    db.initialize()
    now = 1800000000
    db.health("databento_futures", "receiving", "live BBO", now - 1, monotonic_source=True)
    db.health("databento_futures", "receiving", "completed minute bar", now - 62, monotonic_source=True)
    with db.tx() as c:
        recorded = db.get(c, "health:databento_futures")
    view = decorate_health([recorded], {"worker:collector": {"at": now}}, {"futures": True}, now)[0]
    assert recorded["source_ts"] == now - 1
    assert view["status"] == "receiving" and view["age"] == 1
    # Advancing only the HTTP/task clock must not conceal a real stream outage.
    later = decorate_health([recorded], {"worker:collector": {"at": now + 30}}, {"futures": True}, now + 30)[0]
    assert later["status"] == "stale" and later["age"] == 31
    db.engine.dispose()


def test_web_heartbeat_cannot_mask_stopped_secondary_engine_or_import_collector():
    now = 1800000000
    workers = {"worker:web": {"at": now}, "worker:engine": {"at": now - 30}, "worker:collector": {"at": now - 30}}
    names = ("secondary", "project_morning", "project_smoothers")
    items = [{"name": name, "status": "running", "checked_at": now, "detail": "test"} for name in names]
    result = decorate_health(items, workers, {}, now)
    assert all(r["status"] == "stale" for r in result)
    assert result[0]["detail"].startswith("engine worker")
    assert result[1]["detail"].startswith("collector worker")


def test_secondary_stopped_task_and_intentionally_disabled_task_are_distinct():
    now = 1800000000
    worker = {"worker:engine": {"at": now}}
    item = {"name": "secondary", "status": "running", "checked_at": now - 30, "detail": "test"}
    assert decorate_health([item], worker, {}, now)[0]["status"] == "stale"
    item["status"] = "disabled"
    assert decorate_health([item], worker, {}, now)[0]["status"] == "disabled"


def test_recovery_and_audit_health_use_collector_and_detect_stopped_tasks():
    now=1800000000
    items=[dict(name=name,status=status,checked_at=now,detail='fixture')
           for name,status in [('stock_recovery','idle'),('obsidian_history','complete')]]
    web={'worker:web':{'at':now}}
    assert all(h['status']=='stale' for h in decorate_health(items,web,{},now))
    workers={**web,'worker:collector':{'at':now}}
    assert [h['status'] for h in decorate_health(items,workers,{},now)]==['idle','complete']
    workers['worker:collector']['at']=now+100
    assert all(h['status']=='stale' for h in decorate_health(items,workers,{},now+100))
