from copy import deepcopy
from datetime import datetime
import pytest
from sqlalchemy import select
from compass.store import Store,discord_jobs
from compass.alert_ownership import assess,read_trade,report,format_message,quote_error,CT,block_entry
# Module level so the native outbox table is registered on store.meta before
# db.initialize() runs (in-function imports registered it too late).
from compass.native_outbox import queue as native_queue,deliver_one as native_deliver_one,outbox as native_outbox

NOW=datetime(2026,9,23,9,0,tzinfo=CT).timestamp()
@pytest.fixture
def db(tmp_path):
    db=Store('sqlite:///'+str(tmp_path/'ownership.db'));db.initialize()
    yield db
    db.engine.dispose()

def row(i=1,symbol='O:QQQ260923P00500000',source='setup_study',**payload):
    return dict(id=i,symbol=symbol,source=source,ts=NOW,payload=dict(id='study'+str(i),status='option_setup_new',entry=2.1,stop=1.5,target=3.3,last_quote=dict(ts=NOW,bid=2,ask=2.1),**payload))

def changed(r,i,**p):
    return dict(r,id=i,payload={**r['payload'],**p})

@pytest.mark.parametrize('symbol,source,payload,expected',[
 ('O:SPY260923C00600000','setup_study',{},'spy_morning'),
 ('O:QQQ260923P00500000','setup_study',{},'options_0dte'),
 ('O:QQQ261002C00500000','option_ideas',{},'options_ideas'),
 ('O:QQQ261002C00500000','swing_ideas',{},'swing'),
 ('O:QQQ280121C00500000','swing_ideas',{},'swing'),
 ('O:QQQ261002C00500000','smoothers',{},'smoothers'),
])
def test_categories(db,symbol,source,payload,expected):
    with db.tx() as c:
        r=row(symbol=symbol,source=source,**payload);a=assess(db,c,r,NOW)
        assert a['category']==expected
        text=format_message(dict(r,payload=a['payload']))
        assert 'CALL' in text or 'PUT' in text
        if expected=='smoothers':
            # Smoothers ENTRY uses the compact human-readable rendering.
            assert 'Trade ID:' not in text and 'enter by' not in text  # no quote/deadline on this bare row
        else:
            assert 'Observed bid / ask' in text and 'Trade ID:' in text

@pytest.mark.parametrize('patch',[
 {'ts':NOW-61},{'ts':NOW+1},{'bid':0},{'bid':2.2},{'ask':4},{'ts':None}, {'ask':float('nan')}
])
def test_bad_quotes_never_admitted(db,patch):
    r=row();r['payload']['last_quote'].update(patch)
    with db.tx() as c:
        assert assess(db,c,r,NOW)['action']=='research'
        assert not report(db,c,'2026-09-23')['records']

def test_quote_delayed_by_delivery_tick_still_admitted(db):
    # The Discord delivery tick verifies webhooks and sends before it assesses, so a
    # quote that was fresh at selection can be tens of seconds old at assess time.
    # That must not suppress the alert; the 120s entry expiry is the actionability bound.
    r=row();r['payload']['last_quote']['ts']=NOW-30
    with db.tx() as c:
        a=assess(db,c,r,NOW)
        assert a['action']=='publish' and a['category']=='options_0dte'

def test_stock_only_and_mirrors_are_research(db):
    with db.tx() as c:
        assert assess(db,c,row(symbol='AVGO'),NOW)['action']=='research'
        assert assess(db,c,row(2,status_override='ignored'),NOW)['action']=='publish'
        assert assess(db,c,changed(row(),3,status='secondary_review'),NOW)['action']=='research'

def test_primary_lifecycle_and_alternate_contract_suppression(db):
    primary=row()
    with db.tx() as c:
        a=assess(db,c,primary,NOW);tid=a['trade_id']
        assert assess(db,c,primary,NOW+100)==a
        b=assess(db,c,row(2,symbol='O:QQQ260923P00505000',source='other_scanner'),NOW)
        assert b['action']=='suppressed' and b['trade_id']==tid
        secondary=changed(row(2,symbol='O:QQQ260923P00505000',source='other_scanner'),3,status='setup_result',outcome='win',pnl=100)
        assert assess(db,c,secondary,NOW)['action']=='suppressed'
        assert read_trade(db,c,tid)[1]['state']=='open'
        u=changed(primary,4,status='option_idea_update_25',premium_stop=2)
        assert assess(db,c,u,NOW)['action']=='publish'
        assert assess(db,c,changed(u,5),NOW)['reason']=='Unchanged update'
        end=assess(db,c,changed(primary,6,status='setup_result',outcome='loss',pnl=-61),NOW)
        assert end['trade_id']==tid and end['event']=='EXIT'
        assert assess(db,c,changed(primary,7,status='setup_result'),NOW)['action']=='suppressed'
        results=report(db,c,'2026-09-23');g=next(x for x in results['groups'] if x['category']=='options_0dte')
        assert g['ideas']==1 and g['net_pnl'] is None
        c.execute(discord_jobs.insert().values(event_id=1,route='options_0dte',status='sent',queued_at=NOW,confirmation={}))
        g=next(x for x in report(db,c,'2026-09-23')['groups'] if x['category']=='options_0dte')
        assert g['net_pnl']==-61 and g['resolved_delivered']==1

def test_conflict_recorded_and_new_direction_allowed_after_exit(db):
    with db.tx() as c:
        assess(db,c,row(),NOW)
        b=assess(db,c,row(2,symbol='O:QQQ260923C00500000'),NOW)
        assert b['action']=='research' and b['conflict_trade_id']
        assess(db,c,changed(row(),3,status='setup_result',outcome='unresolved'),NOW)
        assert assess(db,c,row(4,symbol='O:QQQ260923C00500000'),NOW)['action']=='publish'

def test_unpublished_entry_cannot_manage_or_count_as_delivered(db):
    with db.tx() as c:
        a=assess(db,c,row(),NOW);block_entry(db,c,a['trade_id'],'Stale in queue',NOW+11)
        assert assess(db,c,changed(row(),2,status='setup_result'),NOW+12)['action']=='suppressed'
        g=next(x for x in report(db,c,'2026-09-23')['groups'] if x['category']=='options_0dte')
        assert g['unpublished']==1 and g['delivered_entries']==0

@pytest.mark.parametrize('reply,expected',[(200,'sent'),(429,'pending'),(500,'ambiguous'),('timeout','ambiguous')])
def test_delivery_receipts_and_unknown_outcomes(db,reply,expected):
    import asyncio,httpx
    from compass.alerts import DeliveryWorker
    from compass.config import Config
    from compass.store import events
    with db.tx() as c:
        r=row();db.append(c,'alert',r['source'],r['symbol'],NOW,r['payload'])
        r=dict(c.execute(select(events).where(events.c.kind=='alert')).mappings().one())
        a=assess(db,c,r,NOW)
        c.execute(discord_jobs.insert().values(event_id=r['id'],route=a['category'],status='pending',queued_at=NOW,confirmation=a))
    worker=DeliveryWorker(db,Config(local=True));calls=[]
    def handler(req):
        calls.append(req)
        if reply=='timeout':raise httpx.ReadTimeout('unknown',request=req)
        return httpx.Response(reply,json={'id':'12345','retry_after':1})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await worker.send(client,'https://discord.com/api/webhooks/1/test',r,a['category'],NOW)
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(discord_jobs.c.status)).scalar_one()==expected
    assert len(calls)==1

def test_stale_delivery_never_sends(db):
    import asyncio,httpx
    from compass.alerts import DeliveryWorker
    from compass.config import Config
    from compass.store import events
    with db.tx() as c:
        r=row();db.append(c,'alert',r['source'],r['symbol'],NOW,r['payload'])
        r=dict(c.execute(select(events).where(events.c.kind=='alert')).mappings().one());a=assess(db,c,r,NOW)
        c.execute(discord_jobs.insert().values(event_id=r['id'],route=a['category'],status='pending',queued_at=NOW,confirmation=a))
    def handler(req):raise AssertionError('stale quote must not send')
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await DeliveryWorker(db,Config(local=True)).send(client,'https://discord.com/api/webhooks/1/test',r,a['category'],NOW+61)
    asyncio.run(run())
    with db.tx() as c:
        assert c.execute(select(discord_jobs.c.status)).scalar_one()=='suppressed'
        assert read_trade(db,c,a['trade_id'])[1]['publication_block']

def test_native_smoothers_uses_same_ledger_and_receipts(db):
    import httpx
    with db.tx() as c:
        db.put(c,'native:ownership:smoothers',dict(owner='compass',previous_sender_paused=True,accepted_at=NOW-2,epoch='test',effective_from=NOW-1))
        p=dict(id='weekly1',status='native_option_entry',contract={'symbol':'QQQ261002C00500000'},track='swing',quote=dict(bid=2,ask=2.1,ts=NOW))
        native_queue(db,c,'smoothers','entry',{'content':'legacy'},NOW,event_time=NOW,cohort_time=NOW,publication=p)
    sent=[]
    def handler(req):
        sent.append(req.content.decode());return httpx.Response(200,json={'id':'1234'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert native_deliver_one(db,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},True,NOW)
        assert not native_deliver_one(db,client,{'smoothers':'https://discord.com/api/webhooks/123/test'},True,NOW+1)
    assert len(sent)==1 and 'SMOOTHERS | ENTRY' in sent[0] and 'Trade ID:' not in sent[0] \
        and 'Event #' not in sent[0]
    with db.tx() as c:
        assert c.execute(select(native_outbox.c.status)).scalar_one()=='delivered'
        g=next(x for x in report(db,c,'2026-09-23')['groups'] if x['category']=='smoothers')
        assert g['ideas']==g['delivered_entries']==1


def _smoothers_entry_payload(**over):
    contract=dict(symbol='O:META261007C00745000',underlying='META',expiration='2026-10-07',type='CALL',strike=745)
    payload=dict(
        id='sig1',project='smoothers',contract=contract,track='swing',status='native_option_entry',
        entry=7.88,underlying_target=751.61,entry_price=744.17,target_price=751.61,
        est_return_pct=53.2,week='2026-10-05',
        quote=dict(bid=7.80,ask=7.95,ts=NOW),
        expires_at=NOW+1800,exit_rule='Underlying target, otherwise Friday close; no premium stop configured',
        publication=dict(trade_id='abc123',category='smoothers',event='ENTRY',contract=contract,horizon='swing'))
    payload.update(over)
    return dict(id='native:xyz',source='smoothers',symbol='O:META261007C00745000',ts=NOW,payload=payload)


def test_smoothers_entry_compact_format():
    # 4-line human format: no internal IDs, quote folded into one line.
    text=format_message(_smoothers_entry_payload())
    lines=text.split('\n')
    assert lines[0]=='SMOOTHERS | ENTRY | META 745 CALL · 2026-10-07'
    assert lines[1]=='Stock $744.17 → target $751.61 · Est return +53%'
    assert lines[2]=='Option $7.80/$7.95 · Strike $745 · 3 DTE · enter by 09:30 CT'
    assert lines[3]=='Exit: Underlying target, otherwise Friday close; no premium stop configured — no broker order'
    assert len(lines)==4
    assert 'Trade ID:' not in text and 'Event #' not in text and 'Quote source' not in text


def test_smoothers_entry_compact_format_no_quote():
    text=format_message(_smoothers_entry_payload(quote={}))
    assert 'quote unavailable' in text
    assert 'Trade ID:' not in text


def test_smoothers_entry_compact_format_missing_optionals():
    # Bare native row (no stock fields, no week, no deadline) still renders cleanly.
    text=format_message(_smoothers_entry_payload(entry_price=None,target_price=None,
        est_return_pct=None,week=None,expires_at=None,exit_rule=None))
    assert 'SMOOTHERS | ENTRY' in text and 'Trade ID:' not in text
    assert 'enter by' not in text and 'DTE' not in text


def test_smoothers_entry_window_is_30_minutes(db):
    # Josh 2026-10-05: 30-minute human entry window replaces the 120s bot window.
    from compass.native_smoothers import queue_signal,ENTRY_WINDOW_SECS
    assert ENTRY_WINDOW_SECS==1800
    p=dict(id='sig9',contract={'symbol':'O:META261007C00745000','underlying':'META',
            'expiration':'2026-10-07','type':'CALL','strike':745},
        entry_premium=7.88,target_price=751.61,entry_price=744.17,est_return_pct=53.2,
        week='2026-10-05',quote={'bid':7.80,'ask':7.95,'quote_at_ms':NOW*1000},
        model_entry_time=NOW)
    with db.tx() as c:
        queue_signal(db,c,p,'entry',{'content':'x'},NOW,event_time=NOW)
        from compass.native_outbox import outbox
        row=c.execute(select(outbox)).mappings().first()
    pub=row['delivery']['publication_input']
    assert pub['expires_at']==NOW+1800
    assert pub['entry_price']==744.17 and pub['week']=='2026-10-05'


def _smoothers_exit_payload(**over):
    contract=dict(symbol='O:COP261009C00127000',underlying='COP',expiration='2026-10-09',type='CALL',strike=127)
    payload=dict(
        id='sig2',project='smoothers',contract=contract,track='swing',status='native_option_exit',
        entry=2.01,underlying_target=128.80,entry_price=124.50,target_price=128.80,
        quote=dict(bid=2.72,ask=3.30,ts=NOW),
        exit_reason='target',signal_status='WIN',exit_underlying=128.80,
        exit_rule='Underlying target, otherwise Friday close; no premium stop configured',
        publication=dict(trade_id='def456',category='smoothers',event='EXIT',contract=contract,horizon='swing'))
    payload.update(over)
    return dict(id='native:xyz',source='smoothers',symbol='O:COP261009C00127000',ts=NOW,payload=payload)


def test_smoothers_exit_target_hit_compact():
    text=format_message(_smoothers_exit_payload())
    lines=text.split('\n')
    assert lines[0]=='SMOOTHERS | EXIT | COP 127 CALL · 2026-10-09'
    assert lines[1]=='Target hit · underlying reached $128.80'
    assert lines[2].startswith('Option $2.72/$3.30 @ ')
    assert 'modeled entry $2.01 (+50% ref)' in lines[2]
    assert len(lines)==3 and 'Research signal' not in text
    assert 'Trade ID:' not in text and 'Event #' not in text and 'Quote source' not in text
    assert 'Entry deadline' not in text


def test_smoothers_exit_target_hit_no_quote():
    text=format_message(_smoothers_exit_payload(quote={}))
    assert 'Option quote unavailable' in text
    assert 'Trade ID:' not in text


def test_smoothers_exit_week_close_target_not_hit():
    text=format_message(_smoothers_exit_payload(exit_reason='close',signal_status='LOSS',
        exit_underlying=124.10,quote={}))
    lines=text.split('\n')
    assert lines[0]=='SMOOTHERS | EXIT | COP 127 CALL · 2026-10-09'
    assert lines[1]=='Week closed · target $128.80 not hit'
    assert lines[2]=='Underlying $124.10 at close'
    assert len(lines)==3 and 'Research signal' not in text
    assert 'Trade ID:' not in text


def test_smoothers_exit_week_close_unresolved():
    text=format_message(_smoothers_exit_payload(exit_reason='close',signal_status='UNRESOLVED',
        exit_underlying=None,quote={}))
    assert 'Week closed · outcome unresolved (missing observations)' in text
    assert 'Trade ID:' not in text
