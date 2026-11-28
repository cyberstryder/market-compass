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


def manifest_key(date_str, kind=KIND):
    return kind + ':' + date_str


def event_key(date_str, symbol, kind=KIND):
    return f'{kind}:{date_str}:{symbol}'


def shape_contract(o):
    """Project a provider contract dict (providers.py chain shape) onto the
    archive schema, quote included, greeks + IV retained."""
    out = {k: o.get(k) for k in CONTRACT_FIELDS}
    q = o.get('quote') or {}
    out['quote'] = {'ts': q.get('ts'), 'bid': q.get('bid'), 'ask': q.get('ask'),
                    'bid_size': q.get('bid_size', 0), 'ask_size': q.get('ask_size', 0)}
    return out


def filter_contracts(contracts, close, date_str, dte_max=DTE_MAX, band=BAND,
                     keep=frozenset()):
    """Keep contracts expiring in [date_str, date_str + dte_max] with strikes
    within +/-band of the underlying close. Returns archive-shaped rows.

    ``keep`` is an exemption set of OCC symbols (the Weeklies board's
    selected contracts for this symbol — see board_contracts): a contract
    whose symbol is in ``keep`` is retained even when its strike or expiry
    falls outside the band/DTE window, because the board's own scorer
    prices exactly those contracts from this archive. The sanity checks
    (symbol present, strike positive) still apply to exempt contracts,
    and every non-exempt contract is filtered exactly as before."""
    end = (_date.fromisoformat(date_str)
           + timedelta(days=dte_max)).isoformat()
    lo, hi = close * (1 - band), close * (1 + band)
    out = []
    for o in contracts:
        expiry = o.get('expiry') or ''
        strike = number(o.get('strike'))
        if not o.get('symbol') or strike is None or strike <= 0:
            continue
        if o['symbol'] in keep:
            out.append(shape_contract(o))
            continue
        if not (date_str <= expiry <= end):
            continue
        if not (lo <= strike <= hi):
            continue
        out.append(shape_contract(o))
    out.sort(key=lambda r: (r['expiry'], r['strike'], r['type'] or ''))
    return out


def board_contracts(c, date_str):
    """The Weeklies board contracts for the week containing ``date_str``,
    grouped by underlying: {ticker: {OCC symbol, ...}}. Read from the
    native smoothers weekly table — the same rows the
    /api/admin/smoothers-board endpoint serves (native_reports.smoothers
    reads this table), so the snapshot keeps exactly the contracts the
    board selected. Returns {} when the week's board does not exist yet.
    The import is lazy: native_smoothers imports this module."""
    from sqlalchemy import select
    from .native_smoothers import weekly
    d = _date.fromisoformat(date_str)
    week = (d - timedelta(days=d.weekday())).isoformat()
    out = {}
    rows = c.execute(select(weekly.c.payload)
                     .where(weekly.c.week == week)).scalars()
    for p in rows:
        if not isinstance(p, dict):
            continue
        occ = (p.get('contract') or {}).get('symbol')
        ticker = p.get('ticker')
        if occ and ticker:
            out.setdefault(ticker, set()).add(occ)
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
    """Primary per OPTION_CHAIN_SOURCE (default Massive), the other
    vendor as fallback — mirrors the chains() policy via
    providers.chain_primary_source. Returns (contracts, complete,
    source). Raises only if both fail."""
    from .providers import chain_primary_source
    if not providers_configured(collectors.cfg):
        raise ChainConfigError(
            'no provider credentials configured on this service '
            '(MASSIVE_API_KEY and APCA_API_KEY_ID/APCA_API_SECRET_KEY '
            'are all unset)')
    if chain_primary_source(collectors.cfg) == 'massive':
        try:
            contracts, complete = await collectors.massive_chain(
                symbol, expiry_start=expiry_start, expiry_end=expiry_end,
                full_greeks=True)
            return contracts, complete, 'massive'
        except Exception:
            contracts, complete = await collectors.alpaca_chain(
                symbol, expiry_start=expiry_start, expiry_end=expiry_end)
            return contracts, complete, 'alpaca'
    try:
        contracts, complete = await collectors.alpaca_chain(
            symbol, expiry_start=expiry_start, expiry_end=expiry_end)
        return contracts, complete, 'alpaca'
    except Exception:
        if not getattr(collectors.cfg, 'massive', ''):
            raise
        contracts, complete = await collectors.massive_chain(
            symbol, expiry_start=expiry_start, expiry_end=expiry_end,
            full_greeks=True)
        return contracts, complete, 'massive'


def _manifest(db, c, date_str, kind=KIND):
    m = db.get(c, manifest_key(date_str, kind))
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


def read_manifest(db, date_str, kind=KIND):
    with db.tx() as c:
        m = _manifest(db, c, date_str, kind)
    m['totals'] = _totals(m)
    m['complete'] = bool(m.get('finished_at')) and not m.get('running')
    return m


async def _snapshot_symbol(db, fetch, symbol, date_str, expiry_start,
                           expiry_end, now, kind=KIND, keep=frozenset()):
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
        rows = filter_contracts(contracts, close, date_str, keep=keep)
        kept = sum(1 for r in rows if r['symbol'] in keep)
        payload = {'date': date_str, 'symbol': symbol, 'source': source,
                   'complete': complete, 'asof': now,
                   'snapshot_kind': kind,
                   'underlying_close': close, 'underlying_close_ts': close_ts,
                   'dte_max': DTE_MAX, 'band': BAND, 'contracts': rows}
        nbytes = len(json.dumps(payload).encode())
        with db.tx() as c:
            inserted = db.append(c, kind, source, symbol, now, payload,
                                 key=event_key(date_str, symbol, kind))
        return {'status': 'ok', 'source': source, 'complete': complete,
                'contracts': len(rows), 'fetched': len(contracts),
                'board_kept': kept,
                'bytes': nbytes, 'inserted': bool(inserted), 'at': time.time()}
    except Exception as e:
        LOG.warning('chain_eod %s %s failed: %s', date_str, symbol,
                    type(e).__name__)
        return {'status': 'error', 'error': type(e).__name__,
                'detail': str(e)[:200], 'at': time.time()}


RETRY_DELAY_SECONDS = 5.0


async def run_snapshot(db, cfg, now=None, date_str=None, symbols=None,
                       fetch=None, collectors=None, kind=KIND,
                       retry_delay=RETRY_DELAY_SECONDS):
    """Run one day's snapshot. Idempotent per date: symbols already 'ok' in
    the manifest (or already present as events) are not re-stored; failed
    symbols are retried on a later re-trigger. Within a run, symbols whose
    attempt ends in error get ONE retry pass after ``retry_delay`` seconds
    (2026-11-27: the per-symbol FeedError set rotates run to run, and a
    same-run retry recovers most of it instead of leaving the day's
    archive with holes until someone re-triggers). Returns the manifest
    with totals."""
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
            m = _manifest(db, c, date_str, kind)
            m['running'] = False
            m['started_at'] = m.get('started_at') or now
            m['finished_at'] = time.time()
            m['fatal'] = NO_PROVIDER_CREDENTIALS
            db.put(c, manifest_key(date_str, kind), m)
        m['totals'] = _totals(m)
        m['complete'] = True
        db.health(kind, 'error',
                  f"Chain snapshot {date_str} ({kind}): no provider "
                  "credentials configured on this service (MASSIVE_API_KEY "
                  "/ APCA_API_KEY_ID+APCA_API_SECRET_KEY missing); "
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
            m = _manifest(db, c, date_str, kind)
            m['running'] = True
            m['fatal'] = None
            m['started_at'] = m.get('started_at') or now
            m['finished_at'] = None
            db.put(c, manifest_key(date_str, kind), m)
        todo = [s for s in symbols
                if m['symbols'].get(s, {}).get('status') != 'ok']
        # Weeklies board force-keep (2026-11-27): the board's selected
        # contracts for this symbol are retained in the snapshot even
        # outside the band/DTE filter, so the board scorer never loses a
        # pick to a >10% mid-week move. EOD archive only — chain_open's
        # morning snapshot keeps the plain filter. A board read failure
        # degrades to no exemptions; it never fails the snapshot.
        keep_by_symbol = {}
        if kind == KIND:
            try:
                with db.tx() as c:
                    keep_by_symbol = board_contracts(c, date_str)
            except Exception:
                LOG.warning('chain_eod %s: board contract lookup failed; '
                            'snapshotting without exemptions', date_str)
        sem = asyncio.Semaphore(CONCURRENCY)
        lock = asyncio.Lock()

        async def one(symbol, retried=False):
            async with sem:
                entry = await _snapshot_symbol(
                    db, fetch, symbol, date_str, expiry_start, expiry_end,
                    now, kind=kind,
                    keep=frozenset(keep_by_symbol.get(symbol, ())))
            if retried:
                entry = {**entry, 'retried': True}
            async with lock:
                with db.tx() as c:
                    m2 = _manifest(db, c, date_str, kind)
                    m2['symbols'][symbol] = entry
                    db.put(c, manifest_key(date_str, kind), m2)
            return entry

        entries = await asyncio.gather(*(one(s) for s in todo))
        # One retry pass for this run's errored symbols (see docstring):
        # exactly the symbols attempted above whose entry is an error,
        # exactly once; ok symbols are never re-attempted.
        errored = [s for s, e in zip(todo, entries)
                   if e.get('status') == 'error']
        if errored:
            if retry_delay:
                await asyncio.sleep(retry_delay)
            await asyncio.gather(*(one(s, retried=True) for s in errored))
        with db.tx() as c:
            m = _manifest(db, c, date_str, kind)
            m['running'] = False
            m['finished_at'] = time.time()
            db.put(c, manifest_key(date_str, kind), m)
        m['totals'] = _totals(m)
        m['complete'] = True
        t = m['totals']
        health_status = ('available' if t['error'] == 0 else
                         'error' if t['ok'] == 0 else 'partial')
        db.health(kind, health_status,
                  f"Chain snapshot {date_str} ({kind}): {t['ok']} ok, "
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
                              collectors=None, kind=KIND):
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
    lease_name = f'{kind}:run'
    with db.tx() as c:
        m = _manifest(db, c, date_str, kind)
        if m.get('finished_at') and not m.get('running'):
            t = _totals(m)
            if not m.get('fatal') and t['error'] == 0 and t['ok'] > 0:
                return {'ok': True, 'already_complete': True,
                        'date': date_str, 'totals': t}
        if m.get('running') and now - (m.get('started_at') or 0) < 7200:
            return {'ok': True, 'already_running': True, 'date': date_str}
        # A stale 'running' marker (process died mid-run) falls through:
        # lease below is the real mutual exclusion.
        owner = kind + ':' + uuid.uuid4().hex
        if not db.lease(c, lease_name, owner, seconds=7200):
            return {'ok': True, 'already_running': True, 'date': date_str}

    def _run():
        try:
            asyncio.run(run_snapshot(db, cfg, now=now, date_str=date_str,
                                     fetch=fetch, collectors=collectors,
                                     kind=kind))
        except Exception:
            LOG.exception('%s snapshot run failed for %s', kind, date_str)
            try:
                with db.tx() as c:
                    m = _manifest(db, c, date_str, kind)
                    m['running'] = False
                    db.put(c, manifest_key(date_str, kind), m)
            except Exception:
                pass
        finally:
            # Free the name now; without this a same-day retry waits out
            # the full lease TTL (2026-10-26 repair).
            try:
                with db.tx() as c:
                    db.release_lease(c, lease_name, owner)
            except Exception:
                pass

    threading.Thread(target=_run, name=f'{kind}-snapshot',
                     daemon=True).start()
    return {'ok': True, 'started': True, 'date': date_str}
