"""Complete, read-only session counts and keyset-paged unresolved observations.

Counts are current database state in the requested creation window, not a
historical status reconstruction. Paging pins the upper creation/finish bound;
newly resolved rows are excluded until the user refreshes that view.
"""
import base64
import json
import math
from datetime import datetime, time
from sqlalchemy import select, func, and_, or_
from .market import calendar, session, day, CT
from .setup_study import trials
from .option_ideas import ideas


def window(now, session_day=None, asset='future', scope='cash'):
    if asset not in ('future', 'option'):
        raise ValueError('Unknown observation asset')
    if scope not in ('cash', 'full') or (asset == 'option' and scope != 'cash'):
        raise ValueError('Full-session scope is available only for futures')
    if scope == 'full':
        from .futures import futures_session, hours_for
        cal = calendar('CMES')
        if session_day is None:
            hours = futures_session(now)
            if hours['open'] > now:
                previous = cal.previous_session(hours['day'])
                hours = hours_for(str(previous.date()))
        else:
            hours = hours_for(session_day)
            if hours['day'] != session_day:
                raise ValueError('Choose a CME trading day for the full-session cohort')
        return dict(day=hours['day'], asset=asset, scope=scope,
                    since=hours['open'], until=hours['close'],
                    through=max(hours['open'], min(now, hours['close'])),
                    basis='Full futures creation cohort from session open to calendar close (normally 17:00–16:00 CT). Counts do not prove quote continuity.')
    if session_day is None:
        label = calendar('XNYS').date_to_session(day(now), direction='previous')
        session_day = str(label.date())
    hours = session(session_day)
    if not hours:
        raise ValueError('Choose an equity trading day for this cash-open cohort')
    # This deliberately preserves the cash-open cohort, including futures
    # observations after the equity close. It is not a 23-hour session census.
    cutoff = datetime.combine(datetime.fromisoformat(session_day).date(), time(16), CT).timestamp() if asset == 'future' else hours[1]
    return dict(day=session_day, asset=asset, scope=scope, since=hours[0], until=cutoff,
                through=min(now, cutoff), basis='Trials started since cash open; futures reporting cutoff 16:00 CT. Not a 23-hour census.')


def _table(asset):
    return (trials, trials.c.started) if asset == 'future' else (ideas, ideas.c.created)


def _conditions(table, started, w):
    conditions = [started >= w['since'], started < w['through']]
    if w['asset'] == 'future':
        conditions.append(table.c.payload['asset'].as_string() == 'future')
    return conditions


def totals(c, w):
    table, started = _table(w['asset'])
    states = dict(c.execute(select(table.c.status, func.count()).where(
        *_conditions(table, started, w)).group_by(table.c.status)).all())
    return dict(**w, states=states, total=sum(states.values()), counts_complete=True,
                truncated=False, counts_basis='Current saved statuses; complete creation-window aggregate')


def gap_page(c, now, asset='future', session_day=None, limit=100, cursor='', scope='cash'):
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('Page size must be 1–100')
    w = window(now, session_day, asset, scope)
    after = None
    asof = now
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if token['v'] != 1 or token['asset'] != asset or token['day'] != w['day'] or token.get('scope', 'cash') != scope:
                raise ValueError()
            asof = float(token['asof'])
            after = (float(token['started']), token['id'])
            if not math.isfinite(asof) or asof > now or not math.isfinite(after[0]) or not isinstance(after[1], str) or not 1 <= len(after[1]) <= 64:
                raise ValueError()
            w['through'] = min(asof, w['until'])
            if not w['since'] <= after[0] < w['through']:
                raise ValueError()
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
            raise ValueError('Invalid gap cursor for this session and asset') from error
    table, started = _table(asset)
    finished = table.c.finished if asset == 'future' else table.c.updated
    conditions = [*_conditions(table, started, w), table.c.status == 'unresolved', finished <= asof]
    total = c.execute(select(func.count()).select_from(table).where(*conditions)).scalar_one()
    if after:
        conditions.append(or_(started > after[0], and_(started == after[0], table.c.id > after[1])))
    rows = c.execute(select(table.c.id, started.label('started'), table.c.payload).where(
        *conditions).order_by(started, table.c.id).limit(limit+1)).mappings().all()
    page = rows[:limit]
    fields = ('symbol','underlying','strategy','track','contract','finished','updated_at',
              'exit_reason','gap_detail','archive_check','collection_version')
    records = [{**{k:r['payload'].get(k) for k in fields}, 'id':r['id'], 'started':r['started']} for r in page]
    has_more = len(rows) > limit
    next_cursor = ''
    if has_more:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(dict(v=1, asset=asset, scope=scope, day=w['day'],
            asof=asof, started=last['started'], id=last['id']), separators=(',',':')).encode()).decode()
    return dict(**w, asof=asof, total=total, records=records, has_more=has_more,
                next_cursor=next_cursor, page_complete=not has_more,
                note='Unresolved records with finish/update clocks at or before the first page. Refresh for newer failures. Missing paths are not losses.')
