"""Prospective feed ablations; never admits, rejects or changes an actual trial."""
from datetime import date, datetime, timedelta

from .futures import futures_session, hours_for
from .instruments import INDEX_FUTURES, future_root
from .market import CT, fresh, is_open, number
from .flow_recovery import freshness as flow_freshness
from .vendor_freshness import confirmation

VERSION = 'futures-feed-comparison-v1'
KEY = VERSION + ':activation'
ARMS = ('price_only', 'cross_market', 'flow', 'combined')
PROXIES = {'ES': 'SPY', 'MES': 'SPY', 'NQ': 'QQQ', 'MNQ': 'QQQ'}
PROTOCOL = 'https://github.com/cyberstryder/market-compass/blob/main/docs/futures-feed-comparison.md'


def activate(db, c, now):
    saved = db.get(c, KEY)
    if saved is not None:
        return saved
    hours = futures_session(now)
    if hours['open'] < now:
        hours = hours_for((date.fromisoformat(hours['day']) + timedelta(days=1)).isoformat())
    sessions = []
    for _ in range(10):
        sessions.append({k: hours[k] for k in ('day', 'open', 'close')})
        hours = hours_for((date.fromisoformat(hours['day']) + timedelta(days=1)).isoformat())
    saved = dict(version=VERSION, at=now, sessions=sessions,
        first_full_session=sessions[0]['day'], evaluation_end=sessions[-1]['close'],
        protocol=PROTOCOL)
    db.put(c, KEY, saved)
    return saved


def daypart(now):
    local = datetime.fromtimestamp(now, CT)
    minute = local.hour * 60 + local.minute
    if minute >= 17*60 or minute < 7*60:
        return 'overnight'
    if minute < 8*60+30:
        return 'preopen'
    if minute < 10*60:
        return 'opening'
    if minute < 13*60:
        return 'midday'
    if minute < 15*60:
        return 'afternoon'
    return 'late'


def current(stamp, now, age):
    stamp = number(stamp)
    return stamp is not None and 0 <= now-stamp <= age


def cross_market(db, c, root, side, now, cash_open):
    if root not in INDEX_FUTURES:
        return dict(selection='not_applicable', reason='no_prespecified_equity_proxy', peers=[])
    if not cash_open:
        return dict(selection='unknown', reason='cash_market_closed', peers=[])
    peers = []
    for symbol in ('SPY', 'QQQ'):
        f, q = db.get(c, 'scanner_features:'+symbol, {}), db.get(c, 'quote:'+symbol, {})
        vwap = number(f.get('vwap'))
        valid = bool(f.get('status') == 'ready' and current(f.get('asof'), now, 90)
            and fresh(q, now) and current(q.get('ts'), now, 5)
            and vwap is not None and vwap > 0 and f.get('htf15_bias') in (-1, 0, 1))
        price = (q['bid']+q['ask'])/2 if valid else None
        aligned = bool(valid and f['htf15_bias'] == side and (price-vwap)*side > 0)
        peers.append(dict(symbol=symbol, available=valid, aligned=aligned,
            bar_at=f.get('asof'), quote_at=q.get('ts'), price=price,
            vwap=vwap, htf15_bias=f.get('htf15_bias')))
    available = all(p['available'] for p in peers)
    aligned = all(p['aligned'] for p in peers)
    return dict(selection='unknown' if not available else 'selected' if aligned else 'skipped',
        reason='missing_or_stale_peer' if not available else 'both_peers_align' if aligned else 'peer_disagreement',
        peers=peers)


def flow_context(db, c, proxy, side, now, cash_open):
    if proxy is None:
        return dict(selection='not_applicable', reason='no_prespecified_flow_proxy')
    if not cash_open:
        return dict(selection='unknown', reason='cash_market_closed', proxy=proxy)
    summary = db.get(c, 'matrix:unusual_activity', {})
    check = flow_freshness(summary, now)
    if not check['eligible_for_live_confirmation']:
        return dict(selection='unknown', reason='flow_'+check['status'], proxy=proxy, freshness=check)
    if summary['source_ts'] > summary['received']:
        return dict(selection='unknown', reason='flow_clock_error', proxy=proxy, freshness=check)
    seen, rows = set(), []
    for row in summary.get('rows', []):
        if (row.get('symbol') != proxy or not current(row.get('source_ts'), now, 120)
                or (number(row.get('score')) or 0) < 85 or (number(row.get('premium')) or 0) < 100000):
            continue
        # A future receipt or a source timestamp after its receipt cannot be evidence at entry.
        received = number(row.get('received', summary.get('received')))
        if received is None or not row['source_ts'] <= received <= now:
            return dict(selection='unknown', reason='flow_row_clock_error', proxy=proxy, freshness=check)
        identity = row.get('vendor_id') or str((row.get('symbol'), row.get('source_ts'),
            row.get('sentiment'), row.get('premium'), row.get('score')))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({k: row.get(k) for k in ('vendor_id', 'symbol', 'source_ts', 'received', 'score', 'premium', 'sentiment')})
    expected, opposite = ('bullish', 'bearish') if side == 1 else ('bearish', 'bullish')
    sentiments = [str(r.get('sentiment') or '').lower() for r in rows]
    counts = {key: sentiments.count(key) for key in ('bullish', 'bearish', 'neutral')}
    counts['unrecognized'] = sum(s not in ('bullish', 'bearish', 'neutral') for s in sentiments)
    selected = expected in sentiments and opposite not in sentiments
    selection = 'unknown' if counts['unrecognized'] else 'selected' if selected else 'skipped'
    reason = ('unrecognized_sentiment' if counts['unrecognized'] else 'opposing_flow' if opposite in sentiments
        else 'aligned_flow' if selected else 'no_qualifying_aligned_flow_in_current_snapshot')
    return dict(selection=selection, reason=reason, proxy=proxy, freshness=check,
        counts=counts, qualifying_rows=len(rows), rows=rows[:8], rows_preview_limit=8,
        basis='Current polled snapshot; not a complete options tape. Decisions use all qualifying snapshot rows.')


def exposure_context(db, c, proxy, now, cash_open):
    if proxy is None:
        return dict(status='not_applicable', gex_state='not_applicable', vex_state='not_applicable')
    obj = db.get(c, 'matrix:'+proxy, {})
    check = confirmation(obj, now)
    status = ('cash_market_closed' if not cash_open else
        'clock_error' if number(obj.get('source_ts')) is not None and number(obj.get('received')) is not None
            and obj['source_ts'] > obj['received'] else check['status'])
    result = dict(proxy=proxy, status=status, freshness=check,
        basis='Sum of supplied strike values; vendor context only, not dealer inventory or a directional entry filter.')
    for metric in ('gex', 'vex'):
        values = [number(r.get(metric)) for r in obj.get('strikes', [])]
        usable = status == 'current' and bool(values) and all(v is not None for v in values)
        total = sum(values) if usable else None
        result[metric+'_state'] = ('unknown' if total is None else 'positive' if total > 0 else 'negative' if total < 0 else 'zero')
        result[metric+'_sum'] = total
        result[metric+'_supplied_strikes'] = len(values)
    return result


def freeze(db, c, signal, now, eligible, assessment):
    activation = activate(db, c, now)
    if now >= activation['evaluation_end']:
        return None  # This version has a fixed end; extending it requires a new protocol.
    hours = futures_session(now, signal['symbol'])
    root = future_root(signal['symbol'])
    proxy, side, cash = PROXIES.get(root), 1 if signal['side'] == 'long' else -1, is_open(now)
    peers = cross_market(db, c, root, side, now, cash)
    flow = flow_context(db, c, proxy, side, now, cash)
    exposure = exposure_context(db, c, proxy, now, cash)
    choices = [peers['selection'], flow['selection']]
    combined = ('not_applicable' if 'not_applicable' in choices else 'unknown' if 'unknown' in choices
        else 'selected' if all(v == 'selected' for v in choices) else 'skipped')
    arms = dict(price_only='selected', cross_market=choices[0], flow=choices[1], combined=combined)
    if not eligible:
        arms = {name: 'excluded' for name in ARMS}
    return dict(version=VERSION, frozen_at=now, activation_at=activation['at'],
        session=hours['day'], phase='evaluation' if now >= activation['sessions'][0]['open'] else 'warmup',
        daypart=daypart(now), timezone='America/Chicago', regime=assessment.get('state', 'unknown'),
        assessment_version=assessment.get('version'), root=root, arms=arms,
        cross_market=peers, flow=flow, exposure=exposure)
