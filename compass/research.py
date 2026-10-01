"""Documented vendor research feeds, independently scheduled and timestamped.

Source: TraderMatrix's interactive REST reference, inspected 2026-09-13.
Vendor analytics are evidence, not instructions and not independently proven signals.
"""
from dataclasses import dataclass
import json
import time
import math
from .market import is_open, number
from .store import identity
from .universe import focus_symbols
from .vendor import source_time
from .vendor_freshness import metadata, confirmation, progress, log_observation, CACHE_POLICY


@dataclass(frozen=True)
class Feed:
    key: str
    label: str
    path: str
    interval: int
    priority: int = 1
    category: str = 'context'
    research_only: bool = False


FEEDS = (
    Feed('signals', 'Breakouts and continuations', '/signals', 60, 10, 'setups'),
    Feed('flow_summary', 'Directional options flow', '/flow/summary?days=0&limit=500', 60, 10, 'flow'),
    Feed('gamma_market', 'Index gamma and flip levels', '/gex/market/overview', 60, 9, 'exposure'),
    Feed('reversals', 'Reversal finder', '/bounce-finder', 180, 8, 'setups'),
    Feed('sector_dashboard', 'Sector flow, technicals and wall events', '/sectors/dashboard?window=today', 120, 7, 'sectors'),
    Feed('market_stats', 'Market health and put/call context', '/market-stats', 120, 7),
    Feed('thermal', 'Index technical conditions', '/market-pulse/thermal', 120, 7),
    Feed('vix', 'VIX', '/market-pulse/vix', 120, 7),
    Feed('economic_calendar', 'Economic releases', '/economic-calendar', 300, 9, 'events'),
    Feed('earnings', 'Earnings calendar', '/earnings', 900, 8, 'events'),
    Feed('earnings_flow', 'Pre-earnings options positioning', '/earnings-flow/flow', 300, 6, 'flow'),
    Feed('market_pulse', 'Market narrative', '/market-pulse', 300, 5),
    Feed('sector_rotation', 'Sector relative-strength rotation', '/sectors/rotation', 300, 5, 'sectors'),
    Feed('repeat_flow', 'Repeated options activity', '/unusual-activity/repeats', 180, 6, 'flow'),
    Feed('institutional', 'Institutional positioning', '/institutional/buys-sells', 1800, 2, 'positioning'),
    Feed('institutional_rotation', 'Institutional sector rotation', '/institutional/sector-rotation', 1800, 2, 'positioning'),
    Feed('institutional_divergences', 'Institutional divergences', '/institutional/divergences', 1800, 2, 'positioning'),
    Feed('insider', 'Reported insider trades', '/insider', 1800, 2, 'disclosures'),
    Feed('politician', 'Reported congressional trades', '/politician-trades', 1800, 2, 'disclosures'),
    Feed('quality', 'Long-term quality', '/long-term/quality', 3600, 1, 'fundamentals'),
    Feed('dividends', 'Dividend calendar', '/long-term/dividends', 3600, 1, 'events'),
) + tuple(Feed('screener_'+slug, label, '/screeners/'+slug+'/run', 600, 4, 'setups')
    for slug, label in (
        ('bullish-pullback', 'Bullish pullbacks'), ('momentum', 'Momentum'),
        ('volatility-squeeze', 'Volatility compression'), ('small-cap', 'Small-cap candidates'),
        ('volatility-surge', 'Volatility expansion'), ('gamma-scan', 'Gamma candidates'),
        ('leveraged', 'Leveraged ETF candidates'), ('daily-cuts', 'Combined screener shortlist')))


def scheduled_feeds(db, c, cfg, now):
    from .tm_expanded import expanded_feeds
    focus = focus_symbols(db, c, cfg, now, cfg.option_focus)
    return list(FEEDS) + [Feed('apex_'+s, s+' Apex levels', '/gex/'+s+'/apex', 300, 8, 'exposure')
                          for s in focus] + expanded_feeds(Feed, focus)


def observation_time(value, *, index_gamma=False):
    """Do not substitute HTTP time, ageSeconds or an undated price for an observation."""
    if not isinstance(value, dict):
        return None
    keys = ('tradeTime', 'signalTime', 'detectedAt', 'snapshotTime', 'computedAt')
    # Confirmed in the index-overview response. Do not interpret arbitrary
    # lastUpdated fields on calendars/disclosures as market observation times.
    for key in keys + (('lastUpdated',) if index_gamma else ()):
        stamp = source_time(value.get(key))
        if stamp is not None:
            return stamp
    return None


def bounded(value, depth=0):
    """Bound untrusted display/context data; retain no executable content."""
    if depth > 8:
        return {'truncated': True}
    if isinstance(value, dict):
        return {str(k)[:100]: bounded(v, depth+1) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [bounded(v, depth+1) for v in value[:500]]
    if isinstance(value, str):
        return value[:12000]
    if isinstance(value,float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def row_list(value):
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in ('results', 'data', 'signals', 'rows', 'events', 'earnings', 'sectors'):
        found = value.get(key)
        if isinstance(found, list):
            return found
        if isinstance(found, dict):
            rows = row_list(found)
            if rows:
                return rows
    return []


def normalize(feed, payload, now):
    if not isinstance(payload, (dict, list)):
        raise ValueError('Research response must be JSON objects or rows')
    if isinstance(payload, dict) and (payload.get('success') is False or payload.get('error')):
        raise ValueError('Vendor reported a research error')
    raw = bounded(payload)
    if len(json.dumps(raw)) > 1_500_000:
        raise ValueError('Research response exceeds the bounded snapshot size')
    body = payload.get('data', payload) if isinstance(payload, dict) else payload
    freshness = payload.get('freshness', {}) if isinstance(payload, dict) else {}
    if not isinstance(freshness, dict):
        freshness = {}
    stamp = observation_time(body) or observation_time(payload) or observation_time(freshness)
    rolling = feed.path.startswith('/gex/') and feed.path.rsplit('/',1)[-1] in ('matrix','apex','apex-evolution')
    if rolling:
        stamp = source_time(body.get('snapshotTime')) if isinstance(body,dict) else None
    items = []
    rows = row_list(payload)
    if feed.key == 'gamma_market' and isinstance(body, dict):
        rows = [row for symbol, row in body.items() if isinstance(row, dict)
                and row.get('symbol') == symbol and isinstance(symbol, str)]
    for row in rows[:500]:
        if not isinstance(row, dict):
            continue
        ticker = row.get('symbol', row.get('ticker'))
        if ticker is not None and (not isinstance(ticker, str) or len(ticker)>20):
            continue
        row_stamp = observation_time(row, index_gamma=feed.key == 'gamma_market')
        items.append({'symbol': ticker.upper() if ticker else None,
            'source_ts': row_stamp, 'data': bounded(row)})
    # Row clocks are not a snapshot clock: old disclosures and upcoming events
    # coexist. Each item retains its own clock and unknown clocks remain unknown.
    return {'key': feed.key, 'label': feed.label, 'category': feed.category,
        'source': 'tradermatrix', 'path': feed.path, 'received': now, 'source_ts': stamp,
        'target_interval': feed.interval, 'research_only': feed.research_only, **metadata(payload),
        'cache_policy': CACHE_POLICY if rolling else None,
        'items': items, 'data': raw, 'status': 'available' if stamp is not None else 'per_item_clocks' if any(r['source_ts'] is not None for r in items) else 'source_time_unknown',
        'coverage': 'Vendor-returned results; not proof of complete market coverage',
        'timestamp_note': 'Calculation or event clock supplied by vendor; fetch time is separate. Disclosures describe past activity.'}


def apex_levels(payload, symbol, now):
    if not isinstance(payload, dict) or payload.get('success') is False:
        raise ValueError('Invalid Apex envelope')
    body = payload.get('data')
    if not isinstance(body, dict) or body.get('symbol') != symbol or not isinstance(body.get('levels'), list):
        raise ValueError('Apex symbol or levels do not match the documented schema')
    stamp = source_time(body.get('snapshotTime'))
    freshness = payload.get('freshness') or {}
    if not isinstance(freshness, dict):
        raise ValueError('Invalid Apex freshness envelope')
    levels = []
    for row in body['levels']:
        if not isinstance(row, dict):
            raise ValueError('Invalid Apex level')
        strike, score = number(row.get('strike')), number(row.get('score'))
        if strike is None or strike<=0 or score is None or not 0<=score<=100:
            raise ValueError('Invalid Apex price or score')
        levels.append({'price': strike, 'score': score, 'kind': 'apex',
                       'net_gex': number(row.get('netGEX')), 'oi': number(row.get('totalOI'))})
    flip = number(body.get('gammaFlip'))
    if flip is not None and flip>0:
        levels.append({'price': flip, 'score': None, 'kind': 'gamma_flip'})
    return {'symbol': symbol, 'source': 'tradermatrix', 'source_ts': stamp, 'received': now,
        'cache_policy': CACHE_POLICY, 'clock_basis': 'data.snapshotTime', 'target_interval': 300,
        'spot': number(body.get('spotPrice')), 'levels': levels, 'mode': body.get('mode'),
        'expirations': body.get('expirationsUsed', []), **metadata(payload),
        'method': 'Vendor Apex ranking and flip; no assumption that a level predicts direction.'}


def catalog(db, c, cfg, now):
    result=[]
    collector_status=db.get(c,'health:research',{}).get('status')
    for feed in scheduled_feeds(db, c, cfg, now):
        item=db.get(c, 'research:'+feed.key)
        job=db.get(c, 'research_job:'+feed.key, {})
        stamp=item.get('source_ts') if item else None
        age=now-stamp if stamp is not None else None
        # Reader services do not need the collector's vendor credential.
        unconfigured=collector_status=='not_configured' or (not collector_status and not cfg.matrix and not item and not job)
        item_clocks = [{'symbol': r.get('symbol'), 'source_ts': r.get('source_ts'),
            'source_age': now-r['source_ts'] if r.get('source_ts') is not None else None}
            for r in (item or {}).get('items', [])]
        for row in item_clocks:
            row.update(confirmation({**(item or {}),'source_ts':row['source_ts']},now,
                max(feed.interval*2,180),max(feed.interval*2,180)))
        states = {r['status'] for r in item_clocks}
        row_status = ('current' if states == {'current'} else 'stale' if states == {'stale'} else
                      'mixed' if states and states != {'source_time_unknown'} else 'source_time_unknown')
        check = confirmation(item or {},now,max(feed.interval*2,180),max(feed.interval*2,180))
        status=('not_configured' if unconfigured else 'disabled' if not cfg.research else
                'error' if job.get('error') else 'waiting' if not item else
                check['status'] if check['status'] not in ('current','source_time_unknown') else
                row_status if stamp is None else 'current')
        result.append({'key': feed.key, 'label': feed.label, 'category': feed.category,
            'status': status, 'source_ts': stamp, 'source_age': age,
            'received': item.get('received') if item else None,
            'target_interval': feed.interval, 'last_error': job.get('error'),
            'poll_age':check['poll_age'],'cached':check['cached'],'vendor_stale':check['vendor_stale'],
            'source_progress':(item or {}).get('source_progress'),
            'research_only':feed.research_only,
            'usage':'context_only' if feed.research_only or feed.category in ('events','disclosures','fundamentals','positioning') else 'timestamped_market_context',
            'eligible_for_live_confirmation':status=='current' and not feed.research_only and feed.category not in ('events','disclosures','fundamentals','positioning'),
            'rows': len(item.get('items', [])) if item else 0,
            'clock_basis': 'snapshot' if stamp is not None else 'per_item', 'item_clocks': item_clocks})
    return result


async def collect(collector, now=None):
    """One due job per turn. All vendor calls share the collector's rate limiter."""
    now=now or time.time()
    db,cfg=collector.db,collector.cfg
    with db.tx() as c:
        feeds=scheduled_feeds(db,c,cfg,now)
        jobs=db.prefix(c,'research_job:')
        due=[]
        for feed in feeds:
            job=jobs.get('research_job:'+feed.key,{})
            interval=feed.interval if is_open(now) else max(feed.interval,900)
            elapsed=now-job.get('attempted_at',0)
            if now<job.get('retry_at',0) or elapsed<interval:
                continue
            # New feeds cycle by priority; established jobs earn bounded lateness.
            rank=(min(elapsed/interval,3) if job else 1)+feed.priority/10
            due.append((rank, -job.get('attempted_at',0), feed))
        if not due:
            return None
        background = [r for r in due if r[2].research_only]
        foreground = [r for r in due if not r[2].research_only]
        last = db.get(c, 'research_expanded_budget', {}).get('attempted_at', 0)
        pool = background if background and now-last >= 30 else foreground
        if not pool:
            return None
        # Oldest attempt first for expansion: short-cadence jobs must not
        # starve unseen history/detail routes when the request budget is full.
        feed=max(pool,key=(lambda value:value[1]) if pool is background else
                 (lambda value:value[:2]))[2]
        if feed.research_only:
            db.put(c, 'research_expanded_budget', {'attempted_at': now})
        old=db.get(c,'research_job:'+feed.key,{})
        db.put(c,'research_job:'+feed.key,{**old,'attempted_at':now})
    try:
        payload,received=await collector.matrix_request(feed.path,feed.key)
        result=normalize(feed,payload,received)
        apex=apex_levels(payload,feed.key[5:],received) if not feed.research_only and feed.key.startswith('apex_') else None
        with db.tx() as c:
            result['source_progress']=progress(db.get(c,'research:'+feed.key,{}),result)
            db.put(c,'research:'+feed.key,result)
            db.append(c,'research','tradermatrix',feed.key,received,result,
                identity('research',feed.key,result['data']))
            db.put(c,'research_job:'+feed.key,{'attempted_at':now,'succeeded_at':received,'failures':0})
            if not feed.research_only and feed.category in ('setups','flow','positioning','disclosures'):
                for row in result['items']:
                    symbol=row.get('symbol')
                    if symbol not in cfg.watch_symbols:
                        continue
                    matches=db.get(c,'research_matches:'+symbol,{})
                    old_match=matches.get(feed.key,{})
                    digest=identity(feed.key,row['data'])
                    matches[feed.key]={'key':feed.key,'label':feed.label,'category':feed.category,
                        'source':'tradermatrix','source_ts':row['source_ts'] or result['source_ts'],
                        'received':received,'digest':digest,
                        **{k:result.get(k) for k in ('cached','vendor_stale','vendor_refresh_seconds')},
                        'interpretation':'Vendor research match; price confirmation remains necessary'}
                    db.put(c,'research_matches:'+symbol,matches)
                    if feed.category=='setups' and old_match.get('digest')!=digest:
                        current=db.get(c,'focus:'+symbol,{})
                        if current.get('priority',0)<50 or received-current.get('at',0)>600:
                            db.put(c,'focus:'+symbol,{'symbol':symbol,'priority':50,'at':received,
                                'reason':feed.label+' match; source freshness checked separately'})
            if apex:
                previous=db.get(c,'apex:'+apex['symbol'],{})
                known={(r['kind'],r['price']):r.get('known_at',previous.get('received',received)) for r in previous.get('levels',[])}
                for level in apex['levels']:
                    level['known_at']=known.get((level['kind'],level['price']),received)
                db.put(c,'apex:'+apex['symbol'],apex)
        log_observation(feed.key,result,received,max(feed.interval*2,180),max(feed.interval*2,180))
        db.health('research','receiving','Independent vendor research jobs; source clocks and per-feed failures retained',
                  last_feed=feed.key,last_success=received)
        return result
    except Exception as error:
        failures=min(8,old.get('failures',0)+1)
        # Provider exception text is already sanitized by Collectors.get.
        detail=str(error)[:180] if type(error).__name__ in {'FeedError','ValueError'} else type(error).__name__
        with db.tx() as c:
            db.put(c,'research_job:'+feed.key,{'attempted_at':now,'error':detail,'failures':failures,
                'retry_at':now+min(1800,30*2**(failures-1))})
        db.health('research','partial','A research feed failed; other feeds continue',last_failed_feed=feed.key)
        return None
