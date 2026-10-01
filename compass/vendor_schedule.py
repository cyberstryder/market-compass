"""Reserve core exposure refreshes independently of the option focus cap."""
from .market import is_open
from .universe import focus_symbols
from sqlalchemy import select
from .store import state
from .vendor_freshness import confirmation, context_check


def matrix_target(db, c, cfg, now, cursor=0):
    core = [s for s in ('SPY', 'QQQ', 'IWM') if s in cfg.watch_symbols]
    # Vendor futures GEX (NQ/ES) is polled for the futures side; these are not
    # equity watchlist symbols so they bypass the watchlist filter below.
    futures_gex = ['NQ', 'ES']
    held = [p.get('underlying', p.get('symbol')) for p in db.prefix(c, 'position:').values()
            if p.get('status') == 'open' and p.get('asset') != 'future']
    focus = focus_symbols(db, c, cfg, now, cfg.option_focus)
    candidates = list(dict.fromkeys(core + futures_gex + held + focus + list(cfg.watch_symbols)))
    jobs = db.prefix(c, 'matrix_job:')
    stamps = {key[7:]:stamp for key,stamp in c.execute(select(state.c.key,
        state.c.value['received'].as_float()).where(state.c.key.startswith('matrix:')))}
    due = {'core': [], 'held': [], 'focus': [], 'background': []}
    for symbol in candidates:
        if symbol not in cfg.watch_symbols and symbol not in futures_gex:
            continue
        tier = 'core' if symbol in core or symbol in futures_gex else 'held' if symbol in held else 'focus' if symbol in focus else 'background'
        job = jobs.get('matrix_job:' + symbol, {})
        attempted = job.get('attempted_at', stamps.get(symbol) or 0)
        interval = 60 if tier in ('core', 'held') else 180 if tier == 'focus' else 1800
        if not is_open(now):
            interval = max(interval, 900)
        if now >= job.get('retry_at', 0) and now-attempted >= interval:
            due[tier].append((attempted, symbol))
    # Core and held positions cannot be evicted by newly discovered setups.
    pool = due['core'] or due['held']
    if not pool:
        pool = due['background'] if cursor % 4 == 3 and due['background'] else due['focus'] or due['background']
    return min(pool)[1] if pool else None


def matrix_health(db, c, cfg, now):
    rows = []
    jobs = db.prefix(c,'matrix_job:')
    for symbol in ('SPY', 'QQQ', 'IWM'):
        if symbol not in cfg.watch_symbols:
            continue
        item = db.get(c,'matrix:'+symbol,{})
        check = confirmation(item,now)
        job = jobs.get('matrix_job:' + symbol, {})
        status = 'error' if job.get('error') else 'waiting' if not item else check['status']
        rows.append(dict(**{**check,'status':status,'eligible_for_live_confirmation':status=='current'},
            context=context_check(item,now), symbol=symbol, source_progress=item.get('source_progress'), retry_at=job.get('retry_at')))
    return rows
