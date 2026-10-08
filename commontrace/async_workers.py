"""Bounded, cancellation-safe offloading of synchronous store operations."""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class WorkerCapacityError(RuntimeError):
    """The service's bounded running/queued operation budget is exhausted."""


class BoundedWorkers:
    """Reuse threads with a hard admission limit, including cancelled requests.

    Cancellation stops waiting for a result; an accepted store write finishes.
    Its slot is held until the underlying operation completes, so cancellation
    cannot evade backpressure. Context variables propagate into each worker.
    ``close`` stops admission and waits off-loop for accepted work to finish.
    """

    def __init__(self, *, workers: int = 8, max_pending: int = 32) -> None:
        for name, value in (("workers", workers), ("max_pending", max_pending)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="commontrace-store")
        self._workers, self._max_pending = workers, max_pending
        self._slots = threading.BoundedSemaphore(max_pending)
        self._state = threading.Lock()
        self._closed = False
        if hasattr(os, "register_at_fork"):
            reference = weakref.ref(self)

            def reset_child() -> None:
                owner = reference()
                if owner is not None:
                    owner._after_fork()

            os.register_at_fork(after_in_child=reset_child)

    def _after_fork(self) -> None:
        # Parent threads and pending work do not exist in the child. Never
        # acquire their inherited locks or reuse the executor's dead workers.
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="commontrace-store")
        self._slots = threading.BoundedSemaphore(self._max_pending)
        self._state = threading.Lock()

    async def run(self, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run off-loop or raise ``WorkerCapacityError`` without queueing."""
        ctx = contextvars.copy_context()
        def invoke() -> T:
            return ctx.run(fn, *args, **kwargs)

        with self._state:
            if self._closed:
                raise RuntimeError("workers are closed")
            if not self._slots.acquire(blocking=False):
                raise WorkerCapacityError("store workers are at capacity; retry later")
            try:
                future = self._pool.submit(invoke)
            except BaseException:
                self._slots.release()
                raise
        future.add_done_callback(lambda _future: self._slots.release())
        waiting = asyncio.wrap_future(future)
        # Retrieve eventual failures even when the requesting task was cancelled.
        waiting.add_done_callback(lambda result: result.exception() if not result.cancelled() else None)
        return await asyncio.shield(waiting)

    async def close(self) -> None:
        """Stop admission and drain all work, including callers that cancelled."""
        with self._state:
            self._closed = True
        await asyncio.to_thread(self._pool.shutdown, wait=True)


# A process-wide pool avoids one executor per short-lived MCP server or session.
# Threads are created lazily, and Python drains the executor at interpreter exit.
STORE_WORKERS = BoundedWorkers()
