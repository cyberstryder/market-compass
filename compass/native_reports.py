"""Reports computed from native records, with optional original-source comparisons."""
import asyncio
import json
import logging
import time
import uuid
from collections import Counter,defaultdict
from sqlalchemy import select
from .native_morning import store as m
from .native_morning import candidate_store as research
from .native_smoothers import weekly,VERSION,monday_for
from .native_outbox import outbox
from .native_routing import receipts
from .morning_report import boundaries
from .morning_schema.outcomes import stock_coverage,stock_exit,path_outcome,extend_baseline
from .projects import records
from .program_parity import morning_options
from .store import identity


def filtered(q,stamp,symbol,lower,upper,ticker):
    if lower is not None:q=q.where(stamp>=lower)
    if upper is not None:q=q.where(stamp<upper)
    if ticker:q=q.where((symbol==ticker)|symbol.endswith(':'+ticker,autoescape=True))
    return q


def option_comparison(native,original):
    other=(original or {}).get('option') or {}
    a=(native.get('contract') or {}).get('symbol');b=(other.get('contract') or {}).get('symbol')
    if not a or not b:return {'status':'unavailable','native_contract':a,'source_contract':b}
    return {'status':'same_contract' if a==b else 'different_contract','native_contract':a,'source_contract':b,
        'native_quote_at_ms':native.get('quote_at_ms'),'source_quote_at_ms':other.get('quote_at_ms'),
        'native_bid':native.get('bid'),'source_bid':other.get('bid')}


def comparison(native,source,fields):
    if source is None:return {'status':'source_unavailable','differences':{}}
    differences={k:{'native':native.get(k),'source':source.get(k)} for k in fields if native.get(k)!=source.get(k)}
    return {'status':'different' if differences else 'matched_fields','differences':differences,'fields':list(fields)}


def morning(db,c,now,limit=100,start='',end='',ticker=''):
    lower,upper=boundaries(start,end)
    rows=c.execute(filtered(select(m.signals).where(m.signals.c.is_test.is_(False)),m.signals.c.signal_at_ms,m.signals.c.ticker,lower,upper,ticker).order_by(m.signals.c.signal_at_ms.desc(),m.signals.c.signal_id).limit(limit+1)).mappings().all()
    truncated={'signals':len(rows)>limit};rows=rows[:limit];ids=[r['signal_id'] for r in rows]
    bars=defaultdict(list);samples=defaultdict(list);jobs={};source={};intents={};origins=defaultdict(list)
    if ids:
        for r in c.execute(select(m.stock_bars).where(m.stock_bars.c.signal_id.in_(ids)).order_by(m.stock_bars.c.open_at_ms)).mappings():
            b=json.loads(r['bar_json'])
            if b['open_at_ms']+60000<=now*1000:bars[r['signal_id']].append(b)
        for r in c.execute(select(m.option_samples).where(m.option_samples.c.signal_id.in_(ids)).order_by(m.option_samples.c.minute)).mappings():
            samples[r['signal_id']].append(dict(minute=r['minute'],status=r['status'],due_at_ms=r['due_at_ms'],quote=json.loads(r['quote_json']) if r['quote_json'] else None))
        jobs={r['signal_id']:dict(r) for r in c.execute(select(m.option_jobs).where(m.option_jobs.c.signal_id.in_(ids))).mappings()}
        source={r['source_id']:dict(r) for r in c.execute(select(records).where(records.c.project=='morning',records.c.source_id.in_(ids))).mappings()}
        keys=[sid+suffix for sid in ids for suffix in ('-signal','-signal-option')]
        intents={r['event_key']:dict(r) for r in c.execute(select(outbox).where(outbox.c.program=='morning',outbox.c.event_key.in_(keys))).mappings()}
        origin_rows=c.execute(select(receipts).where(receipts.c.signal_id.in_(ids)).order_by(receipts.c.received).limit(40001)).mappings().all()
        truncated['receipts']=len(origin_rows)>40000
        grouped={}
        for r in origin_rows[:40000]:
            key=(r['payload']['signal_id'],r['origin'])
            item=grouped.setdefault(key,{'origin':r['origin'],'first_received':r['received'],'last_received':r['received'],'packets':0})
            item.update(last_received=r['received'],packets=item['packets']+1)
        for (sid,_),item in grouped.items():origins[sid]=origins[sid]+[item]
    # Research outcomes use only the native research archive, with bounded tape reads.
    q=select(research.research_records,research.research_sessions.c.session_json).join(research.research_sessions,research.research_sessions.c.session_id==research.research_records.c.session_id).where(research.research_sessions.c.is_test.is_(False))
    selected=c.execute(filtered(q,research.research_records.c.at_ms,research.research_sessions.c.ticker,lower,upper,ticker).order_by(research.research_records.c.at_ms.desc(),research.research_records.c.record_id).limit(limit+1)).mappings().all()
    truncated['candidates']=len(selected)>limit;selected=selected[:limit]
    links={}
    if ids:
        for r in c.execute(select(research.research_records,research.research_sessions.c.session_json).join(research.research_sessions,research.research_sessions.c.session_id==research.research_records.c.session_id).where(research.research_records.c.source_signal_id.in_(ids))).mappings():links[r['source_signal_id']]=r
    session_ids={r['session_id'] for r in selected}|{r['session_id'] for r in links.values()};tapes=defaultdict(list)
    if session_ids:
        tape=c.execute(select(research.research_bars).where(research.research_bars.c.session_id.in_(session_ids)).order_by(research.research_bars.c.session_id,research.research_bars.c.open_at_ms).limit(50001)).mappings().all()
        truncated['research_bars']=len(tape)>50000
        for r in tape[:50000]:tapes[r['session_id']].append(json.loads(r['bar_json']))
    output=[];delivery=[]
    for r in rows:
        sid=r['signal_id'];s=json.loads(r['signal_json']);job=jobs.get(sid,{})
        ref=json.loads(job['quote_json']) if job.get('quote_json') else {'status':job.get('status','not_scheduled')}
        imported=source.get(sid);original=imported['payload'] if imported else None
        checked=morning_options(dict(id=sid,symbol=s['ticker'],source_ts=s['signal_at_ms']/1000,option=ref,option_samples=samples[sid]))
        link=links.get(sid)
        link={'record':json.loads(link['record_json']),'session':json.loads(link['session_json'])} if link else None
        merged,coverage,extended=extend_baseline(s,bars[sid],link,tapes,int(now*1000))
        if not extended:extended={str(h):path_outcome(s['price'],s['signal_at_ms'],merged,h,int(now*1000)) for h in (120,180)}
        output.append({'id':sid,'ticker':s['ticker'],'at':s['signal_at_ms']/1000,'received_at':r['received_at_ms']/1000,
            'initial_received_at_ms':r['initial_received_at_ms'],'origins':origins[sid],'signal':s,'config_id':identity(s['settings'],s['script_version'],s['timeframe_min'],s['stream_id']),
            'coverage':stock_coverage(s,bars[sid],True,int(now*1000)),
            'stock_exits':[stock_exit(s,bars[sid],h) for h in (5,15,30,60)]+[stock_exit(s,bars[sid],target=t,stop=v) for t,v in ((1.,.5),(2.,1.))],
            'extended':extended,'extended_coverage':coverage,'option':ref,'option_samples':samples[sid],'option_calculations':checked,
            'source_signal':comparison(s,original.get('original') if original else None,('price','setup','script_version','settings','features')),
            'source_option':option_comparison(ref,original),
            'source_option_samples':(original or {}).get('option_samples',[]),
            'source_quote_at_ms':(original.get('option') or {}).get('quote_at_ms') if original else None,
            'source_checked_at':imported['updated'] if imported else None})
        expected=[sid+'-signal'] if r['initial_received_at_ms'] else []
        if job.get('status')=='complete':expected.append(sid+'-signal-option')
        source_delivery={d['event_id']:d for d in (original or {}).get('delivery',{}).get('events',[])}
        for key in expected:
            intent=intents.get(key);actual=source_delivery.get(key)
            delivery.append({'signal_id':sid,'event_id':key,'status':'preview_present' if intent else 'missing_native_intent',
                'native_status':intent['status'] if intent else 'missing','source_status':actual.get('status') if actual else 'unknown',
                'source_delivered_at_ms':actual.get('delivered_at_ms') if actual else None,
                'preview':intent['payload'] if intent else None,
                'native_delivery':{k:v for k,v in intent['delivery'].items() if k in ('attempts','error','message_id','finished_at')} if intent else None})
    groups=defaultdict(list)
    for row in output:groups[(row['signal']['setup'],row['config_id'])].append(row)
    stock_groups=[]
    for (setup,config),group in groups.items():
        paired=[r for r in group if all(e['status']=='modeled' for e in r['stock_exits'])]
        stock_groups.append({'setup':setup,'config_id':config,'signals':len(group),'paired':len(paired),
            'excluded':len(group)-len(paired),'rules':[{'rule':e['rule'],'mean_return_pct':sum(r['stock_exits'][i]['return_pct'] for r in paired)/len(paired) if paired else None} for i,e in enumerate(group[0]['stock_exits'])]})
    candidates=[]
    for r in selected:
        p=json.loads(r['record_json']);s=json.loads(r['session_json'])
        candidates.append({'id':r['record_id'],'ticker':s['ticker'],'record':p,'session':s,
            'outcomes':{str(h):path_outcome(p['price'],p['at_ms'],tapes[r['session_id']],h,int(now*1000)) for h in (5,15,30,60,120,180)}})
    compare_lower=lower/1000 if lower is not None else db.get(c,'native-morning-v1:activation',now)
    compare_upper=upper/1000 if upper is not None else None
    sq=filtered(select(records.c.source_id,records.c.source_ts,records.c.payload),records.c.source_ts,records.c.symbol,compare_lower,compare_upper,ticker)
    src=c.execute(sq.where(records.c.project=='morning').order_by(records.c.source_ts.desc()).limit(limit+1)).all()
    truncated['source_inventory']=len(src)>limit
    # Query existence beyond the displayed native page to avoid false missing alerts.
    present=set(c.execute(select(m.signals.c.signal_id).where(m.signals.c.signal_id.in_([r.source_id for r in src[:limit]]))).scalars()) if src else set()
    source_only=[{'id':r.source_id,'at':r.source_ts,'status':'source_entry_not_received' if not r.payload.get('initial_received_at_ms') else 'awaiting_native' if now-r.source_ts<=120 else 'missing_native'} for r in src[:limit] if r.source_id not in present]
    return {'signals':output,'stock_groups':stock_groups,'candidates':candidates,'delivery':delivery,'source_only':source_only,'truncated':truncated,
        'summary':{'signals':len(output),'candidates':len(candidates),'coverage':dict(Counter(r['coverage']['status'] for r in output)),
            'delivery':dict(Counter(r['status'] for r in delivery)),'source_only':len(source_only)},
        'basis':'Native candles and quotes only. Original records are optional comparison evidence. Option returns are quote measurements, not fills. Preview identity does not prove Discord delivery or message-content parity.'}


def smoothers(db,c,now,limit=100,week='',ticker=''):
    week=week or monday_for(now).isoformat()
    from datetime import datetime,timedelta
    date=datetime.strptime(week,'%Y-%m-%d').date()
    if date.weekday()!=0:raise ValueError('Week must be its Monday date')
    q=select(weekly.c.payload).where(weekly.c.week==week)
    if ticker:q=q.where(weekly.c.ticker==ticker)
    rows=c.execute(q.order_by(weekly.c.ticker).limit(limit+1)).scalars().all();truncated=len(rows)>limit;rows=rows[:limit]
    from .native_smoothers_data import ET
    lo=datetime.combine(date,datetime.min.time(),ET).timestamp()
    source=c.execute(filtered(select(records),records.c.source_ts,records.c.symbol,lo,datetime.combine(date+timedelta(days=7),datetime.min.time(),ET).timestamp(),ticker).where(records.c.project=='smoothers').order_by(records.c.symbol,records.c.source_ts).limit(501)).mappings().all()
    by_symbol=defaultdict(list)
    for r in source:by_symbol[r['symbol']].append(r)
    results=[]
    for p in rows:
        matches=by_symbol[p['ticker']];r=matches[0] if len(matches)==1 else None
        other=r['payload'].get('original',{}) if r else None
        pair=comparison(p,other,('direction','entry_price','target_price','signal_type','status'))
        if len(matches)>1:pair={'status':'ambiguous_source_identity','differences':{}}
        results.append({'native':p,'comparison':pair,'source_checked_at':r['updated'] if r else None,
            'source_id':r['source_id'] if r else None,'source_contract':other.get('occ_symbol') if other else None})
    native_symbols=set(c.execute(select(weekly.c.ticker).where(weekly.c.week==week)).scalars())
    state=db.get(c,VERSION+':week:'+week,{})
    return {'week':week,'job':{k:v for k,v in state.items() if k!='config'},'rows':results,
        'source_only':[{'ticker':r['symbol'],'source_id':r['source_id'],'status':'native_week_not_run' if state.get('state') in ('missed','waiting',None) else 'not_selected_or_failed'} for r in source[:500] if r['symbol'] not in native_symbols],
        'truncated':{'native':truncated,'source':len(source)>500},
        'summary':dict(Counter(p['status'] for p in rows)),
        'basis':'Native weekly results, target evidence and latest premium model; stock target outcomes are not realized option P&L. Source comparison is optional and snapshot-timed.'}


def report(db,c,now,limit=100,start='',end='',ticker='',week=''):
    if not 1<=limit<=200:raise ValueError('Limit must be 1–200')
    return {'at':now,'morning':morning(db,c,now,limit,start,end,ticker),'smoothers':smoothers(db,c,now,limit,week,ticker),'cutover_ready':False,
        'remaining_gates':['Direct intake session reconciliation','Full native weekly comparison','Official delivery ownership and rollback rehearsal','Source backup/restore before retirement']}


async def run(db):
    owner=uuid.uuid4().hex
    while True:
        try:
            with db.tx() as c:active=db.lease(c,'native-program-report-v1',owner,180)
            if active:
                def compute():
                    with db.tx() as c:
                        r=report(db,c,time.time())
                        summary={'at':r['at'],'morning':r['morning']['summary'],'smoothers':r['smoothers']['summary'],'cutover_ready':False}
                        db.put(c,'native-program-report-v1:summary',summary)
                    logging.getLogger('uvicorn.error').info('Native program report: %s',summary)
                await asyncio.to_thread(compute)
        except Exception as e:db.health('native_program_report','error',type(e).__name__)
        await asyncio.sleep(120)
