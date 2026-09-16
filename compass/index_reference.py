"""Dated reference levels for estimates; never an index execution feed."""
from datetime import datetime, timedelta
from .market import NY, day, number, session, ts


def prior_session(now):
    d = datetime.fromtimestamp(now, NY).date() - timedelta(days=1)
    for _ in range(10):
        if session(d.isoformat()):
            return d.isoformat()
        d -= timedelta(days=1)
    return None


def reference_pair(spy, spx, reference_day, now, source):
    spy, spx = number(spy), number(spx)
    expected = prior_session(now)
    if reference_day != expected or not spy or not spx or not 8 < spx / spy < 12:
        return {'status': 'unavailable', 'reason': 'Missing or mismatched previous-session SPY/SPX closes'}
    return {'status': 'available', 'reference_day': reference_day,
            'reference_ts': session(reference_day)[1], 'spy_close': spy, 'spx_close': spx,
            'ratio': spx / spy, 'source': source, 'received': now,
            'method': 'SPY level × previous-session SPX/SPY closing ratio; XSP = SPX / 10',
            'limitation': 'Estimated chart references, not live index prices or index-option strikes; basis can change, including around SPY distributions.'}


def yahoo_close(payload, symbol, reference_day):
    rows = (payload.get('chart') or {}).get('result') or []
    if len(rows) != 1 or rows[0].get('meta', {}).get('symbol') != symbol:
        raise ValueError('Index reference symbol mismatch')
    row = rows[0]
    closes = row.get('indicators', {}).get('quote', [{}])[0].get('close', [])
    for stamp, close in zip(row.get('timestamp', []), closes):
        if day(stamp) == reference_day and number(close) and close > 0:
            return float(close)
    raise ValueError('Previous-session close missing')


async def collect(collector, now=None, force=False):
    """At most four public/reference GETs per attempt; no credentials in records."""
    import time
    now = time.time() if now is None else now
    db, cfg = collector.db, collector.cfg
    if not cfg.spy_morning_brief:
        return
    today = session(day(now))
    if not today or (not force and not today[0] - 3.5 * 3600 <= now < today[0] + 1800):
        return
    prior = prior_session(now)
    with db.tx() as c:
        old = db.get(c, 'index_reference:SPY', {})
        if old.get('reference_day') == prior and old.get('status') == 'available':
            return old
        attempt = db.get(c, 'index_reference:attempt', {})
        if now - attempt.get('at', 0) < 3600:
            return
        db.put(c, 'index_reference:attempt', {'at': now})
    errors = []
    result = None
    if cfg.massive:
        try:
            closes = {}
            for symbol in ('I:SPX', 'SPY'):
                data = await collector.get('https://api.massive.com/v2/aggs/ticker/' + symbol +
                    '/range/1/day/' + prior + '/' + prior,
                    params={'apiKey': cfg.massive, 'adjusted': 'false', 'limit': 1})
                rows = data.get('results') or []
                if data.get('ticker') != symbol or len(rows) != 1 or day(ts(rows[0]['t'])) != prior:
                    raise ValueError('Previous-session reference missing')
                closes[symbol] = rows[0]['c']
            result = reference_pair(closes['SPY'], closes['I:SPX'], prior, now, 'Massive daily reference closes')
        except Exception as error:
            errors.append('Massive: ' + type(error).__name__)
    if not result or result.get('status') != 'available':
        try:
            # Raw close, not adjusted close. Both symbols must describe the same session.
            start = int(datetime.fromisoformat(prior).replace(tzinfo=NY).timestamp())
            closes = {}
            for symbol, path in (('^GSPC', '%5EGSPC'), ('SPY', 'SPY')):
                data = await collector.get('https://query1.finance.yahoo.com/v8/finance/chart/' + path,
                    params={'period1': start, 'period2': start + 86400, 'interval': '1d'},
                    headers={'User-Agent': 'Market-Compass/1.0'})
                closes[symbol] = yahoo_close(data, symbol, prior)
            result = reference_pair(closes['SPY'], closes['^GSPC'], prior, now, 'Yahoo Finance raw daily reference closes')
        except Exception as error:
            errors.append('Yahoo: ' + type(error).__name__)
            result = {'status': 'unavailable', 'received': now, 'reason': 'No matched dated index reference', 'errors': errors}
    with db.tx() as c:
        db.put(c, 'index_reference:SPY', result)
    db.health('index_reference', result['status'], result.get('method', result.get('reason')), now)
    return result
