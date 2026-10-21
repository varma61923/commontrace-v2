"""Optional policies are independently versioned from the causal v1 vectors."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from unimem import api_schema, holdout_io
from unimem.hook_kit import HookKit
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord
from unimem.record_api import operate
from unimem.record_visibility import visible
from unimem.session_learning import learn

_VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "conformance"
_GOVERNANCE = json.loads((_VECTORS / "governance-vectors.json").read_text(encoding="utf-8"))
_LEARNING = json.loads((_VECTORS / "learning-injection-vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", _GOVERNANCE["visibility"], ids=lambda row: row["name"])
def test_record_admission_vectors(vector):
    overrides = {key: value for key, value in vector.items() if key not in ("name", "visible", "request_scope")}
    record = MemoryRecord(**{"id": "candidate", "kind": "fact", "version": 1, "payload": {}, **overrides})
    assert visible(record, vector.get("request_scope", {}), at=_GOVERNANCE["at"]) is vector["visible"]


def test_due_record_cascade_vector(tmp_path):
    vector = _GOVERNANCE["erasure"]
    journal = Journal(str(tmp_path))
    records = [MemoryRecord(**vector[name]) for name in ("root", "derived", "survivor")]
    for record in records:
        journal.put(record)
        journal.cache_vector(record.id, "fixture", record.payload["text"], [1.0, 0.0])
    assert journal.current(records[0].id, scope={}) is None
    assert journal.history(records[0].id)
    receipts = journal.reap_due(limit=vector["limit"])
    assert len(receipts) == 1
    assert receipts[0]["deleted"] == vector["deleted"]
    assert receipts[0]["reason"] == vector["reason"]
    for record in records[:2]:
        assert journal.history(record.id) == []
        assert journal.vector(record.id, "fixture", record.payload["text"]) is None
    assert journal.current(records[2].id, scope={}) is not None
    assert journal.reap_due(limit=vector["limit"]) == []


def test_expiration_selection_cannot_erase_a_new_revision(tmp_path):
    journal = Journal(str(tmp_path))
    selected = MemoryRecord(**_GOVERNANCE["erasure"]["root"])
    journal.put(selected)
    revised = replace(selected, version=2, payload={"text": "retention revoked"})
    journal.put(revised)
    with pytest.raises(ValueError, match="version changed"):
        journal.forget(selected.id, reason=selected.payload["forget_reason"], expected_version=selected.version)
    assert journal.current(selected.id, scope={}) == revised
    assert len(journal.history(selected.id)) == 2


def test_untrusted_local_caller_cannot_reap_another_scope(tmp_path):
    journal = Journal(str(tmp_path))
    record = MemoryRecord(**_GOVERNANCE["erasure"]["root"], scope={"user": "owner"})
    journal.put(record)
    with pytest.raises(PermissionError, match="local owner"):
        operate(str(tmp_path), "record-reap-due", {"limit": 1}, context=["user:attacker"], untrusted=True)
    assert journal.history(record.id)


@pytest.mark.parametrize("limit", [0, 101, True, 1.5, "1"])
def test_reaper_wire_contract_rejects_invalid_batch_limits(limit):
    with pytest.raises(ValueError):
        api_schema.validate_request("record-reap-due", {"limit": limit})


@pytest.mark.parametrize("body", [{}, {"limit": 1}, {"limit": 100}])
def test_reaper_wire_contract_accepts_optional_bounded_limit(body):
    api_schema.validate_request("record-reap-due", body)


class _Embedder:
    def __init__(self, agrees=True):
        self.agrees = agrees

    def embed(self, texts, *, query=False):
        return [[1.0, 0.0] if not query or self.agrees else [0.0, 1.0] for _ in texts]


@pytest.mark.parametrize("vector", _LEARNING["injection"], ids=lambda row: row["name"])
def test_injection_policy_vectors(tmp_path, vector):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.01, salt="extension-fixture")
    kit = HookKit(root, "agent", embedder=_Embedder(vector.get("dense_agrees", True)))
    kit.journal.put(MemoryRecord(
        "candidate", "lesson", 1,
        {"text": vector["text"], "trigger": vector.get("trigger", {})},
        status=vector["status"], scope=kit.scope,
    ))
    payload = {"session_id": "run", "prompt": "pytest parser", "budget": vector.get("budget", 600)}
    reply = kit.handle("UserPromptSubmit", payload, deadline_ms=1000, defer_delivery=True)
    assert reply["injected"] == vector["injected"]
    assert reply["shadow"] == vector["shadow"]
    assert holdout_io.read_log(root)[0] == []
    if not vector["injected"]:
        assert reply["context"] == ""
        assert not reply.get("delivery_token")
        return
    assert "read-only reference data" in reply["context"]
    assert len(reply["context"].encode()) <= payload["budget"] * 4
    assert reply["delivery_token"]
    kit.journal.commit_delivery(reply["delivery_token"], scope=kit.scope)
    kit.journal.commit_delivery(reply["delivery_token"], scope=kit.scope)
    rows, corrupt = holdout_io.read_log(root)
    assert not corrupt and len(rows) == 1
    assert kit.handle("UserPromptSubmit", payload, deadline_ms=1000)["injected"] == []
    kit.handle("SubagentStart", {"session_id": "child", "parent_session_id": "run"}, deadline_ms=1000)
    assert kit.handle("UserPromptSubmit", {**payload, "session_id": "child"}, deadline_ms=1000)["injected"] == []


@pytest.mark.parametrize("vector", _LEARNING["learning"], ids=lambda row: row["name"])
def test_learning_cursor_and_quarantine_vectors(tmp_path, vector):
    root = str(tmp_path)
    journal, scope, calls = Journal(root), {"agent": "agent"}, []
    for event, payload in (
        ("UserPromptSubmit", {"prompt": "fix parser"}),
        ("PostToolUseFailure", {"tool_name": "pytest", "error": "AssertionError"}),
        ("SessionEnd", {"succeeded": True, "fix": "Validate parser boundaries before pytest."}),
    ):
        journal.capture("run", event, payload, scope=scope)
    pending = journal.buffer("run", scope=scope)
    fail_stage = vector.get("fail_stage")

    def complete(prompt):
        stage = "WRITER" if "WRITER" in prompt else "CURATOR"
        calls.append(stage)
        if stage == fail_stage:
            raise RuntimeError("fixture provider unavailable")
        if stage == "WRITER":
            return json.dumps({"accept": vector["writer_accepts"]}), {}
        return json.dumps({
            "operation": vector.get("curator_operation", "create"), "kind": "pitfall",
            "text": "Validate parser boundaries before pytest.",
            "trigger": {"event": "PostToolUseFailure", "error": "AssertionError"},
        }), {}

    if fail_stage:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            learn(root, "run", scope=scope, complete=complete)
        assert journal.buffer("run", scope=scope) == pending
        assert journal.search("", scope=scope, kinds=("lesson",), statuses=("quarantined",)) == []
        fail_stage = None
    result = learn(root, "run", scope=scope, complete=complete)
    assert result["operation"] == vector["operation"]
    assert journal.buffer("run", scope=scope) == []
    lessons = journal.search("", scope=scope, kinds=("lesson",), statuses=("quarantined",))
    assert len(lessons) == vector["quarantined"]
    for record in lessons:
        assert record.provenance["source_case_ids"]
        assert record.provenance["source_versions"]
        assert record.provenance["source_traces"] == ["run"]
        assert journal.current(record.id, scope=scope) is None
    before = list(calls)
    assert learn(root, "run", scope=scope, complete=complete)["operation"] == "none"
    assert calls == before
    assert len(journal.search("", scope=scope, kinds=("episode",))) == 1
