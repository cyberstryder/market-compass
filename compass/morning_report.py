"""Bounded stock-only reports from the immutable Morning history mirror."""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta
import logging
import time
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import select, cast, BigInteger
from .morning_history import history, digest, snapshot as import_snapshot
from .morning_schema.candidate_models import failed_filters
from .morning_schema.outcomes import stock_coverage, stock_exit, path_outcome, extend_baseline

HORIZONS = (5, 15, 30, 60, 120, 180)


def boundaries(start, end):
    def parse(value, upper=False):
        day = datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=ZoneInfo('America/New_York'))
        return int((day + timedelta(days=int(upper))).timestamp() * 1000)
    lower, upper = parse(start) if start else None, parse(end, True) if end else None
    if lower is not None and upper is not None and lower >= upper: raise ValueError('Start must not follow end')
    return lower, upper


def select_rows(c, kind, limit, lower, upper, ticker, stream):
    item = history.alias('item')
    parent = history.alias('parent')
    research = kind == 'candidate'
    stamp = cast(item.c.payload['at_ms' if research else 'signal_at_ms'].as_string(), BigInteger)
    meta = parent.c.payload if research else item.c.payload
    q = select(item)
    if research:
        q = q.join(parent, (parent.c.kind == 'session') & (parent.c.source_id == item.c.parent))
    q = q.where(item.c.kind == kind)
    if lower is not None: q = q.where(stamp >= lower)
    if upper is not None: q = q.where(stamp < upper)
    if ticker:
        sym = meta['ticker'].as_string()
        q = q.where((sym == ticker) | sym.endswith(':' + ticker, autoescape=True))
    if stream: q = q.where(meta['stream_id'].as_string() == stream)
    rows = c.execute(q.order_by(stamp.desc(), item.c.source_id).limit(limit + 1)).mappings().all()
    return rows[:limit], len(rows) > limit


def load_kind(c, kind, parents):
    if not parents: return []
    return c.execute(select(history).where(history.c.kind == kind, history.c.parent.in_(sorted(parents)))
        .order_by(history.c.source_id)).mappings().all()


def build_report(db, c, now, limit=100, start='', end='', ticker='', stream=''):
    if not 1 <= limit <= 200: raise ValueError('Limit must be 1–200 per section')
    lower, upper = boundaries(start, end)
    raw_signals, signal_truncated = select_rows(c, 'signal', limit, lower, upper, ticker, stream)
    raw_candidates, candidate_truncated = select_rows(c, 'candidate', limit, lower, upper, ticker, stream)
    signal_ids = {r['source_id'] for r in raw_signals}
    links = {}
    if signal_ids:
        for r in c.execute(select(history).where(history.c.kind == 'candidate',
                history.c.payload['source_signal_id'].as_string().in_(sorted(signal_ids)))).mappings():
            links[r['payload']['source_signal_id']] = r
    session_ids = {r['parent'] for r in raw_candidates} | {r['parent'] for r in links.values()}
    sessions = {r['source_id']: r['payload'] for r in load_kind(c, 'session', session_ids)}
    tapes, candles = defaultdict(list), defaultdict(list)
    for kind, ids, destination in [('research_bar', session_ids, tapes), ('signal_bar', signal_ids, candles)]:
        for r in load_kind(c, kind, ids):
            destination[r['parent']].append({**r['payload'], 'received_at_ms': round(r['source_received'] * 1000)})
        for bars in destination.values(): bars.sort(key=lambda b:b['open_at_ms'])
    expected = {r['parent'] for r in load_kind(c, 'events', signal_ids) if r['payload']['payload']['schema_version'] == 2}
    signals, candidates, stock_groups, candidate_groups = [], [], defaultdict(list), defaultdict(list)
    now_ms = int(now * 1000)
    for raw in raw_signals:
        s = raw['payload']; sid = s['signal_id']; bars = candles[sid]
        coverage = stock_coverage(s, bars, sid in expected or s['script_version'] == '1.3.0', now_ms)
        exits = [stock_exit(s, bars, h) for h in (5,15,30,60)]
        exits += [stock_exit(s, bars, target=t, stop=stop) for t, stop in ((1.,.5),(2.,1.))]
        linked = links.get(sid)
        link = {'record':linked['payload'], 'session':sessions[linked['parent']]} if linked else None
        merged, extended, outcomes = extend_baseline(s, bars, link, tapes, now_ms)
        if s['script_version'] == '1.3.0' and not link:
            outcomes = {str(h):path_outcome(s['price'],s['signal_at_ms'],merged,h,now_ms) for h in (120,180)}
            extended = outcomes['180']
        config = digest({'settings':s['settings'],'tf':s['timeframe_min'],'mode':s['logic_mode'],'version':s['script_version']})[:12]
        central = datetime.fromtimestamp(s['signal_at_ms']/1000, ZoneInfo('America/Chicago'))
        bucket = f'{central.hour:02d}:{central.minute//15*15:02d}'
        row = {'signal_id':sid, 'ticker':s['ticker'],'setup':s['setup'],'at_ms':s['signal_at_ms'],
            'stream_id':s['stream_id'],'config_id':config,'logic_mode':s['logic_mode'],'script_version':s['script_version'],
            'time_bucket_central':bucket,'stock_history':coverage,'modeled_exits':exits,
            'extended_stock_history':extended,'extended_checkpoints':outcomes,
            'source_received_at':raw['source_received'],'imported_at':raw['imported_at']}
        signals.append(row)
        stock_groups[(bucket,s['setup'],s['logic_mode'],config,s['stream_id'])].append(row)
    for raw in raw_candidates:
        r=raw['payload'];s=sessions[raw['parent']]
        config=digest({k:s[k] for k in ('settings','policy','logic_mode','script_version','timeframe_min')})[:12]
        central=datetime.fromtimestamp(r['at_ms']/1000,ZoneInfo('America/Chicago'))
        bucket=f'{central.hour:02d}:{central.minute//15*15:02d}'
        outcomes={str(h):path_outcome(r['price'],r['at_ms'],tapes[raw['parent']],h,now_ms) for h in HORIZONS}
        row={**r,'ticker':s['ticker'],'session_id':raw['parent'],'stream_id':s['stream_id'],
            'config_id':config,'logic_mode':s['logic_mode'],'time_bucket_central':bucket,
            'failed_filters':failed_filters(r),'outcomes':outcomes,'source_received_at':raw['source_received'],
            'imported_at':raw['imported_at']}
        candidates.append(row);candidate_groups[(r['kind'],config,s['stream_id'],bucket)].append(row)
    comparisons=[]
    for (bucket,setup,mode,config,stream_id),members in stock_groups.items():
        complete=[r for r in members if r['stock_history']['status']=='complete']
        paired=[r for r in complete if all(e['status']=='modeled' for e in r['modeled_exits'])]
        comparisons.append({'time_bucket_central':bucket,'setup':setup,'mode':mode,'config_id':config,'stream_id':stream_id,
            'signals':len(members),'stock_complete':len(complete),'paired_stock_n':len(paired),
            'ambiguous_excluded':len(complete)-len(paired),'incomplete_or_legacy':len(members)-len(complete),
            'stock_rules':[{'rule':e['rule'],'n':len(paired),
                'avg_return_pct':sum(r['modeled_exits'][i]['return_pct'] for r in paired)/len(paired) if paired else None,
                'positive_pct':sum(r['modeled_exits'][i]['return_pct']>0 for r in paired)*100/len(paired) if paired else None}
                for i,e in enumerate(members[0]['modeled_exits'])]})
    groups=[]
    for (kind,config,stream_id,bucket),members in candidate_groups.items():
        outcomes={}
        for h in HORIZONS:
            full=[r['outcomes'][str(h)] for r in members if r['outcomes'][str(h)]['status']=='complete']
            outcomes[str(h)]={'complete':len(full),'missing_or_collecting':len(members)-len(full),
                **{'avg_'+metric:sum(r[metric] for r in full)/len(full) if full else None for metric in ('return_pct','best_pct','worst_pct')}}
        groups.append({'kind':kind,'config_id':config,'stream_id':stream_id,'time_bucket_central':bucket,'records':len(members),'outcomes':outcomes})
    return {'version':'morning_stock_report_v1','as_of_ms':now_ms,'scope':{'start':start,'end':end,'ticker':ticker,
        'stream':stream,'date_timezone':'America/New_York','limit_per_section':limit,'selection':'most recent matching rows'},
        'truncated':{'signals':signal_truncated,'candidates':candidate_truncated},'import_status':import_snapshot(db,c,now),
        'signals':signals,'candidates':candidates,'stock_comparisons':comparisons,'candidate_comparisons':groups,
        'summary':{'signals':len(signals),'candidates':len(candidates),'stock_coverage':dict(Counter(r['stock_history']['status'] for r in signals)),
            'candidate_kinds':dict(Counter(r['kind'] for r in candidates))},
        'cutover_ready':False,'notes':[
            'Stock research only. Underlying returns are not option returns, broker fills or realized P&L.',
            'Historical imports are source observations, not forward Compass evidence. Import scans and source candle coverage are separate.',
            'Six stock rules use one complete 60-candle cohort; same-candle target/stop ambiguity is excluded from paired averages.',
            'Candidate horizon averages require contiguous coverage. Best/worst on incomplete rows are observed fragments, not full-path extremes.',
            'Candidate records within one stock/session are correlated. Comparisons are descriptive and not evidence that Compass improves originals.',
            'Dates use New York; entry groups use Chicago. Truncation and missing candles remain explicit.',
            'Option and delivery-history reports, direct webhook intake and strategy/notification transfer remain pending.']}


async def run(db):
    owner=uuid.uuid4().hex
    def audit():
        with db.tx() as c:
            if not db.lease(c,'morning-report',owner,120):return
            report=build_report(db,c,time.time())
            summary={**report['summary'],'at':report['as_of_ms']/1000,'truncated':report['truncated'],'cutover_ready':False}
            db.put(c,'morning_report:status',summary)
        logging.getLogger('uvicorn.error').info('Morning stock report: %s',summary)
    while True:
        try:await asyncio.to_thread(audit)
        except asyncio.CancelledError:raise
        except Exception as exc:logging.getLogger('uvicorn.error').error('Morning stock report failed: %s',type(exc).__name__)
        await asyncio.sleep(300)
