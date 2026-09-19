"""Exercise subscriptions against a gateway with a shortened weekend window."""
import asyncio
from datetime import datetime, timezone
import sys
from types import SimpleNamespace

import pytest

from compass.config import Config
from compass.extra_futures import collect_group
from compass.providers import Collectors
from compass.store import Store


NOW = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc).timestamp()
FLOOR = datetime(2026, 9, 19, 7, 15, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize('exchange,root', [('CME', 'MES'), ('COMEX', 'MGC'),
                                         ('NYMEX', 'MCL'), ('CBOT', 'YM')])
@pytest.mark.parametrize('has_replay', [False, True])
def test_shortened_gateway_window_does_not_block_live_or_reconnect(
        tmp_path, monkeypatch, exchange, root, has_replay):
    db = Store('sqlite:///'+str(tmp_path/'replay.db'))
    db.initialize()
    raw, symbol = root+'Z6', root+'Z6@123'
    sessions = []

    class Error:
        err = 'Invalid start time. Must be 2026-09-19T07:15:00Z or later, or 0'

    class Bar:
        instrument_id = 123
        ts_event = int((NOW-60)*1e9)
        open = high = low = close = 100_000_000_000
        volume = 10

    class Mapping: pass
    class Quote: pass
    class BentoError(Exception): pass

    class Live:
        symbology_map = {123: raw}

        def __init__(self, **kwargs):
            self.subscriptions = []
            sessions.append(self)

        def subscribe(self, **kwargs): self.subscriptions.append(kwargs)
        def add_callback(self, callback, exception_callback):
            self.callback, self.on_error = callback, exception_callback
        def stop(self): pass
        def terminate(self): pass
        def block_for_close(self): pass

        def start(self):
            for subscription in self.subscriptions:
                if 'start' not in subscription: continue
                start = subscription['start']
                if start != 0 and datetime.fromisoformat(start).timestamp() < FLOOR:
                    try: self.callback(Error())
                    except Exception as error: self.on_error(error)
                    return
            if has_replay: self.callback(Bar())

    sdk = SimpleNamespace(Live=Live, ErrorMsg=Error, OHLCVMsg=Bar,
                          SymbolMappingMsg=Mapping, MBP1Msg=Quote, BentoError=BentoError)
    monkeypatch.setitem(sys.modules, 'databento', sdk)
    # Freeze both clocks: the previous implementation used datetime.now().
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return datetime.fromtimestamp(NOW, tz)
    monkeypatch.setattr('compass.providers.datetime', Clock)
    monkeypatch.setattr('compass.extra_futures.datetime', Clock, raising=False)
    monkeypatch.setattr('compass.providers.time.time', lambda: NOW)
    sleeps = []

    async def end_after_two(delay):
        sleeps.append(delay)
        if len(sleeps) == 2: raise asyncio.CancelledError

    monkeypatch.setattr('compass.extra_futures.asyncio.sleep', end_after_two)

    async def run():
        collector = Collectors(db, Config(local=True, databento='test-only', futures=('MES.c.0',)))
        try:
            if exchange == 'CME':
                await collector.futures()
                await collector.futures()
            else:
                with pytest.raises(asyncio.CancelledError):
                    await collect_group(collector, exchange, [root+'.v.0'])
            with db.tx() as c:
                health = db.get(c, 'health:databento_'+('futures' if exchange == 'CME' else exchange.lower()))
                assert health['status'] != 'error', health
                bars = db.recent(c, 'bar', symbol, limit=10)
                assert len(bars) == (1 if has_replay else 0)
                if bars: assert bars[0]['ts'] == NOW-60
                # Replay bars cannot manufacture executable quote evidence.
                assert db.get(c, 'quote:'+symbol) is None
            assert len(sessions) == 2
            for live in sessions:
                bars, quotes = live.subscriptions
                assert bars['schema'] == 'ohlcv-1m' and bars['start'] == 0
                assert quotes['schema'] == 'mbp-1' and 'start' not in quotes
        finally:
            await collector.close()

    try: asyncio.run(run())
    finally: db.engine.dispose()
