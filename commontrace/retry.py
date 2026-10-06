"""Retry with backoff for LLM / embedding HTTP calls (stdlib only).

Adapts zep's ``call_with_retries`` (``ingestion/src/zep_ingest/submitters/
sequential.py``): honor ``Retry-After`` (seconds or HTTP-date, NaN-safe),
retry 429 + unsent-transport errors always, retry 5xx only for idempotent
calls, cap the wait (never honor ``86400`` verbatim), jitter the backoff.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import random
import time
import urllib.error

MAX_RETRIES = 4
MAX_WAIT_SECONDS = 60.0


def retry_after_seconds(value: str | None, attempt: int) -> float:
    """How long to wait: the server's ``Retry-After`` if sane, else backoff.

    ``Retry-After`` may be delta-seconds or an HTTP-date; garbage, negative,
    and NaN values fall back to ``2**(attempt-1)`` with ±25% jitter, capped at
    :data:`MAX_WAIT_SECONDS`.
    """
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            delay = float("nan")
            try:
                when = email.utils.parsedate_to_datetime(str(value))
                if when is not None:
                    now = dt.datetime.now(dt.timezone.utc)
                    delay = (when - now).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
        if delay == delay and delay > 0:  # not NaN, positive
            return min(delay, MAX_WAIT_SECONDS)
    return min(MAX_WAIT_SECONDS, (2.0 ** max(0, attempt - 1)) * (1.0 + random.random() * 0.25))


def is_retryable(exc: BaseException, *, idempotent: bool) -> tuple[bool, str | None]:
    """Whether *exc* merits another attempt, and any server retry hint."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return True, exc.headers.get("Retry-After") if exc.headers else None
        if 500 <= exc.code < 600:
            return idempotent, exc.headers.get("Retry-After") if exc.headers else None
        return False, None
    # Unsent / transport errors (connection refused, reset, DNS, timeout):
    # the request never reached the server, so retrying is always safe.
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return True, None
    return False, None


def call_with_retries(fn, *, max_retries: int = MAX_RETRIES,
                       idempotent: bool = True, sleep=time.sleep):
    """Call ``fn()`` with retry; returns ``(result, error)`` instead of raising.

    ``fn`` raising means the attempt failed; non-retryable errors and an
    exhausted budget return ``(None, exc)``. Mirrors zep's tuple return so a
    bulk caller can record-and-continue instead of losing submitted work.
    """
    error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - classified below
            retry, hint = is_retryable(exc, idempotent=idempotent)
            error = exc
            if not retry or attempt >= max_retries:
                return None, exc
            sleep(retry_after_seconds(hint, attempt))
    return None, error
