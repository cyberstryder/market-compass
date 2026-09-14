"""Optional metals/energy live streams, isolated from the equity-index feed.

Subscriptions themselves verify exchange/schema entitlement. A rejected group
cannot stop ES/MES/NQ/MNQ. No historical API download or new plan purchase.
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from .instruments import FUTURES, DATED
from .futures import risk_day
from .diagnostics import redacted_detail


async def collect_group(collector, exchange, aliases):
    import databento as sdk
    name = 'databento_'+exchange.lower()
    db, cfg = collector.db, collector.cfg
    while True:
        client = None
        errors = []
        try:
            client = sdk.Live(key=cfg.databento, heartbeat_interval_s=15, reconnect_policy='none', slow_reader_behavior='warn')
            collector.extra_live[exchange] = client
            last = {}
            active = {}
            db.health(name, 'connecting', 'Checking live access: '+', '.join(aliases))

            def callback(r):
                if isinstance(r, sdk.ErrorMsg):
                    errors.append(redacted_detail(r.err, (cfg.databento,), 200))
                    client.terminate()
                    return
                if isinstance(r, sdk.SymbolMappingMsg):
                    alias, raw = str(r.stype_in_symbol), str(r.stype_out_symbol)
                    if alias not in aliases or not DATED.fullmatch(raw): return
                    root = alias.split('.')[0]
                    if DATED.fullmatch(raw).group(1) != root: return
                    start, end = r.start_ts/1e9, r.end_ts/1e9
                    now = time.time()
                    if not start <= now < end: return
                    active[root] = r.instrument_id
                    with db.tx() as c:
                        db.put(c, 'contract:'+root, {'root':root, 'raw_symbol':raw,
                            'configured_symbol':alias, 'instrument_id':r.instrument_id,
                            'resolved_symbol':f'{raw}@{r.instrument_id}', 'trading_day':risk_day(now),
                            'mapping_start':start, 'mapping_end':end, 'mapping_source':'databento live volume mapping', 'at':now})
                        db.append(c, 'mapping', 'databento', raw, now,
                            {'instrument_id':r.instrument_id, 'input':alias, 'output':raw})
                    return
                iid = getattr(r, 'instrument_id', None)
                raw = client.symbology_map.get(iid)
                match = DATED.fullmatch(str(raw)) if raw else None
                if not match or match.group(1)+'.v.0' not in aliases: return
                root, symbol = match.group(1), f'{raw}@{iid}'
                stamp = r.ts_event/1e9
                if isinstance(r, sdk.OHLCVMsg):
                    collector.bars('databento', [(symbol,stamp,{'o':r.open/1e9,'h':r.high/1e9,
                        'l':r.low/1e9,'c':r.close/1e9,'v':r.volume,'instrument_id':iid,'alias':str(raw)})])
                elif isinstance(r, sdk.MBP1Msg) and time.monotonic()-last.get(symbol,0)>.25:
                    level = r.levels[0]
                    if level.bid_px<9e18 and level.ask_px<9e18:
                        collector.quote('databento',symbol,{'ts':stamp,'bid':level.bid_px/1e9,'ask':level.ask_px/1e9,
                            'bid_size':level.bid_sz,'ask_size':level.ask_sz},iid)
                        if active.get(root)==iid and time.monotonic()-last.get('health:'+root,0)>5:
                            db.health(name+'_'+root, 'receiving', 'Live access confirmed: '+symbol, stamp, monotonic_source=True)
                            last['health:'+root] = time.monotonic()
                    last[symbol] = time.monotonic()
                if time.monotonic()-last.get('health',0)>5:
                    db.health(name, 'receiving', 'Independent optional futures stream; check each contract quote age', stamp, monotonic_source=True)
                    last['health'] = time.monotonic()

            def on_error(error):
                errors.append(type(error).__name__)
                client.terminate()

            client.add_callback(callback, exception_callback=on_error)

            def run():
                try:
                    client.subscribe(dataset='GLBX.MDP3',schema='ohlcv-1m',stype_in='continuous',symbols=aliases,
                        start=(datetime.now(timezone.utc)-timedelta(hours=23)).isoformat())
                    client.subscribe(dataset='GLBX.MDP3',schema='mbp-1',stype_in='continuous',symbols=aliases)
                    client.start()
                    client.block_for_close()
                finally:
                    client.stop()

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
            collector.extra_live.pop(exchange, None)
        await asyncio.sleep(120)


def tasks(collector):
    groups = {}
    if collector.cfg.databento:
        for alias in collector.cfg.extra_futures:
            groups.setdefault(FUTURES[alias.split('.')[0]][4], []).append(alias)
    return [collect_group(collector, exchange, aliases) for exchange, aliases in groups.items()]
