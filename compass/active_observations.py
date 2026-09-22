"""Deduplicated live research demand; account admission never controls coverage."""
from sqlalchemy import select


def inventory(db,c,now):
    from .option_ideas import ideas
    from .swing_ideas import swings
    from .store import spy_options
    from .spy_timeframes import arms
    from .setup_study import trials
    from .observation_recovery import active
    options=[];stocks=[]
    for table in (ideas,swings,spy_options,arms):
        for p in c.execute(select(table.c.payload).where(table.c.status=='open').order_by(table.c.created)).scalars():
            if p.get('contract'):options.append(p['contract']['symbol'])
            if p.get('underlying'):stocks.append(p['underlying'])
    for p in c.execute(select(trials.c.payload).where(trials.c.status=='open').order_by(trials.c.started)).scalars():
        if p.get('asset')=='option':options.append(p['symbol'])
        elif p.get('asset')=='stock':stocks.append(p['symbol'])
        if p.get('underlying'):stocks.append(p['underlying'])
    # Follow-ups keep collecting independently after their original result is unresolved.
    for p in active(c,now):
        for s in p['symbols']:
            if s.startswith('O:'):options.append(s)
            elif '@' not in s:stocks.append(s)
    for p in db.prefix(c,'position:').values():
        if p.get('status')!='open':continue
        if p.get('asset')=='option':options.append(p['symbol'])
        if p.get('underlying'):stocks.append(p['underlying'])
    return dict(options=list(dict.fromkeys(options)),stocks=list(dict.fromkeys(stocks)))


def select_contracts(active,previous,requested,background,limit):
    pinned=set(active)
    ordered=list(dict.fromkeys([*(s for s in previous if s in pinned),*active,*requested,*background]))
    selected=ordered[:max(0,limit)]
    return selected,sorted(pinned-set(selected))
