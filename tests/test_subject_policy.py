"""Regulated-subject decisions happen before canonical writes, not after retrieval."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from commontrace import frontier_protocol as fp
from commontrace import hierarchical, memory_authority, nextwave_store, purpose
from commontrace import subject_policy as policy
from commontrace.memory_journal import Journal
from commontrace.memory_record import MemoryRecord
from tests.test_capabilities import NOW


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(fp, "now", lambda: NOW.isoformat())


def configure(root, *, subject="adult", context="workplace", consent=True):
    binding = {"id": "work-area", "scopes": ["project:a"], "subject_class": subject, "context": context}
    if subject == "child-under-13":
        binding.update(retention_days=7, retention_purpose=policy.MEDICAL,
            parental_consent={"reference": "verified-parent-receipt-42", "verification_method": "External reviewed VPC",
                "expires_at": (NOW+timedelta(days=2)).isoformat()} if consent else None)
    return policy.compile(root, [binding])


def emotion(identity="emotion", modality="voice"):
    return MemoryRecord(identity, "observation", 1, {"text": "Inferred emotional state."},
        provenance={"source": {"modality": modality, "labels": ["emotion"], "inferred": True}},
        scope={"labels": ["project:a"]})


@pytest.mark.parametrize("context", ["workplace", "education"])
@pytest.mark.parametrize("modality", ["voice", "face", "physiology"])
def test_biometric_emotion_is_quarantined_before_any_record_is_written(tmp_path, context, modality):
    root = str(tmp_path)
    configure(root, context=context)
    journal = Journal(root)
    with pytest.raises(policy.HeldForReview, match="quarantine"):
        journal.put(emotion(modality=modality))
    with journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    decision = policy.explain(root)["decisions"][-1]
    assert decision["decision"] == "quarantine"
    assert "Inferred emotional state" not in fp.wire(policy.explain(root)).decode()


def test_clinical_medical_and_explicit_workplace_medical_exception_are_allowed(tmp_path):
    for context in ("clinical", "workplace"):
        root = str(tmp_path/context)
        configure(root, context=context)
        with purpose.request(root, policy.MEDICAL):
            saved = Journal(root).put(emotion())
        assert saved.id == "emotion"
        assert policy.explain(root)["decisions"][-1]["decision"] == "allow"


def test_unknown_modality_is_held_for_review_even_in_clinical_context(tmp_path):
    root = str(tmp_path)
    configure(root, context="clinical")
    with pytest.raises(policy.HeldForReview, match="unknown-source-modality"):
        Journal(root).put(MemoryRecord("unknown", "fact", 1, {"text": "An untagged observation."},
                                      scope={"labels": ["project:a"]}))
    assert policy.explain(root)["decisions"][-1]["decision"] == "review"


def test_child_biometric_gets_earliest_written_retention_and_is_actually_erased(tmp_path, monkeypatch):
    root = str(tmp_path)
    configure(root, subject="child-under-13", context="home")
    with purpose.request(root, policy.MEDICAL):
        Journal(root).put(emotion())
    rows = policy._retentions(root)
    assert len(rows) == 1
    receipt = next(iter(rows.values()))
    assert receipt["expires_at"] == (NOW+timedelta(days=2)).isoformat()
    assert receipt["namespace"] == "records"
    assert policy.explain(root)["decisions"][-1]["biometric_personal_information"]
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(days=3)).isoformat())
    journal = Journal(root)
    assert journal.current("emotion", scope={"labels": ["project:a"]}) is None
    result = purpose.reap(root)["regulated_retention"][0]
    assert result["status"] == "pending" and result["local_purge"]
    assert purpose.reap(root)["regulated_retention"][0] == result
    with journal.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_missing_parental_consent_denies_child_capture_before_persistence(tmp_path):
    root = str(tmp_path)
    configure(root, subject="child-under-13", context="home", consent=False)
    with purpose.request(root, policy.MEDICAL), policy.ingest(root, {"modality": "voice"}):
        with pytest.raises(policy.HeldForReview, match="parental-consent"):
            Journal(root).capture("session", "speech", {"text": "Sensitive child speech."},
                                  scope={"labels": ["project:a"]})
    assert policy.explain(root)["decisions"][-1]["decision"] == "deny"


@pytest.mark.parametrize("indexed", [False, True])
def test_fact_writes_and_direct_mutations_cannot_skip_modality_enforcement(tmp_path, indexed):
    root = str(tmp_path)
    existing = hierarchical.append_facts(root, [{"statement": "Reviewed public procedure.",
                                                "scopes": ["project:a"]}])[0][0]
    if indexed:
        from commontrace import fact_store

        fact_store.migrate(root)
    configure(root)
    with pytest.raises(policy.HeldForReview):
        hierarchical.append_facts(root, [{"statement": "Untagged incoming fact.", "scopes": ["project:a"]}])
    with pytest.raises(policy.HeldForReview):
        with hierarchical.mutate_facts(root) as facts:
            facts[existing.id].statement = "Untagged replacement."
    assert hierarchical.load_facts(root)[existing.id].statement == existing.statement
    with policy.ingest(root, {"modality": "text"}):
        assert hierarchical.append_facts(root, [{"statement": "Tagged incoming fact.", "scopes": ["project:a"]}])


def test_subject_context_cannot_be_overridden_by_record_metadata(tmp_path):
    root = str(tmp_path)
    configure(root)
    record = emotion()
    record.provenance.update(subject_class="adult", context="clinical", purpose=policy.MEDICAL)
    with pytest.raises(policy.HeldForReview, match="quarantine"):
        Journal(root).put(record)
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError):
            configure(root, context="clinical")


def test_cli_compile_dry_run_and_hash_only_explanation(tmp_path, capsys):
    from commontrace import cli

    root = str(tmp_path)
    binding = {"id": "school", "scopes": [], "subject_class": "adult", "context": "education"}
    assert cli.main(["person", "policy", "compile", json.dumps({"bindings": [binding]}), "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["record"]["bindings"][0]["id"] == "school"
    request = {"record": emotion().to_dict(), "source": {"modality": "voice", "labels": ["emotion"]}}
    # A source tag with emotion+biometrics remains inferred even if the caller omits inferred=True.
    assert cli.main(["person", "policy", "test", json.dumps(request), "--dest", root]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "quarantine"
    assert policy.explain(root)["decisions"] == []


def test_gateway_source_tags_are_validated_before_canonical_add(tmp_path):
    from commontrace.gateway import Gateway
    from tests.test_gateway import AUTH, call

    root = str(tmp_path)
    configure(root)
    server = Gateway(root, token=AUTH["Authorization"].split(" ", 1)[1])
    request = {"items": [{"statement": "Tagged fact from a reviewed source."}], "context": ["project:a"]}
    status, result = call(server, "POST", "/v1/memory/batch", request)
    assert status == 403, result
    assert hierarchical.load_facts(root) == {}
    request["source"] = {"modality": "text"}
    status, result = call(server, "POST", "/v1/memory/batch", request)
    assert status == 200 and result["facts"], result
    assert nextwave_store.latest(root, "subject-sources")


def test_child_expiry_filters_native_search_before_limit_and_capture_buffer(tmp_path, monkeypatch):
    root = str(tmp_path)
    configure(root, subject="child-under-13", context="home")
    scope = {"labels": ["project:a"]}
    journal = Journal(root)
    with purpose.request(root, policy.MEDICAL), policy.ingest(root, {"modality": "text"}):
        journal.put(MemoryRecord("old", "fact", 1, {"text": "Wrist wrist calibration."}, scope=scope))
        journal.capture("session", "message", {"text": "Care information."}, scope=scope, event_id="message")
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(days=3)).isoformat())
    assert journal.search("wrist", scope=scope, limit=1) == []
    assert journal.buffer("session", scope=scope) == []


def test_child_retention_cannot_be_extended_by_recompiling_a_longer_policy(tmp_path, monkeypatch):
    root = str(tmp_path)
    configure(root, subject="child-under-13", context="home")
    with purpose.request(root, policy.MEDICAL):
        Journal(root).put(emotion())
    prior = next(iter(policy._retentions(root).values()))["expires_at"]
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(days=1)).isoformat())
    binding = {"id": "home", "scopes": ["project:a"], "subject_class": "child-under-13", "context": "home",
        "retention_days": 30, "retention_purpose": policy.MEDICAL,
        "parental_consent": {"reference": "new-parent-receipt", "verification_method": "External reviewed VPC",
            "expires_at": (NOW+timedelta(days=40)).isoformat()}}
    policy.compile(root, [binding])
    original = emotion()
    from dataclasses import replace

    with purpose.request(root, policy.MEDICAL):
        Journal(root).put(replace(original, version=2))
    assert next(iter(policy._retentions(root).values()))["expires_at"] == prior


def test_child_fact_expiry_removes_canonical_content_and_policy_tampering_fails_closed(tmp_path, monkeypatch):
    root = str(tmp_path)
    configure(root, subject="child-under-13", context="home")
    with purpose.request(root, policy.MEDICAL), policy.ingest(root, {"modality": "face"}):
        fact = hierarchical.append_facts(root, [{"statement": "Reviewed facial biometric classification.",
                                                "scopes": ["project:a"]}])[0][0]
    monkeypatch.setattr(fp, "now", lambda: (NOW+timedelta(days=3)).isoformat())
    assert not memory_authority.permits(root, fact)
    assert purpose.reap(root)["regulated_retention"][0]["local_purge"]
    assert fact.id not in hierarchical.load_facts(root)
    filename = fp.filename(root, "subject-policy.json")
    with open(filename) as stream:
        signed = json.load(stream)
    signed["record"]["bindings"][0]["subject_class"] = "adult"
    with open(filename, "w") as stream:
        json.dump(signed, stream)
    with pytest.raises(PermissionError, match="signature"):
        Journal(root).put(emotion())
