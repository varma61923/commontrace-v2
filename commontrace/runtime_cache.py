"""Bounded TTL/LRU values and single-flight loading, without holding locks for I/O.

Values must be immutable or copied by the caller. Weights account for retained
keys and values; active loaders and callers are outside the retention budget.
"""
from __future__ import annotations

import math
import os
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

V = TypeVar("V")


@dataclass
class _Flight(Generic[V]):
    ready: threading.Event = field(default_factory=threading.Event)
    value: V | None = None
    error: BaseException | None = None


class RuntimeCache(Generic[V]):
    """Coalesce same-key work, bound retention, and propagate failures to waiters.

    ``clear`` invalidates retention and pending publication. Existing waiters
    still receive their owner's result. Forked children start with empty state.
    No stale values are returned after expiry or a failed refresh.
    """

    def __init__(self, *, max_entries: int, max_bytes: int, ttl: float,
                 weigh: Callable[[Hashable, V], int], clock: Callable[[], float] = time.monotonic) -> None:
        if max_entries < 0 or max_bytes < 0 or not math.isfinite(ttl) or ttl < 0:
            raise ValueError("cache limits must be finite and nonnegative")
        self.max_entries, self.max_bytes, self.ttl = max_entries, max_bytes, ttl
        self._weigh, self._clock = weigh, clock
        self._reset()
        if hasattr(os, "register_at_fork"):
            # The callback must not keep a short-lived cache alive forever.
            ref = weakref.ref(self)

            def reset_child() -> None:
                cache = ref()
                if cache is not None:
                    cache._reset()

            os.register_at_fork(after_in_child=reset_child)

    def _reset(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[float, V, int]] = OrderedDict()
        self._flights: dict[Hashable, _Flight[V]] = {}
        self.bytes_used = 0
        self._next_expiry = float("inf")
        self.hits = self.misses = self.coalesced = self.evictions = 0

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._flights.clear()
            self.bytes_used = 0

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self.bytes_used -= entry[2]
            self._flights.pop(key, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "bytes": self.bytes_used,
                    "hits": self.hits, "misses": self.misses,
                    "coalesced": self.coalesced, "evictions": self.evictions}

    def peek(self, key: Hashable) -> V | None:
        """Return a retained live value without loading or waiting for a flight.

        Does not refresh expiry or affect hit/miss counters. This lets optional
        write optimizations reuse warm state without making cold writes load it.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if self._clock() >= entry[0]:
                self.bytes_used -= self._entries.pop(key)[2]
                return None
            self._entries.move_to_end(key)
            return entry[1]

    def get_or_load(self, key: Hashable, load: Callable[[], V]) -> V:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if self._clock() < entry[0]:
                    self._entries.move_to_end(key)
                    self.hits += 1
                    return entry[1]
                self.bytes_used -= self._entries.pop(key)[2]
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = _Flight[V]()
                self._flights[key] = flight
                self.misses += 1
            else:
                self.coalesced += 1
        assert flight is not None
        if not owner:
            flight.ready.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value  # type: ignore[return-value]
        try:
            value = load()
            size = max(0, self._weigh(key, value))
            with self._lock:
                if self._flights.get(key) is flight:
                    if self.max_entries and self.ttl and size <= self.max_bytes:
                        now = self._clock()
                        # Avoid a full LRU traversal on every miss while all
                        # entries are live. This lower bound may become early
                        # after eviction/invalidation, but never becomes late.
                        if now >= self._next_expiry:
                            for old_key in [k for k, e in self._entries.items() if e[0] <= now]:
                                self.bytes_used -= self._entries.pop(old_key)[2]
                            self._next_expiry = min((e[0] for e in self._entries.values()),
                                                    default=float("inf"))
                        while self._entries and (len(self._entries) >= self.max_entries
                                                 or self.bytes_used + size > self.max_bytes):
                            self.bytes_used -= self._entries.popitem(last=False)[1][2]
                            self.evictions += 1
                        self._entries[key] = (now + self.ttl, value, size)
                        self._next_expiry = min(self._next_expiry, now + self.ttl)
                        self.bytes_used += size
                    del self._flights[key]
                flight.value = value
                flight.ready.set()
            return value
        except BaseException as error:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
                flight.error = error
                flight.ready.set()
            raise
