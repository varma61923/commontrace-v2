"""Bounded parallel fan-out (stdlib only).

Adapts graphiti's ``semaphore_gather`` (``graphiti_core/helpers.py``) for sync
code: run ``fn`` over ``items`` on at most ``max_workers`` threads, results in
input order, first exception propagates after all workers settle. Use this
instead of an unbounded ``ThreadPoolExecutor(max_workers=len(items))`` or a
serial loop for per-space / per-batch fan-out.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
from collections import deque
from collections.abc import Callable, Generator, Iterable
from typing import TypeVar

DEFAULT_MAX_WORKERS = 4
_Input = TypeVar("_Input")
_Output = TypeVar("_Output")


def bounded_map(
    fn: Callable[[_Input], _Output],
    items: Iterable[_Input],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_pending: int | None = None,
) -> list[_Output]:
    """Ordered results with bounded workers *and* bounded submission backlog.

    Consume lazy inputs incrementally. On an error, already running workers
    settle and queued work is cancelled; no more input is consumed.
    ``max_workers<=1`` retains the historical sequential behavior.
    """
    if max_workers <= 1:
        if max_pending is not None and max_pending < 1:
            raise ValueError("max_pending must be positive")
        return [fn(x) for x in items]
    return list(bounded_imap(fn, items, max_workers=max_workers, max_pending=max_pending))


def bounded_imap(
    fn: Callable[[_Input], _Output],
    items: Iterable[_Input],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_pending: int | None = None,
) -> Generator[_Output, None, None]:
    """Yield ordered results with bounded submission and isolated task contexts.

    Source iteration and result consumption stay in the caller; functions run
    in worker threads, including with one worker. Close this iterator when
    stopping early to cancel queued work and drain accepted operations.
    """
    if max_pending is None:
        max_pending = max(1, max_workers) * 2
    if max_pending < 1:
        raise ValueError("max_pending must be positive")
    source = iter(items)
    pending: deque[concurrent.futures.Future[_Output]] = deque()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        try:
            for _ in range(max_pending):
                try:
                    item = next(source)
                except StopIteration:
                    break
                pending.append(pool.submit(contextvars.copy_context().run, fn, item))
            while pending:
                yield pending.popleft().result()
                try:
                    item = next(source)
                except StopIteration:
                    continue
                pending.append(pool.submit(contextvars.copy_context().run, fn, item))
        finally:
            for future in pending:
                future.cancel()
