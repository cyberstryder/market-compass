"""Direct TradingView entries must reach research without the original app."""
import pytest
from sqlalchemy import select, func
from native_morning_fixtures import signal_event, checkpoint_event
from test_secondary import db, seed, NOW
from compass.config import Config
from compass import native_morning_worker as worker
from compass.native_morning import store as native
from compass.native_morning.frames import FEATURES, SOURCE
from compass.native_outbox import outbox
from compass.projects import ingest, records
from compass.secondary import Secondary, reviews
from compass.store import events


def frame(entry=True):
    s = signal_event(stamp=int(NOW*1000))['signal']
    s['script_version'] = '1.3.0'
    s['features'] = [s['features'][k] for k in FEATURES]
    opened = int((NOW-90*60)*1000)
    session = {k:s[k] for k in ('ticker','stream_id','script_version','logic_mode','timeframe_min','settings','is_test')}
    session.update(session_id='direct-session', session_open_ms=opened, activated_at_ms=opened,
        policy=dict(version='shadow_v1',candidate_window_min=180,followup_min=180,
            delayed_bars=3,max_extension_pct=1.,pullback_pct=.3,reclaim_wait_min=20,reclaim_cooldown_min=10))
    return dict(schema_version=4,event_type='frame',observed_at_ms=int(NOW*1000),part=1,
        session=session,research_enabled=True,until_ms=opened+360*60000,
        bars=[[int((NOW-60)*1000),100.,101.,99.,100.,10000.]],records=[],
        source=[s[k] for k in SOURCE],entry=entry,checkpoints=[])


@pytest.mark.parametrize('use_frame',[False,True])
@pytest.mark.parametrize('original_first',[False,True])
def test_direct_entry_reviews_once_without_original_dependency(db,use_frame,original_first):
    worker.initialize(db)
    review = Secondary(db,Config(morning_url='',secondary_alerts=True,secondary_native=False))
    review.tick(NOW-1)
    seed(db,NOW,symbol='DEMO')
    raw = frame() if use_frame else signal_event(stamp=int(NOW*1000))
    def original():
        with db.tx() as c:
            ingest(db,c,'morning',dict(id='fixture-1',symbol='NASDAQ:DEMO',source_ts=NOW,
                side='long',strategy='STANDARD_ORB',version='1.3.0',entry=100.,status='tracking'),NOW)
    if original_first:
        original()
        review.tick(NOW)
    worker.accept(db,raw,NOW)
    worker.accept(db,raw,NOW+1)
    review.tick(NOW+1)
    with db.tx() as c:
        row=c.execute(select(reviews)).mappings().one()
        assert row['decision']['timely']
        assert row['source_ts']==NOW
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='alert')).scalar_one()==1
        assert set(c.execute(select(outbox.c.status)).scalars())=={'shadow'}
        if not original_first:
            assert row['candidate']['intake_origin']=='direct'
            assert c.execute(select(func.count()).select_from(records)).scalar_one()==0
    original()
    review.tick(NOW+2)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(reviews)).scalar_one()==1
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='alert')).scalar_one()==1


@pytest.mark.parametrize('kind',['test','late','relay','checkpoint','recovery'])
def test_non_entry_inputs_do_not_trigger_research(db,kind):
    worker.initialize(db)
    raw=signal_event(stamp=int(NOW*1000),is_test=kind=='test')
    now=NOW
    if kind=='checkpoint':
        raw=checkpoint_event(raw);now=NOW+300
    if kind=='recovery':raw=frame(entry=False)
    if kind=='late':now=NOW+61
    worker.accept(db,raw,now,origin='source_summary_relay' if kind=='relay' else 'direct')
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(events).where(events.c.kind=='morning_entry')).scalar_one()==0


def test_research_event_failure_rolls_back_intake(db,monkeypatch):
    worker.initialize(db)
    original=db.append
    def append(c,kind,*args,**kwargs):
        if kind=='morning_entry':raise RuntimeError('synthetic research failure')
        return original(c,kind,*args,**kwargs)
    monkeypatch.setattr(db,'append',append)
    with pytest.raises(RuntimeError,match='synthetic research failure'):
        worker.accept(db,signal_event(stamp=int(NOW*1000)),NOW)
    with db.tx() as c:
        assert c.execute(select(func.count()).select_from(native.signals)).scalar_one()==0
        assert c.execute(select(func.count()).select_from(outbox)).scalar_one()==0
