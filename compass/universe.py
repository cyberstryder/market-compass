"""A broad price universe and a bounded, rotating options focus list."""
from pathlib import Path
import re


def symbols(value):
    parts = value.replace('\n', ',').split(',') if isinstance(value, str) else value
    result = []
    for item in parts:
        symbol = str(item).strip().upper().split(':')[-1]
        if not symbol:
            continue
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol):
            raise ValueError('Invalid watchlist symbol')
        if symbol not in result:
            result.append(symbol)
    return tuple(result)


EXCLUDED_STOCKS = frozenset({"DJT"})

def saved_watchlist():
    return symbols((Path(__file__).parent / 'watchlist.txt').read_text())


def connected_symbols(db, c, now):
    """Observed equity projects need data even outside the scanner watchlist."""
    from sqlalchemy import select
    from .projects import records
    rows = c.execute(select(records.c.symbol).where(
        records.c.project.in_(("morning", "smoothers")),
        records.c.source_ts >= now-30*86400).distinct().order_by(records.c.symbol).limit(500)).scalars()
    result = []
    for symbol in rows:
        try:
            result.extend(symbols((symbol,)))
        except ValueError:
            continue
    from .obsidian import symbols as watchlist_symbols
    return symbols((*result,*watchlist_symbols(c,now)))


def data_symbols(db, c, cfg, now):
    """Expand collection only; connected projects do not change scanner entries."""
    from .tm_study import requested_symbols,symbol_for
    from .swing_study import requested_symbols as swing_symbols
    from .discovery import requested
    base=list(symbols((*cfg.watch_symbols,*requested(db,c,cfg,now),*connected_symbols(db,c,now))))
    from sqlalchemy import select
    from .store import spy_options
    if c.execute(select(spy_options.c.id).where(spy_options.c.status.in_(('pending','open'))).limit(1)).first():base.append('SPY')
    for symbol in swing_symbols(c):
        if not symbol_for({'symbol':symbol})[1]:base.append(symbol)
    for row in requested_symbols(c,now):
        if symbol_for({'symbol':row.symbol})[1]:continue
        try:base.extend(symbols((row.symbol,)))
        except ValueError:continue
    return tuple(s for s in dict.fromkeys(base) if s not in EXCLUDED_STOCKS)[:500]


def focus_symbols(db, c, cfg, now, limit=12):
    """Open positions first, then fresh setups, then stable index/core coverage."""
    allowed = set(cfg.watch_symbols)
    held = [p.get('underlying', p.get('symbol')) for p in db.prefix(c, 'position:').values()
            if p.get('status') == 'open' and p.get('asset') != 'future']
    from .swing_ideas import active_underlyings
    swing_held = active_underlyings(c)
    requests = sorted(db.prefix(c, 'focus:').values(),
                      key=lambda row: (row.get('priority', 0), row.get('at', 0)), reverse=True)
    active = [r['symbol'] for r in requests if 0 <= now-r.get('at', 0) <= 600]
    core = [s for s in ('SPY', 'QQQ', 'IWM', *cfg.stocks) if s in allowed]
    return list(dict.fromkeys(s for s in held+active+swing_held+core if s in allowed))[:limit]
