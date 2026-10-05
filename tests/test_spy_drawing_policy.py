from copy import deepcopy
from test_spy_brief import db, seed_checks, seed, OPEN, NOW
from compass.config import Config
from compass.store import leases
from compass.spy_brief import BriefWorker, build, delivery_payload
from compass.spy_chart import drawing_changes, prepare_drawing, companion


def report(db):
    seed(db)
    with db.tx() as c:
        p=build(db,c,NOW,'opening')
    p['id']='example'
    return p


def test_material_threshold_and_cumulative_movement(db):
    first=report(db)
    with db.tx() as c:
        assert prepare_drawing(db,c,first)
        changed=deepcopy(first); changed['id']='small'
        changed['chart_levels'][0]['spy']+=.6
        # Remove estimates here so the fixture tests only the SPY price.
        for row in changed['chart_levels']:
            row['spx_estimate']=row['xsp_estimate']=None
        assert not prepare_drawing(db,c,changed)
        changed['id']='large'; changed['chart_levels'][0]['spy']+=.4
        assert prepare_drawing(db,c,changed)
        assert changed['drawing_update']['baseline_id']=='example'
        assert changed['drawing_update']['threshold_spy']==1
        assert not prepare_drawing(db,c,deepcopy(changed))


def test_atr_scaled_threshold_rank_order_and_mapping(db):
    first=report(db); first['context']['atr14_daily']=20
    next=deepcopy(first)
    next['chart_levels'][0]['spy']+=2
    assert drawing_changes(first,next)==(3,[])
    next['chart_levels'][0]['spy']+=1
    assert drawing_changes(first,next)[1]
    next=deepcopy(first)
    apex=[r for r in next['chart_levels'] if r['label'].startswith('Apex #')]
    if len(apex)>1:
        apex[0]['label'],apex[1]['label']=apex[1]['label'],apex[0]['label']
    assert not drawing_changes(first,next)[1]
    next=deepcopy(first)
    next['chart_levels'][0]['spx_estimate']+=40
    assert drawing_changes(first,next)[1]


def test_routine_checks_send_one_drawing_and_preserve_every_plan(db):
    worker=BriefWorker(db,Config(local=True))
    seed_checks(db,OPEN-600)
    first=worker.tick(OPEN-600)
    assert first['drawing_update']['send']
    for minute in (15,30,45,60):
        now=OPEN+minute*60+7
        seed_checks(db,now)
        with db.tx() as c:
            rows=db.get(c,'bar_window:SPY')
            for row in rows: row[6]=760  # unchanged VWAP in this no-material-change scenario
            db.put(c,'bar_window:SPY',rows)
            c.execute(leases.update().values(until=0))
        # Recreating the worker verifies persistence across restarts.
        p=BriefWorker(db,Config(local=True)).tick(now)
        assert not p['drawing_update']['send'], p['drawing_update']
        rendered=delivery_payload(dict(id=1,payload=p),now)
        assert 'message 1 of 2' not in rendered['content']
        assert 'embeds' not in rendered
    with db.tx() as c:
        rows=db.recent(c,'alert',limit=100)
    assert sum(r['payload']['status']=='spy_morning_brief' for r in rows)==5
    assert sum(r['payload']['status']=='spy_chart_prompt' for r in rows)==1


def test_legacy_queued_chart_is_baseline_and_new_day_resets(db):
    p=report(db)
    with db.tx() as c:
        db.append(c,'alert','spy_brief','SPY',NOW,companion(p),'legacy-chart')
        assert not prepare_drawing(db,c,p)
        tomorrow=deepcopy(p); tomorrow['day']='2026-09-17';tomorrow['id']='tomorrow'
        assert prepare_drawing(db,c,tomorrow)


def test_confirmed_entry_with_new_risk_levels_gets_update(db):
    p=report(db); before=deepcopy(p)
    before['chart_levels']=[r for r in before['chart_levels'] if r['label'] not in ('Call stop','Call target')]
    assert 'Call stop became available for entry' in drawing_changes(before,p)[1]
    p['decision_state']='waiting'
    assert not drawing_changes(before,p)[1]
