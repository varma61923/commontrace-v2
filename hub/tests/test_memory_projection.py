"""Real canonical transactions and fenced vector snapshots, without model downloads."""
import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete

from commontrace.memory_record import MemoryRecord
from commontrace.vector_snapshots import SnapshotConflict
from commontrace.vector_store import PostgresVectorIndex, SQLiteVectorIndex, VectorRecord
from hub.db import session_scope
from hub.memory_projection import HostedProjection, ProjectionIdentity
from hub.models import (
    MemoryChangeClock,
    MemoryChangeEvent,
    MemoryEmbeddingCache,
    MemoryRecordProjection,
    Organization,
)


def test_projection_identity_and_owner_are_pinned():
    identity = ProjectionIdentity("memory-records", "provider/revision", 2)
    assert len(identity.key) == 64
    assert identity.key != ProjectionIdentity("memory-records", "provider/other", 2).key
    index = SimpleNamespace(tenant="tenant", namespace=identity.namespace, model=identity.model, dimension=2)
    embedder = SimpleNamespace(identity=identity)
    HostedProjection(object(), "tenant", index=index, embedder=embedder)
    for field, value in (("tenant", "other"), ("model", "other"), ("dimension", 3), ("namespace", "other")):
        wrong = SimpleNamespace(**{**vars(index), field: value})
        with pytest.raises(ValueError, match="identity|owner"):
            HostedProjection(object(), "tenant", index=wrong, embedder=embedder)
    for dimension in (True, 0, 2001):
        with pytest.raises(ValueError):
            ProjectionIdentity("records", "model", dimension)
    with pytest.raises(ValueError, match="256"):
        ProjectionIdentity("records", "m" * 257, 2)


class CountingEmbedder:
    identity = ProjectionIdentity("memory-records", "test/provider-revision-v1", 2)

    def __init__(self):
        self.documents = []
        self.queries = []
        self.entered = self.resume = None

    async def encode_documents(self, texts):
        self.documents.extend(texts)
        if self.entered:
            self.entered.set()
            await self.resume.wait()
        return [[1.0, 0.0] if "apple" in text else [0.0, 1.0] for text in texts]

    async def encode_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


@pytest_asyncio.fixture(params=("sqlite", "postgres"))
async def projection(request, session_factory, tmp_path):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    embedder = CountingEmbedder()
    if request.param == "postgres":
        dsn = os.environ.get("COMMONTRACE_TEST_PGVECTOR_DSN")
        if not dsn:
            pytest.skip("set COMMONTRACE_TEST_PGVECTOR_DSN for hosted PostgreSQL vector projection checks")
        index = await PostgresVectorIndex.open(
            dsn, tenant=org, namespace=embedder.identity.namespace, model=embedder.identity.model,
            dimension=embedder.identity.dimension, approximate=True,
        )
    else:
        index = await SQLiteVectorIndex.open(
            str(tmp_path / "index.sqlite3"), tenant=org, namespace=embedder.identity.namespace,
            model=embedder.identity.model, dimension=embedder.identity.dimension,
        )
    worker = HostedProjection(session_factory, org, index=index, embedder=embedder)
    try:
        yield worker
    finally:
        if request.param == "postgres":
            async with index._pool.acquire() as connection, connection.transaction():
                for table in ("commontrace_vector_stages", "commontrace_vector_readers", "commontrace_vector_builds",
                              "commontrace_vector_snapshots", "commontrace_vector_versions", "commontrace_vector_heads",
                              "commontrace_vectors", "commontrace_vector_sources"):
                    await connection.execute(f"DELETE FROM {table} WHERE tenant=$1", org)
        await index.close()


async def append(worker, identity, content=None, *, version=1, status="active"):
    """Seed the agreed canonical event contract under the production owner lock."""
    async with session_scope(worker.factory, org_id=worker.org_id) as session:
        await worker._lock(session)
        clock = await session.get(MemoryChangeClock, worker.org_id)
        if clock is None:
            clock = MemoryChangeClock(org_id=worker.org_id, sequence=0, source_id=str(uuid.uuid4()))
            session.add(clock)
        clock.sequence += 1
        record = MemoryRecord(identity, "fact", version, {"text": content or ""}, status=status,
                              scope={"tenant": worker.org_id}).to_dict() if content is not None else None
        session.add(MemoryChangeEvent(
            org_id=worker.org_id, sequence=clock.sequence, record_id=identity, version=version,
            operation="upsert" if record else "delete", record=record,
        ))
        old = await session.get(MemoryRecordProjection, (worker.org_id, identity))
        if record:
            if old:
                old.record, old.version, old.change_seq = record, version, clock.sequence
            else:
                session.add(MemoryRecordProjection(org_id=worker.org_id, id=identity, version=version,
                            record=record, scope=record["scope"], change_seq=clock.sequence))
        elif old:
            await session.delete(old)
            await session.execute(delete(MemoryEmbeddingCache).where(
                MemoryEmbeddingCache.org_id == worker.org_id, MemoryEmbeddingCache.record_id == identity,
            ))
        return clock.sequence


@pytest.mark.asyncio
async def test_bounded_incremental_publication_cache_restart_and_rebuild(projection, tmp_path):
    worker = projection
    await append(worker, "a", "apple")
    await append(worker, "b", "banana")
    assert await worker.drain(limit=1) == {"processed": 1, "through": 1}
    cursor, hits = await worker.candidates("query")
    assert cursor == 1 and [hit.key for hit in hits] == ["a"]
    assert await worker.drain() == {"processed": 1, "through": 2}
    await append(worker, "a", "apple", version=2)
    assert await worker.drain() == {"processed": 1, "through": 3}
    assert worker.embedder.documents == ["apple", "banana"]
    restarted = HostedProjection(worker.factory, worker.org_id, index=worker.index, embedder=CountingEmbedder())
    assert await restarted.drain() == {"processed": 0, "through": 3}
    assert restarted.embedder.documents == []
    index = await SQLiteVectorIndex.open(
        str(tmp_path / "lost-index-replacement.sqlite3"), tenant=worker.org_id,
        namespace=worker.identity.namespace, model=worker.identity.model, dimension=2,
    )
    try:
        rebuilt = HostedProjection(worker.factory, worker.org_id, index=index, embedder=CountingEmbedder())
        assert await rebuilt.drain(limit=1) == {"processed": 1, "through": 1}
        assert await rebuilt.drain() == {"processed": 2, "through": 3}
        assert rebuilt.embedder.documents == []
        cursor, hits = await rebuilt.candidates("query")
        assert cursor == 3 and [hit.key for hit in hits] == ["a", "b"]
    finally:
        await index.close()


@pytest.mark.asyncio
async def test_published_but_unacknowledged_generation_recovers_without_embedding(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")
    snapshots = worker.index.snapshots
    publish = snapshots.publish

    async def published_then_failed(lease):
        assert await publish(lease)
        raise RuntimeError("canonical transaction acknowledgement interrupted")

    monkeypatch.setattr(snapshots, "publish", published_then_failed)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await worker.drain()
    assert (await snapshots.head()).revision == 1
    assert await worker.candidates("query") == (0, [])
    monkeypatch.setattr(snapshots, "publish", publish)
    worker.embedder.documents.clear()
    assert await worker.drain() == {"processed": 0, "through": 1}
    assert worker.embedder.documents == []
    assert [hit.key for hit in (await worker.candidates("query"))[1]] == ["a"]


@pytest.mark.asyncio
async def test_failed_and_cancelled_publication_never_acknowledge_private_stage(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")
    snapshots = worker.index.snapshots
    stage = snapshots.stage

    async def stage_cancelled(*args):
        await stage(*args)
        raise asyncio.CancelledError()

    monkeypatch.setattr(snapshots, "stage", stage_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await worker.drain()
    assert (await snapshots.head()).generation is None
    assert await worker.candidates("query") == (0, [])
    monkeypatch.setattr(snapshots, "stage", stage)
    publish = snapshots.publish

    async def refused(_lease):
        return False

    monkeypatch.setattr(snapshots, "publish", refused)
    with pytest.raises(SnapshotConflict):
        await worker.drain()
    assert (await snapshots.head()).generation is None
    monkeypatch.setattr(snapshots, "publish", publish)
    assert (await worker.drain())["through"] == 1


@pytest.mark.asyncio
async def test_delete_serializes_with_embedding_and_canonical_watermark(projection):
    worker = projection
    await append(worker, "a", "apple")
    worker.embedder.entered, worker.embedder.resume = asyncio.Event(), asyncio.Event()
    drain = asyncio.create_task(worker.drain())
    await asyncio.wait_for(worker.embedder.entered.wait(), timeout=5)
    removal = asyncio.create_task(append(worker, "a", version=2))
    # The delete cannot acquire the SAME owner lock until publication finishes.
    await asyncio.sleep(0.05)
    assert not removal.done()
    worker.embedder.resume.set()
    assert (await asyncio.wait_for(drain, timeout=5))["through"] == 1
    assert await asyncio.wait_for(removal, timeout=5) == 2
    # Raw old candidates exist; the journal must reject rows newer than this
    # returned cursor and absent canonical rows (tested by journal integration).
    assert (await worker.candidates("query"))[0] == 1
    assert await worker.drain() == {"processed": 1, "through": 2}
    assert await worker.candidates("query") == (2, [])


@pytest.mark.asyncio
async def test_exact_database_generation_does_not_follow_unacknowledged_external_head(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    await append(worker, "b", "banana")
    publish = worker.index.snapshots.publish

    async def interrupted(lease):
        assert await publish(lease)
        raise RuntimeError("interrupt")

    monkeypatch.setattr(worker.index.snapshots, "publish", interrupted)
    with pytest.raises(RuntimeError):
        await worker.drain()
    assert (await worker.index.snapshots.head()).revision == 2
    cursor, hits = await worker.candidates("query")
    assert cursor == 1 and [hit.key for hit in hits] == ["a"]


@pytest.mark.asyncio
async def test_foreign_snapshot_head_and_source_are_rejected(projection):
    worker = projection
    await append(worker, "a", "apple")
    lease = await worker.index.snapshots.begin(1, "foreign")
    await worker.index.snapshots.stage(lease, [VectorRecord("unsafe", [1.0, 0.0])])
    assert await worker.index.snapshots.publish(lease)
    with pytest.raises(SnapshotConflict, match="canonical"):
        await worker.drain()


@pytest.mark.asyncio
async def test_model_revision_gets_independent_cursor_and_embedding_cache(projection):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    other_embedder = CountingEmbedder()
    other_embedder.identity = ProjectionIdentity("memory-records", "test/provider-revision-v2", 2)
    # Same physical backend, independent model-scoped snapshots.
    other_index = await SQLiteVectorIndex.open(
        ":memory:", tenant=worker.org_id, namespace=other_embedder.identity.namespace,
        model=other_embedder.identity.model, dimension=2,
    )
    try:
        other = HostedProjection(worker.factory, worker.org_id, index=other_index, embedder=other_embedder)
        assert (await other.drain())["processed"] == 1
        assert other_embedder.documents == ["apple"]
        assert worker.embedder.documents == ["apple"]
        assert (await worker.drain())["processed"] == 0
    finally:
        await other_index.close()


@pytest.mark.asyncio
async def test_source_binding_rejects_another_canonical_store(projection):
    worker = projection
    await append(worker, "a", "apple")
    await worker.index.bind_source("hub-records:foreign-source")
    with pytest.raises(ValueError, match="source store"):
        await worker.drain()
    assert worker.embedder.documents == []


@pytest.mark.asyncio
async def test_missing_exact_generation_never_reads_legacy_vectors(projection, tmp_path):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    absent = await SQLiteVectorIndex.open(
        str(tmp_path / "missing.sqlite3"), tenant=worker.org_id, namespace=worker.identity.namespace,
        model=worker.identity.model, dimension=2,
    )
    try:
        await absent.upsert([VectorRecord("legacy-unsafe", [1.0, 0.0])])
        missing = HostedProjection(worker.factory, worker.org_id, index=absent, embedder=worker.embedder)
        with pytest.raises(SnapshotConflict, match="committed"):
            await missing.candidates("query")
    finally:
        await absent.close()


@pytest.mark.asyncio
async def test_inactive_update_removes_candidates_without_embedding(projection):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    await append(worker, "a", "apple", version=2, status="quarantined")
    assert (await worker.drain())["through"] == 2
    assert await worker.candidates("query") == (2, [])
    assert worker.embedder.documents == ["apple"]


@pytest.mark.asyncio
async def test_malformed_embedding_batch_leaves_stream_unacknowledged(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")

    async def malformed(_texts):
        return [[float("nan"), 1.0]]

    monkeypatch.setattr(worker.embedder, "encode_documents", malformed)
    with pytest.raises(ValueError, match="finite"):
        await worker.drain()
    assert (await worker.index.snapshots.head()).generation is None
    assert await worker.candidates("query") == (0, [])


@pytest.mark.asyncio
async def test_query_provider_failure_releases_pinned_reader(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    released = []
    release = worker.index.snapshots.release

    async def tracked(lease):
        released.append(lease)
        await release(lease)

    async def unavailable(_query):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(worker.index.snapshots, "release", tracked)
    monkeypatch.setattr(worker.embedder, "encode_query", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        await worker.candidates("query")
    assert len(released) == 1


@pytest.mark.asyncio
async def test_deleted_record_redacted_history_rebuild_cannot_reembed_erased_text(projection, tmp_path):
    from hub.memory_vector_events import redact_changes

    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    await append(worker, "a", version=2)
    async with session_scope(worker.factory, org_id=worker.org_id) as session:
        await worker._lock(session)
        await redact_changes(session, worker.org_id, ["a"])
    rebuilt_index = await SQLiteVectorIndex.open(
        str(tmp_path / "erased-rebuild.sqlite3"), tenant=worker.org_id, namespace=worker.identity.namespace,
        model=worker.identity.model, dimension=2,
    )
    try:
        embedder = CountingEmbedder()
        rebuilt = HostedProjection(worker.factory, worker.org_id, index=rebuilt_index, embedder=embedder)
        assert await rebuilt.drain(limit=1) == {"processed": 1, "through": 1}
        assert await rebuilt.candidates("query") == (1, [])
        assert await rebuilt.drain(limit=1) == {"processed": 1, "through": 2}
        assert await rebuilt.candidates("query") == (2, [])
        assert embedder.documents == []
    finally:
        await rebuilt_index.close()


@pytest.mark.asyncio
async def test_delete_erases_old_pins_before_ack_and_failure_keeps_event_pending(projection, monkeypatch):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    head = await worker.index.snapshots.head()
    old = await worker.index.snapshots.pin(head.generation)
    await append(worker, "a", version=2)
    erase = worker.index.snapshots.erase

    async def interrupted(_keys):
        raise RuntimeError("erasure unavailable")

    monkeypatch.setattr(worker.index.snapshots, "erase", interrupted)
    with pytest.raises(RuntimeError, match="erasure unavailable"):
        await worker.drain()
    assert (await worker.index.snapshots.head()).revision == 1
    monkeypatch.setattr(worker.index.snapshots, "erase", erase)
    assert (await worker.drain())["through"] == 2
    assert await worker.index.snapshots.search(old, [1.0, 0.0]) == []
    await worker.index.snapshots.release(old)


@pytest.mark.asyncio
async def test_eager_erasure_needs_no_embedding_and_does_not_advance_cursor(projection):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    head = await worker.index.snapshots.head()
    old = await worker.index.snapshots.pin(head.generation)
    await worker.index.upsert([VectorRecord("a", [1.0, 0.0])])
    await append(worker, "a", version=2)
    before = list(worker.embedder.documents)
    assert await worker.erase_records(["a"]) == 2
    assert worker.embedder.documents == before
    assert (await worker.index.snapshots.head()).revision == 1
    assert await worker.index.snapshots.search(old, [1.0, 0.0]) == []
    assert (await worker.drain())["through"] == 2
    await worker.index.snapshots.release(old)


@pytest.mark.asyncio
async def test_delete_then_identifier_reuse_in_same_batch_erases_old_intervals(projection):
    worker = projection
    await append(worker, "a", "apple")
    await worker.drain()
    old = await worker.index.snapshots.pin((await worker.index.snapshots.head()).generation)
    await append(worker, "a", version=2)
    await append(worker, "a", "banana", version=1)
    assert await worker.drain() == {"processed": 2, "through": 3}
    assert await worker.index.snapshots.search(old, [1.0, 0.0]) == []
    cursor, hits = await worker.candidates("query")
    assert cursor == 3 and [(hit.key, hit.score) for hit in hits] == [("a", 0.0)]
    await worker.index.snapshots.release(old)
