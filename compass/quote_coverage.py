"""Read-only archive coverage for the collected stock universe."""
from sqlalchemy import select, func
from .store import events, state


def stock_archive_health(c, symbols, quotes, now, market_open):
    from .universe import INDEX_UNDERLYINGS
    excluded = sorted(set(symbols) & INDEX_UNDERLYINGS)
    symbols = sorted(set(symbols) - INDEX_UNDERLYINGS)
    # Correlated, indexed LIMIT 1 probes: one round trip, no full journal scan.
    latest = select(events.c.ts).where(events.c.kind == 'quote',
        events.c.symbol == func.substr(state.c.key, 7), events.c.ts <= now,
        events.c.received <= now).order_by(events.c.ts.desc()).limit(1).scalar_subquery()
    archived = dict(c.execute(select(state.c.key, latest.label('archived_ts'))
        .where(state.c.key.in_(['quote:'+s for s in symbols]))).all())
    rows = []
    for symbol in sorted(set(symbols)):
        q = quotes.get('quote:'+symbol) or {}
        source = q.get('ts')
        stamp = archived.get('quote:'+symbol)
        lag = max(0, source-stamp) if source is not None and stamp is not None else None
        status = ('no_quote' if source is None else 'missing_archive' if stamp is None
            else 'archive_behind' if lag > 15 else 'retained')
        rows.append(dict(symbol=symbol, status=status, latest_quote_ts=source,
            archived_ts=stamp, lag_seconds=lag,
            live_current=source is not None and 0 <= now-source <= 5))
    missing = [r['symbol'] for r in rows if r['status'] in ('missing_archive','archive_behind')]
    waiting = [r['symbol'] for r in rows if r['status'] == 'no_quote']
    return dict(name='stock_quote_archive', status='partial' if missing else 'waiting' if waiting else 'ready',
        checked_at=now, source_ts=None, rows=rows, missing_symbols=missing, excluded_index_symbols=excluded,
        detail=f"{sum(r['status']=='retained' for r in rows)}/{len(rows)} stock archives caught up to latest quotes. "
            + ('Missing/behind: '+', '.join(missing)+'. ' if missing else '')
            + ('No quote yet: '+', '.join(waiting)+'. ' if waiting else '')
            + ('Index roots outside stock coverage: '+', '.join(excluded)+'. ' if excluded else '')
            + ('Market closed. ' if not market_open else '')
            + 'Archive presence does not establish a complete price path; checkpoint windows are checked separately.')
