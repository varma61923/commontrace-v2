"""Executable acceptance criteria for measured learning and benchmark leakage."""

from benchmarks import learning_stream
from unimem import learning
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


def test_chronological_simulation_has_reproducible_context_specific_effects():
    result = learning_stream.run()
    learning_stream.check(result)
    assert result["simulated"] is True
    # Pure contract fixtures are deterministic; same evidence has a stable audit digest.
    assert learning_stream.run()["evidence_digest"] == result["evidence_digest"]
    assert len(result["results"]) == 6


def test_durable_backend_actually_applies_and_rolls_back_the_measured_policy(tmp_path):
    root = str(tmp_path)
    Journal(root).put(MemoryRecord("useful", "fact", 1, {"text": "Use explicit transaction boundaries."}))
    learning.operate(root, "configure", {"mode": "adaptive"})
    for index in range(200):
        occasion = f"run-{index}"
        assigned = learning.operate(root, "assign", {"occasion_id": occasion, "memory_ids": ["useful"]})
        learning.operate(root, "delivery", {"occasion_id": occasion, "memory_ids": assigned["inject"]})
        learning.operate(root, "outcome", {"occasion_id": occasion, "succeeded": bool(assigned["inject"])})
    report = learning.operate(root, "run")
    assert report["applied"] and report["decisions"][0]["verdict"] == "HELPS"
    before = learning.operate(root, "status")
    assert before["decision_revision"] == 1
    assert list(before["policy"].values()) == [{"rate": 0.05, "disabled": False}]
    assert not learning.operate(root, "run")["applied"]
    rollback = learning.operate(root, "rollback", {"reason": "regression review", "decision_revision": 0})
    assert rollback["policy"] == {} and rollback["decision_revision"] == 2


def test_original_delegation_is_rechecked_before_deferred_learning(tmp_path, monkeypatch):
    from tests.test_capabilities import NOW, policy
    from tests.test_capability_io import authenticate, prepare, proof
    from unimem import capability_io, frontier_protocol

    monkeypatch.setattr(frontier_protocol, "now", lambda: NOW.isoformat())
    root, _ = prepare(tmp_path)
    token = capability_io.grant(root, policy(namespaces=["records"], classes=["fact", "general"]))["token"]
    context = ["agent:a", "project:a"]
    Journal(root).put(
        MemoryRecord(
            "delegated-source",
            "fact",
            1,
            {"text": "Verify calibration."},
            scope={"agent": "a", "labels": ["project:a"]},
        )
    )
    learning.operate(root, "configure", {"mode": "adaptive"}, context=context)
    with capability_io.activate(authenticate(root, token, proof(root, token))):
        assigned = learning.operate(
            root,
            "assign",
            {"occasion_id": "delegated-task", "memory_ids": ["delegated-source"]},
            context=context,
            untrusted=True,
        )
        assert "_authority" not in assigned
        learning.operate(
            root,
            "delivery",
            {"occasion_id": "delegated-task", "memory_ids": assigned["inject"]},
            context=context,
            untrusted=True,
        )
        learning.operate(
            root, "outcome", {"occasion_id": "delegated-task", "succeeded": True}, context=context, untrusted=True
        )
    assert "original_authority_unavailable" not in learning.operate(root, "run", context=context)["blocked"]
    checked = capability_io.check(root, token)
    capability_io.revoke(root, checked["chain_ids"][-1], reason="original credential revoked")
    report = learning.operate(root, "run", context=context)
    assert "original_authority_unavailable" in report["blocked"]
    assert report["applied"] is False
