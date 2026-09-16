"""Full-window SQL summaries and bounded, stable browsing of setup trials."""
import base64
import json
import math
from collections import defaultdict

from sqlalchemy import case, func, select

from . import futures_assessment, futures_variants

WINDOW_DAYS = 30
COUNTS = ('total', 'closed', 'wins', 'losses', 'breakeven', 'targets', 'stops',
          'open', 'unresolved', 'excluded', 'sum_r', 'sum_seconds')


def window(trials, at):
    return (trials.c.started >= at-WINDOW_DAYS*86400) & (trials.c.started <= at)


def summaries(c, trials, at):
    """Aggregate in the database: no payload download or trial-count cutoff."""
    p = trials.c.payload
    dims = {name: trials.c[name] for name in ('symbol', 'strategy', 'side', 'version')}
    dims.update(alerted=p['alerted'].as_boolean(), asset=p['asset'].as_string(),
        fill_version=p['fill_version'].as_string(),
        variants_version=p['entry_variants']['version'].as_string(),
        assessment_version=p['market_assessment']['version'].as_string(),
        state=p['market_assessment']['state'].as_string(),
        selection=p['market_assessment']['proposed_selection'].as_string())
    dims.update({name: p['entry_variants'][name].as_string() for name in futures_variants.NAMES})
    closed = trials.c.status == 'closed'
    pnl = p['pnl'].as_float()
    count = lambda condition: func.sum(case((condition, 1), else_=0))
    measures = dict(total=func.count(), closed=count(closed),
        wins=count(closed & (pnl > 0)), losses=count(closed & (pnl < 0)),
        breakeven=count(closed & (pnl == 0)),
        targets=count(closed & (p['exit_reason'].as_string() == 'target')),
        stops=count(closed & (p['exit_reason'].as_string() == 'stop')),
        **{status: count(trials.c.status == status) for status in ('open', 'unresolved', 'excluded')},
        sum_r=func.sum(case((closed, p['r_multiple'].as_float()), else_=0)),
        sum_seconds=func.sum(case((closed, p['elapsed_seconds'].as_float()), else_=0)))
    q = select(*(value.label(name) for name, value in {**dims, **measures}.items()))
    q = q.where(window(trials, at)).group_by(*dims.values())
    groups, variants, market = (defaultdict(lambda: defaultdict(int)) for _ in range(3))
    old_variants = old_market = 0

    def add(target, row, names=COUNTS):
        for name in names:
            target[name] += row[name] or 0

    # Only grouped scalar buckets reach Python, irrespective of trial count.
    for row in c.execute(q).mappings():
        key = tuple(row[name] for name in ('symbol', 'strategy', 'side', 'version', 'alerted'))
        add(groups[key], row)
        if row['asset'] != 'future':
            continue
        future_key = key[:4] + (row['fill_version'], key[4])
        if row['assessment_version'] == futures_assessment.VERSION:
            add(market[future_key + (row['state'], row['selection'])], row)
        else:
            old_market += row['total']
        if row['variants_version'] != futures_variants.VERSION:
            old_variants += row['total']
            continue
        for name in futures_variants.NAMES:
            group = variants[future_key + (name,)]
            group['total'] += row['total']
            choice = row[name]
            if choice in ('selected', 'skipped', 'unknown', 'excluded'):
                group[choice] += row['total']
            if choice == 'selected':
                add(group, row, ('closed', 'wins', 'losses', 'breakeven', 'open', 'unresolved', 'sum_r'))

    def finish(grouped, dimensions, names):
        result = []
        for key, group in sorted(grouped.items(), key=lambda item: str(item[0])):
            n = group['closed']
            result.append(dict(zip(dimensions, key), **{name: int(group[name]) for name in names},
                win_rate=group['wins']/n if n else None,
                mean_r=group['sum_r']/n if n else None,
                **({'mean_seconds': group['sum_seconds']/n if n else None} if 'targets' in names else {})))
        return result

    base_dims = ('symbol', 'strategy', 'side', 'version', 'alerted')
    future_dims = ('symbol', 'strategy', 'side', 'model', 'fill_version', 'alerted')
    base = finish(groups, base_dims, COUNTS[:-2])
    # Keep metadata and membership semantics shared with the row-based reports.
    return dict(groups=base, count=sum(g['total'] for g in base),
        entry_variants=dict(futures_variants.report([]), pre_activation_trials=old_variants,
            groups=finish(variants, future_dims+('variant',),
                ('total', 'selected', 'skipped', 'unknown', 'excluded', 'open', 'unresolved',
                 'closed', 'wins', 'losses', 'breakeven'))),
        market_assessment=dict(futures_assessment.report([]), pre_activation_trials=old_market,
            groups=finish(market, future_dims+('state', 'selection'),
                ('total', 'closed', 'open', 'unresolved', 'excluded'))))


def record_page(c, trials, now, *, cohort='all', limit=100, asof=None, cursor=''):
    """Keyset pages freeze the window and use ID to break equal-time ties.

    Outcomes can advance while browsing; the cursor never claims an immutable
    outcome snapshot. Filtering happens before the page size is applied.
    """
    if cohort not in ('all', 'alerted', 'quiet') or not 1 <= limit <= 100:
        raise ValueError('Invalid setup record filter or page size')
    anchor = None
    if cursor:
        try:
            version, at, saved_cohort, stamp, id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if (version != 1 or saved_cohort != cohort or not isinstance(id, str) or not 1 <= len(id) <= 64
                    or not math.isfinite(stamp) or not math.isfinite(at)
                    or not at-WINDOW_DAYS*86400 <= stamp <= at
                    or (asof is not None and asof != at)):
                raise ValueError()
            anchor = (stamp, id)
        except (ValueError, TypeError, UnicodeError, OverflowError) as error:
            raise ValueError('Invalid setup record cursor') from error
    else:
        at = now if asof is None else asof
    if not math.isfinite(at) or not 0 < at <= now:
        raise ValueError('Invalid setup report time')
    scope = window(trials, at)
    if cohort != 'all':
        scope &= trials.c.payload['alerted'].as_boolean() == (cohort == 'alerted')
    total = c.execute(select(func.count()).select_from(trials).where(scope)).scalar_one()
    q = select(trials.c.id, trials.c.started, trials.c.payload).where(scope)
    if anchor:
        stamp, id = anchor
        q = q.where((trials.c.started < stamp) | ((trials.c.started == stamp) & (trials.c.id < id)))
    rows = c.execute(q.order_by(trials.c.started.desc(), trials.c.id.desc()).limit(limit+1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if more:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([1, at, cohort, last.started, last.id]).encode()).decode()
    return dict(records=[row.payload for row in rows], total=total, asof=at,
        since=at-WINDOW_DAYS*86400, window_days=WINDOW_DAYS, cohort=cohort,
        limit=limit, has_more=more, next_cursor=next_cursor)
