import json

import pytest

from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord


def test_all_kinds_version_scope_and_expiry(tmp_path):
    journal = Journal(str(tmp_path))
    for kind in ("episode", "fact", "entity", "observation", "profile", "lesson", "directive"):
        journal.put(MemoryRecord(kind, kind, 1, {"text": "use bounded reads"}, scope={"tenant": "a", "user": "u"}))
    assert len(journal.search("", scope={"tenant": "a", "user": "u"})) == 7
    assert not journal.search("", scope={"tenant": "b", "user": "u"})
    journal.put(
        MemoryRecord("fact", "fact", 2, {"text": "new"}, scope={"tenant": "a", "user": "u"}), expected_version=1
    )
    assert [r.payload["text"] for r in journal.search("new", scope={"tenant": "a", "user": "u"})] == ["new"]
    assert [r.is_latest for r in journal.history("fact")] == [False, True]
    with pytest.raises(ValueError):
        journal.put(MemoryRecord("fact", "fact", 3, {"text": "leak"}, scope={"tenant": "b"}))
    journal.put(MemoryRecord("expired", "fact", 1, {"text": "old"}, valid_until="2000-01-01T00:00:00Z"))
    assert not journal.search("old")


def test_buffer_cursor_and_private_redaction(tmp_path):
    journal = Journal(str(tmp_path))
    first = journal.capture(
        "run", "UserPromptSubmit", {"text": "password=hunter2 alice@example.com"}, scope={"agent": "a"}
    )
    assert (
        journal.capture(
            "run",
            "UserPromptSubmit",
            {"text": "password=hunter2 alice@example.com"},
            scope={"agent": "a"},
            event_id=first["id"],
        )["id"]
        == first["id"]
    )
    assert "hunter2" not in json.dumps(journal.buffer("run", scope={"agent": "a"}))
    assert "alice@example.com" not in json.dumps(journal.buffer("run", scope={"agent": "a"}))
    assert not journal.buffer("run", scope={"agent": "b"})
    journal.seal("run", first["sequence"])
    assert not journal.buffer("run", scope={"agent": "a"})


def test_forget_cascades_and_erases_history(tmp_path):
    journal = Journal(str(tmp_path))
    journal.put(MemoryRecord("source", "fact", 1, {"text": "privatephrase"}))
    journal.put(MemoryRecord("child", "observation", 1, {"text": "derivedprivate"}, relations={"derives": ["source"]}))
    journal.cache_vector("child", "model", "derivedprivate", [1.0, 0.0])
    result = journal.forget("source", reason="erasure request")
    assert set(result["deleted"]) == {"source", "child"}
    assert not journal.history("child")
    assert journal.vector("child", "model", "derivedprivate") is None
    assert not journal.search("privatephrase")


def test_injection_claims_are_session_scoped_and_inherited(tmp_path):
    journal = Journal(str(tmp_path))
    assert journal.claim_injections("run", ["a", "b"]) == ["a", "b"]
    assert journal.claim_injections("run", ["a"]) == []
    journal.inherit("child", "run")
    assert journal.claim_injections("child", ["b", "c"]) == ["c"]


def test_observation_versions_and_grounding_retraction(tmp_path):
    from commontrace.observations import consolidate_record_facet

    journal = Journal(str(tmp_path))
    journal.put(MemoryRecord("f", "fact", 1, {"text": "Parser validates 2 fields"}))
    observation = consolidate_record_facet(str(tmp_path), "Parser", "validation", ["f"])
    assert observation.kind == "observation"
    updated = consolidate_record_facet(str(tmp_path), "Parser", "validation", ["f"])
    assert updated.id == observation.id and updated.version == 2
    assert observation.id in journal.forget("f", reason="withdraw grounding")["deleted"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "made-up"},
        {"valid_until": "yesterday"},
        {"scope": {"agent": 4}},
        {"valid_from": "2026-02-02", "valid_until": "2026-01-01"},
        {"is_latest": "true"},
        {"payload": {"forget_after": "2027-01-01"}},
    ],
)
def test_invalid_runtime_envelopes(changes):
    with pytest.raises(ValueError):
        MemoryRecord("a", "fact", 1, changes.pop("payload", {}), **changes)


def test_derivatives_hide_when_grounding_retracted(tmp_path):
    from dataclasses import replace

    journal = Journal(str(tmp_path))
    source = journal.put(MemoryRecord("f", "fact", 1, {"text": "original"}))
    journal.put(MemoryRecord("o", "observation", 1, {"text": "derived"}, relations={"derives": ["f"]}))
    journal.put(replace(source, version=2, status="retired"))
    assert journal.search("derived") == []


def test_capture_collision_rejects_changed_evidence(tmp_path):
    journal = Journal(str(tmp_path))
    journal.capture("s", "UserMessage", {"text": "one"}, scope={}, event_id="message")
    with pytest.raises(ValueError):
        journal.capture("s", "UserMessage", {"text": "changed"}, scope={}, event_id="message")


def test_forget_after_reaps_payload_and_derived_records(tmp_path):
    journal = Journal(str(tmp_path))
    journal.put(
        MemoryRecord(
            "due",
            "fact",
            1,
            {
                "text": "private expiration phrase",
                "forget_after": "2000-01-01T00:00:00+00:00",
                "forget_reason": "expired owner retention",
            },
        )
    )
    journal.put(
        MemoryRecord("derived", "observation", 1, {"text": "private derivative"}, relations={"derives": ["due"]})
    )
    result = journal.reap_due()
    assert set(result[0]["deleted"]) == {"due", "derived"}
    assert journal.history("due") == []
    assert b"private expiration phrase" not in (tmp_path / "memory" / "runtime.sqlite3").read_bytes()


def test_facet_projection_is_atomic_revision_bound_and_scope_isolated(tmp_path):
    from dataclasses import replace

    journal = Journal(str(tmp_path))
    a = journal.put(
        MemoryRecord(
            "a",
            "fact",
            1,
            {"text": "Parser validates 2 fields", "entity": "Parser", "facet": "validation"},
            scope={"agent": "one"},
        )
    )
    observations = journal.search("Parser", scope={"agent": "one"}, kinds=("observation",))
    assert len(observations) == 1
    original = observations[0]
    assert original.provenance["source_versions"] == {"a": 1}
    journal.put(replace(a, version=2, payload={**a.payload, "text": "Parser validates 3 fields"}))
    updated = journal.search("Parser", scope={"agent": "one"}, kinds=("observation",))[0]
    assert updated.id == original.id and updated.version == 2
    assert updated.payload["text"] == "Parser validates 3 fields"
    journal.put(MemoryRecord("other", "fact", 1, a.payload, scope={"agent": "two"}))
    assert journal.search("Parser", scope={"agent": "two"}, kinds=("observation",))[0].id != updated.id
    assert journal.search("Parser", scope={"agent": "one"}, kinds=("observation",))[0].id == updated.id
    journal.put(replace(a, version=3, status="retired"))
    assert journal.search("Parser", scope={"agent": "one"}, kinds=("observation",)) == []


def test_manual_facet_withholds_after_source_revision(tmp_path):
    from dataclasses import replace

    from commontrace.observations import consolidate_record_facet

    journal = Journal(str(tmp_path))
    source = journal.put(MemoryRecord("a", "fact", 1, {"text": "Parser uses two fields"}))
    consolidate_record_facet(str(tmp_path), "Parser", "validation", ["a"])
    assert journal.search("Parser", kinds=("observation",))
    journal.put(replace(source, version=2, payload={"text": "Parser uses three fields"}))
    assert not journal.search("Parser", kinds=("observation",))
