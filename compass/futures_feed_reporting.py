"""Full-cohort SQL aggregation and matched, session-clustered feed contrasts."""
from collections import defaultdict
import hashlib
import random

from sqlalchemy import case, func, select

from .futures_feed_study import ARMS, VERSION

BASE = ('symbol', 'strategy', 'side', 'model', 'fill_version', 'alerted', 'phase')
COUNTS = ('total', 'selected', 'skipped', 'unknown', 'not_applicable', 'excluded',
          'paired_closed', 'selected_closed', 'open', 'unresolved')


def cluster_interval(daily):
    """Ratio of summed paired deltas / common resolved opportunities, by session."""
    rows = sorted((day, n, difference) for day, (n, difference) in daily.items() if n)
    if len(rows) < 10:
        return None
    seed = int.from_bytes(hashlib.sha256(repr(rows).encode()).digest()[:8], 'big')
    rng = random.Random(seed)
    values = []
    for _ in range(2000):
        sample = rng.choices(rows, k=len(rows))
        values.append(sum(r[2] for r in sample)/sum(r[1] for r in sample))
    values.sort()
    return [values[49], values[1949]]


def report(c, trials, now, activation):
    base = dict(version=VERSION, at=now, activation=activation, groups=[], conditions=[], exposure_groups=[],
        coverage='all_retained_trials_in_frozen_window', primary_arm='combined',
        evaluation_locked=not activation or now < activation['evaluation_end'],
        basis='Same resolved opportunities and original net R. Skipped=zero exposure; unknown feeds and unresolved paths are not zero returns.')
    if not activation:
        return base
    p = trials.c.payload
    f = p['feed_comparison']
    dims = {key: trials.c[key] for key in ('symbol', 'strategy', 'side')}
    dims.update(model=trials.c.version, fill_version=p['fill_version'].as_string(),
        alerted=p['alerted'].as_boolean(), phase=f['phase'].as_string(), session=f['session'].as_string(),
        daypart=f['daypart'].as_string(), regime=f['regime'].as_string(),
        gex=f['exposure']['gex_state'].as_string(), vex=f['exposure']['vex_state'].as_string())
    dims.update({arm: f['arms'][arm].as_string() for arm in ARMS})
    r = p['r_multiple'].as_float()
    closed = (trials.c.status == 'closed') & r.is_not(None)
    count = lambda condition: func.sum(case((condition, 1), else_=0))
    metrics = dict(total=func.count(), closed=count(closed), open=count(trials.c.status == 'open'),
        unresolved=count((trials.c.status == 'unresolved') | ((trials.c.status == 'closed') & r.is_(None))),
        sum_r=func.sum(case((closed, r), else_=0)))
    query = select(*(value.label(key) for key, value in {**dims, **metrics}.items())).where(
        trials.c.started >= activation['at'], trials.c.started < activation['evaluation_end'],
        trials.c.started <= now, f['version'].as_string() == VERSION).group_by(*dims.values())
    views = {'groups': (), 'conditions': ('daypart', 'regime'), 'exposure_groups': ('gex', 'vex')}
    buckets = {name: {} for name in views}
    for row in c.execute(query).mappings():
        for view, extra in views.items():
            for arm in ARMS:
                key = tuple(row[k] for k in BASE+extra) + (arm,)
                group = buckets[view].setdefault(key, dict(
                    **{k: 0 for k in COUNTS}, baseline_sum_r=0, filtered_sum_r=0,
                    daily=defaultdict(lambda: [0, 0]), sessions=set()))
                group['total'] += row['total']
                choice = row[arm] if row[arm] in ('selected', 'skipped', 'unknown', 'not_applicable', 'excluded') else 'unknown'
                group[choice] += row['total']
                group['sessions'].add(row['session'])
                if choice not in ('selected', 'skipped'):
                    continue
                group['paired_closed'] += row['closed']
                group['selected_closed'] += row['closed'] if choice == 'selected' else 0
                group['open'] += row['open']
                group['unresolved'] += row['unresolved']
                group['baseline_sum_r'] += row['sum_r'] or 0
                filtered = (row['sum_r'] or 0) if choice == 'selected' else 0
                group['filtered_sum_r'] += filtered
                group['daily'][row['session']][0] += row['closed']
                group['daily'][row['session']][1] += filtered-(row['sum_r'] or 0)
    for view, extra in views.items():
        for key, group in sorted(buckets[view].items(), key=lambda item: str(item[0])):
            dimensions = dict(zip(BASE+extra+('arm',), key))
            n = group['paired_closed']
            locked = dimensions['phase'] == 'evaluation' and base['evaluation_locked']
            usable_sessions = sum(n > 0 for n, _ in group['daily'].values())
            # The primary inference is prespecified by exact setup/contract/direction/cohort.
            # All conditional cuts remain descriptive; no flood of unadjusted significance claims.
            ready = (view == 'groups' and dimensions['phase'] == 'evaluation' and not locked
                and not group['open'] and dimensions['arm'] == 'combined' and usable_sessions >= 10)
            interval = cluster_interval(group['daily']) if ready else None
            visible = n > 0 and not locked
            base[view].append(dict(dimensions, **{k: group[k] for k in COUNTS},
                sessions=len(group['sessions']), usable_sessions=usable_sessions, outcomes_locked=locked,
                baseline_r_per_opportunity=group['baseline_sum_r']/n if visible else None,
                filtered_r_per_opportunity=group['filtered_sum_r']/n if visible else None,
                difference_r_per_opportunity=(group['filtered_sum_r']-group['baseline_sum_r'])/n if visible else None,
                selected_mean_r=group['filtered_sum_r']/group['selected_closed'] if visible and group['selected_closed'] else None,
                session_cluster_95=interval,
                inference='descriptive_unadjusted_multiple_comparisons' if interval else
                    'evaluation_locked' if locked else 'inconclusive_or_exploratory'))
    return base
