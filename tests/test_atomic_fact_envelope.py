"""Lossless, explicitly opted-in AtomicFact envelopes; no legacy coercion."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from commontrace.fact_evidence import FactEvidence
from commontrace.hierarchical import AtomicFact
from commontrace.memory_record import MemoryRecord, atomic_fact_from_record, atomic_fact_record
from commontrace.record_visibility import visible


def full_fact(**changes):
    values = dict(
        id="fact-bridge", statement="  Ada prefers SQLite.  ", category="custom-legacy",
        scopes=["user:ada", "tenant:team", "agent:coder", "session:chat", "repo:project", "custom:lane", "personal"],
        confidence=0.8123456789, confirmations=7, valid_from="2026-01-01T01:02:03+00:00",
        valid_until="2030-01-01T00:00:00Z", expires_at="2031-01-01T00:00:00Z",
        forgotten=False, source_traces=["trace-2", "trace-1", "trace-2"], status="active",
        superseded_by=None, revision="legacy-revision", created_at="2026-01-02T00:00:00Z",
        updated_at="2026-01-03T00:00:00Z", stability="stable",
        evidence=[FactEvidence("fact", "source-1", "a" * 64, "support", "2026-01-02T00:00:00Z"),
                  FactEvidence("lesson", "source-2", "b" * 64, "refute", "2026-01-02T00:00:00Z")],
        evidence_bound=True, min_support=2, evidence_revision="bound-revision", memory_type="world",
        origin={"author": "user", "nested": {"sequence": [1, True, None, "é"]}}, retracted_at=None,
    )
    values.update(changes)
    return AtomicFact(**values)


def test_exact_every_field_roundtrip_and_independent_copies():
    fact = full_fact()
    original = fact.to_dict()
    record = atomic_fact_record(fact, 3, tenant="team")
    assert record.payload == {**original, "text": original["statement"]}
    assert record.provenance == {"atomic_fact_format": "commontrace.atomic-fact.v1",
                                 "source_traces": original["source_traces"]}
    assert record.scope == {"tenant": "team", "user": "ada", "agent": "coder", "session": "chat",
                            "repo": "project", "labels": ["custom:lane", "personal"]}
    assert record.relations == {"updates": [fact.id]}
    assert record.evidence == original["evidence"]
    wire = json.loads(json.dumps(record.to_dict(), allow_nan=False))
    recovered = atomic_fact_from_record(MemoryRecord(**wire))
    assert recovered.to_dict() == original
    recovered.origin["nested"]["sequence"].append("changed")
    assert record.payload["origin"] == original["origin"]
    record.payload["source_traces"].append("changed")
    assert fact.to_dict() == original


def test_blank_legacy_timestamps_and_nonstandard_category_are_preserved():
    fact = full_fact(valid_from="", valid_until=None, expires_at=None, created_at="", updated_at="",
                     revision="", evidence_revision="", evidence=[], evidence_bound=False)
    record = atomic_fact_record(fact, 1)
    assert record.created_at == ""
    assert atomic_fact_from_record(record).to_dict() == fact.to_dict()


@pytest.mark.parametrize("status,forgotten,envelope_status", [
    ("active", False, "active"), ("active", True, "deprecated"),
    ("superseded", False, "deprecated"), ("invalidated", False, "deprecated"),
    ("deleted", False, "deprecated"), ("retired", False, "retired"),
])
def test_legacy_lifecycle_cannot_become_current(status, forgotten, envelope_status):
    fact = full_fact(status=status, forgotten=forgotten)
    record = atomic_fact_record(fact, 2)
    assert record.status == envelope_status
    assert visible(record, record.scope, at="2027-01-01T00:00:00Z") is (status == "active" and not forgotten)
    assert atomic_fact_from_record(record).to_dict() == fact.to_dict()


@pytest.mark.parametrize("scopes,tenant", [
    (["user:a", "user:b"], None), (["tenant:a"], "b"), (["user:"], None),
    (["tenant:"], None), (["repo:" + "x" * 257], None),
])
def test_conflicting_or_invalid_scope_rejected(scopes, tenant):
    with pytest.raises(ValueError):
        atomic_fact_record(full_fact(scopes=scopes), 1, tenant=tenant)


def test_hosted_tenant_binding_is_additive_and_original_scope_order_is_exact():
    fact = full_fact(scopes=["user:ada", "label", "user:ada", ""])
    record = atomic_fact_record(fact, 1, tenant="team")
    assert record.scope == {"tenant": "team", "user": "ada", "labels": ["label", ""]}
    assert atomic_fact_from_record(record).scopes == fact.scopes


@pytest.mark.parametrize("valid_until,expires_at,effective_end", [
    (None, "2028-01-01T00:00:00Z", "2028-01-01T00:00:00Z"),
    ("2030-01-01T00:00:00Z", "2028-01-01T00:00:00Z", "2028-01-01T00:00:00Z"),
    ("2027-01-01T00:00:00Z", "2028-01-01T00:00:00Z", "2027-01-01T00:00:00Z"),
    ("2027-12-31T19:00:00-05:00", "2028-01-01T00:00:00Z", "2027-12-31T19:00:00-05:00"),
])
def test_expiry_is_effective_visibility_end_not_permission_to_erase(valid_until, expires_at, effective_end):
    fact = full_fact(valid_until=valid_until, expires_at=expires_at)
    record = atomic_fact_record(fact, 1)
    assert record.valid_until == effective_end
    assert "forget_after" not in record.payload
    assert "forget_reason" not in record.payload
    assert not visible(record, record.scope, at=effective_end)
    assert atomic_fact_from_record(record).to_dict() == fact.to_dict()


def test_expired_fact_is_hidden_but_audit_history_survives_reaping(tmp_path):
    from commontrace.memory_journal import Journal

    journal = Journal(str(tmp_path))
    fact = full_fact(valid_from="2000-01-01T00:00:00Z", valid_until=None,
                     expires_at="2001-01-01T00:00:00Z", evidence=[], evidence_bound=False)
    record = journal.put(atomic_fact_record(fact, 1))
    assert journal.current(record.id, scope=record.scope) is None
    assert journal.reap_due() == []
    assert len(journal.history(record.id)) == 1
    assert atomic_fact_from_record(journal.history(record.id)[0]).to_dict() == fact.to_dict()


@pytest.mark.parametrize("field,value", [
    ("id", "other"), ("kind", "observation"), ("status", "active"),
    ("confidence", 0.5), ("created_at", "2027-01-01T00:00:00Z"),
    ("valid_from", "2027-01-01T00:00:00Z"), ("valid_until", None),
    ("retracted_at", "2027-01-01T00:00:00Z"), ("evidence", []),
    ("scope", {"tenant": "team", "user": "mallory"}),
    ("provenance", {"atomic_fact_format": "commontrace.atomic-fact.v2"}),
])
def test_inconsistent_envelope_mirrors_rejected(field, value):
    record = atomic_fact_record(full_fact(status="deleted"), 1)
    with pytest.raises(ValueError):
        atomic_fact_from_record(replace(record, **{field: value}))


@pytest.mark.parametrize("change", [
    {"extra": "silently dropped?"}, {"text": "different claim"}, {"confirmations": True},
    {"confirmations": -1}, {"min_support": 0}, {"min_support": True},
    {"confidence": float("nan")}, {"confidence": float("inf")}, {"confidence": "0.8"},
    {"confidence": 10 ** 1000},
    {"forgotten": "false"}, {"evidence_bound": 1}, {"origin": {"bad": float("nan")}},
    {"scopes": "user:a"}, {"source_traces": [1]}, {"updated_at": 0},
    {"status": "unknown"}, {"evidence": [{"kind": "fact"}]}, {"stability": "unknown"},
])
def test_malformed_or_unknown_payload_fields_are_not_coerced(change):
    record = atomic_fact_record(full_fact(), 1)
    payload = {**record.payload, **change}
    with pytest.raises(ValueError):
        atomic_fact_from_record(replace(record, payload=payload))


def test_missing_field_and_unmarked_generic_fact_are_not_implicitly_imported():
    record = atomic_fact_record(full_fact(), 1)
    payload = copy.deepcopy(record.payload)
    payload.pop("updated_at")
    with pytest.raises(ValueError):
        atomic_fact_from_record(replace(record, payload=payload))
    with pytest.raises(ValueError):
        atomic_fact_from_record(MemoryRecord("ordinary", "fact", 1, {"text": "generic memory"}))


@pytest.mark.parametrize("version", [True, 0, -1, "1", 1.5])
def test_version_is_a_positive_integer(version):
    with pytest.raises(ValueError):
        atomic_fact_record(full_fact(), version)


def test_protocol_vectors():
    source = Path(__file__).resolve().parents[1] / "protocol" / "conformance" / "atomic-fact-vectors.json"
    vectors = json.loads(source.read_text())
    for vector in vectors["roundtrip"]:
        payload = vector["fact"]
        fact = AtomicFact(**{**payload, "evidence": [FactEvidence.from_dict(row) for row in payload["evidence"]]})
        record = atomic_fact_record(fact, vector["version"], tenant=vector.get("tenant"))
        assert record.scope == vector["scope"]
        assert record.status == vector["status"]
        assert atomic_fact_from_record(record).to_dict() == payload
    base = vectors["roundtrip"][0]
    payload = base["fact"]
    fact = AtomicFact(**{**payload, "evidence": [FactEvidence.from_dict(row) for row in payload["evidence"]]})
    for vector in vectors["rejected"]:
        record = atomic_fact_record(fact, base["version"], tenant=base.get("tenant"))
        record = replace(record, payload={**record.payload, **vector.get("payload_patch", {})},
                         **vector.get("record_patch", {}))
        with pytest.raises(ValueError, match="AtomicFact|record is not"):
            atomic_fact_from_record(record)
