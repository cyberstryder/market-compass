"""On-demand evidence backfill for the pick checker.

When a pick check runs on a ticker Compass never tracked, pillars report
no_data because the underlying data was never ingested. This module fetches
that data at check time -- vendor apex levels, Alpaca bars, vendor flow --
writes it to the same DB keys the scanners read, and lets the caller
re-evaluate the pillar from it.

Evidence only. Never raises: every failure degrades to "no backfill",
leaving the pillar at no_data. A per-check time budget, per-symbol TTLs,
and a failure cooldown keep it from hanging checks or hammering vendors.

Backfilled raw data carries a ``backfilled`` marker plus timestamp so it is
distinguishable from standing intake. The standing scanners will pick the
keys up on their next pass like any other data.
"""
import copy
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import apex_magnet, gap_continuation, breakouts
from .apex_magnet import _bars_from_window, _bars_from_recent
from .research import apex_levels
from .vendor import flow_page
from .market import day, number

VENDOR_BASE = 'https://api.traderdaddy.pro/api/v1'
ALPACA_BARS_URL = 'https://data.alpaca.markets/v2/stocks/bars'

# Per-check total backfill budget; a check must never hang on vendors.
BACKFILL_BUDGET_S = 20
# Hard ceiling on any single HTTP request.
REQUEST_TIMEOUT_S = 8
# Freshness: intraday-flavored data refetches after 15 min, daily bars after 24h.
TTL_INTRADAY_S = 900
TTL_DAILY_S = 86400
# A failed attempt is not retried for 5 minutes.
FAIL_COOLDOWN_S = 300
# The market-wide flow feed is considered stale after 15 minutes.
FLOW_STALE_S = 900


class _BackfillError(Exception):
    """Expected backfill failure (bad creds, vendor 4xx, schema drift)."""


def _http_get_json(url, headers, timeout_s):
    """Synchronous GET with a hard timeout. Raises on any transport/HTTP/JSON error."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode('utf-8')
    except Exception as e:  # noqa: BLE001 - normalized below
        raise _BackfillError('GET failed: %s: %s' % (type(e).__name__, str(e)[:120]))
    try:
        return json.loads(body)
    except Exception as e:  # noqa: BLE001
        raise _BackfillError('Bad JSON: %s' % (type(e).__name__,))


def _parse_ts(value):
    """Alpaca ISO timestamp -> epoch seconds."""
    s = str(value or '').strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
# Fetchers (pure HTTP; no DB)
# ---------------------------------------------------------------------------

def fetch_apex(symbol, api_key, timeout_s, now):
    """Vendor apex magnet levels for one symbol, parsed like the standing feed."""
    payload = _http_get_json(VENDOR_BASE + '/gex/' + symbol + '/apex',
                             {'X-API-Key': api_key}, timeout_s)
    try:
        apex = apex_levels(payload, symbol, now)
    except (ValueError, KeyError, TypeError) as e:
        raise _BackfillError('Apex schema: %s' % str(e)[:120])
    apex['backfilled'] = True
    apex['backfilled_at'] = now
    return apex


def fetch_bars(symbol, alpaca_key, alpaca_secret, feed, timeframe, days, timeout_s):
    """Alpaca bars -> [(symbol, ts, {o,h,l,c,v})], ascending. Pure; no DB writes."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    params = urllib.parse.urlencode({
        'symbols': symbol, 'timeframe': timeframe, 'start': start,
        'limit': 10000, 'feed': feed or 'sip', 'adjustment': 'split', 'sort': 'asc',
    })
    payload = _http_get_json(ALPACA_BARS_URL + '?' + params,
                             {'APCA-API-KEY-ID': alpaca_key,
                              'APCA-API-SECRET-KEY': alpaca_secret}, timeout_s)
    rows = ((payload.get('bars') or {}).get(symbol) or [])
    items = []
    for b in rows:
        if not isinstance(b, dict):
            continue
        try:
            ts = _parse_ts(b.get('t'))
        except (ValueError, TypeError):
            continue
        p = {k: number(b.get(k)) for k in ('o', 'h', 'l', 'c', 'v')}
        if any(v is None for v in p.values()):
            continue
        items.append((symbol, ts, p))
    return sorted(items, key=lambda r: r[1])


def fetch_flow(api_key, timeout_s, now):
    """One page of the market-wide unusual-activity feed, parsed like the standing job."""
    payload = _http_get_json(
        VENDOR_BASE + '/unusual-activity?timeFrame=today&minPremium=50000&page=1&pageSize=100',
        {'X-API-Key': api_key}, timeout_s)
    try:
        return flow_page(payload, now)
    except (ValueError, KeyError, TypeError) as e:
        raise _BackfillError('Flow schema: %s' % str(e)[:120])


# ---------------------------------------------------------------------------
# Stores (same DB keys the scanners read, backfill-marked)
# ---------------------------------------------------------------------------

def store_apex(db, c, symbol, apex):
    db.put(c, 'apex:' + symbol, apex)


def store_bars(db, c, symbol, items, kind):
    """kind 'bar' (minute) or 'daily'. Merges bar_window for minute bars."""
    if not items:
        return
    db.append_bars(c, kind, 'pick_backfill', items)
    if kind == 'bar':
        window = {r[0]: r for r in db.get(c, 'bar_window:' + symbol, [])}
        for _, t, p in items:
            window[t] = [t, p['o'], p['h'], p['l'], p['c'], p['v']]
        db.put(c, 'bar_window:' + symbol,
               sorted(window.values(), key=lambda r: r[0])[-1800:])


def store_flow(db, c, part, now):
    """Minimal flow summary under the key the pillars read.

    Only written when the standing feed is absent/stale; the standing
    collector overwrites it on its next cycle. Marked so readers can tell
    it is a 1-page on-demand snapshot, not the full multi-page feed.
    """
    today = day(now)
    db.put(c, 'matrix:unusual_activity', {
        'source': 'tradermatrix', 'received': now, 'day': today,
        'rows': part.get('rows') or [], 'total': part.get('total'),
        'backfilled': True, 'backfilled_at': now, 'status': 'partial',
        'coverage_note': ('On-demand 1-page snapshot for pick-check backfill '
                          '(minPremium=50000); standing collector overwrites '
                          'on its next cycle.'),
    })


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _record(db, c, symbol):
    rec = db.get(c, 'pick_backfill:' + symbol, {})
    return rec if isinstance(rec, dict) else {}


def backfill(db, c, cfg, symbol, wants, now, budget_s=BACKFILL_BUDGET_S):
    """Fetch on-demand data for a symbol. Never raises.

    wants: subset of {'apex', 'bars', 'flow'} ('bars' = minute + daily).
    Returns {'fetched': [...], 'errors': {...}} listing data types actually
    refreshed this call.
    """
    symbol = str(symbol or '').upper()
    rec = _record(db, c, symbol)
    fetched_ts = rec.get('fetched') or {}
    if (not rec.get('succeeded_at')
            and now - rec.get('attempted_at', 0) < FAIL_COOLDOWN_S):
        return {'fetched': [], 'errors': {'_': 'cooldown'}}
    fetched, errors = [], {}
    deadline = time.monotonic() + max(1, budget_s)

    def remain():
        return max(1.0, deadline - time.monotonic())

    def fresh(key, ttl):
        return now - fetched_ts.get(key, 0) < ttl

    matrix_key = getattr(cfg, 'matrix', '') or ''
    akey = getattr(cfg, 'alpaca_key', '') or ''
    asecret = getattr(cfg, 'alpaca_secret', '') or ''
    feed = getattr(cfg, 'feed', 'sip') or 'sip'

    def attempt(key, fn):
        if time.monotonic() >= deadline:
            errors[key] = 'budget_exhausted'
            return None
        try:
            return fn()
        except _BackfillError as e:
            errors[key] = str(e)[:160]
        except Exception as e:  # noqa: BLE001 - never sink the check
            errors[key] = '%s (unexpected)' % type(e).__name__
        return None

    try:
        if 'apex' in wants and not fresh('apex', TTL_INTRADAY_S):
            def _do():
                if not matrix_key:
                    raise _BackfillError('TRADERMATRIX_API_KEY not configured')
                apex = fetch_apex(symbol, matrix_key, min(REQUEST_TIMEOUT_S, remain()), now)
                store_apex(db, c, symbol, apex)
                return True
            if attempt('apex', _do):
                fetched.append('apex')
                fetched_ts['apex'] = now
        if 'bars' in wants:
            if not fresh('minute_bars', TTL_INTRADAY_S):
                def _do_min():
                    if not (akey and asecret):
                        raise _BackfillError('Alpaca credentials not configured')
                    items = fetch_bars(symbol, akey, asecret, feed, '1Min', 2,
                                       min(REQUEST_TIMEOUT_S, remain()))
                    store_bars(db, c, symbol, items, 'bar')
                    return True
                if attempt('minute_bars', _do_min):
                    fetched.append('minute_bars')
                    fetched_ts['minute_bars'] = now
            if not fresh('daily_bars', TTL_DAILY_S):
                def _do_day():
                    if not (akey and asecret):
                        raise _BackfillError('Alpaca credentials not configured')
                    items = fetch_bars(symbol, akey, asecret, feed, '1Day', 130,
                                       min(REQUEST_TIMEOUT_S, remain()))
                    store_bars(db, c, symbol, items, 'daily')
                    return True
                if attempt('daily_bars', _do_day):
                    fetched.append('daily_bars')
                    fetched_ts['daily_bars'] = now
        if 'flow' in wants and not fresh('flow', TTL_INTRADAY_S):
            def _do_flow():
                if not matrix_key:
                    raise _BackfillError('TRADERMATRIX_API_KEY not configured')
                part = fetch_flow(matrix_key, min(REQUEST_TIMEOUT_S, remain()), now)
                store_flow(db, c, part, now)
                return True
            if attempt('flow', _do_flow):
                fetched.append('flow')
                fetched_ts['flow'] = now
    finally:
        rec.update(attempted_at=now, fetched=fetched_ts)
        if fetched:
            rec['succeeded_at'] = now
        if errors:
            rec['last_errors'] = errors
        try:
            db.put(c, 'pick_backfill:' + symbol, rec)
        except Exception:  # noqa: BLE001 - bookkeeping must not sink the check
            pass
    return {'fetched': fetched, 'errors': errors}


# ---------------------------------------------------------------------------
# Triggers: no_data because the underlying data is absent (not "no signal")
# ---------------------------------------------------------------------------

def apex_needs(db, c, symbol, now):
    """True when there is no usable vendor apex snapshot for the symbol."""
    apex = db.get(c, 'apex:' + symbol)
    if not isinstance(apex, dict):
        return True
    return now - (apex.get('received') or 0) >= TTL_INTRADAY_S


def flow_needs(db, c, now):
    """True only when the market-wide flow feed itself is absent/stale.

    A reachable feed with zero prints for the symbol is a real "no flow"
    answer -- that must NOT trigger a backfill.
    """
    flow = db.get(c, 'matrix:unusual_activity')
    if not isinstance(flow, dict):
        return True
    return now - (flow.get('received') or 0) >= FLOW_STALE_S


def gap_needs(db, c, symbol, now):
    """True when today's gap state was never evaluated for the symbol.

    An evaluated-but-excluded state (e.g. gap below minimum) is a real
    answer, not missing data.
    """
    return db.get(c, 'gap_cont:%s:%s' % (symbol, day(now))) is None


def breakout_needs(db, c, symbol):
    """True when there are too few daily bars to evaluate breakouts."""
    return len(breakouts.daily_bars(db, c, symbol)) < 16


# ---------------------------------------------------------------------------
# Re-evaluation from backfilled (or pre-existing) data
# ---------------------------------------------------------------------------

def _minute_bars(db, c, symbol):
    window = _bars_from_window(db.get(c, 'bar_window:' + symbol, []))
    if window:
        return window
    return _bars_from_recent(db.recent(c, 'bar', symbol, limit=500))


def _daily_closes(db, c, symbol):
    rows = db.recent(c, 'daily', symbol, limit=25)
    return [r['payload']['c'] for r in reversed(rows)
            if (r.get('payload') or {}).get('c')]


def eval_apex(db, c, symbol, direction, spot, target, now):
    """Classify the magnet row directly from the apex snapshot + bars."""
    apex = db.get(c, 'apex:' + symbol)
    if not isinstance(apex, dict):
        return None, None
    bars = _minute_bars(db, c, symbol)
    row = apex_magnet.classify_row(symbol, apex, bars, _daily_closes(db, c, symbol),
                                   None, now)
    if not isinstance(row, dict) or 'excluded' in row:
        return None, None
    from . import pick_check as pc
    section = pc._apex_section({'rows': [row]}, symbol, direction, spot, target)
    return section, row.get('spot')


def eval_gap(db, c, cfg, symbol, direction, now):
    """Run the gap state machine for one symbol with on-demand large-cap inclusion.

    The standing $2B filter bounds standing compute; a user-requested check
    evaluates the named ticker regardless, and says so in the basis.
    Returns None (writing nothing) when the minimum inputs for a real
    evaluation are absent -- we do not claim an evaluation we could not run.
    """
    from .market import session
    today = day(now)
    bounds = session(today)
    if not bounds:
        return None
    sess_open = bounds[0]
    bars = gap_continuation._bars(db, c, symbol, since=sess_open - 3600)
    daily_rows = db.recent(c, 'daily', symbol, limit=5)
    if not bars or len(daily_rows) < 2:
        return None
    shim = copy.copy(cfg)
    caps = gap_continuation.large_caps(cfg) | {symbol}
    shim.gap_large_caps = ','.join(sorted(caps))
    apex = db.get(c, 'apex:' + symbol)
    flow = db.get(c, 'matrix:unusual_activity', {}) or {}
    ctx = {'flow_rows': flow.get('rows') or [],
           'sector_dashboard': db.get(c, 'research:sector_dashboard', {}),
           'sector_map': db.get(c, 'research:extra_sector_map', {}),
           'apex': {symbol: apex} if isinstance(apex, dict) else {}}
    try:
        state = gap_continuation._advance(db, c, symbol, now, shim, ctx)
    except Exception:  # noqa: BLE001 - evaluation must not sink the check
        return None
    if not isinstance(state, dict):
        return None
    state['backfilled'] = True
    try:
        db.put(c, 'gap_cont:%s:%s' % (symbol, day(now)), state)
    except Exception:  # noqa: BLE001
        pass
    from . import pick_check as pc
    try:
        return pc._gap_section(gap_continuation.display(db, c, now),
                               symbol, direction)
    except Exception:  # noqa: BLE001
        return None


def eval_breakout(db, c, symbol, direction):
    """Detect range breaks / triangle coils directly from daily bars."""
    bars = breakouts.daily_bars(db, c, symbol)
    if len(bars) < 16:
        return None
    fresh, forming = [], []
    for pattern, lookback in breakouts.RANGE_FLAVORS.items():
        try:
            brk = breakouts.detect_range_break(bars, lookback)
        except Exception:  # noqa: BLE001
            continue
        if brk:
            fresh.append({'symbol': symbol, 'direction': brk.get('direction'),
                          'level': brk.get('level'), 'status': 'fresh',
                          'pattern': pattern})
    try:
        coil = breakouts.detect_triangle(bars[-16:-1])
        if coil:
            state, tdir = breakouts.triangle_state(coil, bars[-1])
            if state == 'firing':
                fresh.append({'symbol': symbol, 'direction': tdir,
                              'level': None, 'status': 'firing',
                              'pattern': coil[0]})
            else:
                forming.append({'symbol': symbol, 'direction': tdir,
                                'level': None, 'status': 'forming',
                                'pattern': coil[0]})
        else:
            coil_now = breakouts.detect_triangle(bars[-15:])
            if coil_now:
                state, tdir = breakouts.triangle_state(coil_now, bars[-1])
                forming.append({'symbol': symbol, 'direction': tdir,
                                'level': None, 'status': 'forming',
                                'pattern': coil_now[0]})
    except Exception:  # noqa: BLE001
        pass
    from . import pick_check as pc
    return pc._breakout_section({'fresh': fresh, 'forming': forming}, symbol, direction)


def tape_flow_context(db, c, symbol, now):
    """Unconfirmed institutional flow context for a symbol with no trials.

    Returns an 'info' section when today's flow feed holds prints for the
    symbol, else None. Explicitly not a tape confirmation -- there are no
    Compass trials to confirm against.
    """
    flow = db.get(c, 'matrix:unusual_activity', {}) or {}
    rows = [r for r in (flow.get('rows') or [])
            if str(r.get('symbol') or '').upper() == symbol
            and (r.get('source_ts') or 0) >= now - 86400]
    if not rows:
        return None
    call_px = puts_px = 0.0
    for r in rows:
        prem = number(r.get('premium')) or 0
        otype = str(r.get('option_type') or '').lower()
        if otype.startswith('c'):
            call_px += prem
        elif otype.startswith('p'):
            puts_px += prem
    return {'alignment': 'info',
            'note': ('%d institutional print(s) ($%.0f call / $%.0f put premium) '
                     'in today\'s flow feed; no Compass trials to confirm against '
                     '-- unconfirmed context only.' % (len(rows), call_px, puts_px)),
            'detail': {'prints': len(rows), 'call_premium': round(call_px, 2),
                       'put_premium': round(puts_px, 2)},
            'basis': ('Raw vendor flow prints for the symbol; tape-confirmed '
                      'status requires a Compass setup trial, which does not exist '
                      'for untracked symbols.')}




def reevaluate(db, c, cfg, symbol, direction, spot, target, now, pillars):
    """Re-run pillar evaluations after a backfill attempt.

    pillars: set of pillar names whose data may now be present.
    Returns ({pillar_name: section}, row_spot_or_None). The flow-context
    upgrade applies whenever tape has no trials, whether or not the flow
    feed itself was backfilled this check. Never raises.
    """
    out, row_spot = {}, None
    try:
        if 'apex' in pillars:
            section, asp = eval_apex(db, c, symbol, direction, spot, target, now)
            if section is not None:
                out['apex'] = section
                row_spot = asp
        if 'gap' in pillars:
            section = eval_gap(db, c, cfg, symbol, direction, now)
            if section is not None:
                out['gap'] = section
        if 'breakout' in pillars:
            section = eval_breakout(db, c, symbol, direction)
            if section is not None:
                out['breakout'] = section
        ctx = tape_flow_context(db, c, symbol, now)
        if ctx is not None:
            out['tape'] = ctx
    except Exception:  # noqa: BLE001 - re-evaluation must not sink the check
        pass
    return out, row_spot
