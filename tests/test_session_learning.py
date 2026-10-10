import json

import pytest

from commontrace.memory_journal import Journal
from commontrace.session_learning import learn, segment


def captured(root):
    journal = Journal(root)
    for event, payload in [
        ("UserPromptSubmit", {"prompt": "fix parser"}),
        ("PostToolUseFailure", {"tool_name": "pytest", "error": "AssertionError"}),
        ("SessionEnd", {"succeeded": True, "fix": "Validate the parser boundary before invoking pytest."}),
    ]:
        journal.capture("r", event, payload, scope={"agent": "a"})
    return journal


def model(prompt):
    if "WRITER" in prompt:
        return json.dumps({"accept": True, "reason": "novel supported case"}), {}
    return json.dumps(
        {
            "operation": "create",
            "kind": "pitfall",
            "text": "Validate parser boundaries before pytest.",
            "trigger": {"event": "PostToolUseFailure", "error": "AssertionError"},
        }
    ), {}


def test_failed_writer_does_not_seal_and_retry_is_idempotent(tmp_path):
    root = str(tmp_path)
    journal = captured(root)

    def bad(prompt):
        if "WRITER" in prompt:
            raise RuntimeError("provider down")
        return model(prompt)

    with pytest.raises(RuntimeError):
        learn(root, "r", scope={"agent": "a"}, complete=bad)
    assert journal.buffer("r", scope={"agent": "a"})
    result = learn(root, "r", scope={"agent": "a"}, complete=model)
    assert result["operation"] == "create"
    assert not journal.buffer("r", scope={"agent": "a"})
    assert learn(root, "r", scope={"agent": "a"}, complete=model)["operation"] == "none"
    records = journal.search("", scope={"agent": "a"}, statuses=("quarantined",), kinds=("lesson",))
    assert len(records) == 1
    assert records[0].provenance["source_case_ids"]


def test_hard_caps_thin_none_and_writer_rejection(tmp_path):
    assert max(len(batch) for batch in segment([{"payload": {"text": "x" * 2000}}] * 100)) <= 32
    journal = Journal(str(tmp_path))
    journal.capture("thin", "UserPromptSubmit", {"text": "hello"}, scope={})
    assert learn(str(tmp_path), "thin", scope={}, complete=model)["operation"] == "none"
    root = str(tmp_path / "second")
    captured(root)

    def reject(prompt):
        return (json.dumps({"accept": False, "reason": "duplicate"}), {}) if "WRITER" in prompt else model(prompt)

    assert learn(root, "r", scope={"agent": "a"}, complete=reject)["operation"] == "none"
    assert not Journal(root).search("", scope={"agent": "a"}, kinds=("lesson",), statuses=("quarantined",))


def test_lifecycle_changes_are_atomic_and_causal_log_drift_blocks(tmp_path, monkeypatch):
    from dataclasses import replace

    from commontrace import integrity
    from commontrace.commands import experiment_cmd
    from commontrace.measure import content_revision
    from commontrace.memory_record import MemoryRecord
    from commontrace.session_learning import reconcile

    journal = Journal(str(tmp_path))
    prior = journal.put(MemoryRecord("old", "lesson", 1, {"text": "prior"}))
    new = journal.put(
        MemoryRecord(
            "new",
            "lesson",
            1,
            {"text": "improved", "operation": "update", "target_id": "old"},
            status="shadow",
            relations={"updates": ["old"]},
        )
    )
    rows = [
        integrity.Assignment(
            "new", str(i), bool(i % 2), rate=0.5, succeeded=bool(i % 2), revision=content_revision("improved")
        )
        for i in range(2000)
    ]
    monkeypatch.setattr(experiment_cmd, "_load", lambda _: (rows, 0.5, 0))
    monkeypatch.setattr(experiment_cmd, "scope_to_current_salt", lambda _, data: (data, "", 0))
    # Assignments not produced by this salt's randomization must never promote a lesson.
    assert reconcile(str(tmp_path)) == []
    assert journal.history("new")[-1].status == "shadow"
    with pytest.raises(ValueError):
        journal.put_many([replace(prior, version=2, status="deprecated"), replace(new, version=99)])
    assert journal.history("old")[-1].status == "active"


def test_reflection_leaves_new_task_unsealed(tmp_path):
    root = str(tmp_path)
    journal = captured(root)
    journal.capture("r", "UserPromptSubmit", {"prompt": "new unfinished task"}, scope={"agent": "a"})
    assert learn(root, "r", scope={"agent": "a"}, complete=model)["operation"] == "create"
    pending = journal.buffer("r", scope={"agent": "a"})
    assert len(pending) == 1
    assert pending[0]["payload"]["prompt"] == "new unfinished task"


def test_native_tool_case_does_not_infer_run_success(tmp_path):
    root = str(tmp_path)
    journal = Journal(root)
    journal.capture("n", "UserPromptSubmit", {"prompt": "fix parser"}, scope={})
    journal.capture("n", "PostToolUseFailure", {"tool_name": "pytest", "error": "AssertionError"}, scope={})
    outcome = journal.capture("n", "PostToolUse", {"tool_name": "pytest", "exit_code": 0}, scope={})
    journal.capture("n", "Stop", {}, scope={})

    def native(prompt):
        if "CASE:" in prompt:
            return json.dumps(
                {"fix": "Validate parser boundaries before pytest.", "outcome_message_id": outcome["id"]}
            ), {}
        return model(prompt)

    assert learn(root, "n", scope={}, complete=native)["operation"] == "create"
    case = journal.search("", scope={}, kinds=("episode",))[0]
    assert case.payload["outcome_signal"] is True
    assert case.payload["outcome_scope"] == "tool"


@pytest.mark.parametrize("beneficial", [True, False])
def test_real_randomized_log_controls_promotion_and_retirement(tmp_path, beneficial):
    from commontrace import holdout_io
    from commontrace.measure import content_revision
    from commontrace.memory_record import MemoryRecord
    from commontrace.session_learning import reconcile

    root = str(tmp_path)
    config = holdout_io.configure(root, rate=0.5, salt="controlled-fixture")
    journal = Journal(root)
    lesson = journal.put(MemoryRecord("tested", "lesson", 1, {"text": "Check parser boundaries."}, status="shadow"))
    revision = content_revision(lesson.payload["text"])
    journal.authorize_evaluation("tested", actor="test operator", occasions=1000, expires="2099-01-01T00:00:00+00:00")
    for index in range(1000):
        withheld = holdout_io.assign_and_log(
            root, ["tested"], occasion_id=str(index), rate=config.rate, salt=config.salt,
                revisions={"tested": revision}, durable=False
        )
        injected = "tested" not in withheld
        holdout_io.record_outcome(root, str(index), injected == beneficial, durable=False)
    changed = reconcile(root)
    assert changed[0]["integrity"] == "SOUND"
    assert journal.history("tested")[-1].status == ("active" if beneficial else "retired")
