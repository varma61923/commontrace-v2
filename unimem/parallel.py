"""Bounded parallel fan-out (stdlib only).

Adapts graphiti's ``semaphore_gather`` (``graphiti_core/helpers.py``) for sync
code: run ``fn`` over ``items`` on at most ``max_workers`` threads, results in
input order, first exception propagates after all workers settle. Use this
instead of an unbounded ``ThreadPoolExecutor(max_workers=len(items))`` or a
serial loop for per-space / per-batch fan-out.
"""
from __future__ import annotations

import concurrent.futures
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

DEFAULT_MAX_WORKERS = 4


def bounded_map(fn: Callable[[Any], Any], items: Iterable[Any],
                *, max_workers: int = DEFAULT_MAX_WORKERS,
                max_pending: int | None = None) -> list[Any]:
    """Ordered results with bounded workers *and* bounded submission backlog.

    Consume lazy inputs incrementally. On an error, already running workers
    settle and queued work is cancelled; no more input is consumed.
    ``max_workers<=1`` retains the historical sequential behavior.
    """
    if max_pending is None:
        max_pending = max(1, max_workers) * 2
    if max_pending < 1:
        raise ValueError("max_pending must be positive")
    if max_workers <= 1:
        return [fn(x) for x in items]
    source = iter(items)
    pending: deque[concurrent.futures.Future] = deque()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        try:
            for _ in range(max_pending):
                try:
                    item = next(source)
                except StopIteration:
                    break
                pending.append(pool.submit(fn, item))
            while pending:
                results.append(pending.popleft().result())
                try:
                    item = next(source)
                except StopIteration:
                    continue
                pending.append(pool.submit(fn, item))
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return results
