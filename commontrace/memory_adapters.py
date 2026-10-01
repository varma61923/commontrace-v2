"""Causal measurement for memory held in Mem0, Letta, Zep, Claude Managed
Agents memory stores and AWS Bedrock AgentCore.

Each adapter turns one vendor client's own retrieval call into `Item(id,
text)`s; `MeasuredMemory` runs them through `CausalMemory`'s holdout, so
`commontrace experiment` reports a verdict per memory. Callers pass an
already-configured client -- this module imports no vendor SDK.

Written against the published source of mem0ai 2.2.1, letta-client
1.12.1, zep-cloud 3.30.0, anthropic 1.11.0 and bedrock-agentcore 1.24.0,
not against live accounts.

Harm withdrawal always stops the memory being handed out at recall time:
by itself when the store's harm policy is `withdraw` (the verdict decides,
see commontrace/measure.py), or by hand with `withdraw()`. Deleting it at the
source as well is opt-in (`delete_at_source=True`, or `delete_harmful=True`
for the automatic case), since it removes data from the customer's own store
-- and a deleted memory cannot be re-tested after a rewrite.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from commontrace import holdout_io, memory_sources, paths
from commontrace.measure import DEFAULT_CHECK_EVERY, CausalMemory, Recall


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    #: The vendor's own record, untouched.
    raw: Any = None


class Mem0Adapter:
    """`Memory` (open source) or `MemoryClient` (platform). Both return
    `{"results": [{"id", "memory", ...}]}` from `search`."""

    name = "mem0"
    can_delete = True

    def __init__(self, client: Any, **search_kwargs: Any) -> None:
        self.client = client
        self.search_kwargs = search_kwargs

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        response = self.client.search(query, **{**self.search_kwargs, **kwargs})
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
    """An agent's CORE memory blocks (the ones that sit in its context window), listed with
    `client.agents.blocks.list(agent_id)` as `letta-client` defines it.

    Archival passages are fetched on demand, so withholding is just not returning them.
    Core blocks are always in context, so they are measured the way a whole-store memory
    is: every block is eligible on every occasion, `query` is ignored, and the caller puts
    `render(items)` of what `recall` returned into the prompt instead of letting Letta
    compile the blocks. Detach/attach per session is NOT used: a block can be attached to
    several agents, so detaching it for one occasion would change every other session
    that shares it.

    A persona or safety block should not be a control; pass `pinned=adapter.block_ids("persona")`
    to `MeasuredMemory` so it is always delivered and never measured. Deleting a block is
    the agent owner's decision, so `can_delete` is False.
    """

    name = "letta-core"
    can_delete = False

    def __init__(self, client: Any, *, agent_id: str) -> None:
        self.client = client
        self.agent_id = agent_id
        self._labels: dict[str, str] = {}

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

    def delete(self, item_id: str) -> None:
        raise NotImplementedError("a core memory block is the agent owner's to remove")

    @staticmethod
    def render(items: Iterable[Item]) -> str:
        return "\n\n".join(
            f"<{getattr(i.raw, 'label', None) or i.id}>\n{i.text}\n</{getattr(i.raw, 'label', None) or i.id}>"
            for i in items)


class ZepAdapter:
    """Zep graph search. `scope="edges"` (the default) measures facts;
    nodes and episodes can be measured but not deleted through here."""

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
        return self.scope == "edges"

    def search(self, query: str, **kwargs: Any) -> list[Item]:
        response = self.client.graph.search(query=query, scope=self.scope, **self.target, **kwargs)
        field = self._TEXT_FIELD[self.scope]
        return [Item(r.uuid_, getattr(r, field, "") or "", r) for r in getattr(response, self.scope) or []]

    def delete(self, item_id: str) -> None:
        if not self.can_delete:
            raise NotImplementedError(f"deleting {self.scope} is not supported here")
        self.client.graph.edge.delete(item_id)


class ClaudeMemoryStoreAdapter:
    """A Claude Managed Agents memory store.

    The store has no search call and a session mounts it whole, so every
    memory is eligible on every occasion and `query` is ignored. Give the
    session `render(items)` of what `recall` returns instead of mounting the
    live store, or withheld memories reach the agent anyway.
    """

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
                continue  # a memory_prefix (directory) entry
            self._sha[memory.id] = memory.content_sha256
            items.append(Item(memory.id, memory.content or "", memory))
        return items

    def delete(self, item_id: str) -> None:
        # Only the exact version that was measured: if the memory changed
        # since, the server refuses rather than deleting the new text.
        kwargs: dict[str, Any] = {"memory_store_id": self.memory_store_id}
        if item_id in self._sha:
            kwargs["expected_content_sha256"] = self._sha[item_id]
        self.client.beta.memory_stores.memories.delete(item_id, **kwargs)

    @staticmethod
    def render(items: Iterable[Item]) -> str:
        return "\n\n".join(f"## {getattr(i.raw, 'path', i.id)}\n{i.text}" for i in items)


class AgentCoreAdapter:
    """Bedrock AgentCore long-term memory: `MemoryClient.retrieve_memories`,
    which returns `[{"memoryRecordId", "content": {"text"}, ...}]`."""

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
        self, adapter: Any, *, root: str | None = None, source_key: str | None = None,
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
            # A memory another system wrote is text this product never
            # admitted, so it is screened the way an injected lesson is.
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
                # Blocklisted first, so a failed delete still stops delivery.
                memory_sources.withdraw_key(self.root, self.source_key, item_id)
                try:
                    self.adapter.delete(item_id)
                except Exception:  # noqa: BLE001 - it stays blocklisted; recall must not fail
                    pass
        return result

    def record_outcome(self, occasion_id: str, *, succeeded: bool) -> bool:
        return holdout_io.record_outcome(self.root, occasion_id, succeeded)

    def withdraw(self, item_id: str, *, delete_at_source: bool = False) -> None:
        # Blocklist first: if the source delete then fails, recall still
        # stops handing the memory out.
        memory_sources.withdraw_key(self.root, self.source_key, item_id)
        if delete_at_source:
            if not self.adapter.can_delete:
                raise NotImplementedError(f"{self.adapter.name} cannot delete here; it stays blocklisted")
            self.adapter.delete(item_id)

    def reinstate(self, item_id: str) -> bool:
        return memory_sources.reinstate_key(self.root, self.source_key, item_id)
