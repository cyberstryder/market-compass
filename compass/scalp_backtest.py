"""Historical gap-fade backtest for the 0DTE stock scalp watchlist.

Reads 1-minute bars from the events table (kind='bar', covered by the
events_kind_symbol_time index on (kind, symbol, ts)) and replays the
gap-fade signal rule: a >=2% gap between the prior session's last close
and the current session's first open.

Bars live in the events table, not the state table, so this deliberately
does NOT use db.prefix('bar:...') (that keyspace does not exist in state).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .market import NY, number

GAP_THRESHOLD_PCT = 2.0
SESSION_OPEN_H, SESSION_OPEN_M = 9, 30  # ET
SESSION_CLOSE_H = 16  # ET (last bar must be before this)
OPEN_WINDOW_S = 600  # first 10 minutes of the session


def _payload(row):
    """Raw SQL skips SQLAlchemy's JSON deserializer (SQLite returns str)."""
    p = row["payload"]
    if isinstance(p, str):
        import json
        try:
            p = json.loads(p)
        except Exception:
            return {}
    return p or {}

_OPEN_Q = text("""
    SELECT ts, payload FROM events
    WHERE kind='bar' AND symbol=:s AND ts>=:lo AND ts<:hi
    ORDER BY ts LIMIT 1""")
_CLOSE_Q = text("""
    SELECT ts, payload FROM events
    WHERE kind='bar' AND symbol=:s AND ts>=:lo AND ts<:hi
    ORDER BY ts DESC LIMIT 1""")


def _session_open_utc(day):
    return datetime(day.year, day.month, day.day,
                    SESSION_OPEN_H, SESSION_OPEN_M, tzinfo=NY).timestamp()


def _prev_trading_day(day):
    pd = day - timedelta(days=1)
    while pd.weekday() >= 5:
        pd -= timedelta(days=1)
    return pd


def run_gap_backtest(db, days, symbols, now=None):
    """Replay gap-fade signals over the last `days` trading days per symbol.

    Returns {symbol: {'signals': n, 'details': [last-10 signal dicts]}}.
    A signal dict carries date, gap_pct, direction ('short' fades a gap-up,
    'long' fades a gap-down), entry (session open) and prior_close.

    Optimized: fetches all bars per symbol in one query instead of 2 per day.
    """
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    today = datetime.fromtimestamp(now, tz=NY).date()
    results = {}

    # Calculate date range
    start_date = today - timedelta(days=days * 3 + 15)  # Buffer for weekends/holidays
    start_ts = datetime(start_date.year, start_date.month, start_date.day,
                       0, 0, tzinfo=NY).timestamp()
    end_ts = datetime(today.year, today.month, today.day,
                     23, 59, tzinfo=NY).timestamp()

    with db.tx() as c:
        for symbol in symbols:
            # Fetch all 1-min bars for this symbol in the date range in ONE query
            # We need session opens (9:30 ET) and prior closes (4:00 ET)
            rows = c.execute(
                text("""
                    SELECT ts, payload FROM events
                    WHERE kind='bar' AND symbol=:s AND ts>=:lo AND ts<:hi
                    ORDER BY ts
                """),
                {"s": symbol, "lo": start_ts, "hi": end_ts}
            ).mappings().all()

            # Index bars by date for fast lookup
            # Key: date -> {'open': px, 'close': px}
            daily = {}
            for row in rows:
                ts = row['ts']
                dt = datetime.fromtimestamp(ts, tz=NY)
                d = dt.date()
                payload = _payload(row)
                if d not in daily:
                    daily[d] = {}
                # 9:30 ET is session open (within first 5 min)
                # 16:00 ET is session close (within last 5 min)
                hm = (dt.hour, dt.minute)
                if hm >= (9, 30) and hm < (9, 35):
                    if 'open' not in daily[d]:
                        o = number(payload.get("o"))
                        if o:
                            daily[d]['open'] = o
                if hm >= (15, 55):
                    c_px = number(payload.get("c"))
                    if c_px:
                        daily[d]['close'] = c_px

            # Now find gaps
            signals = []
            checked = 0
            back = 0
            while checked < days and back < days * 3 + 15:
                back += 1
                d = today - timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                checked += 1

                if d not in daily or 'open' not in daily[d]:
                    continue
                open_px = daily[d]['open']

                pd = _prev_trading_day(d)
                if pd not in daily or 'close' not in daily[pd]:
                    continue
                prior_close = daily[pd]['close']

                gap_pct = (open_px - prior_close) / prior_close * 100.0
                if abs(gap_pct) >= GAP_THRESHOLD_PCT:
                    signals.append({
                        "date": d.isoformat(),
                        "gap_pct": round(gap_pct, 2),
                        "direction": "short" if gap_pct > 0 else "long",
                        "entry": round(open_px, 2),
                        "prior_close": round(prior_close, 2),
                    })
            results[symbol] = {"signals": len(signals),
                               "details": signals[-10:]}
    return results
