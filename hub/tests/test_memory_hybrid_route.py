"""Actual Hub route with PostgreSQL records and real snapshot publication."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
import pytest_asyncio

from commontrace.fact_evidence import FactEvidence, claim_revision
from commontrace.hierarchical import AtomicFact
from commontrace.memory_record import MemoryRecord, atomic_fact_record
from commontrace.vector_store import PostgresVectorIndex, SQLiteVectorIndex
from hub.memory_records import PostgresJournal
from hub.tests.test_rest import _app, _client, _key, _org_with_key

pytestmark = pytest.mark.asyncio


def _fact(identity, statement, *, scopes=("user:alice",), evidence=(), bound=False, minimum=1):
    return AtomicFact(
        id=identity, statement=statement, category="general", scopes=list(scopes), confidence=0.9,
        confirmations=1, valid_from="2020-01-01T00:00:00Z", valid_until=None, source_traces=[],
        status="active", created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z",
        evidence=list(evidence), evidence_bound=bound, min_support=minimum, origin={},
    )


def _derived_fact(identity, statement, source, *, polarity="support"):
    receipt = FactEvidence("fact", source.id, claim_revision(source), polarity,
                           "2026-10-12T00:00:00Z")
    fact = _fact(identity, statement, evidence=[receipt], bound=True)
    return replace(fact, evidence_revision=claim_revision(fact))


class Embeddings:
    def __init__(self, identity):
        self.identity = identity
        self.documents = []

    async def encode_documents(self, texts):
        self.documents.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    async def encode_query(self, query):
        return [1.0, 0.0]


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def projections(session_factory, config, tmp_path, request):
    from hub.memory_projection import HostedProjection, ProjectionIdentity

    class Factory:
        def __init__(self):
            self.opened = []
            self.instances = {}

        @asynccontextmanager
        async def acquire(self, org):
            self.opened.append(org)
            if org not in self.instances:
                identity = ProjectionIdentity("memory-records", "fixture:embeddings@revision-1", 2)
                if request.param == "postgres":
                    from sqlalchemy import text

                    from hub.db import session_scope

                    async with session_scope(session_factory) as session:
                        if not (await session.execute(text(
                            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')"))).scalar_one():
                            pytest.skip("optional pgvector extension is not installed")
                    index = await PostgresVectorIndex.open(
                        config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1), tenant=org,
                        namespace=identity.namespace, model=identity.model, dimension=2, approximate=True, ef_search=64)
                else:
                    index = await SQLiteVectorIndex.open(str(tmp_path / (org + ".sqlite3")),
                        tenant=org, namespace=identity.namespace, model=identity.model, dimension=2)
                self.instances[org] = HostedProjection(session_factory, org, index=index,
                                                       embedder=Embeddings(identity))
            yield self.instances[org]

    factory = Factory()
    yield factory
    for projection in factory.instances.values():
        await projection.index.close()


async def test_route_publishes_and_retrieves_dense_only_current_record(session_factory, config, projections):
    org, key = await _org_with_key(session_factory)
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections
    async with _client(app) as client:
        response = await client.post("/v1/memory/record-put", headers=_key(key), json={
            "record": MemoryRecord("banana", "fact", 1, {"text": "Banana supply is stable."}).to_dict(),
            "context": ["user:alice"],
        })
        assert response.status_code == 200
        response = await client.post("/v1/memory/record-search", headers=_key(key), json={
            "query": "orchard", "context": ["user:alice"],
        })
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["results"]] == ["banana"]
    assert projections.opened and set(projections.opened) == {org}
    projection = projections.instances[org]
    assert projection.embedder.documents == ["Banana supply is stable."]


async def test_hybrid_route_rechecks_tenant_and_user_scope(session_factory, config, projections):
    org, key = await _org_with_key(session_factory)
    other, other_key = await _org_with_key(session_factory)
    for owner in (org, other):
        store = PostgresJournal(session_factory, owner)
        for user in ("alice", "bob"):
            await store.put(MemoryRecord(user, "fact", 1, {"text": owner + " banana"}, scope={"user": user}))
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections
    async with _client(app) as client:
        for owner, raw in ((org, key), (other, other_key)):
            response = await client.post("/v1/memory/record-search", headers=_key(raw),
                json={"query": "orchard", "context": ["user:alice"]})
            assert response.status_code == 200
            rows = response.json()["results"]
            assert len(rows) == 1 and rows[0]["id"] == "alice"
            assert rows[0]["scope"] == {"tenant": owner, "user": "alice"}
    assert set(projections.opened) == {org, other}


async def test_dense_hit_cannot_score_a_revision_written_after_its_snapshot(session_factory, config, projections):
    org, key = await _org_with_key(session_factory)
    store = PostgresJournal(session_factory, org)
    original = await store.put(MemoryRecord("banana", "fact", 1, {"text": "Banana supply"}))
    async with projections.acquire(org) as projection:
        await projection.drain()
        candidates = projection.candidates

        async def edit_after_lookup(query, *, top_k):
            result = await candidates(query, top_k=top_k)
            await store.put(replace(original, version=2, payload={"text": "New database policy"}))
            return result

        projection.candidates = edit_after_lookup
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections
    async with _client(app) as client:
        response = await client.post("/v1/memory/record-search", headers=_key(key), json={"query": "orchard"})
        assert response.status_code == 200
        assert response.json()["results"] == []


async def test_optional_projection_outage_keeps_lexical_results(session_factory, config):
    org, key = await _org_with_key(session_factory)
    await PostgresJournal(session_factory, org).put(
        MemoryRecord("lexical", "fact", 1, {"text": "Orchard operations"}))

    class Unavailable:
        @asynccontextmanager
        async def acquire(self, org):
            raise RuntimeError("provider outage with private diagnostic")
            yield

    app = _app(session_factory, config)
    app.state.memory_projection_factory = Unavailable()
    async with _client(app) as client:
        response = await client.post("/v1/memory/record-search", headers=_key(key), json={"query": "orchard"})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["results"]] == ["lexical"]
        assert "private diagnostic" not in response.text


async def test_hosted_atomic_fact_rechecks_bound_revision(session_factory, config, projections):
    org, key = await _org_with_key(session_factory)
    store = PostgresJournal(session_factory, org)
    source = _fact("source", "Ada works on orbital navigation.")
    await store.put(atomic_fact_record(source, 1, tenant=org))
    derived = _derived_fact("derived", "Ada works in the orchard lab.", source)
    await store.put(atomic_fact_record(derived, 1, tenant=org))
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections

    async with _client(app) as client:
        response = await client.post("/v1/memory/record-search", headers=_key(key),
                                     json={"query": "unicorn", "context": ["user:alice"]})
        assert response.status_code == 200
        assert "derived" in {row["id"] for row in response.json()["results"]}

        corrected = replace(source, statement="Ada works on deep sea mapping.")
        await store.put(atomic_fact_record(corrected, 2, tenant=org))
        response = await client.post("/v1/memory/record-search", headers=_key(key),
                                     json={"query": "unicorn", "context": ["user:alice"]})
        assert response.status_code == 200
        assert "derived" not in {row["id"] for row in response.json()["results"]}


async def test_hosted_atomic_fact_rejects_foreign_fact_and_unmapped_lesson_sources(
    session_factory, config, projections
):
    org, key = await _org_with_key(session_factory)
    store = PostgresJournal(session_factory, org)
    foreign = _fact("foreign", "Bob works on orbital navigation.", scopes=("user:bob",))
    await store.put(atomic_fact_record(foreign, 1, tenant=org))
    lesson_receipt = FactEvidence("lesson", "local-runbook", "b" * 64, "support",
                                  "2026-10-12T00:00:00Z")
    lesson_derived = _fact("lesson-derived", "Ada works in the orchard lab.",
                           evidence=[lesson_receipt], bound=True)
    lesson_derived = replace(lesson_derived, evidence_revision=claim_revision(lesson_derived))
    await store.put(atomic_fact_record(lesson_derived, 1, tenant=org))
    foreign_derived = _derived_fact("foreign-derived", "Ada works in the orchard lab.", foreign)
    await store.put(atomic_fact_record(foreign_derived, 1, tenant=org))
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections

    async with _client(app) as client:
        response = await client.post("/v1/memory/record-search", headers=_key(key),
                                     json={"query": "unicorn", "context": ["user:alice"]})
    assert response.status_code == 200
    result_ids = {row["id"] for row in response.json()["results"]}
    assert "lesson-derived" not in result_ids
    assert "foreign-derived" not in result_ids


async def test_hosted_refutation_hides_fact_and_forget_erases_its_dependents(
    session_factory, config, projections
):
    org, key = await _org_with_key(session_factory)
    store = PostgresJournal(session_factory, org, projection_factory=projections)
    source = _fact("source", "Ada works on orbital navigation.")
    await store.put(atomic_fact_record(source, 1, tenant=org))
    refuted = _derived_fact("refuted", "Ada works in the orchard lab.", source, polarity="refute")
    await store.put(atomic_fact_record(refuted, 1, tenant=org))
    app = _app(session_factory, config)
    app.state.memory_projection_factory = projections

    async with _client(app) as client:
        response = await client.post("/v1/memory/record-search", headers=_key(key),
                                     json={"query": "unicorn", "context": ["user:alice"]})
        assert response.status_code == 200
        assert "refuted" not in {row["id"] for row in response.json()["results"]}

        async with projections.acquire(org) as projection:
            head = await projection.index.snapshots.head()
            old_reader = await projection.index.snapshots.pin(head.generation)
        erased = await client.post("/v1/memory/record-forget", headers=_key(key),
                                   json={"id": "source", "reason": "owner erasure",
                                         "context": ["user:alice"]})
        assert erased.status_code == 200
        body = erased.json()
        assert body["deleted"] == ["refuted", "source"]
        assert body["vector_cleanup"]["pending"] is False
        async with projections.acquire(org) as projection:
            assert await projection.index.snapshots.search(old_reader, [1.0, 0.0], top_k=20) == []
            await projection.index.snapshots.release(old_reader)

        response = await client.post("/v1/memory/record-search", headers=_key(key),
                                     json={"query": "unicorn", "context": ["user:alice"]})
        assert response.status_code == 200
        assert response.json()["results"] == []
