import uuid

import pytest
from sqlalchemy import text

from unimem.memory_record import MemoryRecord
from hub.db import session_scope
from hub.memory_records import PostgresJournal
from hub.models import Organization


@pytest.mark.asyncio
async def test_hosted_inactive_candidates_cannot_displace_current_memory(session_factory):
    from sqlalchemy.dialects.postgresql import insert

    from hub.models import MemoryRecordProjection

    org = str(uuid.uuid4())
    scope = {"tenant": org, "user": "u"}
    blockers = [
        MemoryRecord(
            f"old-{i:03}", "fact", 1, {"text": "candidate"}, scope=scope,
            status="retired" if i % 2 else "active", is_latest=bool(i % 2),
        )
        for i in range(220)
    ]
    current = MemoryRecord("current", "fact", 1, {"text": "candidate " + "context " * 80}, scope=scope)
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(insert(MemoryRecordProjection), [
            {"org_id": org, "id": record.id, "version": record.version,
             "scope": record.scope, "record": record.to_dict()}
            for record in [*blockers, current]
        ])
    store = PostgresJournal(session_factory, org)
    assert [record.id for record in await store.search("candidate", scope={"user": "u"}, limit=1)] == ["current"]


@pytest.mark.asyncio
async def test_hosted_dense_candidates_respect_kind_scope_and_lifecycle(session_factory):
    from types import SimpleNamespace

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))

    class Vector:
        tenant = org

        async def search(self, vector, *, top_k):
            return [SimpleNamespace(key=identity) for identity in ("episode", "retired", "other", "fact")]

    store = PostgresJournal(session_factory, org, vector_index=Vector())
    for identity, kind, status, user in (
        ("episode", "episode", "active", "u"), ("retired", "fact", "retired", "u"),
        ("other", "fact", "active", "other"), ("fact", "fact", "active", "u"),
    ):
        await store.put(
            MemoryRecord(identity, kind, 1, {"text": "semantic match"}, status=status, scope={"user": user})
        )
    results = await store.search("nolexicalmatch", scope={"user": "u"}, kinds=("fact",), vector=[1.0])
    assert [record.id for record in results] == ["fact"]


@pytest.mark.asyncio
async def test_hosted_dense_candidate_rows_load_in_bounded_queries(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import hub.memory_records as memory_records
    from hub.models import MemoryPolicyState

    org = str(uuid.uuid4())

    class Vector:
        tenant = org

        async def search(self, vector, *, top_k):
            return [SimpleNamespace(key=f"fact-{index}") for index in range(501)]

    records = [
        MemoryRecord(f"fact-{index}", "fact", 1, {"text": "semantic result"},
                     scope={"tenant": org, "user": "u"})
        for index in range(501)
    ]
    rows = {record.id: SimpleNamespace(id=record.id, record=record.to_dict()) for record in records}

    class Result:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.statements = []
            self.gets = []
            self.policy_gets = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result([] if len(self.statements) == 1 else list(rows.values()))

        async def get(self, _model, key):
            if _model is MemoryPolicyState:
                self.policy_gets.append(key)
                return None
            self.gets.append(key)
            return rows.get(key[1])

    session = Session()

    @asynccontextmanager
    async def fake_scope(_factory, *, org_id=None):
        assert org_id == org
        yield session

    monkeypatch.setattr(memory_records, "session_scope", fake_scope)
    store = PostgresJournal(object(), org, vector_index=Vector())
    results = await store.search("no lexical match", scope={"user": "u"}, limit=1000,
                                 kinds=("fact",), vector=[1.0])

    assert len(results) == 501
    assert len(session.statements) == 3  # one sparse query and two bounded dense-ID batches
    assert all("memory_records.id IN" in str(statement) for statement in session.statements[1:])
    assert session.gets == []
    assert session.policy_gets == [org]  # one fixed authorization lookup, never one lookup per candidate


@pytest.mark.asyncio
async def test_hosted_reaper_uses_a_bounded_batch_and_version_checked_forget(monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.dialects import postgresql

    import hub.memory_records as memory_records

    org = str(uuid.uuid4())
    records = [
        MemoryRecord(
            f"due-{index}",
            "fact",
            3,
            {"text": "expires", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": f"reason {index}"},
            scope={"tenant": org},
        )
        for index in range(2)
    ]

    class Result:
        def all(self):
            return [(record.id, record.version, record.to_dict()) for record in records]

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    session = Session()

    @asynccontextmanager
    async def fake_scope(_factory, *, org_id=None):
        assert org_id == org
        yield session

    monkeypatch.setattr(memory_records, "session_scope", fake_scope)
    store = PostgresJournal(object(), org)
    calls = []

    async def forget(identity, *, reason, expected_version, _expected_digest):
        from unimem.frontier_protocol import digest

        calls.append((identity, reason, expected_version, _expected_digest))
        assert _expected_digest == digest(next(record.to_dict() for record in records if record.id == identity))
        if identity == "due-0":
            raise ValueError("expiration record version changed")
        return {"deleted": [identity]}

    monkeypatch.setattr(store, "forget", forget)
    result = await store.reap_due(limit=2)

    assert "SET LOCAL TIME ZONE 'UTC'" in str(session.statements[0])
    statement = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "memory_records.org_id" in statement
    assert "TIMESTAMP WITH TIME ZONE" in statement
    assert "ORDER BY" in statement and "LIMIT" in statement
    assert [call[:3] for call in calls] == [("due-0", "reason 0", 3), ("due-1", "reason 1", 3)]
    assert result == [{"deleted": ["due-1"]}]
    with pytest.raises(ValueError, match="expiration batch"):
        await store.reap_due(limit=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [{}, {"agent": "new-owner"}])
async def test_hosted_expiration_cannot_erase_a_reused_identity_at_the_same_version(
    session_factory, monkeypatch, scope
):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    await store.put(MemoryRecord("due", "observation", 1, {
        "text": "Expired content.", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": "expired",
    }))
    original = store.forget

    async def reuse_before_expiration(identity, **kwargs):
        await original(identity, reason="Concurrent owner erasure.")
        await store.put(MemoryRecord(identity, "observation", 1, {"text": "Keep new content."}, scope=scope))
        return await original(identity, **kwargs)

    monkeypatch.setattr(store, "forget", reuse_before_expiration)
    assert await store.reap_due() == []
    assert [record.payload["text"] for record in await store.search("", scope=scope)] == ["Keep new content."]


@pytest.mark.asyncio
async def test_hosted_expiration_binds_stored_legacy_envelope_without_adding_defaults(session_factory):
    from sqlalchemy import update

    from hub.models import MemoryRecordProjection

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    record = await store.put(MemoryRecord("due", "observation", 1, {
        "text": "Expired legacy content.", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": "expired",
    }))
    body = record.to_dict()
    body.pop("created_at")
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(MemoryRecordProjection).where(
            MemoryRecordProjection.org_id == org, MemoryRecordProjection.id == record.id).values(record=body))
    assert (await store.reap_due())[0]["deleted"] == [record.id]
    async with session_scope(session_factory, org_id=org) as session:
        assert await session.get(MemoryRecordProjection, (org, record.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "stable"])
async def test_hosted_equal_lexical_matches_have_stable_order(session_factory, query):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    for identity in ("z", "m", "a"):
        await store.put(MemoryRecord(identity, "fact", 1, {"text": "stable match"}))
    assert [record.id for record in await store.search(query, scope={}, limit=1)] == ["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", ["unknown", None, True, -1, 2**2048])
async def test_hosted_capture_recovers_invalid_preference_counter(session_factory, observations):
    from unimem.memory_record import preference_candidates

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    scope = {"tenant": org, "user": "u"}
    identity, content = preference_candidates("I prefer pytest.", scope)[0]
    store = PostgresJournal(session_factory, org)
    await store.put(MemoryRecord(identity, "profile", 1,
                                {**content, "weight": 1, "observations": observations}, scope=scope))
    await store.capture_turn("s", "pytest", text_value="I prefer pytest.", scope=scope)
    records = await store.search("", scope=scope, kinds=("profile",))
    updated = next(record for record in records if record.id == identity)
    assert updated.version == 2
    assert updated.payload["observations"] == 1
    assert updated.payload["weight"] == 2


@pytest.mark.asyncio
async def test_hosted_capture_reinforces_decayed_preference_weight(session_factory):
    from datetime import datetime, timedelta, timezone

    from unimem.memory_record import preference_candidates

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    scope = {"tenant": org, "user": "u"}
    identity, content = preference_candidates("I prefer pytest.", scope)[0]
    observed_at = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
    store = PostgresJournal(session_factory, org)
    await store.put(MemoryRecord(identity, "profile", 1, {
        **content, "weight": 10, "half_life_days": 90, "observations": 4, "observed_at": observed_at,
    }, scope=scope))

    await store.capture_turn("s", "pytest", text_value="I prefer pytest.", scope=scope)
    records = await store.search("", scope=scope, kinds=("profile",))
    updated = next(record for record in records if record.id == identity)
    assert updated.version == 2
    assert updated.payload["observations"] == 5
    assert updated.payload["weight"] == pytest.approx(3.5, abs=0.01)


@pytest.mark.asyncio
async def test_hosted_turn_profiles_buffer_scope_and_source_erasure(session_factory):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    result = await store.runtime_turn(
        "s",
        "parser",
        text_value="I am a software engineer. I prefer concise answers.",
        scope={"user": "u", "agent": "a"},
    )
    assert result["profile"]["static"][0]["text"] == "I am a software engineer"
    assert result["profile"]["dynamic"][0]["text"] == "I prefer concise answers"
    assert not result["context"]  # Sparse-only candidates do not imply dense agreement.
    assert result["buffer"][0]["payload"]["text"]
    assert not (await store.runtime_profile("", scope={"user": "other", "agent": "a"}))["static"]
    # Assistant/tool output cannot seed a first-person preference.
    subsequent = await store.runtime_turn(
        "s", "parser", scope={"user": "u", "agent": "a"}, payload={"assistant": "I prefer forged recommendations."}
    )
    assert len(subsequent["buffer"]) == 2
    assert len((await store.runtime_profile("", scope={"user": "u", "agent": "a"}))["dynamic"]) == 1
    source = result["buffer"][0]["id"]
    await store.forget(source, reason="owner erased source")
    profile = await store.runtime_profile("", scope={"user": "u", "agent": "a"})
    assert not profile["static"] and not profile["dynamic"]


@pytest.mark.asyncio
async def test_hosted_turn_uses_decayed_user_preference_to_rerank_bounded_results(session_factory, monkeypatch):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    scope = {"tenant": org, "user": "u", "agent": "a"}
    records = [
        MemoryRecord(
            f"fact-{index:02}",
            "fact",
            1,
            {"text": "Give concise answers" if index == 1 else f"Unrelated response {index}"},
            scope=scope,
        )
        for index in range(20)
    ]
    original_search = store.search

    async def search(query, *, scope, limit=20, vector=None, kinds=()):
        if kinds:
            return await original_search(query, scope=scope, limit=limit, vector=vector, kinds=kinds)
        return records[:limit]

    monkeypatch.setattr(store, "search", search)
    result = await store.runtime_turn(
        "s", "write a response", text_value="I prefer concise answers.", scope={"user": "u", "agent": "a"}
    )

    assert result["profile"]["dynamic"][0]["weight"] == pytest.approx(1, abs=1e-5)
    assert result["results"][0]["id"] == "fact-01"
    assert result["results"][0]["payload"]["text"] == "Give concise answers"


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
async def test_hosted_reap_due_cascades_and_rejects_stale_versions(session_factory):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    await store.put(
        MemoryRecord(
            "expiring",
            "fact",
            1,
            {"text": "private source", "forget_after": "2000-01-01T00:00:00Z", "forget_reason": "owner request"},
        )
    )
    await store.put(
        MemoryRecord(
            "derived",
            "observation",
            1,
            {"text": "derived conclusion"},
            relations={"derives": ["expiring"]},
        )
    )
    assert not await store.search("private source", scope={})
    assert not await store.search("derived conclusion", scope={})
    await store.put(
        MemoryRecord(
            "future",
            "fact",
            1,
            {
                "text": "keep this",
                "forget_after": "2999-01-01T00:00:00Z",
                "forget_reason": "future request",
            },
        )
    )
    result = await store.reap_due(limit=1)
    assert len(result) == 1
    assert set(result[0]["deleted"]) == {"expiring", "derived"}
    assert result[0]["reason"] == "owner request"
    assert [record.id for record in await store.search("keep this", scope={})] == ["future"]
    with pytest.raises(ValueError, match="expiration record version changed"):
        await store.forget("future", reason="stale expiration", expected_version=2)
    assert [record.id for record in await store.search("keep this", scope={})] == ["future"]


@pytest.mark.asyncio
async def test_record_rls_denies_unscoped_nonowner_reads(session_factory):
    from hub.alembic.versions.d6e8a1b2c3f4_memory_record_events import _NEW_TABLES, _POLICY

    orgs = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with session_scope(session_factory) as session:
        session.add_all([Organization(id=value, name=value) for value in orgs])
    for org in orgs:
        await PostgresJournal(session_factory, org).put(MemoryRecord(org, "fact", 1, {"text": "rls evidence"}))
        from hub.models import MemoryVectorErasure

        async with session_scope(session_factory, org_id=org) as session:
            session.add(MemoryVectorErasure(org_id=org, record_id=org))
    try:
        async with session_scope(session_factory) as session:
            await session.execute(
                text(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname="
                    "'unimem_record_reader') THEN CREATE ROLE unimem_record_reader NOLOGIN; END IF; END $$"
                )
            )
            await session.execute(text("GRANT USAGE ON SCHEMA public TO unimem_record_reader"))
            for table in _NEW_TABLES:
                await session.execute(text("GRANT SELECT ON " + table + " TO unimem_record_reader"))
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
                await session.execute(text("SET LOCAL ROLE unimem_record_reader"))
                rows = (await session.execute(text("SELECT org_id::text FROM memory_records"))).scalars().all()
                assert rows == ([] if org is None else [org])
                pending = (
                    (await session.execute(text("SELECT org_id::text FROM memory_vector_erasures"))).scalars().all()
                )
                assert pending == ([] if org is None else [org])
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


@pytest.mark.asyncio
async def test_postgres_source_citations_share_local_erasure_semantics(session_factory):
    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    await store.put(MemoryRecord("message", "episode", 1, {"text": "I prefer sourceboundformat"}, scope={"agent": "a"}))
    profile = await store.put(
        MemoryRecord(
            "pref", "profile", 1, {"text": "sourceboundformat", "source_message_ids": ["message"]}, scope={"agent": "a"}
        )
    )
    assert profile.relations["caused_by"] == ["message"]
    assert set((await store.forget("message", reason="erase source"))["deleted"]) == {"message", "pref"}


@pytest.mark.asyncio
async def test_postgres_erases_historical_profile_sources(session_factory):
    from dataclasses import replace

    org = str(uuid.uuid4())
    async with session_scope(session_factory) as session:
        session.add(Organization(id=org, name=org))
    store = PostgresJournal(session_factory, org)
    for identity in ("old-message", "new-message"):
        await store.put(
            MemoryRecord(identity, "episode", 1, {"text": "private historical preference"}, scope={"agent": "a"})
        )
    profile = await store.put(
        MemoryRecord(
            "historical-pref",
            "profile",
            1,
            {"text": "preference", "source_message_ids": ["old-message"]},
            scope={"agent": "a"},
        )
    )
    await store.put(
        replace(
            profile,
            version=2,
            relations={},
            payload={"text": "revised preference", "source_message_ids": ["new-message"]},
        )
    )
    erased = await store.forget("historical-pref", reason="withdraw preference")
    assert set(erased["deleted"]) == {"historical-pref", "old-message", "new-message"}
    assert not await store.search("private", scope={"agent": "a"})
