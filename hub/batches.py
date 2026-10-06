"""Bounded sequential RPC batches preserve single-item authorization policies."""
from __future__ import annotations

import inspect

MAX_TRACE_BATCH = 25


async def run_batch(handler, items: list, *, field: str | None = None) -> dict:
    """Validate all argument shapes, then commit independent ordered operations.

    Each handler retains its authorization, quota, idempotency and audit rules.
    This is deliberately not an atomic transaction across the whole batch.
    """
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TRACE_BATCH:
        return {"error": "invalid_batch", "detail": "batch must contain 1 to 25 items"}
    arguments = []
    signature = inspect.signature(handler)
    for item in items:
        if field is None:
            if not isinstance(item, dict):
                return {"error": "invalid_batch", "detail": "contributions must be objects"}
            args = item
        else:
            if not isinstance(item, str) or not item or len(item) > 128:
                return {"error": "invalid_batch", "detail": "trace ids must be nonempty strings up to 128 chars"}
            args = {field: item}
        try:
            signature.bind(**args)
        except TypeError:
            return {"error": "invalid_batch", "detail": "item has missing or unsupported argument fields"}
        arguments.append(args)
    return {"results": [await handler(**args) for args in arguments]}
