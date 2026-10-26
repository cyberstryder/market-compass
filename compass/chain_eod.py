"""End-of-day option-chain snapshot archive (2026-10-26, Step 0b).

Why this exists: the live chain (``chain:SYM`` state) is overwritten on every
refresh, the old ``chain_archive`` event copy was removed 2026-10-21 as
write-only bulk, and the Massive chain path discarded theta/vega. The project
therefore retained NO option-chain/IV history, so no options book could be
modeled retrospectively (Claude's what-must-change §1.3, verified in
Task 004).

This module retains ONE snapshot per underlying per trading day, forever:
the chain as served by the existing provider pipeline (Massive primary,
Alpaca fallback, via ``Collectors.massive_chain``/``alpaca_chain`` with
``full_greeks=True``), filtered to strikes within +/-10% of the underlying
close and expiries <= 60 DTE, greeks + IV included.

Storage: one event per (date, symbol) in the events table, kind
``chain_eod``, event key ``chain_eod:<date>:<SYM>`` — the key makes a re-run
for the same date a no-op per symbol (``Store.append`` dedupes on key).
A per-day manifest in state (``chain_eod:<date>``) records every symbol's
outcome — ok or error, source, contract count, payload bytes — so failures
are recorded, never silently skipped, and never fail the day's batch.

Registry: the kind is registered in ``copy_archive.RETENTION`` (the R2 pump
copies it like every other retained kind) and in
``archive_prune.EVENT_POLICY`` with a permanence-horizon retention (see
there). Nothing prunes this kind: the archive is the point.

Trigger: POST/GET ``/api/admin/chain-eod-snapshot`` (cron-token gated,
see app.py), fired once daily after the close by the goal cron
``eod-chain-snapshot``. History before 2026-10-26 does not exist and is
never fabricated.

Repair 2026-10-26 (first fire captured nothing): the endpoint runs on the
Dashboard service, which holds NONE of the provider credentials (no
MASSIVE_API_KEY, no APCA pair — only the Collector service has them), so
every symbol failed with an Alpaca HTTP 401 in ~4 s. Guards added so this
class of failure can never again pass as a completed day: a credential
preflight that records a day-level ``fatal`` instead of per-symbol noise,
``already_complete`` only for a finished day with zero errors, and the run
lease is released when the run ends so a same-day re-trigger can retry.
"""
import asyncio
import json
import logging
import time
from datetime import date as _date
from datetime import timedelta

from .market import day, number

LOG = logging.getLogger('uvicorn.error')

KIND = 'chain_eod'
DTE_MAX = 60
BAND = 0.10
CONCURRENCY = 3
NO_PROVIDER_CREDENTIALS = 'no_provider_credentials'


class ChainConfigError(Exception):
    """No option-chain provider is configured on this service."""


def providers_configured(cfg):
    """True when at least one chain provider has credentials in this
    process: Massive (API key) or Alpaca (key + secret pair). The snapshot
    endpoint's service must hold one of them or every fetch is a 401."""
    if getattr(cfg, 'massive', None):
        return True
    return bool(getattr(cfg, 'alpaca_key', None)
                and getattr(cfg, 'alpaca_secret', None))

# Fields kept per contract in the archive. ``underlying``/``multiplier`` are
# hoisted to the payload level (identical for every row); the live path's
# diagnostic clutter (gamma_field/greek_fields) is not research data.
CONTRACT_FIELDS = ('symbol', 'expiry', 'strike', 'type', 'oi', 'oi_date',
                   'delta', 'gamma', 'theta', 'vega', 'iv', 'volume')


def manifest_key(date_str):
    return KIND + ':' + date_str


def event_key(date_str, symbol):
    return f'{KIND}:{date_str}:{symbol}'


def shape_contract(o):
    """Project a provider contract dict (providers.py chain shape) onto the
    archive schema, quote included, greeks + IV retained."""
    out = {k: o.get(k) for k in CONTRACT_FIELDS}
    q = o.get('quote') or {}
    out['quote'] = {'ts': q.get('ts'), 'bid': q.get('bid'), 'ask': q.get('ask'),
                    'bid_size': q.get('bid_size', 0), 'ask_size': q.get('ask_size', 0)}
    return out


def filter_contracts(contracts, close, date_str, dte_max=DTE_MAX, band=BAND):
    """Keep contracts expiring in [date_str, date_str + dte_max] with strikes
    within +/-band of the underlying close. Returns archive-shaped rows."""
    end = (_date.fromisoformat(date_str)
           + timedelta(days=dte_max)).isoformat()
    lo, hi = close * (1 - band), close * (1 + band)
    out = []
    for o in contracts:
        expiry = o.get('expiry') or ''
        strike = number(o.get('strike'))
        if not o.get('symbol') or strike is None or strike <= 0:
            continue
        if not (date_str <= expiry <= end):
            continue
        if not (lo <= strike <= hi):
            continue
        out.append(shape_contract(o))
    out.sort(key=lambda r: (r['expiry'], r['strike'], r['type'] or ''))
    return out


def underlying_close(db, c, symbol, now):
    """The underlying reference for the band: fresh quote mid, else the last
    bar's close — the same convention chains() uses for selection_reference.
    Returns (price, ts) or (None, None)."""
    q = db.get(c, 'quote:' + symbol)
    if q and -1 <= now - q['ts'] < 120 and 0 < (q.get('bid') or 0) <= (q.get('ask') or 0):
        return (q['bid'] + q['ask']) / 2, q['ts']
    last_bar = db.recent(c, 'bar', symbol, limit=1)
    if last_bar:
        return number(last_bar[0]['payload'].get('c')), last_bar[0]['ts']
    return None, None


async def fetch_chain(collectors, symbol, expiry_start, expiry_end):
    """Massive primary, Alpaca fallback — mirrors the chains() policy.
    Returns (contracts, complete, source). Raises only if both fail."""
    if not providers_configured(collectors.cfg):
        raise ChainConfigError(
            'no provider credentials configured on this service '
            '(MASSIVE_API_KEY and APCA_API_KEY_ID/APCA_API_SECRET_KEY '
            'are all unset)')
    if collectors.cfg.massive:
        try:
            contracts, complete = await collectors.massive_chain(
                symbol, expiry_start=expiry_start, expiry_end=expiry_end,
                full_greeks=True)
            return contracts, complete, 'massive'
        except Exception:
            contracts, complete = await collectors.alpaca_chain(
                symbol, expiry_start=expiry_start, expiry_end=expiry_end)
            return contracts, complete, 'alpaca'
    contracts, complete = await collectors.alpaca_chain(
        symbol, expiry_start=expiry_start, expiry_end=expiry_end)
    return contracts, complete, 'alpaca'


def _manifest(db, c, date_str):
    m = db.get(c, manifest_key(date_str))
    if not isinstance(m, dict) or m.get('date') != date_str:
        m = {'date': date_str, 'started_at': None, 'finished_at': None,
             'running': False, 'fatal': None, 'symbols': {}}
    return m


def _totals(manifest):
    syms = manifest.get('symbols', {})
    ok = [s for s in syms.values() if s.get('status') == 'ok']
    err = [s for s in syms.values() if s.get('status') == 'error']
    return {'ok': len(ok), 'error': len(err),
            'contracts': sum(s.get('contracts', 0) for s in ok),
            'bytes': sum(s.get('bytes', 0) for s in ok)}


def read_manifest(db, date_str):
    with db.tx() as c:
        m = _manifest(db, c, date_str)
    m['totals'] = _totals(m)
    m['complete'] = bool(m.get('finished_at')) and not m.get('running')
    return m


async def _snapshot_symbol(db, fetch, symbol, date_str, expiry_start,
                           expiry_end, now):
    """Fetch + shape + store one symbol. Returns the manifest entry.
    Never raises: a failure is an entry with status 'error'."""
    try:
        with db.tx() as c:
            close, close_ts = underlying_close(db, c, symbol, now)
        if close is None:
            return {'status': 'error', 'error': 'no_underlying_close',
                    'at': time.time()}
        contracts, complete, source = await fetch(symbol, expiry_start,
                                                  expiry_end)
        rows = filter_contracts(contracts, close, date_str)
        payload = {'date': date_str, 'symbol': symbol, 'source': source,
                   'complete': complete, 'asof': now,
                   'underlying_close': close, 'underlying_close_ts': close_ts,
                   'dte_max': DTE_MAX, 'band': BAND, 'contracts': rows}
        nbytes = len(json.dumps(payload).encode())
        with db.tx() as c:
            inserted = db.append(c, KIND, source, symbol, now, payload,
                                 key=event_key(date_str, symbol))
        return {'status': 'ok', 'source': source, 'complete': complete,
                'contracts': len(rows), 'fetched': len(contracts),
                'bytes': nbytes, 'inserted': bool(inserted), 'at': time.time()}
    except Exception as e:
        LOG.warning('chain_eod %s %s failed: %s', date_str, symbol,
                    type(e).__name__)
        return {'status': 'error', 'error': type(e).__name__,
                'detail': str(e)[:200], 'at': time.time()}


async def run_snapshot(db, cfg, now=None, date_str=None, symbols=None,
                       fetch=None, collectors=None):
    """Run one day's snapshot. Idempotent per date: symbols already 'ok' in
    the manifest (or already present as events) are not re-stored; failed
    symbols are retried. Returns the manifest with totals."""
    now = time.time() if now is None else now
    date_str = date_str or day(now)
    expiry_start = date_str
    expiry_end = day(now + DTE_MAX * 86400)
    symbols = list(cfg.watch_symbols) if symbols is None else list(symbols)
    if fetch is None and not providers_configured(cfg):
        # Deployment-context guard (2026-10-26 repair): the service
        # serving the trigger holds no provider credentials, so every
        # fetch would fail with a provider 401. Record ONE day-level
        # fatal instead of a per-symbol error per watch symbol, and
        # leave the day retryable — start_snapshot_background does not
        # treat a fatal day as already complete.
        with db.tx() as c:
            m = _manifest(db, c, date_str)
            m['running'] = False
            m['started_at'] = m.get('started_at') or now
            m['finished_at'] = time.time()
            m['fatal'] = NO_PROVIDER_CREDENTIALS
            db.put(c, manifest_key(date_str), m)
        m['totals'] = _totals(m)
        m['complete'] = True
        db.health(KIND, 'error',
                  f"EOD chain snapshot {date_str}: no provider credentials "
                  "configured on this service (MASSIVE_API_KEY / "
                  "APCA_API_KEY_ID+APCA_API_SECRET_KEY missing); "
                  "nothing captured",
                  poll_ts=time.time(), date=date_str, **m['totals'])
        return m
    own_collectors = None
    if fetch is None:
        from .providers import Collectors
        own_collectors = collectors or Collectors(db, cfg)
        coll = own_collectors

        async def fetch(symbol, es, ee):
            return await fetch_chain(coll, symbol, es, ee)
    try:
        with db.tx() as c:
            m = _manifest(db, c, date_str)
            m['running'] = True
            m['fatal'] = None
            m['started_at'] = m.get('started_at') or now
            m['finished_at'] = None
            db.put(c, manifest_key(date_str), m)
        todo = [s for s in symbols
                if m['symbols'].get(s, {}).get('status') != 'ok']
        sem = asyncio.Semaphore(CONCURRENCY)
        lock = asyncio.Lock()

        async def one(symbol):
            async with sem:
                entry = await _snapshot_symbol(db, fetch, symbol, date_str,
                                               expiry_start, expiry_end, now)
            async with lock:
                with db.tx() as c:
                    m2 = _manifest(db, c, date_str)
                    m2['symbols'][symbol] = entry
                    db.put(c, manifest_key(date_str), m2)
        await asyncio.gather(*(one(s) for s in todo))
        with db.tx() as c:
            m = _manifest(db, c, date_str)
            m['running'] = False
            m['finished_at'] = time.time()
            db.put(c, manifest_key(date_str), m)
        m['totals'] = _totals(m)
        m['complete'] = True
        t = m['totals']
        health_status = ('available' if t['error'] == 0 else
                         'error' if t['ok'] == 0 else 'partial')
        db.health(KIND, health_status,
                  f"EOD chain snapshot {date_str}: {t['ok']} ok, "
                  f"{t['error']} failed, {t['contracts']} contracts, "
                  f"{t['bytes']} bytes",
                  poll_ts=time.time(), date=date_str, **t)
        return m
    finally:
        if own_collectors is not None:
            try:
                await own_collectors.client.aclose()
            except Exception:
                pass


def start_snapshot_background(db, cfg, date_str=None, now=None, fetch=None,
                              collectors=None):
    """Start run_snapshot on a daemon thread unless the day is already
    complete or a run started recently is still marked running. A day is
    'already complete' only when it finished with every symbol ok — a
    finished day with errors (or a fatal) falls through and retries the
    missing symbols; that is what makes a same-day re-trigger a recovery
    path instead of a no-op. Returns a status dict for the endpoint."""
    import threading
    import uuid
    now = time.time() if now is None else now
    date_str = date_str or day(now)
    with db.tx() as c:
        m = _manifest(db, c, date_str)
        if m.get('finished_at') and not m.get('running'):
            t = _totals(m)
            if not m.get('fatal') and t['error'] == 0 and t['ok'] > 0:
                return {'ok': True, 'already_complete': True,
                        'date': date_str, 'totals': t}
        if m.get('running') and now - (m.get('started_at') or 0) < 7200:
            return {'ok': True, 'already_running': True, 'date': date_str}
        # A stale 'running' marker (process died mid-run) falls through:
        # lease below is the real mutual exclusion.
        owner = 'chain_eod:' + uuid.uuid4().hex
        if not db.lease(c, 'chain_eod:run', owner, seconds=7200):
            return {'ok': True, 'already_running': True, 'date': date_str}

    def _run():
        try:
            asyncio.run(run_snapshot(db, cfg, now=now, date_str=date_str,
                                     fetch=fetch, collectors=collectors))
        except Exception:
            LOG.exception('chain_eod snapshot run failed for %s', date_str)
            try:
                with db.tx() as c:
                    m = _manifest(db, c, date_str)
                    m['running'] = False
                    db.put(c, manifest_key(date_str), m)
            except Exception:
                pass
        finally:
            # Free the name now; without this a same-day retry waits out
            # the full lease TTL (2026-10-26 repair).
            try:
                with db.tx() as c:
                    db.release_lease(c, 'chain_eod:run', owner)
            except Exception:
                pass

    threading.Thread(target=_run, name='chain-eod-snapshot',
                     daemon=True).start()
    return {'ok': True, 'started': True, 'date': date_str}
