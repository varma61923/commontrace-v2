"""Bounded parallel fan-out (stdlib only).

Adapts graphiti's ``semaphore_gather`` (``graphiti_core/helpers.py``) for sync
code: run ``fn`` over ``items`` on at most ``max_workers`` threads, results in
input order, first exception propagates after all workers settle. Use this
instead of an unbounded ``ThreadPoolExecutor(max_workers=len(items))`` or a
serial loop for per-space / per-batch fan-out.
"""
from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Callable, Iterable
from typing import Any

DEFAULT_MAX_WORKERS = 4


def bounded_map(fn: Callable[[Any], Any], items: Iterable[Any],
                *, max_workers: int = DEFAULT_MAX_WORKERS) -> list[Any]:
    """``[fn(x) for x in items]`` on a bounded pool, input order preserved."""
    items = list(items)
    if not items:
        return []
    if len(items) == 1 or max_workers <= 1:
        return [fn(x) for x in items]
    gate = threading.Semaphore(max(1, max_workers))

    def _guarded(item: Any) -> Any:
        with gate:
            return fn(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(_guarded, items))
