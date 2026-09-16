"""Reviewable Morning routing plans and receipt provenance; no external routing writes."""
from contextlib import contextmanager
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import Table,Column,String,Float,JSON,select,text
from .store import meta,identity
from .native_config import RevisionConflict

KEY='native:morning:routing'
receipts=Table('native_morning_receipts_v1',meta,Column('id',String(64),primary_key=True),
    Column('signal_id',String(500),index=True),Column('event_id',String(600),nullable=False,index=True),Column('origin',String(32),nullable=False),
    Column('received',Float,nullable=False),Column('payload',JSON,nullable=False))
ET=ZoneInfo('America/New_York')


def day(stamp):return datetime.fromtimestamp(stamp,ET).date().isoformat()


@contextmanager
def routing_guard(db,shared=False):
    # Intake holds a shared lock across its commits; an operator change takes
    # the exclusive lock. Independent intake requests may run concurrently.
    with db.engine.begin() as c:
        if c.dialect.name=='postgresql':
            function='pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
            c.execute(text('SELECT '+function+'(8675309061)'))
        yield c


def snapshot(db,c,now):
    state=db.get(c,KEY,{'revision':None,'schedule':[],'prepared':None})
    return dict(state,effective_mode=mode_for(state,day(now)),
        endpoint='/hooks/native/morning',credential='Existing scoped Morning token; not included in this response',
        official_sender='original',external_routing_changed=False,
        instructions=['Prepare a future-session additional shadow delivery; retain the existing TradingView-to-Morning route.',
            'Schedule only after arranging the additional shadow alert. Never replace the primary webhook while the original remains the official sender. This control does not edit TradingView.',
            'Keep the original official sender active during comparisons. No live sender activation is provided here.',
            'Cancel a future route before it starts, or prepare the next-session relay route for rollback.'])


def mode_for(state,date):
    eligible=[p for p in state.get('schedule',[]) if p['effective_session']<=date]
    return max(eligible,key=lambda p:p['effective_session'])['mode'] if eligible else 'parallel_shadow'


def change(db,expected,action,now,mode=None,effective_session=None,reason=''):
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=300:raise ValueError('Provide a reason, up to 300 characters')
    with routing_guard(db) as c:
        state=db.locked_get(c,KEY,{'revision':None,'schedule':[],'prepared':None})
        if state['revision']!=expected:raise RevisionConflict('Routing changed; reload before saving')
        previous=state['revision']
        if action=='prepare':
            if mode not in ('direct_shadow','relay_shadow'):raise ValueError('Invalid routing mode')
            date=datetime.strptime(effective_session or '','%Y-%m-%d').date()
            today=datetime.fromtimestamp(now,ET).date()
            if not today<date<=today+timedelta(days=30):raise ValueError('Choose a future session within 30 days')
            from .market import session
            if not session(date.isoformat()):raise ValueError('Choose an actual stock-market session')
            state['prepared']={'mode':mode,'effective_session':date.isoformat(),'reason':reason.strip()}
        elif action=='schedule':
            prepared=state.get('prepared')
            if not prepared or prepared['effective_session']<=day(now):raise ValueError('Prepare a future session first')
            state['schedule']=[p for p in state['schedule'] if p['effective_session']!=prepared['effective_session']]+[prepared]
            state['prepared']=None
        elif action=='cancel_future':
            state['schedule']=[p for p in state['schedule'] if p['effective_session']<=day(now)]
            state['prepared']=None
        else:raise ValueError('Unknown routing action')
        state['revision']=identity('morning-route-v1',previous,state,now,reason)
        db.put(c,KEY,state)
        db.append(c,'native_route_revision','morning','',now,dict(state,action=action,reason=reason,parent=previous),state['revision'])
        return snapshot(db,c,now)


def allowed(db,c,origin,session_day):
    mode=mode_for(db.get(c,KEY,{}),session_day)
    if mode=='direct_shadow' and origin!='direct':return False
    if mode=='relay_shadow' and origin=='direct':return False
    return True


def record(db,c,payload,origin,now,result):
    event_id=payload.event_id
    key=identity('native-receipt-v1',event_id,origin)
    # No credentials or request headers are retained.
    signal=payload.signal() if payload.event_type=='frame' else getattr(payload,'signal',None)
    data={'signal_id':signal.signal_id if signal else None,'status':result['status'],'schema_version':payload.schema_version,'event_type':payload.event_type,
          'checksum':identity(payload.model_dump())}
    c.execute(db.insert(receipts).values(id=key,signal_id=data['signal_id'],event_id=event_id,origin=origin,received=now,payload=data).on_conflict_do_nothing(index_elements=['id']))
