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

    def _occasion(self, occasion_id: str | None) -> str | None:
        if not self.agent_id and occasion_id is None:
            return None
        return memory_control.occasion(occasion_id, principal=self.agent_id)

    def _request(self, operation: str, payload: dict) -> dict:
        if not self.url:
            raise RuntimeError("HTTP URL not configured")
        req = urllib.request.Request(self.url.rstrip("/") + "/v1/memory/" + operation,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + self.token})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None  # A gateway redirect must never forward the bearer key.

        with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("gateway response exceeds 8 MiB")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("gateway response must be an object")
            return result

    def add(self, text: str, *, local: bool = True, complete=None, memory_type: str = "general",
            entity_model=None, entity_model_path: str | None = None) -> dict:
        if self.url:
            if entity_model is not None or entity_model_path:
                raise ValueError("local extraction adapters must be configured on the gateway host")
            return self._request("add", {"text": text, "local": local, "context": self.context,
                                         "memory_type": memory_type})
        from commontrace import memory_authority

        with memory_authority.restricted_writer(self.agent_id or "local", "agent" if self.agent_id else "operator"):
            return additive_extract.extract(self.root, text, local=local, complete=complete,
                                             scopes=self.context, memory_type=memory_type,
                                             entity_model=entity_model, entity_model_path=entity_model_path)

    def profile(self, query: str = "", *, limit: int = 10, occasion_id: str | None = None,
                action_class: str = "") -> dict:
        occasion_id = self._occasion(occasion_id)
        if self.url:
            return self._request("profile", {"query": query, "limit": limit, "context": self.context,
                                             "occasion_id": occasion_id, "action_class": action_class})
        return memory_control.profile(self.root, query, context=self.context, limit=limit,
                                      occasion_id=occasion_id, action_class=action_class)

    def search(self, query: str, *, recipe: str = "balanced", retriever: str = "hybrid",
               limit: int = 10, center: str = "", as_of: str | None = None, action_class: str = "",
               **local_options) -> list[dict]:
        if self.url:
            if local_options:
                raise ValueError("injected adapters are local-only")
            return self._request("search", {"query": query, "recipe": recipe, "retriever": retriever,
                "limit": limit, "center": center, "as_of": as_of, "context": self.context,
                "action_class": action_class})["results"]
        from commontrace.search_recipes import REGISTRY

        return REGISTRY.retrieve(retriever, self.root, query, recipe=recipe, limit=limit, center=center,
                                 as_of=as_of, context=self.context, action_class=action_class, **local_options)

    def batch(self, items: list[dict]) -> dict:
        if self.url:
            return self._request("batch", {"items": items, "context": self.context})
        from commontrace import ingestion_contract, memory_authority

        with memory_authority.restricted_writer(self.agent_id or "local", "agent" if self.agent_id else "operator"):
            return ingestion_contract.batch(self.root, items, context=self.context)

    def propose(self, text: str, *, sources: list[str]) -> dict:
        if self.url:
            return self._request("propose", {"text": text, "sources": sources, "context": self.context})
        return memory_control.proposal(self.root, text, sources=sources, context=self.context)

    def reflect(self, query: str, *, budget: int = 600, occasion_id: str | None = None,
                exploration_slots: int = 0, action_class: str = "") -> dict:
        occasion_id = self._occasion(occasion_id)
        if self.url:
            return self._request("reflect", {"query": query, "budget": budget, "context": self.context,
                                             "occasion_id": occasion_id, "exploration_slots": exploration_slots,
                                             "action_class": action_class})
        result = memory_control.reflect(self.root, query, context=self.context, budget=budget,
                                        occasion_id=occasion_id, exploration_slots=exploration_slots,
                                        action_class=action_class, record_receipt=False)
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
                                                    occasion_id=occasion_id, causal=False, action_class=action_class)
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
                shared.append({"evidence": measured.items, "directives": [{"id": namespace+r["id"],
                    "text": r["text"], "layer": "directive"} for r in rules], "namespace": namespace,
                               "withheld": [r["id"] for r in candidates if r["id"] not in kept],
                               "occasion_id": result["occasion_id"]})
            result["shared"] = shared
        from commontrace import assurance

        combined = [*result["evidence"]]
        for shared in result.get("shared", []):
            combined.extend(shared["evidence"])
            combined.extend(shared["directives"])
        result["authority_sources"] = sorted(set(result["authority_sources"]+[r["id"] for r in combined]))
        rules = [{"id": rid, "text": "mandatory directive", "layer": "directive"}
                 for rid in result["authority_sources"] if rid not in {r["id"] for r in combined}]
        assurance.record_recall(self.root, result["occasion_id"], [*combined, *rules], result["context"], self.context)
        return result

    def outcome(self, occasion_id: str, succeeded: bool) -> bool:
        occasion_id = self._occasion(occasion_id)
        if self.url:
            return self._request("outcome", {"occasion_id": occasion_id, "succeeded": succeeded})["recorded"]
        from commontrace import causal_policy, holdout_io, policy

        if not isinstance(succeeded, bool):
            raise ValueError("succeeded must be boolean")
        policy.outcome(self.root, occasion_id, float(succeeded))
        exploration = causal_policy.record_outcome(self.root, occasion_id, float(succeeded))
        return holdout_io.record_outcome(self.root, occasion_id, succeeded) or exploration

    def check_action(self, tool: str, *, tags: list[str] | None = None) -> None:
        if self.url:
            result = self._request("check-action", {"tool": tool, "tags": tags or [], "context": self.context})
            if result.get("allowed") is not True:
                raise PermissionError("action blocked by a directive")
        else:
            memory_control.check_action(self.root, tool, tags=tags, context=self.context)
            if self.agent_id:
                from commontrace import memfs

                for shared_root in memfs.attached_roots(self.root, agent_id=self.agent_id):
                    memory_control.check_action(shared_root, tool, tags=tags, context=self.context)


def wrap(completion: Callable, *, root: str = ".", budget: int = 600, agent_id: str = "",
         prices: dict | None = None) -> Callable:
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
        import time

        from commontrace import assurance

        started = time.monotonic()
        response = completion(*args, messages=augmented, **kwargs)
        assurance.usage(root, recalled["occasion_id"], response, seconds=time.monotonic()-started,
                        provider="openai-compatible", prices=prices)
        if query:
            from commontrace import trace_io

            choices = response.get("choices", []) if isinstance(response, dict) else []
            answer = choices[0].get("message", {}).get("content", "") if choices else ""
            if not answer and getattr(response, "choices", None):
                answer = getattr(response.choices[0].message, "content", "") or ""
            if answer:
                from commontrace import memory_authority

                with memory_authority.restricted_writer(agent_id or "completion", "agent"):
                    trace_io.write_new(root, title=query[:200], context=query, solution=answer,
                                       tags=["completion"], extra={"scopes": memory.context,
                                       "source_traces": recalled["authority_sources"],
                                       "extensions": {"profile": {"occasion_id": recalled["occasion_id"]}}})
        return response
    return remembered
