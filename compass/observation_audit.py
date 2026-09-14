"""Bounded operational evidence from existing records; never rescore reviews."""
import asyncio
import json
import logging
import time
from collections import Counter
from sqlalchemy import select
from .market import day, fresh, is_open, session
from .quote_coverage import stock_archive_health
from .secondary import reviews, comparisons, VERSION as REVIEW_VERSION
from .store import events
from .universe import data_symbols

VERSION = 'stock-observation-audit-v1'
# First whole second after PR23 collector rollout completed. Earlier PR22
# retained quotes may exist, but this conservative boundary has both fixes.
CORRECTED_SINCE = 1789414991.0
LOG = logging.getLogger('uvicorn.error')


def summarize_reviews(rows, since, now):
    corrected = [r for r in rows if since <= r['decided_at'] <= now and r['version'] == REVIEW_VERSION]
    checkpoint_groups = {}
    for row in rows:
        key = (row['project'], row['symbol'], row['version'])
        for horizon, point in row['measurements'].get('horizons', {}).items():
            due = point.get('due')
            if due is None or due < since or due > now:
                continue
            group = checkpoint_groups.setdefault((*key, horizon), Counter())
            status = point.get('status', 'missing')
            group['due'] += 1
            group[status] += 1
            if status == 'observed':
                group[point.get('observation_source', 'unspecified_source')] += 1
            if status == 'pending' and now > due+30:
                group['overdue'] += 1
    checkpoints = [dict(project=k[0], symbol=k[1], version=k[2], minutes=int(k[3]), **v)
                   for k,v in sorted(checkpoint_groups.items())]
    return dict(corrected_reviews=len(corrected),
        decision_counts=dict(Counter(r['verdict'] for r in corrected)),
        comparisons=comparisons(corrected), checkpoints=checkpoints,
        earlier_reviews=len(rows)-len(corrected),
        focus=[dict(project=r['project'], symbol=r['symbol'], decided_at=r['decided_at'],
            version=r['version'], verdict=r['verdict'], timely=r['decision'].get('timely'),
            reasons=r['decision'].get('reasons', [])[:5],
            measurement_state=r['measurements'].get('state'),
            horizons=r['measurements'].get('horizons', {}))
            for r in rows if r['symbol'] in ('TGT', 'SBUX')][:20])


def build(db, c, cfg, now, since=CORRECTED_SINCE):
    since = max(since, now-30*86400)
    symbols = data_symbols(db,c,cfg,now)
    quotes = db.prefix(c,'quote:')
    coverage = stock_archive_health(c,symbols,quotes,now,is_open(now))
    post_fix = [r['symbol'] for r in coverage['rows'] if r['archived_ts'] is not None and r['archived_ts'] >= since]
    focus_quotes = {}
    for symbol in ('TGT', 'SBUX'):
        found = []
        for order in (events.c.ts.asc(), events.c.ts.desc()):
            row = c.execute(select(events.c.ts,events.c.received,events.c.payload).where(
                events.c.kind=='quote',events.c.symbol==symbol,events.c.ts>=since,
                events.c.ts<=now,events.c.received>=since,events.c.received<=now)
                .order_by(order).limit(1)).mappings().first()
            found.append(None if row is None else dict(source_ts=row['ts'],received=row['received'],
                usable_when_recorded=bool(row['payload'].get('ts')==row['ts'] and
                    row['ts']<=row['received'] and fresh(row['payload'],row['received']))))
        focus_quotes[symbol] = dict(first=found[0],last=found[1])
    hours = session(day(now))
    review_start = min(since, hours[0]) if hours else since
    columns = [reviews.c[k] for k in ('project','symbol','decided_at','candidate','version',
        'verdict','decision','measurements','source_outcome')]
    rows = c.execute(select(*columns).where(reviews.c.project.in_(('morning','smoothers')),
        reviews.c.decided_at>=review_start,reviews.c.decided_at<=now)
        .order_by(reviews.c.decided_at.desc(),reviews.c.id).limit(5001)).mappings().all()
    result = summarize_reviews(rows[:5000],since,now)
    return dict(version=VERSION,at=now,since=since,review_start=review_start,
        review_rows=len(rows[:5000]),truncated=len(rows)>5000,limit=5000,
        market_open=is_open(now),archive_counts=dict(Counter(r['status'] for r in coverage['rows'])),
        collected_symbols=len(symbols),post_fix_archives=len(post_fix),
        no_post_fix_archive=sorted(set(symbols)-set(post_fix)),
        archive_problems=[r for r in coverage['rows'] if r['status']!='retained'],
        focus_archives=focus_quotes,**result,
        basis='Stored stock reviews only. Comparisons use post-fix v3 decisions. Checkpoints include earlier decisions with post-fix deadlines. Midpoint direction, not trade P&L; archive endpoints do not prove path completeness.')


def capture(db,cfg,now):
    with db.tx() as c:
        previous = db.get(c,'observation_audit:report',{})
        if now-previous.get('at',0)<600:
            return
        result = build(db,c,cfg,now)
        db.put(c,'observation_audit:report',result)
    # Private operational logs contain only bounded measurement diagnostics,
    # never original webhook bodies, credentials, or broker/account details.
    summary = {k:v for k,v in result.items() if k not in ('comparisons','checkpoints','focus')}
    LOG.info('Stock observation audit: %s',json.dumps(summary,sort_keys=True))
    for section in ('comparisons','checkpoints','focus'):
        for row in result[section][:200]:
            LOG.info('Stock observation audit %s: %s',section,json.dumps(row,sort_keys=True))
        if len(result[section])>200:
            LOG.info('Stock observation audit log limit: section=%s shown=200 total=%s; complete bounded report in authenticated API',section,len(result[section]))


async def run(db,cfg):
    while True:
        try:
            await asyncio.to_thread(capture,db,cfg,time.time())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning('Stock observation audit failed: %s',type(error).__name__)
        await asyncio.sleep(600)
