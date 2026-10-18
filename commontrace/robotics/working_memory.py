"""Local, bounded keyframe working memory for latency-critical robot loops."""
from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

from commontrace import frontier_protocol as fp

AUDIT = "robot-working-memory.jsonl"
MAX_KEYFRAMES = 4096


class WorkingMemory:
    """In-process memory; methods never perform network calls or disk I/O."""
    def __init__(self, *, capacity_tokens: int = 16_384, budget_ms: float = 5.0) -> None:
        self.capacity = fp.count(capacity_tokens, 1, 1_000_000)
        self.budget_ms = fp.number(budget_ms, .1, 100)
        self._items: dict[str, dict[str, Any]] = {}

    def add(self, identity: str, vector: Sequence[float], *, tokens: int, episode_id: str,
            predictive: bool = False, safety: bool = False, utility: float = 0,
            future_retrieval: float = 0) -> dict[str, Any]:
        identity, episode_id = fp.text(identity, "keyframe id", 128), fp.text(episode_id, "episode id", 128)
        fp.count(len(self._items), 0, MAX_KEYFRAMES - 1)
        fp.count(tokens, 1, 1_000_000)
        fp.number(utility, -1, 1)
        fp.number(future_retrieval, 0, 1)
        if type(predictive) is not bool or type(safety) is not bool:
            raise ValueError("predictive and safety markers must be booleans")
        values = [fp.number(value, -1e6, 1e6) for value in vector]
        fp.count(len(values), 1, 4096)
        if not any(values):
            raise ValueError("keyframe vector cannot be zero")
        norm = math.sqrt(math.fsum(value * value for value in values))
        unit_vector = [value / norm for value in values]
        self._items[identity] = {"id": identity, "vector": values, "tokens": tokens,
            "unit_vector": unit_vector,
            "episode_id": episode_id, "predictive": predictive, "safety": safety,
            "utility": utility, "future_retrieval": future_retrieval, "added_at": fp.now()}
        eviction = self.evict_to_capacity() if self.used_tokens() > self.capacity else {"evicted": []}
        return {"id": identity, "items": len(self._items), "tokens": self.used_tokens(),
                "protected": safety, "evicted": eviction["evicted"]}

    def used_tokens(self) -> int:
        return sum(row["tokens"] for row in self._items.values())

    def evict_to_capacity(self) -> dict[str, Any]:
        """Use retention's utility heuristic; protected safety keyframes always remain."""
        from commontrace import retention

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
            del self._items[identity]
        return {"kept": sorted(self._items), "evicted": removed,
                "protected_over_budget": protected_tokens > self.capacity,
                "kind": selected["kind"]}

    def retrieve(self, vector: Sequence[float], *, budget_ms: float | None = None) -> dict[str, Any]:
        values = [fp.number(value, -1e6, 1e6) for value in vector]
        if not values or not any(values):
            raise ValueError("working-memory query vector cannot be empty or zero")
        norm = math.sqrt(math.fsum(value * value for value in values))
        query = [value / norm for value in values]
        limit = self.budget_ms if budget_ms is None else fp.number(budget_ms, .1, 100)
        started = time.perf_counter()
        best: tuple[float, dict[str, Any]] | None = None
        timed_out = False
        for index, row in enumerate(self._items.values()):
            if index % 16 == 0 and time.perf_counter() - started > limit / 1000:
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
        from commontrace import hierarchical, lineage

        episode_id = fp.text(episode_id, "episode id", 128)
        promoted = []
        for row in sorted(self._items.values(), key=lambda item: item["id"]):
            if row["episode_id"] != episode_id or not row["predictive"]:
                continue
            statement = f"Predictive robot keyframe {row['id']} from episode {episode_id}"
            fact, _ = hierarchical.add_fact(root, statement, scopes=list(scopes))
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
