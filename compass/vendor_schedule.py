"""Reserve core exposure refreshes independently of the option focus cap."""
from .market import is_open
from .universe import focus_symbols
from sqlalchemy import select
from .store import state


def matrix_target(db, c, cfg, now, cursor=0):
    core = [s for s in ('SPY', 'QQQ', 'IWM') if s in cfg.watch_symbols]
    held = [p.get('underlying', p.get('symbol')) for p in db.prefix(c, 'position:').values()
            if p.get('status') == 'open' and p.get('asset') != 'future']
    focus = focus_symbols(db, c, cfg, now, cfg.option_focus)
    candidates = list(dict.fromkeys(core + held + focus + list(cfg.watch_symbols)))
    jobs = db.prefix(c, 'matrix_job:')
    stamps = {key[7:]:stamp for key,stamp in c.execute(select(state.c.key,
        state.c.value['received'].as_float()).where(state.c.key.startswith('matrix:')))}
    due = {'core': [], 'held': [], 'focus': [], 'background': []}
    for symbol in candidates:
        if symbol not in cfg.watch_symbols:
            continue
        tier = 'core' if symbol in core else 'held' if symbol in held else 'focus' if symbol in focus else 'background'
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
    clocks = {key[7:]:(stamp,received) for key,stamp,received in c.execute(select(state.c.key,
        state.c.value['source_ts'].as_float(),state.c.value['received'].as_float())
        .where(state.c.key.in_(['matrix:SPY','matrix:QQQ','matrix:IWM'])))}
    jobs = db.prefix(c,'matrix_job:')
    for symbol in ('SPY', 'QQQ', 'IWM'):
        if symbol not in cfg.watch_symbols:
            continue
        stamp,received = clocks.get(symbol,(None,None))
        age = now-stamp if stamp is not None else None
        job = jobs.get('matrix_job:' + symbol, {})
        status = ('error' if job.get('error') else 'waiting' if age is None else
                  'clock_error' if age < -1 else 'stale' if age > 180 else 'current')
        rows.append(dict(symbol=symbol, status=status, source_ts=stamp, source_age=age,
                         received=received, retry_at=job.get('retry_at')))
    return rows
