"""Coalesce concurrent read-only dashboard refreshes without rewriting clocks."""
from threading import Lock
from time import monotonic


class SnapshotCache:
    def __init__(self, ttl=2, clock=monotonic):
        self.ttl, self.clock = ttl, clock
        self.lock = Lock()
        self.value = None
        self.expires = 0

    def get(self, build):
        # Only one request may build the large snapshot. Followers share that
        # completed result briefly rather than occupying more DB connections.
        # Failed builds are not cached; expired evidence is never returned as
        # a successful refresh after a failure.
        with self.lock:
            if self.value is not None and self.clock() < self.expires:
                return self.value
            value = build()
            self.value, self.expires = value, self.clock() + self.ttl
            return value
