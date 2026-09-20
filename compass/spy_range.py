"""Prospective v3 daily-plan range verification; never used by timeframe research."""
import math
from datetime import datetime

VERSION = 'spy-provider-range-v1'


def valid_bars(data, start, end, step):
    if not isinstance(data.get('bars'), list) or data.get('next_page_token'):
        raise ValueError('Incomplete SPY provider response')
    result = {}
    for bar in data['bars']:
        ts = datetime.fromisoformat(bar['t'].replace('Z', '+00:00')).timestamp()
        if ts < start or ts + step > end or (ts-start) % step or ts in result:
            raise ValueError('Invalid SPY provider timestamp')
        if not all(isinstance(bar.get(k), (int, float)) and math.isfinite(bar[k]) for k in ('o','h','l','c','v')):
            raise ValueError('Invalid SPY provider prices')
        if not 0 < bar['l'] <= min(bar['o'], bar['c']) <= max(bar['o'], bar['c']) <= bar['h'] or bar['v'] < 0:
            raise ValueError('Invalid SPY provider OHLC')
        result[ts] = bar
    return result


def verify(start, end, minutes, five, fetched_at, feed):
    expected = set(range(int(start), int(end), 300))
    complete = end > start and (end-start) % 300 == 0 and set(five) == expected
    matches = complete and all(
        (bucket := [b for t,b in minutes.items() if ts <= t < ts+300])
        and math.isclose(max(b['h'] for b in bucket), five[ts]['h'], abs_tol=1e-8, rel_tol=0)
        and math.isclose(min(b['l'] for b in bucket), five[ts]['l'], abs_tol=1e-8, rel_tol=0)
        for ts in expected)
    return dict(version=VERSION, start=start, end=end, fetched_at=fetched_at, feed=feed,
        verified=bool(matches), five_minute_bars=len(five), expected_five_minute_bars=len(expected),
        minutes=[[t,b['h'],b['l']] for t,b in sorted(minutes.items())],
        reason='Complete provider five-minute range matches each observed minute bucket' if matches
        else 'Missing five-minute bars or minute/five-minute range mismatch')


def apply_verified_range(db, c, context, now, day):
    """Opt-in only from daily-plan build. Keep the original clock gate visible."""
    if context.get('status') == 'closed':
        return
    context['premarket_clock_complete'] = context['premarket_complete']
    context['premarket_coverage_basis'] = 'minute_clock_coverage'
    proof = db.get(c, 'spy-provider-range:'+day, {})
    summary = {k:v for k,v in proof.items() if k != 'minutes'}
    context['premarket_range_verification'] = summary
    # Full-session verification only. Preopen retains its original minute gate.
    opening = context['session_open']
    start = opening - 330*60
    if not (proof.get('version') == VERSION and proof.get('verified') is True
            and proof.get('feed') == 'sip' and proof.get('start') == start
            and proof.get('end') == opening and now >= opening
            and 0 <= now-proof.get('fetched_at', 0) <= 120):
        return
    stored = sorted([[r[0], r[2], r[3]] for r in db.get(c, 'bar_window:SPY', [])
                     if len(r) >= 6 and start <= r[0] < opening])
    if stored != proof.get('minutes'):
        return
    context['premarket_complete'] = True
    context['premarket_coverage_basis'] = 'verified_provider_five_minute_range'
