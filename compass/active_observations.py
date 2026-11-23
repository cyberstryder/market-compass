"""Deduplicated live research demand; account admission never controls coverage."""
from sqlalchemy import select


def inventory(db,c,now,include_followups=True):
    from .option_ideas import ideas
    from .swing_ideas import swings
    from .swings_c2 import episodes as c2_episodes
    from .store import connors_ssc_episodes
    from .store import overnight_omc_episodes
    from .store import sh_spy_options_episodes
    from .store import stock_sweeps_episodes
    from .store import spy_options
    from .spy_timeframes import arms
    from .setup_study import trials
    from .observation_recovery import active
    options=[];stocks=[]
    # c2_episodes (2026-11-02): the C2 forward scorer's chosen contracts
    # pin for their whole open life, like open positions (scorecard §4.1).
    # connors_ssc_episodes (2026-11-03): same pinning for the Connors
    # SSC premium scorer's open episodes.
    # Only string symbols may enter the lists: a non-string value (e.g.
    # a legacy Stock Sweeps episode whose 'underlying' field carries a
    # leg-result dict) is unhashable and 500s the /api/state build at
    # the INDEX_UNDERLYINGS set test below (2026-11-22 incident).
    for table in (ideas,swings,spy_options,arms,c2_episodes,connors_ssc_episodes,overnight_omc_episodes,sh_spy_options_episodes,stock_sweeps_episodes):
        for p in c.execute(select(table.c.payload).where(table.c.status=='open').order_by(table.c.created)).scalars():
            if p.get('contract') and isinstance(p['contract'].get('symbol'),str):options.append(p['contract']['symbol'])
            if isinstance(p.get('underlying'),str) and p['underlying']:stocks.append(p['underlying'])
    for p in c.execute(select(trials.c.payload).where(trials.c.status=='open').order_by(trials.c.started)).scalars():
        if p.get('asset')=='option' and isinstance(p.get('symbol'),str):options.append(p['symbol'])
        elif p.get('asset')=='stock' and isinstance(p.get('symbol'),str):stocks.append(p['symbol'])
        if isinstance(p.get('underlying'),str) and p['underlying']:stocks.append(p['underlying'])
    # Pin daily research around entry and endpoint windows, even without a paper trade.
    from .smoothers_daily import quote_demand
    stocks.extend(quote_demand(db,c,now))
    # Follow-ups keep collecting independently after their original result is unresolved.
    for p in active(c,now) if include_followups else []:
        for s in p['symbols']:
            if s.startswith('O:'):options.append(s)
            elif '@' not in s:stocks.append(s)
    for p in db.prefix(c,'position:').values():
        if p.get('status')!='open':continue
        if p.get('asset')=='option' and isinstance(p.get('symbol'),str):options.append(p['symbol'])
        if isinstance(p.get('underlying'),str) and p['underlying']:stocks.append(p['underlying'])
    # 0DTE paper trades live under trade:0dte-, not position:, so the loops
    # above never pinned them: their contracts rode last in the request queue
    # under the subscription cap and could sit uncovered while open.
    for t in db.prefix(c,'trade:0dte-').values():
        if isinstance(t,dict) and t.get('status')=='open' and isinstance(t.get('symbol'),str) and t['symbol']:
            options.append(t['symbol'])
            if isinstance(t.get('underlying'),str) and t['underlying']:stocks.append(t['underlying'])
    from .universe import INDEX_UNDERLYINGS
    return dict(options=list(dict.fromkeys(o for o in options if isinstance(o,str))),
                stocks=list(dict.fromkeys(s for s in stocks if isinstance(s,str) and s not in INDEX_UNDERLYINGS)))


def select_contracts(active,previous,requested,background,limit,pending=()):
    pinned=set(active)
    # Fill order under the cap: pinned open observations first (never
    # dropped -- structural), then the pending tranche (0DTE selection
    # candidates from setup_study.option_requests, imminence-ordered
    # by the producer; reserved ahead of general requests since
    # 2026-11-14, funnel fix Defect 2B), then other requested, then
    # focus background.
    ordered=list(dict.fromkeys([*(s for s in previous if s in pinned),*active,*pending,*requested,*background]))
    # Active positions are never dropped, even if over the limit.
    # The limit applies to requested/background only.
    selected=list(dict.fromkeys([*pinned,*ordered]))[:max(len(pinned),limit)]
    return selected,sorted(pinned-set(selected))
