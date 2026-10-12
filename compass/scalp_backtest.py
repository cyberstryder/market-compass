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
    """
    now = now if now is not None else datetime.now(timezone.utc).timestamp()
    today = datetime.fromtimestamp(now, tz=NY).date()
    results = {}
    with db.tx() as c:
        for symbol in symbols:
            signals = []
            checked = 0
            back = 0
            # Calendar-day cap keeps this bounded even for large `days`.
            while checked < days and back < days * 3 + 15:
                back += 1
                d = today - timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                checked += 1
                open_ts = _session_open_utc(d)
                row = c.execute(
                    _OPEN_Q,
                    {"s": symbol, "lo": open_ts, "hi": open_ts + OPEN_WINDOW_S},
                ).mappings().first()
                if not row:
                    continue  # holiday or missing data: skip, never fabricate
                open_px = number(_payload(row).get("o"))
                if not open_px:
                    continue
                pd = _prev_trading_day(d)
                p_open = _session_open_utc(pd)
                p_close = datetime(pd.year, pd.month, pd.day,
                                   SESSION_CLOSE_H, 0, tzinfo=NY).timestamp()
                prow = c.execute(
                    _CLOSE_Q,
                    {"s": symbol, "lo": p_open, "hi": p_close},
                ).mappings().first()
                if not prow:
                    continue
                prior_close = number(_payload(prow).get("c"))
                if not prior_close:
                    continue
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
