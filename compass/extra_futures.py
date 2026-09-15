"""Optional metals, energy and Dow live streams, isolated from the ES/NQ feed.

Subscriptions themselves verify exchange/schema entitlement. A rejected group
cannot stop ES/MES/NQ/MNQ. No historical API download or new plan purchase.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from .instruments import FUTURES, DATED
from .futures import risk_day, lead_contract
from .diagnostics import redacted_detail
from .futures_replay import ReplayBars
from .futures_ingress import IngressProbe
from .futures_writer import FuturesWriter


def subscription_plan(db,c,aliases,now):
    """Dow uses the same customary lead quarter as ES/NQ, in its own stream."""
    if not all(a.split('.')[0] in ('YM','MYM') for a in aliases):
        return 'continuous',list(aliases),{}
    targets={lead_contract(a.split('.')[0],risk_day(now))['raw_symbol']:a for a in aliases}
    requested=set(targets)
    from sqlalchemy import select
    from .setup_study import trials
    held=[p.get('symbol') for p in db.prefix(c,'position:').values()
          if p.get('status')=='open' and p.get('asset')=='future']
    held+=list(c.execute(select(trials.c.symbol).where(trials.c.status=='open')).scalars())
    roots={a.split('.')[0] for a in aliases}
    for symbol in held:
        match=DATED.fullmatch(symbol or '')
        if match and match.group(1) in roots:
            requested.add(symbol.split('@')[0])
    return 'raw_symbol',sorted(requested),targets


def mapping_window(start_ns, end_ns, observed_at, now, quote_ts=None):
    undefined = 18446744073709551615
    start = None if start_ns == undefined else start_ns/1e9
    end = None if end_ns == undefined else end_ns/1e9
    unspecified = start is None or end is None
    if unspecified:
        # Missing provider bounds require a short lease renewed by live quotes.
        if quote_ts is None or not 0 <= now-quote_ts <= 5:
            return None
        start = observed_at if start is None else start
        end = min(end, quote_ts+30) if end is not None else quote_ts+30
    if not start <= now < end:
        return None
    return start, end, unspecified


async def collect_group(collector, exchange, aliases):
    import databento as sdk
    name = 'databento_'+exchange.lower()
    db, cfg = collector.db, collector.cfg
    while True:
        client = None
        writer = None
        errors = []
        try:
            client = sdk.Live(key=cfg.databento, heartbeat_interval_s=15, reconnect_policy='none', slow_reader_behavior='warn')
            collector.extra_live[exchange] = client
            last = {}
            mappings = {}
            with db.tx() as c:
                stype,requested,targets=subscription_plan(db,c,aliases,time.time())
            writer = FuturesWriter(collector,exchange)
            replay = ReplayBars(writer)
            probe = IngressProbe(exchange)
            db.health(name, 'connecting', 'Checking live access: '+', '.join(requested))
            logging.getLogger('uvicorn.error').info('Optional futures subscription: exchange=%s stype=%s symbols=%s',exchange,stype,requested)

            def activate(root, now, quote_ts=None):
                mapping = mappings[root]
                window = mapping_window(mapping['start_ns'], mapping['end_ns'], mapping['observed_at'], now, quote_ts)
                if window is None:
                    return False
                start, end, unspecified = window
                raw, iid, alias = mapping['raw'], mapping['iid'], mapping['alias']
                with db.tx() as c:
                    db.put(c, 'contract:'+root, {'root':root, 'raw_symbol':raw,
                        'configured_symbol':alias, 'instrument_id':iid,
                        'resolved_symbol':f'{raw}@{iid}', 'trading_day':risk_day(now),
                        'mapping_start':start, 'mapping_end':end, 'provider_interval_unspecified':unspecified,
                        'mapping_source':('CME customary lead quarter; Databento explicit mapping' if targets else
                            'databento live volume mapping')+('; fresh-quote lease' if unspecified else ''), 'at':now})
                return True

            def callback(r):
                replay.flush_due()
                if targets and time.monotonic()-last.get('roll_check',0)>60:
                    last['roll_check']=time.monotonic()
                    if any(lead_contract(alias.split('.')[0],risk_day(time.time()))['raw_symbol']!=raw
                           for raw,alias in targets.items()):
                        client.terminate()
                        return
                if isinstance(r, sdk.ErrorMsg):
                    errors.append(redacted_detail(r.err, (cfg.databento,), 200))
                    client.terminate()
                    return
                if isinstance(r, sdk.SymbolMappingMsg):
                    source_alias = str(r.stype_in_symbol)
                    raw = source_alias if targets else str(r.stype_out_symbol)
                    alias = targets.get(source_alias) if targets else source_alias
                    logging.getLogger('uvicorn.error').info(
                        'Optional futures mapping: %s -> %s id=%s start_ns=%s end_ns=%s',
                        alias, raw, r.instrument_id, r.start_ts, r.end_ts)
                    if alias not in aliases or not DATED.fullmatch(raw): return
                    root = alias.split('.')[0]
                    if DATED.fullmatch(raw).group(1) != root: return
                    now = time.time()
                    mappings[root] = {'alias':alias, 'raw':raw, 'iid':r.instrument_id,
                        'start_ns':r.start_ts, 'end_ns':r.end_ts, 'observed_at':now}
                    activate(root, now)
                    with db.tx() as c:
                        db.append(c, 'mapping', 'databento', raw, now,
                            {'instrument_id':r.instrument_id, 'input':alias, 'output':raw})
                    return
                iid = getattr(r, 'instrument_id', None)
                raw = client.symbology_map.get(iid)
                match = DATED.fullmatch(str(raw)) if raw else None
                if not match or match.group(1)+'.v.0' not in aliases: return
                if targets and str(raw) not in requested: return
                root, symbol = match.group(1), f'{raw}@{iid}'
                stamp = r.ts_event/1e9
                if isinstance(r, sdk.MBP1Msg):
                    probe.quote(symbol,r)
                if isinstance(r, sdk.OHLCVMsg):
                    replay.add((symbol,stamp,{'o':r.open/1e9,'h':r.high/1e9,
                        'l':r.low/1e9,'c':r.close/1e9,'v':r.volume,'instrument_id':iid,'alias':str(raw)}))
                elif isinstance(r, sdk.MBP1Msg) and time.monotonic()-last.get(symbol,0)>.25:
                    level = r.levels[0]
                    if level.bid_px<9e18 and level.ask_px<9e18:
                        probe.writing(symbol,lambda: writer.quote('databento',symbol,{'ts':stamp,'bid':level.bid_px/1e9,'ask':level.ask_px/1e9,
                            'bid_size':level.bid_sz,'ask_size':level.ask_sz},iid))
                        if mappings.get(root,{}).get('iid')==iid and time.monotonic()-last.get('health:'+root,0)>5:
                            def record_health(root=root,symbol=symbol,stamp=stamp):
                                if activate(root,time.time(),stamp):
                                    db.health(name+'_'+root,'receiving','Live access confirmed: '+symbol,stamp,monotonic_source=True)
                            writer.metadata(record_health)
                            last['health:'+root] = time.monotonic()
                    last[symbol] = time.monotonic()
                if time.monotonic()-last.get('health',0)>5:
                    writer.metadata(lambda stamp=stamp:db.health(name,'receiving','Independent optional futures stream; check each contract quote age',stamp,monotonic_source=True))
                    last['health'] = time.monotonic()

            def on_error(error):
                errors.append(type(error).__name__)
                client.terminate()

            client.add_callback(probe.wrap(callback), exception_callback=on_error)

            def run():
                try:
                    client.subscribe(dataset='GLBX.MDP3',schema='ohlcv-1m',stype_in=stype,symbols=requested,
                        start=(datetime.now(timezone.utc)-timedelta(hours=23)).isoformat())
                    client.subscribe(dataset='GLBX.MDP3',schema='mbp-1',stype_in=stype,symbols=requested)
                    client.start()
                    client.block_for_close()
                finally:
                    client.stop()
                    try: replay.flush()
                    finally: writer.close()

            await asyncio.to_thread(run)
            if errors:
                raise RuntimeError('; '.join(errors))
            db.health(name, 'waiting', 'Optional live stream closed; reconnect scheduled')
        except asyncio.CancelledError:
            if client: client.terminate()
            raise
        except Exception as error:
            db.health(name, 'error', redacted_detail(error,(cfg.databento,),220)+'; index stream unaffected; retry in 120s')
        finally:
            if client: client.stop()
            if writer and not writer.closed:
                await asyncio.to_thread(writer.close)
            collector.extra_live.pop(exchange, None)
        await asyncio.sleep(120)


def tasks(collector):
    groups = {}
    if collector.cfg.databento:
        for alias in collector.cfg.extra_futures:
            groups.setdefault(FUTURES[alias.split('.')[0]][4], []).append(alias)
    return [collect_group(collector, exchange, aliases) for exchange, aliases in groups.items()]
