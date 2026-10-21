"""Finite permissions govern canonical bytes and ranking inputs, not only reports."""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests import test_policy_io
from unimem import frontmatter, hierarchical, lesson_io, memory_authority, nextwave_store, paths, policy_io
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord

clock = test_policy_io.clock
configured_policy = test_policy_io.configured_policy


def activate(root, policy):
    certificate = policy_io.certify(root, policy)
    receipt = policy_io.review(root, certificate, reason="Reviewed actual storage and retrieval permissions.")
    policy_io.activate(root, certificate, review_receipt=receipt)


def test_canonical_fact_write_and_read_and_scope_tampering(tmp_path, clock):
    root = str(tmp_path)
    allowed = hierarchical.append_facts(root, [{"statement": "Approved wrist calibration.", "scopes": ["project:a"]}])
    hidden = hierarchical.append_facts(root, [{"statement": "Private medical calibration.",
                                                "scopes": ["health", "project:a"]}])
    activate(root, configured_policy())
    assert memory_authority.permits(root, allowed[0][0])
    assert not memory_authority.permits(root, hidden[0][0])
    forged = deepcopy(hidden[0][0])
    forged.scopes = ["project:a"]
    assert not memory_authority.permits(root, forged)
    with pytest.raises(PermissionError, match="policy"):
        hierarchical.append_facts(root, [{"statement": "Disallowed medical write.",
                                         "scopes": ["health", "project:a"]}])
    assert len(hierarchical.load_facts(root)) == 2


def test_runtime_policy_filters_before_result_limit_and_cannot_shadow_capture(tmp_path, clock):
    root = str(tmp_path)
    journal = Journal(root)
    scope = {"labels": ["project:a"]}
    journal.put(MemoryRecord("a-hidden", "episode", 1, {"text": "Wrist wrist calibration."}, scope=scope))
    journal.put(MemoryRecord("z-visible", "fact", 1, {"text": "Wrist calibration."}, scope=scope))
    journal.capture("s", "sensor", {"text": "Private raw capture."}, scope=scope, event_id="capture")
    policy = configured_policy()
    policy["domain"]["memory_classes"].extend(["fact", "episode", "capture"])
    policy["rules"].append({"id": "raw", "effect": "deny", "memory_classes": ["episode", "capture"]})
    activate(root, policy)
    assert [row.id for row in journal.search("wrist", scope=scope, limit=1)] == ["z-visible"]
    assert journal.latest("a-hidden") is None and not journal.history("a-hidden")
    assert not journal.buffer("s", scope=scope)
    with pytest.raises(PermissionError, match="policy"):
        journal.capture("s", "sensor", {"text": "Cannot persist."}, scope=scope)
    with pytest.raises(PermissionError, match="policy"):
        journal.put(MemoryRecord("new", "episode", 1, {"text": "Cannot persist."}, scope=scope))


def test_lesson_artifact_and_policy_administration_have_separate_permissions(tmp_path, clock):
    import os

    root = str(tmp_path)
    os.makedirs(paths.lessons_dir(root))
    path = os.path.join(paths.lessons_dir(root), "lesson_test.md")
    lesson_io.write_lesson(path, {"status": "active", "scopes": ["project:a"]}, "Original instruction.", root=root)
    nextwave_store.put(root, "reports", "private", {"text": "Private artifact."}, scopes=["project:a"])
    activate(root, configured_policy())
    from unimem import lesson_admission

    fm, body = frontmatter.read(path)
    assert not lesson_admission.eligible(root, path, fm, body)
    with pytest.raises(PermissionError, match="policy"):
        lesson_io.write_lesson(path, fm, "Changed instruction.", root=root)
    assert frontmatter.read(path)[1] == body
    with pytest.raises(PermissionError, match="policy"):
        nextwave_store.get(root, "reports", "private", owner=True)
    with pytest.raises(PermissionError, match="policy"):
        nextwave_store.put(root, "reports", "new", {"text": "Disallowed."})
    # Exact proposal artifacts remain accessible only inside operator administration.
    assert policy_io.certify(root, configured_policy())["certificate"]
    with pytest.raises(PermissionError, match="policy"):
        nextwave_store.get(root, "memory-policies", policy_io.current(root)["policy_hash"], owner=True)


def test_recall_and_export_actions_do_not_inherit_read_permission(tmp_path, clock):
    from unimem.commands.export_cmd import _lesson_rows
    from unimem.measure import CausalMemory

    root = str(tmp_path)
    facts = hierarchical.append_facts(root, [{"statement": "Wrist calibration.", "scopes": []}])
    policy = configured_policy()
    policy["rules"][0]["actions"] = ["read", "write"]
    activate(root, policy)
    assert memory_authority.permits(root, facts[0][0])

    def retrieve(query):
        return [fact for fact, _ in facts if memory_authority.permits(root, fact)]

    memory = CausalMemory(retrieve, root=root, key=lambda row: row.id, text=lambda row: row.statement)
    assert memory.recall("wrist", occasion_id="test") == []
    assert memory_authority.permits(root, facts[0][0])  # context restored after recall
    assert list(_lesson_rows(root, None, None)) == []


def test_malformed_runtime_labels_fail_closed(tmp_path, clock):
    root = str(tmp_path)
    activate(root, configured_policy())
    for labels in ([{}], [1], "project:a", None):
        assert not policy_io.permits(root, {"scopes": labels})


@pytest.mark.parametrize("indexed", [False, True])
def test_direct_canonical_transaction_cannot_bypass_write_policy_or_block_owner_erasure(tmp_path, clock, indexed):
    from unimem import fact_store

    root = str(tmp_path)
    facts = hierarchical.append_facts(root, [{"statement": "Original governed instruction."}])
    identity = facts[0][0].id
    if indexed:
        fact_store.migrate(root)
    policy = configured_policy()
    policy["rules"][0]["actions"] = ["read"]
    activate(root, policy)
    with pytest.raises(PermissionError, match="policy"):
        with hierarchical.mutate_facts(root) as view:
            view[identity].statement = "An unapproved replacement."
    assert hierarchical.load_facts(root)[identity].statement == "Original governed instruction."
    # Canonical owner removal is an independent privacy operation, not memory creation.
    with hierarchical.mutate_facts(root) as view:
        del view[identity]
    assert identity not in hierarchical.load_facts(root)


def test_read_denial_does_not_prevent_scoped_owner_runtime_erasure(tmp_path, clock):
    from unimem import record_api

    root = str(tmp_path)
    journal = Journal(root)
    journal.put(MemoryRecord("hidden", "episode", 1, {"text": "Private removable content."}, scope={"agent": "owner"}))
    policy = configured_policy()
    activate(root, policy)
    assert journal.history("hidden") == []
    with pytest.raises(PermissionError):
        record_api.operate(root, "record-forget", {"id": "hidden", "reason": "Wrong owner"},
                           context=["agent:other"], untrusted=True)
    record_api.operate(root, "record-forget", {"id": "hidden", "reason": "Owner requested erasure"},
                       context=["agent:owner"], untrusted=True)
    with journal.connect() as db:
        assert db.execute("SELECT 1 FROM records WHERE id='hidden'").fetchone() is None
