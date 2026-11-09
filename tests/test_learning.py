"""Durable learning across real canonical source revisions and concurrent callers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime

import pytest

from unimem import jobs, learning
from unimem.learning_contract import LearningConflict, LearningUnavailable
from unimem.memory_journal import Journal
from unimem.memory_record import MemoryRecord


@pytest.fixture
def root(tmp_path):
    path = str(tmp_path)
    Journal(path).put(MemoryRecord(id="m", kind="fact", version=1, payload={"text": "private source"}))
    learning.operate(path, "configure", {"mode": "adaptive"})
    return path


def assign(root, occasion="o", **options):
    return learning.operate(root, "assign", {"occasion_id": occasion, "memory_ids": ["m"], **options})


def finish(root, assignment, succeeded=True):
    occasion = assignment["occasion_id"]
    learning.operate(root, "delivery", {"occasion_id": occasion, "memory_ids": assignment["inject"]})
    return learning.operate(root, "outcome", {"occasion_id": occasion, "succeeded": succeeded})


def populate(root, count=180, harmful=False):
    for index in range(count):
        assignment = assign(root, str(index))
        finish(root, assignment, bool(assignment["inject"]) != harmful)


def test_source_admission_and_scope_are_not_caller_overridable(root):
    journal = Journal(root)
    journal.put(MemoryRecord(id="other", kind="fact", version=1, payload={"text": "secret"}, scope={"agent": "other"}))
    with pytest.raises(LearningUnavailable):
        learning.operate(root, "assign", {"occasion_id": "x", "memory_ids": ["other"]})
    with pytest.raises(ValueError):
        learning.operate(root, "assign", {"occasion_id": "x", "memory_ids": ["m"], "scope": {"agent": "other"}})
    with pytest.raises(LearningUnavailable):
        learning.operate(root, "status", context=["agent:other"])
    for operation, payload in [("configure", {"mode": "adaptive"}), ("run", {}), ("rollback", {"reason": "test"})]:
        with pytest.raises(PermissionError):
            learning.operate(root, operation, payload, untrusted=True)


def test_retries_are_durable_but_changed_input_and_outcome_conflict(root):
    first = assign(root)
    assert datetime.fromisoformat(first["at"]).tzinfo is not None
    assert assign(root) == first
    with pytest.raises(LearningConflict):
        assign(root, task_context="new")
    finished = finish(root, first)
    assert learning.operate(root, "outcome", {"occasion_id": "o", "succeeded": True}) == finished
    with pytest.raises(LearningConflict):
        learning.operate(root, "outcome", {"occasion_id": "o", "succeeded": False})
    assert learning.operate(root, "status")["pending_work"] is True
    with Journal(root).connect() as db:
        raw = db.execute("SELECT body FROM learning_assignments").fetchone()[0]
    assert "private source" not in raw


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("directive", {"text": "Required instruction"}),
        ("fact", {"text": "Required fact", "protected": True}),
        ("profile", {"text": "Critical limit", "critical": True}),
        ("profile", {"text": "Critical legacy limit", "critical": "required"}),
        ("lesson", {"text": "Required procedure", "lesson_kind": "directive"}),
    ],
)
def test_governance_channels_are_never_withheld(root, kind, payload):
    Journal(root).put(MemoryRecord("mandatory", kind, 1, payload))
    for index in range(12):
        assignment = learning.operate(root, "assign", {"occasion_id": f"required-{index}", "memory_ids": ["mandatory"]})
        assert assignment["inject"] == ["mandatory"]
        assert assignment["arms"][0]["rate"] == 0


def test_sources_are_revalidated_on_retry_delivery_and_run(root):
    assignment = assign(root)
    record = Journal(root).current("m", scope={})
    Journal(root).put(replace(record, version=2, payload={"text": "updated"}))
    with pytest.raises(LearningConflict):
        assign(root)
    with pytest.raises(LearningConflict):
        learning.operate(root, "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]})
    report = learning.operate(root, "run")
    assert report["stale_sources"] == ["m"]
    assert not report["applied"]


def test_explicit_control_delivery_and_partial_receipts(root):
    assignments = [assign(root, str(i)) for i in range(20)]
    control = next(a for a in assignments if not a["inject"])
    assert (
        learning.operate(root, "delivery", {"occasion_id": control["occasion_id"], "memory_ids": []})["delivery"] == []
    )
    injected = next(a for a in assignments if a["inject"])
    learning.operate(root, "delivery", {"occasion_id": injected["occasion_id"], "memory_ids": []})
    with pytest.raises(LearningConflict):
        learning.operate(root, "delivery", {"occasion_id": injected["occasion_id"], "memory_ids": ["m"]})
    with pytest.raises(ValueError):
        learning.operate(root, "delivery", {"occasion_id": control["occasion_id"], "memory_ids": ["m"]})


def test_delayed_graded_outcomes_keep_assignment_time_utility(root):
    utility = {"terms": {"speed": {"low": 0, "high": 100}}}
    old = learning.operate(root, "configure", {"mode": "observe", "utility": utility})
    assignment = assign(root)
    learning.operate(root, "configure", {"mode": "observe", "utility": {"terms": {"speed": {"low": 0, "high": 200}}}})
    result = learning.operate(
        root, "outcome", {"occasion_id": "o", "experiment_id": old["experiment_id"], "metrics": {"speed": 50}}
    )
    assert result["outcome"]["reward"] == 0.5
    assert result["outcome"]["utility_digest"] == assignment["utility_digest"]


@pytest.mark.parametrize(
    "mode,harmful,applied",
    [("adaptive", False, True), ("adaptive", True, True), ("observe", False, False), ("off", False, False)],
)
def test_actual_measured_policy_lifecycle(root, mode, harmful, applied):
    learning.operate(root, "configure", {"mode": mode})
    populate(root, harmful=harmful)
    result = learning.operate(root, "run")
    assert result["applied"] is applied
    if applied:
        policy = learning.operate(root, "status")["policy"]
        assert list(policy.values())[0]["disabled"] is harmful
        if not harmful:
            assert list(policy.values())[0]["rate"] == 0.05
        assert learning.operate(root, "run")["applied"] is False
        restored = learning.operate(root, "rollback", {"reason": "regression", "decision_revision": 0})
        assert restored["policy"] == {}
        assert restored["decision_revision"] == 2
        assert learning.operate(root, "status")["occasions"] == 180
        assert learning.operate(root, "run")["applied"] is False


def test_concurrent_conflicting_outcomes_accept_exactly_one(root):
    assign(root)

    def write(value):
        try:
            learning.operate(root, "outcome", {"occasion_id": "o", "succeeded": value})
            return "accepted"
        except LearningConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, (True, False))) == ["accepted", "conflict"]
    with Journal(root).connect() as db:
        assert db.execute("SELECT pending FROM learning_experiments").fetchone()[0] == 1


def test_existing_jobs_recover_atomic_pending_work_after_restart(root, monkeypatch):
    finish(root, assign(root))
    assert learning.operate(root, "status")["pending_work"]
    real = learning.operate

    def crash(*args, **kwargs):
        raise RuntimeError("worker died")

    monkeypatch.setattr(learning, "operate", crash)
    assert jobs.run_pending(root)["failed"] == 1
    monkeypatch.setattr(learning, "operate", real)
    assert learning.operate(root, "status")["pending_work"]
    # Existing failed-job backoff must be respected; direct numeric recovery is
    # independently durable and later delivery of that queued job is a no-op.
    assert learning.run_pending(root) == {"processed": 1, "failed": 0}
    assert not learning.operate(root, "status")["pending_work"]


def test_policy_and_checkpoint_rollback_together_on_worker_failure(root, monkeypatch):
    finish(root, assign(root))
    real = learning.contract.evaluate

    def crash(config, rows):
        raise RuntimeError("numeric worker interrupted")

    monkeypatch.setattr(learning.contract, "evaluate", crash)
    with pytest.raises(RuntimeError):
        learning.operate(root, "run")
    assert learning.operate(root, "status")["pending_work"]
    monkeypatch.setattr(learning.contract, "evaluate", real)
    assert learning.operate(root, "run")["decision_revision"] == 0


def test_budget_never_selectively_drops_incomplete_evidence(root):
    finish(root, assign(root, "one"))
    assign(root, "two")
    report = learning.operate(root, "run", {"limit": 1})
    assert report["blocked"] == ["evidence_budget_exceeded"]
    status = learning.operate(root, "status")
    assert status["pending_work"]
    assert status["report"]["blocked"] == ["evidence_budget_exceeded"]
    assert status["report"]["evidence"] == 2
    assert learning.run_pending(root) == {"processed": 1, "failed": 0}
    assert not learning.operate(root, "status")["pending_work"]


@pytest.mark.parametrize("context", ["agent:a", [None], [1], [""], ["agent:"], ["x" * 257], ["a"] * 129])
def test_direct_local_context_validation_is_transport_independent(root, context):
    with pytest.raises(ValueError):
        learning.operate(root, "status", context=context)


def test_authenticated_principal_cannot_read_another_scope_status(root):
    from unimem.memory_control import REQUIRED_PRINCIPAL_SCOPE

    token = REQUIRED_PRINCIPAL_SCOPE.set("agent:other")
    try:
        with pytest.raises(PermissionError):
            learning.operate(root, "status", context=[])
    finally:
        REQUIRED_PRINCIPAL_SCOPE.reset(token)


def test_consent_withdrawal_blocks_source_delivery_and_replay(root):
    from datetime import datetime, timedelta, timezone

    from unimem import purpose

    allowed = purpose.DPV + "ImproveHealthcare"
    consent = purpose.consent(
        root,
        "subject@example.test",
        ["record:m"],
        [allowed],
        controller="Reviewed controller",
        collection_method="Explicit consent",
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    with pytest.raises(LearningUnavailable):
        assign(root)
    with purpose.request(root, allowed):
        assignment = assign(root)
    purpose.withdraw(root, consent["receipt"]["record"]["id"])
    with purpose.request(root, allowed):
        with pytest.raises(LearningUnavailable):
            assign(root)
        with pytest.raises(LearningUnavailable):
            learning.operate(root, "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]})
        result = learning.operate(root, "run")
    assert result["stale_sources"] == ["m"]
    assert not result["applied"]


def test_fresh_revision_learns_while_stale_evidence_is_retained(root):
    assign(root, "old")
    record = Journal(root).current("m", scope={})
    Journal(root).put(replace(record, version=2, payload={"text": "reviewed update"}))
    populate(root)
    result = learning.operate(root, "run")
    assert result["applied"] is True
    assert result["stale_sources"] == ["m"]
    assert learning.operate(root, "status")["occasions"] == 181
    assert any(d["revision"] == 2 and d["verdict"] == "HELPS" for d in result["decisions"])


def test_failed_delivery_cannot_be_dropped_to_claim_improvement(root):
    populate(root)
    for index in range(100):
        assignment = assign(root, "partial-" + str(index))
        if assignment["inject"]:
            learning.operate(root, "delivery", {"occasion_id": assignment["occasion_id"], "memory_ids": []})
            learning.operate(root, "outcome", {"occasion_id": assignment["occasion_id"], "succeeded": False})
            break
        finish(root, assignment, False)
    else:
        pytest.fail("deterministic randomization did not produce a treatment arm")
    result = learning.operate(root, "run")
    assert result["applied"] is False
    assert "incomplete_delivery" in result["blocked"]
    assert result["decisions"] == []


def test_over_budget_job_settles_refusal_and_only_fresh_evidence_requeues(root):
    import json

    finish(root, assign(root, "seed"))
    journal = Journal(root)
    with journal.connect(write=True) as db:
        namespace, experiment_id, request_digest, serialized = db.execute(
            "SELECT namespace,experiment,request_digest,body FROM learning_assignments WHERE occasion='seed'"
        ).fetchone()
        seed = json.loads(serialized)
        # Seed a realistic over-capacity durable cohort without ten thousand
        # transport round trips. No evaluator is allowed to sample this cohort.
        db.executemany(
            "INSERT INTO learning_assignments VALUES(?,?,?,?,?)",
            (
                (
                    namespace,
                    experiment_id,
                    f"large-{i}",
                    request_digest,
                    json.dumps({**seed, "occasion_id": f"large-{i}", "delivery": None, "outcome": None}),
                )
                for i in range(10000)
            ),
        )
    assert jobs.run_pending(root)["done"] == 1
    status = learning.operate(root, "status")
    assert status["occasions"] == 10001
    assert status["report"]["evidence"] == 10001
    assert status["report"]["blocked"] == ["evidence_budget_exceeded"]
    assert status["decision_revision"] == 0
    assert status["policy"] == {}
    assert not status["pending_work"]
    assert len(jobs.list_jobs(root)) == 1
    assert jobs.run_pending(root)["done"] == 0
    assert learning.enqueue_pending(root) == 0
    assert len(jobs.list_jobs(root)) == 1
    learning.operate(root, "outcome", {"occasion_id": "large-0", "succeeded": False})
    assert learning.operate(root, "status")["pending_work"]
    assert jobs.run_pending(root)["done"] == 1
    assert not learning.operate(root, "status")["pending_work"]
    assert len(jobs.list_jobs(root)) == 2
    assert jobs.run_pending(root)["done"] == 0


def test_duplicate_checkpoint_job_clears_recovered_work_request(root):
    finish(root, assign(root))
    first = learning.operate(root, "run")
    with Journal(root).connect(write=True) as db:
        # Simulate recovery of an already evaluated outbox request. A second
        # worker must acknowledge it without inventing another policy revision.
        db.execute("UPDATE learning_experiments SET pending=1")
    assert jobs.run_pending(root)["done"] == 1
    status = learning.operate(root, "status")
    assert not status["pending_work"]
    assert status["decision_revision"] == first["decision_revision"]
    assert status["report"]["evidence"] == 1
    assert jobs.run_pending(root)["done"] == 0
    assert len(jobs.list_jobs(root)) == 1


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("directive", {"text": "interlock"}),
        ("profile", {"text": "critical preference", "critical": True}),
        ("lesson", {"text": "mandatory instruction", "lesson_kind": "directive"}),
    ],
)
def test_mandatory_canonical_memory_channels_are_never_experimental(root, kind, payload):
    Journal(root).put(MemoryRecord(id="mandatory", kind=kind, version=1, payload=payload))
    for occasion in ("one", "two", "three"):
        assignment = learning.operate(root, "assign", {"occasion_id": occasion, "memory_ids": ["mandatory"]})
        assert assignment["inject"] == ["mandatory"]
        assert assignment["arms"][0]["rate"] == 0


def test_original_actor_revocation_blocks_owner_worker_promotion(root):
    import json

    from unimem import memory_authority, policy_io, policy_proof

    policy = {
        "version": 1,
        "domain": {
            "principals": [
                {"id": "operator:local", "class": "admin", "attributes": {}},
                {"id": "agent:learner", "class": "learner", "attributes": {}},
            ],
            "actions": list(policy_proof.ACTIONS),
            "memory_classes": ["fact"],
            "label_sets": [[]],
            "purposes": ["default"],
        },
        "rules": [
            {"id": "admin", "effect": "allow", "principal_classes": ["admin"]},
            {"id": "learner", "effect": "allow", "principals": ["agent:learner"], "actions": ["read", "recall"]},
        ],
    }
    certificate = policy_io.certify(root, policy)
    receipt = policy_io.review(root, certificate, reason="Reviewed actor-specific learning access")
    policy_io.activate(root, certificate, review_receipt=receipt)
    with memory_authority.writer("learner", "agent"):
        populate(root)
    with Journal(root).connect() as db:
        stored = json.loads(db.execute("SELECT body FROM learning_assignments LIMIT 1").fetchone()[0])
    assert stored["_authority"] == {"principal": "learner", "authority": "agent", "purpose": ""}
    assert "_authority" not in finish(root, assign(root, "owner-receipt"))
    narrowed = {**policy, "rules": [policy["rules"][0]]}
    policy_io.activate(root, policy_io.certify(root, narrowed))
    assert Journal(root).current("m", scope={}) is not None
    with memory_authority.writer("learner", "agent"):
        assert Journal(root).current("m", scope={}) is None
        with pytest.raises(PermissionError):
            learning.operate(root, "run")
    result = learning.operate(root, "run")
    assert "original_authority_unavailable" in result["blocked"]
    assert result["decisions"] == []
    assert not result["applied"]
    assert learning.operate(root, "status")["policy"] == {}


def test_original_processing_purpose_is_replayed_without_worker_inheritance(root):
    from unimem import purpose

    with purpose.request(root, "original-processing-purpose"):
        assignment = assign(root)
        finish(root, assignment)
    with purpose.request(root, "worker-purpose"):
        result = learning.operate(root, "run")
        assert purpose.current(root) == "worker-purpose"
    assert "original_authority_unavailable" not in result["blocked"]
    assert "_authority" not in assignment


def test_untrusted_sdk_source_admission_uses_agent_writer(root, monkeypatch):
    from unimem import memory_authority

    learning.operate(root, "configure", {"mode": "observe"}, context=["agent:reader"])
    actors = []
    original = Journal._runtime_readable

    def readable(journal, record):
        actors.append(memory_authority.bound_writer())
        return original(journal, record)

    monkeypatch.setattr(Journal, "_runtime_readable", readable)
    learning.operate(
        root, "assign", {"occasion_id": "agent", "memory_ids": ["m"]}, context=["agent:reader"], untrusted=True
    )
    assert actors and all(actor == ("reader", "agent") for actor in actors)
    assert memory_authority.bound_writer() == ("local", "operator")
