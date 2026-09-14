"""Batch replayed bars so database writes do not hold up live stream callbacks."""
import time


class ReplayBars:
    def __init__(self, collector):
        self.collector = collector
        self.pending = []
        self.last_flush = time.monotonic()

    def add(self, row):
        self.pending.append(row)
        if len(self.pending) >= 500 or row[1] >= time.time()-120:
            self.flush()

    def flush_due(self):
        if time.monotonic()-self.last_flush >= 2:
            self.flush()

    def flush(self):
        if self.pending:
            self.collector.bars('databento', self.pending)
            self.pending = []
        self.last_flush = time.monotonic()
