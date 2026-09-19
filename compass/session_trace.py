"""Bounded frozen review and current-session evidence, without rescoring."""
from collections import Counter
from sqlalchemy import select
from .store import events
from .secondary import reviews
from .market import fresh, day, session
from .setup_study import trials


def quote_clock(q, at):
    if not q: return None
    return dict(source=q.get('source'),source_ts=q.get('ts'),write_clock=q.get('received'),
        age_at_check=at-q['ts'] if q.get('ts') else None,usable=fresh(q,at))


def tgt_trace(db,c,now):
    hours=session(day(now))
    if not hours:return []
    found=c.execute(select(reviews).where(reviews.c.project=='morning',reviews.c.symbol=='TGT',
        reviews.c.decided_at>=hours[0],reviews.c.decided_at<=now).order_by(reviews.c.decided_at.desc()).limit(3)).mappings().all()
    result=[]
    for r in found:
        candidate=r['candidate']; available=candidate.get('available_at',candidate['source_ts']); deadline=available+60
        data=c.execute(select(events.c.ts,events.c.received,events.c.payload).where(events.c.kind=='quote',
            events.c.symbol=='TGT',events.c.ts>=available-10,events.c.ts<=deadline+30,
            events.c.received<=now).order_by(events.c.ts,events.c.id).limit(1001)).all()
        sampled=data[:1000]; usable=[x for x in sampled if x.ts<=x.received and fresh(x.payload,x.received)]
        captured=r['inputs'].get('captured_at',r['decided_at'])
        by_deadline=[x for x in usable if x.received<=deadline and x.ts>=available]
        at_capture=[x for x in usable if x.received<=captured and 0<=captured-x.ts<=5]
        result.append(dict(id=r['id'],source_ts=r['source_ts'],available_at=available,deadline=deadline,
            decided_at=r['decided_at'],captured_at=captured,verdict=r['verdict'],
            frozen_quote=quote_clock(r['inputs'].get('quote'),captured),technical=r['inputs'].get('technical'),
            rows=len(sampled),truncated=len(data)>1000,usable_by_deadline=len(by_deadline),
            usable_at_capture=len(at_capture),
            archive_delay_max=max((x.received-x.ts for x in sampled),default=None),
            first=None if not sampled else quote_clock(sampled[0].payload,sampled[0].received),
            last=None if not sampled else quote_clock(sampled[-1].payload,sampled[-1].received),
            note='Write/insert clocks are not network-arrival or transaction-commit timestamps; archive availability is approximate. No decision rewritten.'))
    return result


def futures_session_audit(db,c,now):
    from .session_gaps import window, totals, gap_page
    w=window(now)
    result=totals(c,w)
    page=gap_page(c,now,session_day=w['day'],limit=12)
    return dict(**result,gaps=page['records'],gap_details_truncated=page['has_more'],
        gap_total=page['total'],gap_next_cursor=page['next_cursor'],
        gap_records_url='/api/session-gaps?asset=future&session_day='+w['day'],
        at=now)
