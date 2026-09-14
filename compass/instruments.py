"""Explicit futures specifications; fees are illustrative per-side research costs."""
import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

# tick, dollars per point, illustrative fee, portfolio quantity ceiling, exchange
FUTURES = {
    'MES': (.25, 5, 1.5, 2, 'CME'), 'MNQ': (.25, 2, 1.5, 2, 'CME'),
    'ES': (.25, 50, 2.5, 1, 'CME'), 'NQ': (.25, 20, 2.5, 1, 'CME'),
    'YM': (1, 5, 2.5, 1, 'CBOT'), 'MYM': (1, .5, 1.5, 2, 'CBOT'),
    'MGC': (.1, 10, 1.5, 2, 'COMEX'), 'GC': (.1, 100, 2.5, 1, 'COMEX'),
    'SIL': (.005, 1000, 1.5, 2, 'COMEX'), 'SI': (.005, 5000, 2.5, 1, 'COMEX'),
    'MCL': (.01, 100, 1.5, 2, 'NYMEX'), 'CL': (.01, 1000, 2.5, 1, 'NYMEX'),
}
INDEX_FUTURES = frozenset(('MES', 'MNQ', 'ES', 'NQ', 'YM', 'MYM'))
ROOTS = '|'.join(sorted(FUTURES, key=len, reverse=True))
DATED = re.compile(r'^(' + ROOTS + r')([FGHJKMNQUVXZ])(\d{1,4})(?:@\d+)?$')
FUTURE = re.compile(r'^(' + ROOTS + r')(?:[12]!|\.[cvn]\.0|[FGHJKMNQUVXZ]\d{1,4})(?:@\d+)?$')

def future_root(symbol):
    match = FUTURE.fullmatch(symbol.split(':')[-1])
    return match.group(1) if match else None

def future_spec(symbol):
    root = future_root(symbol)
    if not root:
        return None
    tick, multiplier, fee, maximum, exchange = FUTURES[root]
    return dict(tick=tick, multiplier=multiplier, fee=fee, max_qty=maximum,
                asset='future', root=root, exchange=exchange)

def tick_price(price, tick, up):
    step = Decimal(str(tick))
    return float((Decimal(str(price))/step).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)*step)

def configured(cfg):
    return tuple(dict.fromkeys((*cfg.futures, *cfg.extra_futures)))
