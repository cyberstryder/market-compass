"""One-shot read-only inspection of the saved SPY opening report and minute path."""
import json
import os
import time
from datetime import datetime
from sqlalchemy import text, select
from .store import Store, events, discord_jobs
from .market import CT, session
from .spy_chart import prompt

DAY = '2026-09-16'

def main():
    db = Store(os.environ['DATABASE_URL'])
    now = time.time()
    opening = session(DAY)[0]
    with db.tx() as c:
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text("SET LOCAL statement_timeout = '15000ms'"))
        reports = []
        for phase in ('preopen', 'opening'):
            key = 'spy-brief:' + DAY + ':' + phase
            p = db.get(c, key)
            if not p:
                reports.append({'key': key, 'present': False})
                continue
            reports.append({k: p.get(k) for k in (
                'id', 'phase', 'generated_at', 'expires_at', 'decision',
                'context', 'plans', 'chart_levels', 'mapping')})
            reports[-1]['chart_prompt_chars_at_delivery'] = len(prompt(p, p['generated_at'])) + 12
        raw = db.get(c, 'bar_window:SPY', [])
        rows = sorted({r[0]: r for r in raw if len(r) >= 6 and opening <= r[0] < opening + 1800
                       and r[0] + 60 <= now}.values(), key=lambda r: r[0])
        first = [r for r in rows if r[0] < opening + 900]
        candles = [dict(at_ct=datetime.fromtimestamp(r[0], CT).strftime('%H:%M'),
                        o=r[1], h=r[2], l=r[3], c=r[4]) for r in rows]
        first_complete = {r[0] for r in first} == {opening + 60*i for i in range(15)}
        aggregate = dict(complete=first_complete, bars=len(first),
            o=first[0][1], h=max(r[2] for r in first), l=min(r[3] for r in first), c=first[-1][4]) if first else {}
        delivery = [dict(r) for r in c.execute(select(events.c.key, discord_jobs.c.route, discord_jobs.c.status)
            .select_from(events.outerjoin(discord_jobs, events.c.id == discord_jobs.c.event_id))
            .where(events.c.source == 'spy_brief', events.c.key.like('spy-brief:' + DAY + ':%'))
            .order_by(events.c.id)).mappings()]
        result = dict(day=DAY, inspected_at=now, reports=reports, first15=aggregate,
                      minutes=candles, delivery=delivery,
                      database_read_only=c.execute(text('SHOW transaction_read_only')).scalar_one())
    db.engine.dispose()
    print('SPY_OPENING_CHART_AUDIT ' + json.dumps(result, sort_keys=True, allow_nan=False), flush=True)

if __name__ == '__main__':
    main()
