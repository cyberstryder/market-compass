"""Session-based SPY daily evidence and bounded independent readiness recovery."""
import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import case, func, select

from .index_reference import prior_session
from .market import NY, day, number, session, ts
from .store import events

VERSION = 'spy-daily-readiness-v1'


def required_days(now):
    days = []
    previous = now
    for _ in range(15):
        label = prior_session(previous)
        if label is None:
            break
        days.append(label)
        previous = session(label)[0]
    return sorted(days)


def valid_daily(p):
    return (all(number(p.get(k)) is not None for k in ('o', 'h', 'l', 'c', 'v'))
            and 0 < p['l'] <= min(p['o'], p['c']) <= max(p['o'], p['c']) <= p['h']
            and p['v'] >= 0)


def evidence(db, c, now):
    days = required_days(now)
    bounds = []
    for label in days:
        start = datetime.fromisoformat(label).replace(tzinfo=NY)
        bounds.append(((events.c.ts >= start.timestamp()) &
                       (events.c.ts < (start+timedelta(days=1)).timestamp()), label))
    # Rank revisions within a trading date BEFORE selecting rows. A raw-row
    # limit can otherwise hide completed sessions behind repeated revisions.
    label = case(*bounds, else_=None)
    ranked = select(events.c.payload, events.c.id, label.label('session_day'),
        func.row_number().over(partition_by=label,
            order_by=(events.c.ts.desc(), events.c.id.desc())).label('rank')).where(
        events.c.kind == 'daily', events.c.symbol == 'SPY',
        events.c.ts >= datetime.fromisoformat(days[0]).replace(tzinfo=NY).timestamp(),
        events.c.ts < datetime.fromisoformat(day(now)).replace(tzinfo=NY).timestamp()).subquery()
    rows = list(c.execute(select(ranked).where(ranked.c.rank == 1,
        ranked.c.session_day.is_not(None))).mappings())
    daily = {r['session_day']: r['payload'] for r in rows if valid_daily(r['payload'])}
    missing = [d for d in days if d not in daily]
    ordered = [daily[d] for d in days if d in daily]
    atr = (sum(max(b['h']-b['l'], abs(b['h']-a['c']), abs(b['l']-a['c']))
               for a,b in zip(ordered, ordered[1:])) / 14
           if len(days) == 15 and not missing else None)
    return dict(version=VERSION, day=day(now), checked_at=now,
        status='ready' if atr is not None and atr > 0 else 'blocked',
        required_days=days, usable_days=sorted(daily), missing_or_invalid_days=missing,
        atr14_daily=atr, prior=daily.get(days[-1], {}),
        selected_event_ids=[r['id'] for r in rows],
        basis='Latest archived revision per required completed session; 14 mean true ranges')


def record(db, now):
    with db.tx() as c:
        result = evidence(db, c, now)
        legacy = db.recent(c, 'daily', 'SPY', limit=90)
        legacy_days = sorted({day(r['ts']) for r in legacy if day(r['ts']) in result['required_days']})
        result['legacy_90_row_session_count'] = len(legacy_days)
        result['legacy_missing_days'] = [d for d in result['required_days'] if d not in legacy_days]
        db.put(c, 'spy-daily-readiness:'+day(now), result)
    db.health('spy_daily_readiness', 'available' if result['status']=='ready' else 'partial',
        'SPY daily ATR '+result['status']+'; missing/invalid sessions: '+
        ','.join(result['missing_or_invalid_days']), now)
    return result


async def collect(collector, now=None):
    now = time.time() if now is None else now
    hours = session(day(now))
    if not hours or not hours[0]-3.5*3600 <= now < hours[1]:
        return
    result = await asyncio.to_thread(record, collector.db, now)
    if result['status'] != 'ready':
        # Dedicated request: not queued behind all-watchlist minute history.
        days = result['required_days']
        start = datetime.fromisoformat(days[0]).replace(tzinfo=NY)
        end = datetime.fromisoformat(days[-1]).replace(tzinfo=NY)+timedelta(days=1)
        data = await collector.get('https://data.alpaca.markets/v2/stocks/SPY/bars',
            collector.alpaca_headers, dict(timeframe='1Day', start=start.isoformat(),
                end=(end-timedelta(milliseconds=1)).isoformat(), limit=1000,
                feed=collector.cfg.feed, adjustment='split', sort='asc'))
        if not isinstance(data.get('bars'), list) or data.get('next_page_token'):
            raise ValueError('Incomplete SPY daily history response')
        items, seen = [], set()
        for p in data['bars']:
            stamp = ts(p.get('t'))
            label = day(stamp) if stamp is not None else None
            if label not in days or label in seen or not valid_daily(p):
                raise ValueError('Invalid SPY completed daily history')
            seen.add(label)
            items.append(('SPY', stamp, {k:p[k] for k in ('o','h','l','c','v','vw') if k in p}))
        await asyncio.to_thread(collector.bars, 'alpaca', items, 'daily')
        result = await asyncio.to_thread(record, collector.db, now)
    logging.getLogger('uvicorn.error').info('SPY daily readiness: %s', result)
    return result
