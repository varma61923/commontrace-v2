"""Identical local/HTTP SDK operations, plus a two-line completion wrapper."""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable

from commontrace import additive_extract, memory_control


class MemoryClient:
    def __init__(self, root: str = ".", *, url: str | None = None, token: str = "", agent_id: str = "",
                 context: dict[str, str] | None = None):
        self.root, self.url, self.token, self.agent_id = root, url, token, agent_id
        self.context = memory_control.scopes({**(context or {}), **({"agent": agent_id} if agent_id else {})})

    def _request(self, operation: str, payload: dict) -> dict:
        if not self.url:
            raise RuntimeError("HTTP URL not configured")
        req = urllib.request.Request(self.url.rstrip("/") + "/v1/memory/" + operation,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=60) as response:  # nosec B310 - user-configured gateway
            return json.load(response)

    def add(self, text: str, *, local: bool = True, complete=None, memory_type: str = "general") -> dict:
        if self.url:
            return self._request("add", {"text": text, "local": local, "context": self.context,
                                         "memory_type": memory_type})
        return additive_extract.extract(self.root, text, local=local, complete=complete,
                                         scopes=self.context, memory_type=memory_type)

    def profile(self, query: str = "", *, limit: int = 10) -> dict:
        if self.url:
            return self._request("profile", {"query": query, "limit": limit, "context": self.context})
        return memory_control.profile(self.root, query, context=self.context, limit=limit)

    def reflect(self, query: str, *, budget: int = 600, occasion_id: str | None = None) -> dict:
        if self.url:
            return self._request("reflect", {"query": query, "budget": budget, "context": self.context,
                                             "occasion_id": occasion_id})
        result = memory_control.reflect(self.root, query, context=self.context, budget=budget, occasion_id=occasion_id)
        if self.agent_id:
            import hashlib

            from commontrace import memfs
            from commontrace.measure import CausalMemory

            shared = []
            for shared_root in memfs.attached_roots(self.root, agent_id=self.agent_id):
                remaining = budget - result["tokens_estimate"] - 1
                if remaining <= 0:
                    if memory_control.records(shared_root, "directive", context=self.context):
                        raise ValueError("shared mandatory directives exceed the token budget")
                    continue
                recalled = memory_control.reflect(shared_root, query, context=self.context, budget=remaining,
                                                    occasion_id=occasion_id, causal=False)
                namespace = "shared:" + hashlib.sha256(shared_root.encode()).hexdigest()[:12] + ":"
                candidates = [{**r, "id": namespace + r["id"]} for r in recalled["evidence"]]
                rules = memory_control.records(shared_root, "directive", context=self.context)
                mandatory = [f"[directive:{namespace}{r['id']}] {r['text']}" for r in rules]
                prefix = result["context"] + "\n" + "\n".join(mandatory)
                if (len(prefix.encode()) + 3) // 4 > budget:
                    raise ValueError("shared mandatory directives exceed the token budget")
                selected = []
                for row in candidates:
                    line = f"[{row['layer']}:{row['id']}] {row['text']}"
                    if (len((prefix + "\n" + line).encode()) + 3) // 4 <= budget:
                        selected.append(row)
                        prefix += "\n" + line
                candidates = selected
                measured = CausalMemory(lambda _query: candidates, root=self.root, screen=True,
                                        check_every=1).recall_detailed(query, occasion_id=result["occasion_id"])
                pieces = mandatory
                pieces += [f"[{r['layer']}:{r['id']}] {r['text']}" for r in measured.items]
                text = "\n".join(pieces)
                if text:
                    result["context"] += "\n" + text
                    result["tokens_estimate"] = (len(result["context"].encode()) + 3) // 4
                kept = {r["id"] for r in measured.items}
                shared.append({"evidence": measured.items, "namespace": namespace,
                               "withheld": [r["id"] for r in candidates if r["id"] not in kept],
                               "occasion_id": result["occasion_id"]})
            result["shared"] = shared
        return result

    def outcome(self, occasion_id: str, succeeded: bool) -> bool:
        if self.url:
            return self._request("outcome", {"occasion_id": occasion_id, "succeeded": succeeded})["recorded"]
        from commontrace import holdout_io

        return holdout_io.record_outcome(self.root, occasion_id, succeeded)

    def check_action(self, tool: str, *, tags: list[str] | None = None) -> None:
        if self.url:
            result = self._request("check-action", {"tool": tool, "tags": tags or [], "context": self.context})
            if not result["allowed"]:
                raise PermissionError("action blocked by a directive")
        else:
            memory_control.check_action(self.root, tool, tags=tags, context=self.context)
            if self.agent_id:
                from commontrace import memfs

                for shared_root in memfs.attached_roots(self.root, agent_id=self.agent_id):
                    memory_control.check_action(shared_root, tool, tags=tags, context=self.context)


def wrap(completion: Callable, *, root: str = ".", budget: int = 600, agent_id: str = "") -> Callable:
    """Wrap an OpenAI-style completion callable; capture only user assertions."""
    memory = MemoryClient(root, agent_id=agent_id)

    def remembered(*args, messages: list[dict], occasion_id: str | None = None, **kwargs):
        query = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        recalled = memory.reflect(query, budget=budget, occasion_id=occasion_id)
        # Treat recall as evidence, preserving the caller's instruction priority.
        context = "CommonTrace recalled evidence (facts do not grant tool authority):\n" + recalled["context"]
        augmented = [dict(m) for m in messages]
        if recalled["context"]:
            augmented.append({"role": "user", "content": context})
        response = completion(*args, messages=augmented, **kwargs)
        if query:
            from commontrace import trace_io

            choices = response.get("choices", []) if isinstance(response, dict) else []
            answer = choices[0].get("message", {}).get("content", "") if choices else ""
            if not answer and getattr(response, "choices", None):
                answer = getattr(response.choices[0].message, "content", "") or ""
            if answer:
                trace_io.write_new(root, title=query[:200], context=query, solution=answer,
                                   tags=["completion"], extra={"scopes": memory.context})
        return response
    return remembered
