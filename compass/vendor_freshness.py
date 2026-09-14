"""Separate vendor observation clocks, retrieval clocks and cache metadata."""
from .market import number
from datetime import datetime
import logging


def vendor_clock(value):
    if not isinstance(value,str): return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def metadata(payload):
    envelope = payload if isinstance(payload, dict) else {}
    flags = envelope.get('freshness')
    flags = flags if isinstance(flags, dict) else {}
    return dict(cached=envelope.get('cached') if isinstance(envelope.get('cached'), bool) else None,
        vendor_envelope_ts=vendor_clock(envelope.get('timestamp')),
        vendor_stale=flags.get('stale') is True or envelope.get('stale') is True,
        vendor_refresh_seconds=number(flags.get('refreshSeconds')),
        vendor_computed_ts=vendor_clock(flags.get('computedAt')),
        vendor_next_refresh_ts=vendor_clock(flags.get('nextRefreshAt')),
        vendor_session_state=flags.get('sessionState') if flags.get('sessionState') in
            ('live','closed','pre','post','premarket','afterhours','overnight') else None)


def confirmation(item, now, source_limit=180, poll_limit=180):
    source, received = number(item.get('source_ts')), number(item.get('received'))
    source_age = now-source if source is not None else None
    poll_age = now-received if received is not None else None
    status = ('vendor_stale' if item.get('vendor_stale') or item.get('stale') else
        'clock_error' if (source_age is not None and source_age < 0) or (poll_age is not None and poll_age < 0) else
        'poll_stale' if poll_age is None or poll_age > poll_limit else
        'source_time_unknown' if source_age is None else
        'stale' if source_age > source_limit else 'current')
    return dict(status=status, eligible_for_live_confirmation=status=='current',
        source_ts=source, source_age=source_age, received=received, poll_age=poll_age,
        cached=item.get('cached'), vendor_stale=bool(item.get('vendor_stale') or item.get('stale')),
        vendor_refresh_seconds=item.get('vendor_refresh_seconds'),
        vendor_computed_ts=item.get('vendor_computed_ts'),
        vendor_envelope_ts=item.get('vendor_envelope_ts'),
        vendor_next_refresh_ts=item.get('vendor_next_refresh_ts'),
        vendor_session_state=item.get('vendor_session_state'),
        vendor_refresh_overdue_seconds=max(0,now-item['vendor_next_refresh_ts'])
            if number(item.get('vendor_next_refresh_ts')) is not None else None,
        source_limit=source_limit, poll_limit=poll_limit)


def progress(previous, item):
    """Repeated timestamps are evidence of no clock advance, not a cache diagnosis."""
    stamp, old = item.get('source_ts'), previous.get('source_ts')
    prior = previous.get('source_progress', {})
    same = stamp is not None and stamp == old
    return dict(source_first_seen_at=prior.get('source_first_seen_at',previous.get('received')) if same else item.get('received'),
        repeated_source_polls=prior.get('repeated_source_polls',0)+1 if same else 0,
        source_regressed=stamp is not None and old is not None and stamp < old,
        note='Repeated source time does not identify whether caching, quiet activity or vendor delay caused it.')


def log_observation(label, item, now, source_limit=180, poll_limit=180):
    check = confirmation(item,now,source_limit,poll_limit)
    logging.getLogger('uvicorn.error').info(
        'Vendor freshness: feed=%s status=%s source_ts=%s received=%s cached=%s repeated_source_polls=%s refresh_seconds=%s next_refresh_ts=%s session=%s',
        label,check['status'],check['source_ts'],check['received'],check['cached'],
        item.get('source_progress',{}).get('repeated_source_polls',0),
        check['vendor_refresh_seconds'],check['vendor_next_refresh_ts'],check['vendor_session_state'])


CACHE_POLICY = 'tradermatrix-support-2026-09-14'


def cache_window(now):
    """Vendor-confirmed rolling TTL, using the US equity session calendar."""
    from .market import day, session
    hours = session(day(now))
    return 86400 if hours is None else 900 if hours[0] <= now < hours[1] else 21600


def context_check(item, now):
    """Cache health is separate from live confirmation and collection health.

    On session transitions use the stricter TTL of computation and evaluation;
    a weekend snapshot cannot become current RTH context on Monday.
    """
    strict = confirmation(item, now)
    source = number(item.get('source_ts'))
    rolling = item.get('cache_policy') == CACHE_POLICY
    ttl = cache_window(now) if rolling else None
    age = strict['source_age']
    context_limit = min(ttl,cache_window(source)) if ttl is not None and age is not None and 0<=age<86400 else ttl
    cache_status = ('unknown_policy' if not rolling else 'source_time_unknown' if source is None else
        'clock_error' if source > now or (number(item.get('received')) is not None and source > item['received']) else
        'within_expected_cache' if age < ttl else 'refresh_due')
    # Poll age is independent of source age; allow the existing off-hours jobs.
    poll_limit = max(180,min(600,(number(item.get('target_interval')) or 90)*2)) if cache_window(now) == 900 else 1800
    poll_age = strict['poll_age']
    available = (cache_status == 'within_expected_cache' and age < context_limit and not strict['vendor_stale'] and
        poll_age is not None and 0 <= poll_age <= poll_limit)
    return dict(cache_policy=item.get('cache_policy'), cache_status=cache_status,
        expected_cache_seconds=ttl, context_source_limit=context_limit, source_ts=source, source_age=age,
        expected_refresh_at=source+ttl if ttl is not None and source is not None else None,
        poll_age=poll_age, context_poll_limit=poll_limit, eligible_for_context=available,
        eligible_for_live_confirmation=strict['eligible_for_live_confirmation'] and cache_status != 'clock_error',
        usage='positioning_context; entry timing requires independent current prices and completed bars')
