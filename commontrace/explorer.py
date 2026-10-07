"""Read-only, bounded retrieval inspection for the authenticated console.

Exploration never records an occasion, changes an experiment, or calls a model.
The gateway supplies the caller's scope and already-namespaced conversation space.
"""
from __future__ import annotations

import time
from typing import Any

from commontrace import lesson_cache, recall


class ExplorerError(ValueError):
    """Invalid exploration input; safe to display to the caller."""


def _integer(data: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ExplorerError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def inspect(root: str, data: dict[str, Any], *, scope: str = "", space: str | None = None) -> dict[str, Any]:
    """Inspect eligible memory without writing outcome or holdout state."""
    question = data.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ExplorerError("question must be a non-empty string of at most 2000 characters")
    channels = data.get("channels", ["lessons", "facts"])
    if (not isinstance(channels, list) or not channels or len(channels) > 3
            or any(not isinstance(c, str) or c not in ("lessons", "facts", "conversations") for c in channels)
            or len(set(channels)) != len(channels)):
        raise ExplorerError("channels must be distinct lessons, facts or conversations")
    if "conversations" in channels and not space:
        raise ExplorerError("a conversation space is required for conversation exploration")
    budget = _integer(data, "budget", 1500, 50, 8000)
    evidence_budget = _integer(data, "evidence_budget", 512, 0, 2000)
    fact_scorer = data.get("fact_scorer", "overlap-v1")
    if not isinstance(fact_scorer, str) or fact_scorer not in ("overlap-v1", "bm25-v1"):
        raise ExplorerError("fact_scorer must be overlap-v1 or bm25-v1")
    as_of = data.get("as_of")
    if as_of is not None:
        if not isinstance(as_of, str) or len(as_of) > 64:
            raise ExplorerError("as_of must be an ISO date or timestamp")
        try:
            lesson_cache.parse_moment(as_of)
        except (TypeError, ValueError) as exc:
            raise ExplorerError("as_of must be an ISO date or timestamp") from exc
    started = time.perf_counter()
    result = recall.recall(root, question.strip(), budget=budget, channels=tuple(channels),
                           as_of=as_of, spaces=[space] if space else None, embedder="none",
                           per_channel=12, evidence_budget=evidence_budget, scope=scope, fact_scorer=fact_scorer)
    return {**result.to_dict(), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "read_only": True, "scope": scope, "answer_accuracy_measured": False}
