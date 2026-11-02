"""Underlying-first scoring from stored 1-minute bars.

Both option books (swing ideas, 0DTE scanner shadow) used to score only the
option quote path, so an observation existed only when a streamed contract
quote happened to arrive. The 2026-11-01 replay of both books on two years
of 1-minute bars (claude-exchange/from-claude/2026-11-01-swings-0dte) scored
the SIGNAL on the underlying instead. This module brings that measurement
into production: every candidate gets a fixed-horizon return and a
stop/target path on the underlying from the `bar` events the collector
already stores, independent of any option quote. Option P&L stays a
separate, optional measurement.

Conventions (identical to the offline harness):
  * entry = open of the first completed 1-minute bar after the signal,
    +/- SLIP in the trade direction;
  * fixed horizon = open of the bar `minutes` after the entry bar;
  * bracket = stop/target on bar extremes from the entry bar on, stop first
    when both are touched in one bar, stop fills SLIP through, a gap past
    the level fills at that bar's open, time stop at `end`;
  * R = signed move / initial risk; bps = signed move / entry * 1e4.
Every function is pure on a list of bar dicts {ts,o,h,l,c} so it can be
tested without a database; `bars_between` is the only query.
"""
from sqlalchemy import select
from .store import events

SLIP = 0.01
MAX_REFERENCE_AGE = 300  # a horizon price may come from a bar completed up to 5 min earlier


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def bars_between(c, symbol, start, end, limit=20000):
    """Completed 1-minute bars with start <= ts < end, latest revision per
    minute, ascending. Bounded; a window past `limit` rows is truncated at
    the newest rows and the caller sees a shorter list."""
    rows = c.execute(select(events.c.id, events.c.ts, events.c.payload).where(
        events.c.kind == 'bar', events.c.symbol == symbol,
        events.c.ts >= start, events.c.ts < end).order_by(events.c.ts.desc(), events.c.id.desc())
        .limit(limit)).all()
    latest = {}
    for rid, ts, p in rows:
        if ts in latest:
            continue
        o, h, l, cl = (_num(p.get(k)) for k in ('o', 'h', 'l', 'c'))
        if None in (o, h, l, cl) or min(o, h, l, cl) <= 0:
            continue
        latest[ts] = dict(ts=ts, o=o, h=h, l=l, c=cl)
    return [latest[t] for t in sorted(latest)]


def entry_from(bars, signal_time, side):
    """First completed bar that starts at/after the signal bar's close."""
    sign = 1 if side == 'long' else -1
    for i, b in enumerate(bars):
        if b['ts'] >= signal_time:
            return i, round(b['o'] + sign * SLIP, 4)
    return None, None


def price_at(bars, target):
    """Close of the last bar completed (ts+60) at or before `target`, if it
    completed within MAX_REFERENCE_AGE of target."""
    best = None
    for b in bars:
        if b['ts'] + 60 <= target:
            best = b
        else:
            break
    if best is None or target - (best['ts'] + 60) > MAX_REFERENCE_AGE:
        return None
    return best['c']


def horizon_return(bars, entry_index, entry, side, minutes):
    """Directional bps from entry to the open of the bar `minutes` later,
    None when the window runs past the available bars."""
    j = entry_index + minutes
    if j >= len(bars):
        return None
    sign = 1 if side == 'long' else -1
    return sign * (bars[j]['o'] / entry - 1) * 1e4


def bracket(bars, entry_index, entry, side, stop, target, end=None):
    """Walk stop/target on bar extremes from the entry bar. Returns dict with
    exit, exited_at, reason ('stop'|'target'|'time'|'incomplete'), R, bps."""
    sign = 1 if side == 'long' else -1
    risk = sign * (entry - stop)
    if risk <= 0:
        return dict(reason='invalid_geometry', R=None, bps=None, exit=None, exited_at=None, risk=risk)
    last = None
    for b in bars[entry_index:]:
        if end is not None and b['ts'] + 60 > end:
            break
        last = b
        hit_stop = b['l'] <= stop if sign == 1 else b['h'] >= stop
        hit_tgt = b['h'] >= target if sign == 1 else b['l'] <= target
        if hit_stop:  # stop first when both touch inside one bar
            px = stop - sign * SLIP
            if (sign == 1 and b['o'] < stop) or (sign == -1 and b['o'] > stop):
                px = b['o'] - sign * SLIP
            return _finish(entry, px, sign, risk, 'stop', b['ts'] + 60)
        if hit_tgt:
            px = target - sign * SLIP
            if (sign == 1 and b['o'] > target) or (sign == -1 and b['o'] < target):
                px = b['o'] - sign * SLIP
            return _finish(entry, px, sign, risk, 'target', b['ts'] + 60)
    if last is None:
        return dict(reason='incomplete', R=None, bps=None, exit=None, exited_at=None, risk=risk)
    # time stop only when the bars reach the end of the window
    complete = end is not None and last['ts'] + 60 >= end - 60
    return _finish(entry, last['c'] - sign * SLIP, sign, risk, 'time' if complete else 'incomplete', last['ts'] + 60)


def _finish(entry, px, sign, risk, reason, at):
    move = sign * (px - entry)
    return dict(reason=reason, exit=round(px, 4), exited_at=at, risk=risk,
                R=round(move / risk, 4), bps=round(move / entry * 1e4, 2))


def summarize(items, key='R'):
    """Mean / win rate / n over records carrying a numeric `key`; None-safe."""
    vals = [v for v in (i.get(key) for i in items) if isinstance(v, (int, float))]
    if not vals:
        return dict(n=0, mean=None, win_rate=None)
    return dict(n=len(vals), mean=round(sum(vals) / len(vals), 4),
                win_rate=round(sum(1 for v in vals if v > 0) / len(vals), 4))
