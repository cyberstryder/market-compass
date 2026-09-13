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


def saved_watchlist():
    return symbols((Path(__file__).parent / 'watchlist.txt').read_text())


def focus_symbols(db, c, cfg, now, limit=12):
    """Open positions first, then fresh setups, then stable index/core coverage."""
    allowed = set(cfg.watch_symbols)
    held = [p.get('underlying', p.get('symbol')) for p in db.prefix(c, 'position:').values()
            if p.get('status') == 'open' and p.get('asset') != 'future']
    requests = sorted(db.prefix(c, 'focus:').values(),
                      key=lambda row: (row.get('priority', 0), row.get('at', 0)), reverse=True)
    active = [r['symbol'] for r in requests if 0 <= now-r.get('at', 0) <= 600]
    core = [s for s in ('SPY', 'QQQ', 'IWM', *cfg.stocks) if s in allowed]
    return list(dict.fromkeys(s for s in held+active+core if s in allowed))[:limit]
