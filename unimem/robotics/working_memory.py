"""Local, bounded keyframe working memory for latency-critical robot loops."""
from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Sequence
from typing import Any

from unimem import frontier_protocol as fp

AUDIT = "robot-working-memory.jsonl"
MAX_KEYFRAMES = 4096


class WorkingMemory:
    """In-process memory; methods never perform network calls or disk I/O."""
    def __init__(self, *, capacity_tokens: int = 16_384, budget_ms: float = 5.0) -> None:
        self.capacity = fp.count(capacity_tokens, 1, 1_000_000)
        self.budget_ms = fp.number(budget_ms, .1, 100)
        self._items: dict[str, dict[str, Any]] = {}
        self._tokens = 0
        self._pid = os.getpid()
        self._lock = threading.RLock()

    def _guard(self) -> threading.RLock:
        if self._pid != os.getpid():
            # A child must never wait on a lock owned by a vanished thread.
            self._lock = threading.RLock()
            self._tokens = sum(row["tokens"] for row in self._items.values())
            self._pid = os.getpid()
        return self._lock

    @staticmethod
    def _vector(vector: Sequence[float]) -> list[float]:
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes, bytearray)):
            raise ValueError("working-memory vector must be a bounded sequence")
        fp.count(len(vector), 1, 4096, "vector dimensions")
        values = [fp.number(value, -1e6, 1e6) for value in vector]
        if not any(values):
            raise ValueError("working-memory vector cannot be zero")
        return values

    @staticmethod
    def _unit(values: Sequence[float]) -> list[float]:
        # Normalize after scaling, so squaring tiny finite components cannot
        # underflow and a subnormal norm cannot distort cosine similarities.
        scale = max(abs(value) for value in values)
        scaled = [value / scale for value in values]
        norm = math.hypot(*scaled)
        return [value / norm for value in scaled]

    def add(self, identity: str, vector: Sequence[float], *, tokens: int, episode_id: str,
            predictive: bool = False, safety: bool = False, utility: float = 0,
            future_retrieval: float = 0) -> dict[str, Any]:
        identity, episode_id = fp.text(identity, "keyframe id", 128), fp.text(episode_id, "episode id", 128)
        fp.count(tokens, 1, 1_000_000)
        fp.number(utility, -1, 1)
        fp.number(future_retrieval, 0, 1)
        if type(predictive) is not bool or type(safety) is not bool:
            raise ValueError("predictive and safety markers must be booleans")
        values = self._vector(vector)
        unit_vector = self._unit(values)
        row = {"id": identity, "vector": values, "tokens": tokens,
            "unit_vector": unit_vector,
            "episode_id": episode_id, "predictive": predictive, "safety": safety,
            "utility": utility, "future_retrieval": future_retrieval, "added_at": fp.now()}
        with self._guard():
            previous = self._items.get(identity)
            if previous is None:
                fp.count(len(self._items), 0, MAX_KEYFRAMES - 1)
            elif previous["safety"] and not safety:
                raise ValueError("a protected safety keyframe cannot be demoted")
            self._items[identity] = row
            self._tokens += tokens - (previous["tokens"] if previous else 0)
            eviction = self.evict_to_capacity() if self._tokens > self.capacity else {"evicted": []}
            return {"id": identity, "items": len(self._items), "tokens": self._tokens,
                    "protected": safety, "evicted": eviction["evicted"]}

    def used_tokens(self) -> int:
        with self._guard():
            return self._tokens

    def evict_to_capacity(self) -> dict[str, Any]:
        """Use retention's utility heuristic; protected safety keyframes always remain."""
        with self._guard():
            return self._evict_locked()

    def _evict_locked(self) -> dict[str, Any]:
        from unimem import retention

        if self.used_tokens() <= self.capacity:
            return {"kept": sorted(self._items), "evicted": [], "protected_over_budget": False,
                    "kind": "no eviction needed"}
        protected = {key for key, row in self._items.items() if row["safety"]}
        protected_tokens = sum(self._items[key]["tokens"] for key in protected)
        budget = max(0, self.capacity - protected_tokens)
        candidates = [{"id": row["id"], "effect": max(0, row["utility"]),
                      "future_retrieval": row["future_retrieval"], "tokens": row["tokens"]}
                     for row in self._items.values() if row["id"] not in protected]
        selected = retention.dream_budget(candidates, tokens=budget)
        keep = protected | {row["id"] for row in selected["changes"]}
        removed = sorted(set(self._items) - keep)
        for identity in removed:
            self._tokens -= self._items.pop(identity)["tokens"]
        return {"kept": sorted(self._items), "evicted": removed,
                "protected_over_budget": protected_tokens > self.capacity,
                "kind": selected["kind"]}

    def retrieve(self, vector: Sequence[float], *, budget_ms: float | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        values = self._vector(vector)
        query = self._unit(values)
        limit = self.budget_ms if budget_ms is None else fp.number(budget_ms, .1, 100)
        best: tuple[float, dict[str, Any]] | None = None
        timed_out = False
        lock = self._guard()
        acquired = lock.acquire(timeout=max(0.0, limit / 1000 - (time.perf_counter() - started)))
        items: tuple[dict[str, Any], ...] = ()
        if acquired:
            try:
                # Rows are replaced atomically, never mutated after publication.
                items = tuple(self._items.values())
            finally:
                lock.release()
        else:
            timed_out = True
        for index, row in enumerate(items):
            if index > 0 and time.perf_counter() - started > limit / 1000:
                timed_out = True
                break
            candidate = row["unit_vector"]
            if len(candidate) != len(query):
                continue
            score = sum(a * b for a, b in zip(query, candidate))
            if best is None or score > best[0]:
                best = score, row
        elapsed = (time.perf_counter() - started) * 1000
        return {"result": None if best is None else {"id": best[1]["id"], "episode_id": best[1]["episode_id"],
                "score": best[0], "safety": best[1]["safety"]}, "over_budget": timed_out or elapsed > limit,
                "elapsed_ms": elapsed, "budget_ms": limit, "kind": "best local result so far"}

    def consolidate(self, root: str, episode_id: str, *, scopes: Sequence[str] = ()) -> dict[str, Any]:
        """Promote predictive episode keyframes to long-term facts with audit lineage."""
        from unimem import hierarchical, lineage

        episode_id = fp.text(episode_id, "episode id", 128)
        promoted = []
        with self._guard():
            items = tuple(self._items.values())
        for row in sorted(items, key=lambda item: item["id"]):
            if row["episode_id"] != episode_id or not row["predictive"]:
                continue
            statement = f"Predictive robot keyframe {row['id']} from episode {episode_id}"
            fact, _ = hierarchical.add_fact(root, statement, scopes=list(scopes),
                                            source_trace_id="working-memory:" + fp.digest([episode_id, row["id"]]))
            body = {"id": "wm-" + fp.digest([episode_id, row["id"], fact.id])[:32],
                    "episode_id": episode_id, "keyframe_id": row["id"], "fact_id": fact.id,
                    "predictive": True, "safety": row["safety"], "scopes": sorted(set(scopes))}
            lineage.attest(root, body, fp.sign(root, body), inputs=[episode_id],
                           operation="working-memory-consolidation")
            signed = fp.append(root, AUDIT, body)
            promoted.append({"keyframe_id": row["id"], "fact_id": fact.id, "sequence": signed["record"]["sequence"]})
        return {"episode_id": episode_id, "promoted": promoted, "audited": True}


def percentile_latency(samples: Sequence[float], percentile: float = .99) -> float:
    fp.count(len(samples), 1, 100_000)
    values = sorted(fp.number(sample, 0, 10_000, "retrieval latency") for sample in samples)
    q = fp.number(percentile, .5, 1)
    return values[min(len(values) - 1, math.ceil(q * len(values)) - 1)]
