from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from commontrace import holdout_io, memory_sources, paths
from commontrace.measure import DEFAULT_CHECK_EVERY, CausalMemory, Recall


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    raw: Any = None


class MemoryAdapter(Protocol):
    """Structural contract for provider adapters; no SDK dependency in the core."""

    @property
    def name(self) -> str: ...

    @property
    def can_delete(self) -> bool: ...

    def search(self, query: str, **kwargs: Any) -> list[Item]: ...

    def delete(self, item_id: str) -> None: ...


_SCOPE_KEYS = frozenset({"user_id", "agent_id", "session_id", "run_id", "org_id", "tenant_id", "filters"})


class Mem0Adapter:
    name = "mem0"
    can_delete = True

    def __init__(self, client: Any, **search_kwargs: Any) -> None:
        self.client = client
        self.search_kwargs = copy.deepcopy(search_kwargs)

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        for key in _SCOPE_KEYS & self.search_kwargs.keys() & kwargs.keys():
            if kwargs[key] != self.search_kwargs[key]:
                raise ValueError(f"cannot override pinned memory scope {key!r}")
        response = self.client.search(query, **copy.deepcopy({**self.search_kwargs, **kwargs}))
        rows = response.get("results", []) if isinstance(response, dict) else response or []
        return [Item(str(r["id"]), str(r.get("memory") or ""), r) for r in rows if r.get("id")]

    def delete(self, item_id: str) -> None:
        self.client.delete(item_id)


class LettaAdapter:
    """An agent's archival memory: `client.agents.passages.search`."""

    name = "letta"
    can_delete = True

    def __init__(self, client: Any, *, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        response = self.client.agents.passages.search(self.agent_id, query=query, **kwargs)
        return [Item(r.id, r.content or "", r) for r in response.results or []]

    def delete(self, item_id: str) -> None:
        self.client.agents.passages.delete(item_id, agent_id=self.agent_id)


class LettaCoreBlockAdapter:
    name = "letta-core"

    def __init__(self, client: Any, *, agent_id: str, allow_delete: bool = False) -> None:
        self.client = client
        self.agent_id = agent_id
        self.allow_delete = allow_delete
        self._labels: dict[str, str] = {}

    @property
    def can_delete(self) -> bool:
        return self.allow_delete

    def search(self, query: str = "", **kwargs: Any) -> list[Item]:
        items = []
        for block in self.client.agents.blocks.list(self.agent_id, **kwargs):
            if not getattr(block, "id", None):
                continue
            self._labels[block.id] = getattr(block, "label", None) or block.id
            items.append(Item(block.id, getattr(block, "value", "") or "", block))
        return items

    def block_ids(self, *labels: str) -> list[str]:
        """Ids of the blocks with these labels, for `pinned=`."""
        if not self._labels:
            self.search()
        return [bid for bid, label in self._labels.items() if label in labels]

    def delete(self, item_id: str, *, force: bool = False) -> None:
        """Detach or delete a core block from the agent."""
        if not self.allow_delete and not force:
            raise NotImplementedError("a core memory block is the agent owner's to remove")

        blocks_mgr = getattr(getattr(self.client, "agents", None), "blocks", None)
        if blocks_mgr and hasattr(blocks_mgr, "detach"):
            try:
                blocks_mgr.detach(item_id, agent_id=self.agent_id)
                self._labels.pop(item_id, None)
                return
            except TypeError:
                blocks_mgr.detach(block_id=item_id, agent_id=self.agent_id)
                self._labels.pop(item_id, None)
                return

        if hasattr(self.client, "blocks") and hasattr(self.client.blocks, "delete"):
            self.client.blocks.delete(item_id)
            self._labels.pop(item_id, None)
            return

        raise NotImplementedError(
            f"Letta client does not support block detachment or deletion for agent {self.agent_id}"
        )

    @staticmethod
    def render(items: Iterable[Item]) -> str:
        return "\n\n".join(
            f"<{getattr(i.raw, 'label', None) or i.id}>\n{i.text}\n</{getattr(i.raw, 'label', None) or i.id}>"
            for i in items)


class ZepAdapter:
    name = "zep"
    _TEXT_FIELD = {"edges": "fact", "nodes": "summary", "episodes": "content"}

    def __init__(
        self, client: Any, *, user_id: str | None = None, graph_id: str | None = None,
        scope: str = "edges",
    ) -> None:
        if (user_id is None) == (graph_id is None):
            raise ValueError("pass exactly one of user_id or graph_id")
        if scope not in self._TEXT_FIELD:
            raise ValueError(f"scope must be one of {sorted(self._TEXT_FIELD)}")
        self.client = client
        self.target = {"user_id": user_id} if user_id is not None else {"graph_id": graph_id}
        self.scope = scope

    @property
    def can_delete(self) -> bool:
        if self.scope == "edges":
            return True
        if self.scope == "nodes":
            graph_api = getattr(self.client, "graph", None)
            node_api = getattr(graph_api, "node", getattr(graph_api, "nodes", None))
            return bool(node_api and hasattr(node_api, "delete"))
        if self.scope == "episodes":
            graph_api = getattr(self.client, "graph", None)
            ep_api = getattr(graph_api, "episode", getattr(graph_api, "episodes", None))
            return bool(ep_api and hasattr(ep_api, "delete"))
        return False

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        response = self.client.graph.search(query=query, scope=self.scope, **self.target, **kwargs)
        field = self._TEXT_FIELD[self.scope]
        return [Item(r.uuid_, getattr(r, field, "") or "", r) for r in getattr(response, self.scope) or []]

    def delete(self, item_id: str) -> None:
        if not self.can_delete:
            raise NotImplementedError(f"deleting {self.scope} is not supported here")
        if self.scope == "edges":
            self.client.graph.edge.delete(item_id)
        elif self.scope == "nodes":
            node_api = getattr(self.client.graph, "node", getattr(self.client.graph, "nodes", None))
            node_api.delete(item_id)
        elif self.scope == "episodes":
            ep_api = getattr(self.client.graph, "episode", getattr(self.client.graph, "episodes", None))
            ep_api.delete(item_id)


class ClaudeMemoryStoreAdapter:
    """A Claude Managed Agents memory store."""

    name = "claude-memory-store"
    can_delete = True

    def __init__(self, client: Any, *, memory_store_id: str, path_prefix: str | None = None) -> None:
        self.client = client
        self.memory_store_id = memory_store_id
        self.path_prefix = path_prefix
        self._sha: dict[str, str] = {}

    def search(self, query: str = "", **kwargs: Any) -> list[Item]:
        params: dict[str, Any] = {"view": "full"}
        if self.path_prefix:
            params["path_prefix"] = self.path_prefix
        items = []
        for memory in self.client.beta.memory_stores.memories.list(self.memory_store_id, **params):
            if getattr(memory, "type", None) != "memory":
                continue
            self._sha[memory.id] = memory.content_sha256
            items.append(Item(memory.id, memory.content or "", memory))
        return items

    def delete(self, item_id: str) -> None:
        kwargs: dict[str, Any] = {"memory_store_id": self.memory_store_id}
        if item_id in self._sha:
            kwargs["expected_content_sha256"] = self._sha[item_id]
        self.client.beta.memory_stores.memories.delete(item_id, **kwargs)

    @staticmethod
    def render(items: Iterable[Item]) -> str:
        return "\n\n".join(f"## {getattr(i.raw, 'path', i.id)}\n{i.text}" for i in items)


class AgentCoreAdapter:
    name = "agentcore"
    can_delete = True

    def __init__(
        self, client: Any, *, memory_id: str, namespace: str | None = None,
        namespace_path: str | None = None,
    ) -> None:
        if (namespace is None) == (namespace_path is None):
            raise ValueError("pass exactly one of namespace or namespace_path")
        self.client = client
        self.memory_id = memory_id
        self.target = {"namespace": namespace} if namespace is not None else {"namespace_path": namespace_path}

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        records = self.client.retrieve_memories(
            memory_id=self.memory_id, query=query, **self.target, **kwargs,
        )
        items = []
        for record in records or []:
            content = record.get("content")
            text = content.get("text", "") if isinstance(content, dict) else ""
            if record.get("memoryRecordId"):
                items.append(Item(record["memoryRecordId"], text, record))
        return items

    def delete(self, item_id: str) -> None:
        self.client.delete_memory_record(memoryId=self.memory_id, memoryRecordId=item_id)


class MeasuredMemory:
    """Any adapter above, with holdout, outcome recording and withdrawal."""

    def __init__(
        self, adapter: MemoryAdapter, *, root: str | None = None, source_key: str | None = None,
        pinned: Iterable[str] = (), on_harm: str | None = None, delete_harmful: bool = False,
        check_every: int = DEFAULT_CHECK_EVERY, screen_injection: bool = True,
    ) -> None:
        self.adapter = adapter
        self.root = paths.resolve_root(root)
        self.source_key = source_key or f"adapter:{adapter.name}"
        self._delete_harmful = delete_harmful
        self._causal = CausalMemory(
            self._eligible, root=self.root, key=lambda i: i.id, text=lambda i: i.text,
            pinned=pinned, scorer=f"adapter:{adapter.name}", on_harm=on_harm, check_every=check_every,
            screen=screen_injection,
        )

    def _eligible(self, query: str, **kwargs: Any) -> list[Item]:
        blocked = memory_sources.blocked_ids_for_key(self.root, self.source_key)
        return [i for i in self.adapter.search(query, **kwargs) if i.id not in blocked]

    def recall(self, query: str = "", *, occasion_id: str, **kwargs: Any) -> list[Item]:
        return self.recall_detailed(query, occasion_id=occasion_id, **kwargs).items

    def recall_detailed(self, query: str = "", *, occasion_id: str, **kwargs: Any) -> Recall:
        result = self._causal.recall_detailed(query, occasion_id=occasion_id, **kwargs)
        if self._delete_harmful and result.withdrawn and self.adapter.can_delete:
            for item_id in result.withdrawn:
                memory_sources.withdraw_key(self.root, self.source_key, item_id)
                try:
                    self.adapter.delete(item_id)
                except Exception:  # noqa: BLE001 - it stays blocklisted; recall must not fail
                    pass
        return result

    def record_outcome(self, occasion_id: str, *, succeeded: bool) -> bool:
        return holdout_io.record_outcome(self.root, occasion_id, succeeded)

    def withdraw(self, item_id: str, *, delete_at_source: bool = False) -> None:
        memory_sources.withdraw_key(self.root, self.source_key, item_id)
        if delete_at_source:
            if not self.adapter.can_delete:
                raise NotImplementedError(f"{self.adapter.name} cannot delete here; it stays blocklisted")
            self.adapter.delete(item_id)

    def reinstate(self, item_id: str) -> bool:
        return memory_sources.reinstate_key(self.root, self.source_key, item_id)
