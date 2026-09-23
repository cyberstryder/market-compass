"""Prospective v3 daily-plan range verification; never used by timeframe research."""
import math
from datetime import datetime

VERSION = 'spy-provider-range-v2'


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


def verify(start, end, minutes, five, fetched_at, feed, fifteen=None):
    """Verify observed extrema against complete provider aggregates, not clocks.

    A missing 5m bucket is never assumed empty. A complete covering 15m bar
    can attest its range only if the observed minute extrema match exactly.
    The unfinished 5m tail requires every completed one-minute bar.
    """
    fifteen=fifteen or {}
    cursor=start; segments=[]; failures=[]
    equal=lambda a,b: math.isclose(a,b,abs_tol=1e-8,rel_tol=0)
    while cursor<end:
        remaining=end-cursor
        step=300 if remaining>=300 else 60
        source=five if step==300 else minutes
        # Prefer existing 5m proof. Use 15m only on aligned complete intervals
        # containing an absent 5m bucket, never to override a price mismatch.
        if (remaining>=900 and (cursor-start)%900==0
                and any(cursor+i*300 not in five for i in range(3))):
            step=900;source=fifteen
        bar=source.get(cursor)
        bucket=[b for t,b in minutes.items() if cursor<=t<cursor+step]
        ok=bool(bar and bucket and equal(max(b['h'] for b in bucket),bar['h'])
                and equal(min(b['l'] for b in bucket),bar['l']))
        if step==900:
            for t in range(int(cursor),int(cursor+900),300):
                existing=five.get(t)
                observed=[b for stamp,b in minutes.items() if t<=stamp<t+300]
                if existing and (not observed or not equal(max(b['h'] for b in observed),existing['h'])
                        or not equal(min(b['l'] for b in observed),existing['l'])):
                    ok=False
        if not ok: failures.append(dict(start=cursor,seconds=step,reason='missing_aggregate_or_range_mismatch'))
        segments.append(dict(start=cursor,seconds=step,matched=ok))
        cursor+=step
    verified=bool(end>start and (end-start)%60==0 and not failures)
    return dict(version=VERSION,start=start,end=end,fetched_at=fetched_at,feed=feed,
        verified=verified,five_minute_bars=len(five),expected_five_minute_bars=int((end-start)//300),
        fifteen_minute_bars=len(fifteen),verification_segments=segments,failed_segments=failures,
        minutes=[[t,b['h'],b['l']] for t,b in sorted(minutes.items())],
        reason='Provider aggregate ranges match observed minutes; no bars synthesized' if verified
        else 'Missing provider aggregate or minute/aggregate range mismatch')


def apply_verified_range(db, c, context, now, day):
    """Opt-in only from daily-plan build. Keep the original clock gate visible."""
    if context.get('status') == 'closed':
        return
    context['premarket_clock_complete'] = context['premarket_complete']
    context['premarket_coverage_basis'] = 'minute_clock_coverage'
    proof = db.get(c, 'spy-provider-range:'+day, {})
    summary = {k:v for k,v in proof.items() if k != 'minutes'}
    context['premarket_range_verification'] = summary
    # A completed provider range can cover preopen too. Any newer completed
    # minutes must be present individually; no carry-forward over a gap.
    opening = context['session_open']
    start = opening - 330*60
    end=min(int(now//60)*60,opening)
    proof_end=proof.get('end',0)
    if not (proof.get('version') == VERSION and proof.get('verified') is True
            and proof.get('feed') == 'sip' and proof.get('start') == start
            and start+3600 <= proof_end <= end and end-proof_end <= 120
            and 0 <= now-proof.get('fetched_at', 0) <= 120):
        return
    stored = sorted([[r[0], r[2], r[3]] for r in db.get(c, 'bar_window:SPY', [])
                     if len(r) >= 6 and start <= r[0] < proof_end])
    if stored != proof.get('minutes'):
        return
    tail={r[0] for r in db.get(c,'bar_window:SPY',[]) if len(r)>=6 and proof_end<=r[0]<end
          and all(isinstance(v,(int,float)) and math.isfinite(v) for v in r[1:6])
          and 0<r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2] and r[5]>=0}
    if tail != set(range(int(proof_end),int(end),60)):
        return
    context['premarket_complete'] = True
    context['premarket_coverage_basis'] = ('verified_provider_multiframe_range'
        if any(s['seconds']==900 for s in proof.get('verification_segments',[])) or now<opening
        else 'verified_provider_five_minute_range')
