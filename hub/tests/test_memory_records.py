import uuid

import pytest
from sqlalchemy import text

from commontrace.memory_record import MemoryRecord
from hub.db import session_scope
from hub.memory_records import PostgresJournal
from hub.models import Organization


@pytest.mark.asyncio
async def test_records_scope_versions_and_derived_erasure(session_factory):
    orgs = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with session_scope(session_factory) as session:
        session.add_all([Organization(id=identity, name=identity) for identity in orgs])
    a, b = [PostgresJournal(session_factory, identity) for identity in orgs]
    await a.put(MemoryRecord("source", "fact", 1, {"text": "pytest parser"}, scope={"user": "alice"}))
    await a.put(
        MemoryRecord(
            "derived",
            "observation",
            1,
            {"text": "pytest validation"},
            scope={"user": "alice"},
            relations={"derives": ["source"]},
        )
    )
    assert len(await a.search("pytest", scope={"user": "alice"})) == 2
    assert not await a.search("pytest", scope={"user": "bob"})
    assert not await b.search("pytest", scope={"user": "alice"})
    with pytest.raises(ValueError):
        await a.put(MemoryRecord("source", "fact", 2, {"text": "cross-scope"}, scope={"user": "bob"}))
    assert set((await a.forget("source", reason="erasure"))["deleted"]) == {"source", "derived"}
    assert not await a.search("pytest", scope={"user": "alice"})
    async with session_scope(session_factory, org_id=orgs[0]) as session:
        assert not (await session.execute(text("SELECT id FROM memory_record_events"))).all()


@pytest.mark.asyncio
async def test_record_rls_denies_unscoped_nonowner_reads(session_factory):
    from hub.alembic.versions.d6e8a1b2c3f4_memory_record_events import _NEW_TABLES, _POLICY

    orgs = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with session_scope(session_factory) as session:
        session.add_all([Organization(id=value, name=value) for value in orgs])
    for org in orgs:
        await PostgresJournal(session_factory, org).put(MemoryRecord(org, "fact", 1, {"text": "rls evidence"}))
    try:
        async with session_scope(session_factory) as session:
            await session.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname="
                    "'commontrace_record_reader') THEN CREATE ROLE commontrace_record_reader NOLOGIN; END IF; END $$"
                )
            )
            await session.execute(text("GRANT USAGE ON SCHEMA public TO commontrace_record_reader"))
            for table in _NEW_TABLES:
                await session.execute(text("GRANT SELECT ON " + table + " TO commontrace_record_reader"))
                await session.execute(text("ALTER TABLE " + table + " ENABLE ROW LEVEL SECURITY"))
                await session.execute(text("ALTER TABLE " + table + " FORCE ROW LEVEL SECURITY"))
                await session.execute(
                    text(
                        "CREATE POLICY org_isolation ON "
                        + table
                        + " USING ("
                        + _POLICY
                        + ") WITH CHECK ("
                        + _POLICY
                        + ")"
                    )
                )
        for org in [None, *orgs]:
            async with session_scope(session_factory, org_id=org) as session:
                await session.execute(text("SET LOCAL ROLE commontrace_record_reader"))
                rows = (await session.execute(text("SELECT org_id::text FROM memory_records"))).scalars().all()
                assert rows == ([] if org is None else [org])
    finally:
        async with session_scope(session_factory) as session:
            for table in _NEW_TABLES:
                await session.execute(text("DROP POLICY IF EXISTS org_isolation ON " + table))
                await session.execute(text("ALTER TABLE " + table + " NO FORCE ROW LEVEL SECURITY"))
                await session.execute(text("ALTER TABLE " + table + " DISABLE ROW LEVEL SECURITY"))


@pytest.mark.asyncio
async def test_postgres_facet_versions_refresh_and_retract(session_factory):
    from dataclasses import replace

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    source = await store.put(
        MemoryRecord(
            "facet-source",
            "fact",
            1,
            {"text": "Parser validates 2 fields", "entity": "Parser", "facet": "validation"},
            scope={"user": "a"},
        )
    )
    records = await store.search("Parser", scope={"user": "a"})
    observation = next(record for record in records if record.kind == "observation")
    assert observation.provenance["source_versions"] == {"facet-source": 1}
    await store.put(replace(source, version=2, payload={**source.payload, "text": "Parser validates 3 fields"}))
    records = await store.search("Parser", scope={"user": "a"})
    updated = next(record for record in records if record.kind == "observation")
    assert updated.id == observation.id and updated.version == 2
    assert updated.payload["text"] == "Parser validates 3 fields"
    assert not await store.search("Parser", scope={"user": "b"})
    await store.put(replace(source, version=3, status="retired"))
    assert not await store.search("Parser", scope={"user": "a"})


@pytest.mark.asyncio
async def test_failed_vector_erasure_is_durable_and_cannot_delete_reused_id(session_factory):
    class Vector:
        def __init__(self, org):
            self.tenant, self.fail, self.deleted = org, True, []

        async def delete(self, ids):
            if self.fail:
                raise OSError("vector service offline")
            self.deleted.extend(ids)

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    vector = Vector(org)
    store = PostgresJournal(session_factory, org, vector_index=vector)
    record = MemoryRecord("erase-vector", "fact", 1, {"text": "private vector content"})
    await store.put(record)
    result = await store.forget(record.id, reason="owner erasure")
    assert result["vector_cleanup"]["pending"]
    assert not await store.search("private", scope={})
    with pytest.raises(ValueError, match="pending vector erasure"):
        await store.put(record)
    restarted = PostgresJournal(session_factory, org, vector_index=vector)
    vector.fail = False
    assert await restarted.retry_vector_erasures() == {"completed": 1, "pending": False}
    assert vector.deleted == [record.id]
    await restarted.put(record)
