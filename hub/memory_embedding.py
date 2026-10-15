"""Server-owned, revision-bound embeddings and bounded hosted projection pools."""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text

from commontrace import configuration, embeddings
from commontrace.vector_store import PostgresVectorIndex
from hub.db import session_scope
from hub.memory_projection import HostedProjection, ProjectionIdentity
from hub.models import MemoryChangeClock, MemoryProjectionState


class LocalEmbedder:
    """Use the existing provider's prefixes and normalization with its actual revision."""

    def __init__(self, provider, identity):
        self.provider, self.identity = provider, identity
        self._lock = asyncio.Lock()

    @classmethod
    async def load(cls, tag):
        provider = embeddings.provider(tag, local_files_only=True)
        if provider.spec.provider not in ("local", "fastembed"):
            raise embeddings.EmbeddingError(
                "hosted providers require a server-supplied revision-bound projection factory")
        model = await asyncio.to_thread(provider._load)
        if provider.spec.provider == "fastembed":
            revision, dimension = await asyncio.to_thread(provider.projection_identity)
            model_identity = provider.spec.tag + ":" + revision + ":adapter-v1"
        else:
            revision = None
            for module in getattr(model, "_modules", {}).values():
                config = getattr(getattr(module, "auto_model", None), "config", None)
                commit = getattr(config, "_commit_hash", None)
                if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40,64}", commit):
                    revision = commit
                    break
            if revision is None:
                raise embeddings.EmbeddingError(
                    "local embedding model has no immutable revision; supply a revision-bound factory")
            dimension = model.get_sentence_embedding_dimension()
            model_identity = provider.spec.tag + ":hf-" + revision + ":adapter-v1"
        identity = ProjectionIdentity(
            "memory-records", model_identity, dimension)
        return cls(provider, identity)

    async def encode_documents(self, texts):
        async with self._lock:
            return await asyncio.to_thread(self.provider.embed, texts, query=False)

    async def encode_query(self, query):
        async with self._lock:
            return (await asyncio.to_thread(self.provider.embed, [query], query=True))[0]


@dataclass
class _Entry:
    projection: HostedProjection
    users: int = 0


class ProjectionFactory:
    """Bound tenant pools, reuse weights, and close resources with the Hub lifespan.

    Requests provide only authenticated tenancy. Provider/model selection comes
    from the existing server configuration, never from a memory API payload.
    """

    def __init__(self, session_factory, database_url, *, capacity=32):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("projection pool capacity must be in 1..256")
        self.factory, self.database_url, self.capacity = session_factory, database_url, capacity
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._embedder = None
        self._retry_after = 0.0
        self._closed = False

    async def _prepare(self):
        if self._embedder is not None:
            return self._embedder
        if time.monotonic() < self._retry_after:
            raise embeddings.EmbeddingError("optional hosted projection is temporarily unavailable")
        try:
            # Check infrastructure before loading/downloading any optional weights.
            async with session_scope(self.factory) as session:
                installed = (await session.execute(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"))).scalar_one()
            if not installed:
                raise embeddings.EmbeddingError("pgvector extension is unavailable")
            tag = configuration.getenv("COMMONTRACE_FACT_EMBEDDER", "bge-small")
            if tag in ("none", "off"):
                raise embeddings.EmbeddingError("dense retrieval is disabled in server configuration")
            self._embedder = await LocalEmbedder.load(tag)
            return self._embedder
        except Exception:
            self._retry_after = time.monotonic() + 60.0
            raise

    async def _open(self, org, identity):
        return await PostgresVectorIndex.open(self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1),
            tenant=org, namespace=identity.namespace, model=identity.model, dimension=identity.dimension,
            min_size=0, max_size=2, approximate=True, ef_search=64)

    @asynccontextmanager
    async def acquire(self, org):
        async with self._lock:
            if self._closed:
                raise RuntimeError("projection factory is closed")
            entry = self._entries.get(org)
            if entry is None:
                if len(self._entries) >= self.capacity:
                    idle = next((key for key, value in self._entries.items() if value.users == 0), None)
                    if idle is None:
                        raise RuntimeError("all projection pools are busy")
                    await self._entries.pop(idle).projection.index.close()
                embedder = await self._prepare()
                index = await self._open(org, embedder.identity)
                try:
                    entry = _Entry(HostedProjection(self.factory, org, index=index, embedder=embedder))
                except BaseException:
                    await index.close()
                    raise
                self._entries[org] = entry
            self._entries.move_to_end(org)
            entry.users += 1
        try:
            yield entry.projection
        finally:
            async with self._lock:
                entry.users -= 1

    async def erase_records(self, org, identities):
        """Erase every registered model's vectors without loading its model weights.

        Retained canonical tombstones also allow an unavailable projection to
        perform the same erasure when it next becomes reachable.
        """
        async with session_scope(self.factory, org_id=org) as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 61923))"),
                                  {"owner": "commontrace-memory:" + org})
            states = (await session.execute(select(MemoryProjectionState).where(
                MemoryProjectionState.org_id == org))).scalars().all()
            clock = await session.get(MemoryChangeClock, org)
            if clock is None:
                return {"completed": 0, "pending": False}
            for state in states:
                identity = ProjectionIdentity(state.namespace, state.model, state.dimension)
                index = await self._open(org, identity)
                try:
                    await index.bind_source("hub-records:" + clock.source_id)
                    await index.snapshots.erase(identities)
                    await index.delete(identities)
                finally:
                    await index.close()
            return {"completed": len(states), "pending": False}

    async def close(self):
        async with self._lock:
            self._closed = True
            entries, self._entries = list(self._entries.values()), OrderedDict()
        results = await asyncio.gather(*(entry.projection.index.close() for entry in entries), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise RuntimeError("a hosted vector pool failed to close") from errors[0]
