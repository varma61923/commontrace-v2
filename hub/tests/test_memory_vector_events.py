"""Ordered hosted replay and erasure tested against an owned Postgres database."""

import asyncio
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from unimem.memory_record import MemoryRecord
from hub.db import session_scope
from hub.memory_vector_events import append_change, redact_changes
from hub.models import MemoryChangeClock, MemoryChangeEvent, MemoryEmbeddingCache, Organization


async def _org(factory):
    identity = str(uuid.uuid4())
    async with session_scope(factory) as session:
        session.add(Organization(id=identity, name=identity))
    return identity


def _record(org, identity="fact", version=1):
    return MemoryRecord(identity, "fact", version, {"text": "private source"}, scope={"tenant": org})


@pytest.mark.asyncio
async def test_append_rollback_preserves_clock_and_snapshot(session_factory):
    org = await _org(session_factory)
    original = _record(org)
    async with session_scope(session_factory, org_id=org) as session:
        assert await append_change(session, org, original) == 1
    with pytest.raises(RuntimeError, match="abort"):
        async with session_scope(session_factory, org_id=org) as session:
            assert await append_change(session, org, _record(org, version=2)) == 2
            raise RuntimeError("abort")
    async with session_scope(session_factory, org_id=org) as session:
        clock = await session.get(MemoryChangeClock, org)
        assert clock.sequence == 1
        assert uuid.UUID(clock.source_id)
        rows = (await session.execute(select(MemoryChangeEvent))).scalars().all()
        assert [(row.sequence, row.record) for row in rows] == [(1, original.to_dict())]
        assert await append_change(session, org, _record(org, version=2)) == 2


@pytest.mark.asyncio
async def test_parallel_appends_allocate_independent_tenant_sequences(session_factory):
    orgs = [await _org(session_factory), await _org(session_factory)]

    async def append(org, index):
        async with session_scope(session_factory, org_id=org) as session:
            return await append_change(session, org, _record(org, str(index)))

    results = await asyncio.gather(*(append(org, i) for org in orgs for i in range(12)))
    assert sorted(results[:12]) == list(range(1, 13))
    assert sorted(results[12:]) == list(range(1, 13))
    async with session_scope(session_factory) as session:
        clocks = (await session.execute(select(MemoryChangeClock))).scalars().all()
        assert len({row.source_id for row in clocks}) == 2


@pytest.mark.asyncio
async def test_redaction_erases_every_revision_and_cached_projection_only_for_owner(session_factory):
    org, other = await _org(session_factory), await _org(session_factory)
    for owner in (org, other):
        async with session_scope(session_factory, org_id=owner) as session:
            await append_change(session, owner, _record(owner))
            await append_change(session, owner, _record(owner, version=2))
            await append_change(session, owner, _record(owner, "keep"))
            for projection in ("a" * 64, "b" * 64):
                session.add(MemoryEmbeddingCache(
                    org_id=owner, projection=projection, record_id="fact", checksum="c" * 64,
                    vector=[0.2, 0.8],
                ))
    async with session_scope(session_factory, org_id=org) as session:
        await redact_changes(session, org, ["fact"])
        assert await append_change(session, org, None, record_id="fact", version=2) == 4
    async with session_scope(session_factory) as session:
        rows = (await session.execute(select(MemoryChangeEvent).order_by(
            MemoryChangeEvent.org_id, MemoryChangeEvent.sequence
        ))).scalars().all()
        erased = [row for row in rows if row.org_id == org and row.record_id == "fact"]
        assert [(row.sequence, row.operation, row.record) for row in erased] == [
            (1, "delete", None), (2, "delete", None), (4, "delete", None),
        ]
        assert all(row.record is not None for row in rows if row.org_id == other or row.record_id == "keep")
        cache = (await session.execute(select(MemoryEmbeddingCache))).scalars().all()
        assert len(cache) == 2 and all(row.org_id == other for row in cache)


@pytest.mark.asyncio
async def test_invalid_event_cannot_allocate_a_clock(session_factory):
    org, other = await _org(session_factory), str(uuid.uuid4())
    async with session_scope(session_factory, org_id=org) as session:
        for kwargs in ({}, {"record_id": "id", "version": 0}, {"record_id": "id", "version": True}):
            with pytest.raises(ValueError):
                await append_change(session, org, None, **kwargs)
        with pytest.raises(ValueError, match="tenant"):
            await append_change(session, org, _record(other))
        assert await session.get(MemoryChangeClock, org) is None


@pytest.mark.asyncio
async def test_redaction_rollback_restores_snapshot_and_cached_vector(session_factory):
    org = await _org(session_factory)
    async with session_scope(session_factory, org_id=org) as session:
        await append_change(session, org, _record(org))
        session.add(MemoryEmbeddingCache(
            org_id=org, projection="a" * 64, record_id="fact", checksum="b" * 64, vector=[1, 0],
        ))
    with pytest.raises(RuntimeError, match="abort erasure"):
        async with session_scope(session_factory, org_id=org) as session:
            await redact_changes(session, org, ["fact"])
            await append_change(session, org, None, record_id="fact", version=1)
            raise RuntimeError("abort erasure")
    async with session_scope(session_factory, org_id=org) as session:
        row = await session.get(MemoryChangeEvent, (org, 1))
        assert row.operation == "upsert" and row.record["payload"]["text"] == "private source"
        assert (await session.get(MemoryChangeClock, org)).sequence == 1
        assert await session.get(MemoryChangeEvent, (org, 2)) is None
        assert (await session.execute(select(MemoryEmbeddingCache.vector))).scalar_one() == [1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [[], ["NaN", 0], [None, 0], [True, 0]])
async def test_cache_rejects_empty_or_non_numeric_vectors(session_factory, vector):
    org = await _org(session_factory)
    with pytest.raises(DBAPIError, match="check constraint"):
        async with session_scope(session_factory, org_id=org) as session:
            session.add(MemoryEmbeddingCache(
                org_id=org, projection="a" * 64, record_id="fact", checksum="b" * 64, vector=vector,
            ))


def _migration(connection, filename, operation="upgrade"):
    path = Path(__file__).parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location("stream_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, operation)()


@pytest.mark.asyncio
async def test_migration_backfills_current_snapshots_and_forces_fail_closed_rls(session_factory):
    """Exercise frozen upgrade on pre-stream data as a non-bypass owner and reader.

    Precreate ``stream_reader`` (NOSUPERUSER NOBYPASSRLS) in the disposable test
    cluster; ordinary Hub DB jobs without that dedicated reader skip this test.
    """
    engine = session_factory.kw["bind"]
    async with engine.connect() as connection:
        role = (await connection.execute(text(
            "SELECT rolname FROM pg_roles WHERE rolname = 'stream_reader' AND NOT rolsuper AND NOT rolbypassrls"
        ))).scalar_one_or_none()
    if role is None:
        pytest.skip("migration RLS test requires precreated non-bypass stream_reader")
    schema = "stream_migration_" + uuid.uuid4().hex
    org, other = str(uuid.uuid4()), str(uuid.uuid4())
    old = "d6e8a1b2c3f4_memory_record_events.py"
    new = "f8a0b3c4d5e6_memory_vector_stream.py"
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE organizations (id UUID PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO organizations VALUES (:org),(:other)"),
                                     {"org": org, "other": other})
            await connection.run_sync(_migration, old)
            for owner in (org, other):
                await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": owner})
                for identity, version in (("z-last", 3), ("a-first", 1)):
                    record = _record(owner, identity, version)
                    # Explicit SQL avoids the future ORM's change_seq default.
                    await connection.execute(text(
                        "INSERT INTO memory_records(org_id,id,version,record,scope) "
                        "VALUES (:org,:id,:version,CAST(:record AS jsonb),CAST(:scope AS jsonb))"
                    ), {"org": owner, "id": identity, "version": version,
                        "record": json.dumps(record.to_dict()), "scope": json.dumps(record.scope)})
            await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": other})
            await connection.run_sync(_migration, new)
            assert (await connection.execute(text("SELECT current_setting('app.org_id')"))).scalar_one() == other
            for owner in (org, other):
                await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": owner})
                rows = (await connection.execute(text(
                    "SELECT sequence,record_id,version,record->>'id' FROM memory_change_events ORDER BY sequence"
                ))).all()
                assert [tuple(row) for row in rows] == [(1, "a-first", 1, "a-first"), (2, "z-last", 3, "z-last")]
                positions = (await connection.execute(text(
                    "SELECT id,change_seq FROM memory_records ORDER BY id"
                ))).all()
                assert [tuple(row) for row in positions] == [("a-first", 1), ("z-last", 2)]
                assert (await connection.execute(text("SELECT sequence FROM memory_change_clocks"))).scalar_one() == 2
                await connection.execute(text(
                    "INSERT INTO memory_projection_states(org_id,projection,namespace,model,dimension) "
                    "VALUES (:org,repeat('a',64),'facts','model@revision',2)"
                ), {"org": owner})
                await connection.execute(text(
                    "INSERT INTO memory_embedding_cache(org_id,projection,record_id,checksum,vector) "
                    "VALUES (:org,repeat('a',64),'a-first',repeat('b',64),'[1,0]'::jsonb)"
                ), {"org": owner})
            enforced = (await connection.execute(text(
                "SELECT relname FROM pg_class WHERE relnamespace=current_schema()::regnamespace "
                "AND relname LIKE 'memory_%' AND relrowsecurity AND relforcerowsecurity"
            ))).scalars().all()
            assert set(("memory_change_clocks", "memory_change_events",
                        "memory_projection_states", "memory_embedding_cache")) <= set(enforced)
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO stream_reader'))
            await connection.execute(text(f'GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA "{schema}" TO stream_reader'))
        reader = create_async_engine(engine.url.set(username="stream_reader"))
        tables = ("memory_change_clocks", "memory_change_events", "memory_projection_states", "memory_embedding_cache")
        try:
            async with reader.begin() as connection:
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                assert (await connection.execute(text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ))).scalar_one() is False
                for table in tables:
                    assert (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
                await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": org})
                for table in tables:
                    assert (await connection.execute(text(
                        f"SELECT count(*) FROM {table}"
                    ))).scalar_one() == (2 if table == "memory_change_events" else 1)
                    assert (await connection.execute(text(
                        f"SELECT count(*) FROM {table} WHERE org_id=:other"
                    ), {"other": other})).scalar_one() == 0
            for table, columns, values in (
                ("memory_change_clocks", "org_id", ":org"),
                ("memory_change_events", "org_id,sequence,record_id,version,operation", ":org,3,'x',1,'delete'"),
                ("memory_projection_states", "org_id,projection,namespace,model,dimension",
                 ":org,repeat('a',64),'facts','m@rev',2"),
                ("memory_embedding_cache", "org_id,projection,record_id,checksum,vector",
                 ":org,repeat('a',64),'x',repeat('b',64),'[1,0]'::jsonb"),
            ):
                for current_org in (None, org):
                    with pytest.raises(DBAPIError, match="row-level security"):
                        async with reader.begin() as connection:
                            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                            if current_org:
                                await connection.execute(text(
                                    "SELECT set_config('app.org_id',:org,true)"
                                ), {"org": current_org})
                            await connection.execute(text(
                                f"INSERT INTO {table} ({columns}) VALUES ({values})"
                            ), {"org": other})
        finally:
            await reader.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.run_sync(_migration, new, "downgrade")
            assert (await connection.execute(text("SELECT to_regclass('memory_change_events')"))).scalar_one() is None
            assert (await connection.execute(text("SELECT count(*) FROM organizations"))).scalar_one() == 2
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
