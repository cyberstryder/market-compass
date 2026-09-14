"""Bounded stream timing diagnostics; never create or refresh a quote."""
from collections import Counter
import json
import logging
import time


class IngressProbe:
    def __init__(self, stream, clock=time.time, monotonic=time.monotonic, emit=None):
        self.stream, self.clock, self.monotonic = stream, clock, monotonic
        self.emit = emit or (lambda row: logging.getLogger('uvicorn.error').info(
            'Futures ingress: %s', json.dumps(row, sort_keys=True)))
        self.started = clock()
        self.last_emit = monotonic()
        self.previous_callback = None
        self.symbols = {}
        self.types = Counter()
        self.max_callback_seconds = 0
        self.max_callback_gap = 0

    def quote(self, symbol, record):
        """Called for every mapped MBP1 before the sampling throttle."""
        if symbol not in self.symbols and len(self.symbols) >= 32:
            return
        row = self.symbols.setdefault(symbol, dict(records=0, valid_bbo=0, invalid_bbo=0,
            selected=0, write_calls_completed=0, max_write_seconds=0,
            max_source_gap=0, max_arrival_gap=0, max_event_age=0, max_sdk_receive_age=0))
        now = self.clock()
        stamp = record.ts_event/1e9
        if row.get('last_source_ts') is not None:
            row['max_source_gap'] = max(row['max_source_gap'], stamp-row['last_source_ts'])
            row['max_arrival_gap'] = max(row['max_arrival_gap'], now-row['last_callback_at'])
        level = record.levels[0]
        valid = 0 < level.bid_px < level.ask_px < 9e18 and level.bid_sz > 0 and level.ask_sz > 0
        row['records'] += 1
        row['valid_bbo' if valid else 'invalid_bbo'] += 1
        row.update(last_source_ts=stamp,last_callback_at=now)
        row['max_event_age'] = max(row['max_event_age'],now-stamp)
        received = getattr(record,'ts_recv',None)
        if received is not None and 0 < received < 9e18:
            row['last_sdk_receive_ts'] = received/1e9
            row['max_sdk_receive_age'] = max(row['max_sdk_receive_age'],now-received/1e9)

    def writing(self, symbol, fn):
        row = self.symbols.get(symbol)
        if row is not None: row['selected'] += 1
        start = self.monotonic()
        try:
            result = fn()
            if row is not None: row['write_calls_completed'] += 1
            return result
        finally:
            if row is not None:
                row['max_write_seconds'] = max(row['max_write_seconds'],self.monotonic()-start)

    def wrap(self, callback):
        def measured(record):
            start = self.monotonic()
            if self.previous_callback is not None:
                self.max_callback_gap = max(self.max_callback_gap,start-self.previous_callback)
            self.previous_callback = start
            self.types[type(record).__name__] += 1
            try:
                return callback(record)
            finally:
                self.max_callback_seconds = max(self.max_callback_seconds,self.monotonic()-start)
                if self.monotonic()-self.last_emit >= 30:
                    self.last_emit = self.monotonic()
                    self.emit(dict(stream=self.stream,started=self.started,at=self.clock(),
                        callback_types=dict(self.types),max_callback_seconds=self.max_callback_seconds,
                        max_callback_gap=self.max_callback_gap,symbols=self.symbols,
                        basis='Cumulative per connection. SDK receive clock is not local socket arrival. Write calls are not proof of committed inserts. No quotes synthesized.'))
        return measured
