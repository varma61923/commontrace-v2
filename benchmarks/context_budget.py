"""Enforce a selected text-token cap through repeated native context assembly.

No substring truncation or stale source-selection proof: each returned context
and its source identities come from a complete native retrieval/assembly call.
"""
from __future__ import annotations


def retrieve_with_cap(retrieve, budget: int, count, *, max_attempts: int = 16):
    """Lower the native estimated budget until measured context fits, or fail closed.

    This bounds the context, not the entire reader prompt. Assembly need not be
    monotonic; every result is counted, and there is a finite attempt bound.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("context text budget must be a positive integer")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
        raise ValueError("context budget attempts must be positive")
    native_budget = budget
    for attempt in range(1, max_attempts + 1):
        result = retrieve(native_budget)
        actual = count(result.context)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
            raise ValueError("context token counter returned an invalid count")
        if actual <= budget:
            return result, {"native_budget": native_budget, "text_tokens": actual, "attempts": attempt}
        if native_budget <= 1:
            break
        # Make strict progress even for adversarial/non-monotonic assembly.
        native_budget = max(1, min(native_budget - 1, native_budget * budget // actual - 1))
    raise ValueError("no fitting native context was found within the attempt limit")
