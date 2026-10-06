"""Thread-safe provider circuit with one recovery probe and generation fencing."""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.0, retry_after)
        super().__init__("provider circuit is open")


class CircuitBreaker:
    """Open after consecutive transient failures; admit one probe after cooldown.

    Older in-flight successes cannot close a newly opened circuit. Permanent
    failures do not trip it. Cancellation releases a recovery probe without
    declaring an unavailable provider healthy.
    """

    def __init__(self, *, threshold: int = 5, recovery_seconds: float = 30,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if threshold < 1 or not math.isfinite(recovery_seconds) or recovery_seconds <= 0:
            raise ValueError("circuit threshold and recovery interval must be positive and finite")
        self.threshold, self.recovery_seconds, self._clock = threshold, recovery_seconds, clock
        self._lock = threading.Lock()
        self._failures = self._generation = 0
        self._opened: float | None = None
        self._probe = False

    def call(self, fn: Callable[[], T], *, transient: Callable[[BaseException], bool]) -> T:
        with self._lock:
            now = self._clock()
            probe = self._opened is not None
            if probe:
                remaining = self.recovery_seconds - (now - self._opened)
                if remaining > 0 or self._probe:
                    raise CircuitOpenError(remaining)
                self._probe = True
            generation = self._generation
        try:
            result = fn()
        except BaseException as error:
            retryable = isinstance(error, Exception) and transient(error)
            with self._lock:
                if self._generation == generation:
                    if probe:
                        self._probe = False
                    if retryable:
                        self._failures += 1
                        if probe or self._failures >= self.threshold:
                            self._opened = self._clock()
                            self._generation += 1
                    elif isinstance(error, Exception):
                        # A permanent response establishes provider reachability,
                        # while still propagating its real error to this caller.
                        self._failures = 0
                        if probe:
                            self._opened = None
                            self._generation += 1
            raise
        with self._lock:
            if self._generation == generation:
                self._failures = 0
                if probe:
                    self._probe = False
                    self._opened = None
                    self._generation += 1
        return result
