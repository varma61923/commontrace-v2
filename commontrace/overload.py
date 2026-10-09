"""Shared adaptive provider admission: fail fast during a bounded server cooldown."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from commontrace.exceptions import InfrastructureError
from commontrace.retry import retry_after_seconds


class Overloaded(InfrastructureError):
    code = "provider_cooldown"

    def __init__(self, delay: float):
        self.retry_after = max(0.0, min(60.0, delay))
        super().__init__(f"Provider cooldown: retry in {self.retry_after:.1f}s.")


class OverloadPolicy:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until = 0.0
        self._failures = 0
        self._lock = threading.Lock()

    def admit(self) -> None:
        with self._lock:
            delay = self._until-self._clock()
        if delay > 0:
            raise Overloaded(delay)

    def on_error(self, error: BaseException) -> None:
        seen = set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            response = getattr(error, "response", {})
            meta = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
            status = (getattr(error, "code", None) or getattr(error, "status_code", None)
                      or meta.get("HTTPStatusCode"))
            if status in (429, 503, 529):
                headers = getattr(error, "headers", None) or meta.get("HTTPHeaders", {})
                with self._lock:
                    self._failures = min(6, self._failures+1)
                    delay = retry_after_seconds(headers.get("Retry-After", headers.get("retry-after")),
                                                self._failures)
                    self._until = max(self._until, self._clock()+delay)
                return
            error = error.__cause__

    def on_success(self) -> None:
        with self._lock:
            if self._clock() >= self._until:
                self._failures = 0
