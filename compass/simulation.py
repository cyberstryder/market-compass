"""Shared, conservative bracket prices for forward simulations only."""
from .instruments import tick_price
from .market import number

FILL_VERSION = 'sampled-bracket-v2'
FILL_DESCRIPTION = 'Executable bid/ask; one adverse entry/stop tick; outward-rounded stop and 2R target; no favorable fill beyond target'


def bracket(signal, quote, spec):
    long = signal['side'] == 'long'
    entry = tick_price(quote['ask']+spec['tick'] if long else quote['bid']-spec['tick'], spec['tick'], long)
    # An option signal's inherited invalidation belongs to its underlying.
    stop = number(signal.get('invalidation')) if spec['asset'] != 'option' else None
    distance = number(signal.get('stop_distance'))
    if stop is None and distance is not None:
        stop = entry + (-1 if long else 1)*distance
    if stop is None:
        raise ValueError('Explicit price invalidation is required')
    stop = tick_price(stop, spec['tick'], not long)
    distance = (entry-stop)*(1 if long else -1)
    if distance <= 0 or min(entry,stop) <= 0 or (quote['bid'] <= stop if long else quote['ask'] >= stop):
        raise ValueError('Setup already invalidated or invalid price')
    target = tick_price(entry+(1 if long else -1)*2*distance, spec['tick'], long)
    return dict(entry=entry, stop=stop, target=target, distance=distance)


def exit_price(position, quote, reason):
    if reason == 'target':
        return position['target']
    long = position['side'] == 'long'
    price = quote['bid']-position['tick'] if long else quote['ask']+position['tick']
    return tick_price(price, position['tick'], not long)
