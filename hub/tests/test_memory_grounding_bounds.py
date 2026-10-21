from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from unimem.memory_record import MemoryRecord
from hub import memory_records
from hub.models import MemoryPolicyState


def _hosted_graph(monkeypatch, records, candidates):
    rows = {record.id: SimpleNamespace(id=record.id, record=record.to_dict()) for record in records}

    class Result:
        def scalars(self):
            return self

        def all(self):
            return [rows[identity] for identity in candidates]

    class Session:
        gets = 0

        async def execute(self, _statement):
            return Result()

        async def get(self, model, key):
            if model is MemoryPolicyState:
                return None
            self.gets += 1
            return rows.get(key[1])

    session = Session()
    session.rows = rows

    @asynccontextmanager
    async def scope(_factory, *, org_id=None):
        assert org_id == "owner"
        yield session

    monkeypatch.setattr(memory_records, "session_scope", scope)
    return memory_records.PostgresJournal(object(), "owner"), session


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_mode", [False, True])
async def test_deep_candidate_cannot_poison_shared_shallow_evidence(monkeypatch, capability_mode):
    scope = {"tenant": "owner", "user": "u"}

    def record(identity, parents):
        return MemoryRecord(identity, "observation", 1, {"text": identity}, scope=scope,
                            relations={"derives": parents})

    records = [record("a-long", ["path-0"]), record("b-short", ["shared"])]
    records.extend(record(f"path-{index}", [f"path-{index + 1}" if index < 60 else "shared"])
                   for index in range(61))
    records.extend([record("shared", ["leaf"]), record("leaf", ["terminal"]), record("terminal", [])])
    candidates = [item.id for item in records] if capability_mode else ["a-long", "b-short"]
    store, session = _hosted_graph(monkeypatch, records, candidates)
    if capability_mode:
        admitted = await store._cap_allowed_ids(session, scope, memory_records.now(), ())
        assert "a-long" not in admitted
        assert "b-short" in admitted
    else:
        result = await store.search("", scope={"user": "u"})
        assert [item.id for item in result] == ["b-short"]
        session.rows["terminal"].record["status"] = "retired"
        assert await store.search("", scope={"user": "u"}) == []


@pytest.mark.asyncio
async def test_hosted_grounding_bounds_database_lookups_and_deduplicates_edges(monkeypatch):
    from unimem.record_visibility import GroundingState

    scope = {"tenant": "owner", "user": "u"}
    leaves = [MemoryRecord(f"leaf-{index}", "observation", 1, {"text": "source"}, scope=scope)
              for index in range(10)]
    wide = MemoryRecord("wide", "observation", 1, {"text": "wide"}, scope=scope,
                        relations={"derives": [record.id for record in leaves]})
    duplicate = MemoryRecord("duplicate", "observation", 1, {"text": "duplicate"}, scope=scope,
                             relations={"derives": [leaves[0].id] * 5000})
    monkeypatch.setattr(memory_records, "GroundingState", lambda: GroundingState(remaining=8))
    store, session = _hosted_graph(monkeypatch, [wide, *leaves], [wide.id])
    assert await store.search("", scope={"user": "u"}) == []
    assert session.gets <= 7
    store, session = _hosted_graph(monkeypatch, [duplicate, *leaves], [duplicate.id])
    assert [record.id for record in await store.search("", scope={"user": "u"})] == [duplicate.id]
    assert session.gets == 1
