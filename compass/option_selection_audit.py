"""Read-only evidence for quiet same-day portfolio selection."""
from collections import Counter
from sqlalchemy import select
from .store import state, events
from .market import day
from .futures import risk_day
from .paper_risk import snapshot as paper_risk_snapshot


def snapshot(db,c,cfg,now):
    since=now-86400
    rows=c.execute(select(state.c.value).where(state.c.key.startswith('pending_options:'),state.c.updated>=since)
        .order_by(state.c.updated.desc(),state.c.key.desc()).limit(5001)).scalars().all()
    truncated=len(rows)>5000
    rows=rows[:5000]
    known=[r for r in rows if r.get('last_selection')]
    positions=[p for p in db.prefix(c,'position:').values() if p.get('status')=='open']
    risk_dates=list(dict.fromkeys([day(now),risk_day(now)]))
    skips=c.execute(select(events.c.payload).where(events.c.kind=='alert',events.c.source=='engine',
        events.c.ts>=since,events.c.payload['status'].as_string()=='options_skipped')
        .order_by(events.c.ts.desc(),events.c.id.desc()).limit(1001)).scalars().all()
    return dict(at=now,since=since,version='0dte-selection-audit-v1',
        enabled=bool(cfg.scanner_paper),scanner_enabled=bool(cfg.scanner),
        candidates=len(rows),truncated=truncated,states=dict(Counter(r.get('status','unknown') for r in rows)),
        instrumented=len(known),missing_diagnostics=len(rows)-len(known),
        last_attempt_reasons=dict(Counter(r['last_selection'].get('reason','Unknown') for r in known)),
        terminal_reasons=dict(Counter(r.get('reason','No terminal reason saved') for r in rows if r.get('status')=='expired')),
        contract_rejections=dict(Counter(x['reason'] for r in known for x in r['last_selection'].get('rejections',[]))),
        recent=[dict(symbol=r['signal']['symbol'],status=r.get('status'),updated_at=r.get('updated_at'),
            reason=r.get('reason'),selection=r.get('last_selection')) for r in rows[:12]],
        notification_skip_reasons=dict(Counter(r.get('reason','Unknown') for r in skips[:1000])),
        notification_skips_truncated=len(skips)>1000,
        paper_portfolios=paper_risk_snapshot(db,c,cfg,now),
        legacy_risk=[dict(day=d,**db.get(c,'risk:'+d,{'realized':0,'entries':0})) for d in risk_dates],
        current_risk_day=risk_day(now),cash_date=day(now),
        open_positions=dict(Counter(p.get('asset','unknown') for p in positions)),
        limits=dict(risk_per_entry=cfg.risk,max_entries=cfg.max_entries,concurrent_positions=3),
        note='Retries updated in the last 24 hours, counted once per candidate using the last saved attempt. Contract counts can include several contracts per candidate. Notification reasons overlap retry candidates. Current risk is context, not proof of a historical rejection; older retries may have no detailed evidence.')

