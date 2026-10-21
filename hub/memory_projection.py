"""Durable hosted dense candidates using the existing fenced snapshot engine.

Canonical writes and this bounded worker share one tenant advisory lock. The
external publication precedes the transactional watermark; a deterministic
generation makes an interrupted acknowledgement recoverable without embedding.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, text

from hub.db import session_scope
from hub.models import MemoryChangeClock, MemoryChangeEvent, MemoryEmbeddingCache, MemoryProjectionState
from unimem.vector_snapshots import SnapshotConflict
from unimem.vector_store import MAX_DIMENSION, VectorHit, VectorRecord, _keys, _label, _limit, _vector


@dataclass(frozen=True)
class ProjectionIdentity:
    namespace: str
    model: str
    dimension: int

    def __post_init__(self):
        _label(self.namespace, "projection namespace")
        _label(self.model, "projection model")
        if len(self.namespace) > 256 or len(self.model) > 256:
            raise ValueError("hosted projection labels must contain at most 256 characters")
        if type(self.dimension) is not int or not 1 <= self.dimension <= MAX_DIMENSION:
            raise ValueError("projection dimension must be in 1..2000")

    @property
    def key(self) -> str:
        value = json.dumps([self.namespace, self.model, self.dimension], separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()


class Embedder(Protocol):
    identity: ProjectionIdentity

    async def encode_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def encode_query(self, text: str) -> list[float]: ...


class HostedProjection:
    def __init__(self, factory, org_id: str, *, index, embedder: Embedder):
        identity = embedder.identity
        if not isinstance(identity, ProjectionIdentity):
            raise ValueError("embedder must expose a projection identity")
        if not org_id or index.tenant != org_id:
            raise ValueError("projection owner must match authenticated organization")
        if (index.namespace, index.model, index.dimension) != (
            identity.namespace, identity.model, identity.dimension
        ):
            raise ValueError("vector and embedder projection identity must match")
        self.factory, self.org_id, self.index, self.embedder = factory, org_id, index, embedder
        self.identity = identity

    def _generation(self, source: str, cursor: int) -> str:
        value = json.dumps([source, self.identity.key, cursor], separators=(",", ":"))
        return "hub-records:" + hashlib.sha256(value.encode()).hexdigest()

    async def _lock(self, session):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 61923))"),
            {"owner": "unimem-memory:" + self.org_id},
        )

    async def _state(self, session):
        key = (self.org_id, self.identity.key)
        state = await session.get(MemoryProjectionState, key)
        if state is None:
            state = MemoryProjectionState(
                org_id=self.org_id, projection=self.identity.key, namespace=self.identity.namespace,
                model=self.identity.model, dimension=self.identity.dimension, cursor=0, generation=None,
            )
            session.add(state)
        elif (state.namespace, state.model, state.dimension) != (
            self.identity.namespace, self.identity.model, self.identity.dimension
        ):
            raise ValueError("stored projection identity mismatch")
        return state

    async def drain(self, *, limit: int = 128) -> dict:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("projection batch must be in 1..10000")
        async with session_scope(self.factory, org_id=self.org_id) as session:
            await self._lock(session)
            clock = await session.get(MemoryChangeClock, self.org_id)
            if clock is None:
                return {"processed": 0, "through": 0}
            await self.index.bind_source("hub-records:" + clock.source_id)
            state = await self._state(session)
            head = await self.index.snapshots.head()
            if head.generation is not None:
                if (head.revision > clock.sequence
                        or head.generation != self._generation(clock.source_id, head.revision)):
                    raise SnapshotConflict("external snapshot does not match canonical change stream")
                # Both recovery after publication/acknowledgement failure and a
                # restored older external index resume at its validated revision.
                state.cursor, state.generation = head.revision, head.generation
            else:
                # Never trust a durable DB cursor when the external index is lost.
                state.cursor, state.generation = 0, None
            events = (await session.execute(
                select(MemoryChangeEvent).where(
                    MemoryChangeEvent.org_id == self.org_id, MemoryChangeEvent.sequence > state.cursor,
                ).order_by(MemoryChangeEvent.sequence).limit(limit)
            )).scalars().all()
            if not events:
                return {"processed": 0, "through": state.cursor}
            through = events[-1].sequence
            generation = self._generation(clock.source_id, through)
            latest = {event.record_id: event for event in events}
            records, deleted, misses = [], [], []
            for event in latest.values():
                body = event.record
                payload = body.get("payload", {}) if body else {}
                content = payload.get("text")
                if (event.operation != "upsert" or not body or body.get("status") != "active"
                        or body.get("is_latest") is not True or not isinstance(content, str) or not content.strip()):
                    deleted.append(event.record_id)
                    continue
                checksum = hashlib.sha256(content.encode()).hexdigest()
                cached = await session.get(
                    MemoryEmbeddingCache, (self.org_id, self.identity.key, event.record_id, checksum)
                )
                if cached is None:
                    misses.append((event.record_id, checksum, content))
                else:
                    records.append(VectorRecord(event.record_id, _vector(cached.vector, self.identity.dimension),
                                                checksum=checksum))
            if misses:
                vectors = await self.embedder.encode_documents([content for _, _, content in misses])
                if len(vectors) != len(misses):
                    raise ValueError("embedding batch cardinality mismatch")
                for (record_id, checksum, _), vector in zip(misses, vectors):
                    vector = list(_vector(vector, self.identity.dimension))
                    session.add(MemoryEmbeddingCache(
                        org_id=self.org_id, projection=self.identity.key, record_id=record_id,
                        checksum=checksum, vector=vector,
                    ))
                    records.append(VectorRecord(record_id, vector, checksum=checksum))
            # Erase even if a later event in this batch legitimately reuses an
            # identity: its new vector must not preserve erased old intervals.
            hard_deleted = list(dict.fromkeys(event.record_id for event in events if event.operation == "delete"))
            if hard_deleted:
                # A tombstone alone preserves historical vectors for old pins.
                # Erasure must remove those copies before acknowledgement.
                await self.index.snapshots.erase(hard_deleted)
                await self.index.delete(hard_deleted)
            lease = await self.index.snapshots.begin(through, generation, full=head.generation is None,
                                                     lease_seconds=300)
            if lease is None:
                raise SnapshotConflict("external snapshot advanced outside the canonical owner lock")
            try:
                await self.index.snapshots.stage(lease, records, deleted)
                if not await self.index.snapshots.publish(lease):
                    raise SnapshotConflict("projection publication lost its fence")
            except BaseException:
                await asyncio.shield(self.index.snapshots.abort(lease))
                raise
            state.cursor, state.generation = through, generation
            return {"processed": len(events), "through": through}

    async def erase_records(self, identities) -> int:
        """Erase current/retained vectors without loading any embedding model.

        Canonical deletion has already committed its retained tombstones. This
        eager erasure does not acknowledge those events or advance a cursor.
        """
        keys = _keys(identities)
        if not keys:
            return 0
        async with session_scope(self.factory, org_id=self.org_id) as session:
            await self._lock(session)
            clock = await session.get(MemoryChangeClock, self.org_id)
            if clock is None:
                raise ValueError("canonical change source must exist before vector erasure")
            await self.index.bind_source("hub-records:" + clock.source_id)
            versions = await self.index.snapshots.erase(keys)
            legacy = await self.index.delete(keys)
            return versions + legacy

    async def candidates(self, query: str, *, top_k: int = 100,
                         allowed_ids: set[str] | None = None) -> tuple[int, list[VectorHit]]:
        _limit(top_k)
        lease = None
        try:
            async with session_scope(self.factory, org_id=self.org_id) as session:
                state = await session.get(MemoryProjectionState, (self.org_id, self.identity.key))
                clock = await session.get(MemoryChangeClock, self.org_id)
                if state is None or clock is None or state.generation is None:
                    return 0, []
                cursor, generation = state.cursor, state.generation
                if generation != self._generation(clock.source_id, cursor):
                    raise SnapshotConflict("canonical projection generation mismatch")
                await self.index.bind_source("hub-records:" + clock.source_id)
                # Pin this exact committed generation. Generic index search may
                # fall back to legacy upserts for a missing requested generation.
                lease = await self.index.snapshots.pin(generation, lease_seconds=300)
            vector = _vector(await self.embedder.encode_query(query), self.identity.dimension)
            if allowed_ids is None:
                hits = await self.index.snapshots.search(lease, vector, top_k=top_k)
            else:
                hits = await self.index.snapshots.search(lease, vector, top_k=top_k, allowed_ids=allowed_ids)
            return cursor, hits
        finally:
            if lease is not None:
                await asyncio.shield(self.index.snapshots.release(lease))
