"""Separate vendor observation clocks, retrieval clocks and cache metadata."""
from .market import number
import logging


def metadata(payload):
    envelope = payload if isinstance(payload, dict) else {}
    flags = envelope.get('freshness')
    flags = flags if isinstance(flags, dict) else {}
    return dict(cached=envelope.get('cached') if isinstance(envelope.get('cached'), bool) else None,
        vendor_stale=flags.get('stale') is True or envelope.get('stale') is True,
        vendor_refresh_seconds=number(flags.get('refreshSeconds')))


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
        'Vendor freshness: feed=%s status=%s source_ts=%s received=%s cached=%s repeated_source_polls=%s',
        label,check['status'],check['source_ts'],check['received'],check['cached'],
        item.get('source_progress',{}).get('repeated_source_polls',0))
