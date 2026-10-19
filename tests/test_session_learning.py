import hashlib
import json

import pytest

from commontrace.hook_kit import HookKit
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


@pytest.mark.parametrize("explicit_parent", [False, True])
def test_subagent_start_namespaces_session_and_inherits_parent_claims(tmp_path, explicit_parent):
    root = str(tmp_path)
    agent, parent, child = "worker", "session-1", "subagent-1"
    scope = {"agent": agent}
    parent_run = hashlib.sha256(json.dumps([scope, parent], sort_keys=True).encode()).hexdigest()
    journal = Journal(root)
    journal.claim_injections(parent_run, ["injected-memory"])
    with journal.connect(write=True) as db:
        db.execute("INSERT INTO decisions VALUES(?,?)", (parent_run, "decision-memory"))
    payload = {"session_id": parent, "agent_id": child}
    if explicit_parent:
        payload["parent_session_id"] = parent

    HookKit(root, agent, embedder="none").handle("SubagentStart", payload, capture_only=True)

    child_session = f"{parent}:{child}"
    child_run = hashlib.sha256(json.dumps([scope, child_session], sort_keys=True).encode()).hexdigest()
    assert journal.unclaimed(child_run, ["injected-memory", "decision-memory"]) == []
    captured_rows = journal.buffer(child_run, scope=scope)
    assert captured_rows and captured_rows[0]["payload"]["session_id"] == child_session
    assert captured_rows[0]["payload"]["parent_session_id"] == parent


def test_subagent_stop_records_causal_holdout_outcome_for_claimed_memory(tmp_path):
    from commontrace import holdout_io

    root = str(tmp_path)
    agent, parent, child = "worker", "session-1", "subagent-1"
    scope = {"agent": agent}
    child_session = f"{parent}:{child}"
    run_id = hashlib.sha256(json.dumps([scope, child_session], sort_keys=True).encode()).hexdigest()
    occasion = f"{agent}:{run_id}"
    holdout_io.configure(root, rate=.25, salt="subagent-outcome")
    holdout_io.assign_and_log(root, ["newly-claimed-memory"], occasion_id=occasion, rate=.25,
                              salt="subagent-outcome")
    journal = Journal(root)
    journal.claim_injections(run_id, ["newly-claimed-memory"])

    HookKit(root, agent, embedder="none").handle(
        "SubagentStop", {"session_id": parent, "parent_session_id": parent, "agent_id": child,
                         "succeeded": True}, capture_only=True,
    )

    assert journal.unclaimed(run_id, ["newly-claimed-memory"]) == []
    assert holdout_io.read_outcomes(root)[occasion] is True


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


@pytest.mark.parametrize("text", ["x" * 50000, "😀" * 5000, '\\"\n' * 10000], ids=["ascii", "unicode", "escapes"])
def test_oversize_segment_never_truncates_or_dispatches(text):
    import copy

    entries = [{"id": "m", "sequence": 1, "event": "PostToolUseFailure", "payload": {"text": text}}]
    original = copy.deepcopy(entries)
    calls = []
    with pytest.raises(ValueError, match="budget"):
        segment(entries, max_tokens=100, complete=lambda prompt: calls.append(prompt))
    assert entries == original
    assert calls == []


def test_semantic_segment_counts_whole_serialized_prompt():
    entries = [{"payload": {"text": '😀 \\"\n' * 3}, "sequence": i} for i in range(40)]
    calls = []

    def boundaries(prompt):
        calls.append(prompt)
        assert len(prompt.encode("utf-8")) <= 512
        return '{"boundaries": []}', {}

    batches = segment(entries, max_tokens=512, max_messages=32, complete=boundaries)
    assert [row for batch in batches for row in batch] == entries
    assert max(map(len, batches)) <= 32
    assert calls


@pytest.mark.parametrize("option,value", [("max_tokens", 0), ("max_tokens", True), ("max_messages", 0),
                                         ("max_messages", 2.5), ("max_messages", -1)])
def test_segment_rejects_invalid_limits_before_work(option, value):
    with pytest.raises(ValueError):
        segment([], **{option: value})


def test_curator_preselector_cannot_reintroduce_unbounded_captures(tmp_path):
    root = str(tmp_path)
    journal = Journal(root)
    journal.capture("bounded", "UserPromptSubmit", {"prompt": "fix parser"}, scope={})
    for _ in range(24):
        journal.capture("bounded", "PreToolUse", {"tool_name": "pytest", "arguments": "x" * 600}, scope={})
    journal.capture("bounded", "SessionEnd", {"succeeded": True, "fix": "Validate parser boundaries."}, scope={})
    captured_rows = journal.buffer("bounded", scope={})
    calls = []

    def bounded_model(prompt):
        calls.append(prompt)
        assert len(prompt.encode("utf-8")) <= 6000
        return model(prompt)

    result = learn(root, "bounded", scope={}, complete=bounded_model)
    assert result["operation"] == "create"
    assert len(calls) == 2
    offered = json.loads(calls[0].split("\n", 1)[1])
    assert 0 < len(offered["selected"]) < len(captured_rows)
    assert len(offered["cases"]) <= 9
    assert len(offered["prior"]) <= 10
    assert not journal.buffer("bounded", scope={})


def test_curator_prior_slice_uses_task_relevance_before_top_k(tmp_path):
    from commontrace.memory_record import MemoryRecord

    root = str(tmp_path)
    journal = captured(root)
    for index in range(15):
        journal.put(MemoryRecord(
            f"a-unrelated-{index:02}", "lesson", 1,
            {"text": f"Document the on-call rotation for service {index}."},
            scope={"agent": "a"},
        ))
    journal.put(MemoryRecord(
        "z-relevant", "lesson", 1,
        {"text": "When pytest fails on parser boundaries, validate the parser input first."},
        scope={"agent": "a"},
    ))
    calls = []

    def update_relevant_prior(prompt):
        calls.append(prompt)
        if "WRITER" in prompt:
            return json.dumps({"accept": True, "reason": "grounded update"}), {}
        offered = json.loads(prompt.split("\n", 1)[1])
        offered_prior_ids = {record["id"] for record in offered["prior"]}
        assert "z-relevant" in offered_prior_ids
        assert not any(identity.startswith("a-unrelated-") for identity in offered_prior_ids)
        return json.dumps({
            "operation": "update",
            "target_id": "z-relevant",
            "kind": "procedure",
            "text": "Validate parser boundaries before running pytest.",
            "trigger": {},
        }), {}

    result = learn(root, "r", scope={"agent": "a"}, complete=update_relevant_prior)
    assert result["operation"] == "update"
    assert len(calls) == 2
    updated = journal.search(
        "parser", scope={"agent": "a"}, kinds=("lesson",), statuses=("quarantined",), limit=1
    )[0]
    assert updated.relations["updates"] == ["z-relevant"]
    assert updated.provenance["source_case_ids"]


def test_oversize_capture_keeps_later_actionable_work_unsealed(tmp_path):
    journal = Journal(str(tmp_path))
    journal.capture("r", "UserPromptSubmit", {"prompt": "😀" * 4000}, scope={"agent": "a"})
    captured(str(tmp_path))
    before = journal.buffer("r", scope={"agent": "a"})
    calls = []
    with pytest.raises(ValueError, match="budget"):
        learn(str(tmp_path), "r", scope={"agent": "a"}, complete=lambda prompt: calls.append(prompt))
    assert calls == []
    assert journal.buffer("r", scope={"agent": "a"}) == before
    assert not journal.search("", scope={"agent": "a"}, kinds=("lesson",), statuses=("quarantined",))
    assert learn(str(tmp_path), "r", scope={"agent": "a"}, complete=model, max_tokens=80000)["operation"] == "create"
    assert not journal.buffer("r", scope={"agent": "a"})


def test_writer_oversize_proposal_never_dispatches_or_seals(tmp_path):
    journal = captured(str(tmp_path))
    before = journal.buffer("r", scope={"agent": "a"})
    calls = []

    def oversized_model(prompt):
        calls.append(prompt)
        assert len(prompt.encode("utf-8")) <= 6000
        return json.dumps({"operation": "create", "kind": "procedure", "text": "x" * 9000}), {}

    with pytest.raises(ValueError, match="budget"):
        learn(str(tmp_path), "r", scope={"agent": "a"}, complete=oversized_model)
    assert len(calls) == 1
    assert journal.buffer("r", scope={"agent": "a"}) == before


def test_arena_budget_and_citations_match_supplied_evidence():
    from commontrace.session_learning import arena

    entries = [{"id": str(i), "sequence": i, "event": "PostToolUseFailure", "payload": {"text": "x" * 500}}
               for i in range(32)]
    calls = []

    def extractor(prompt):
        calls.append(prompt)
        assert len(prompt.encode("utf-8")) <= 6000
        return "candidate " + str(len(calls)), {}

    result = arena(entries, extractor, extractor)
    assert len(calls) == 2
    assert calls[0] == calls[1] == result["prompt"]
    supplied = json.loads(result["prompt"])
    assert result["source_ids"] == [row["id"] for row in supplied]
    assert 0 < len(supplied) < len(entries)


def test_required_prior_evidence_cannot_be_dropped_to_fit(tmp_path):
    from commontrace.memory_record import MemoryRecord

    journal = captured(str(tmp_path))
    journal.put(MemoryRecord(
        "large-prior", "lesson", 1, {"text": "Fix parser " + "x" * 7000}, scope={"agent": "a"}
    ))
    before = journal.buffer("r", scope={"agent": "a"})
    calls = []
    with pytest.raises(ValueError, match="budget"):
        learn(str(tmp_path), "r", scope={"agent": "a"}, complete=lambda prompt: calls.append(prompt))
    assert calls == []
    assert journal.buffer("r", scope={"agent": "a"}) == before


def test_bound_token_counter_can_use_model_specific_counts():
    entries = [{"sequence": i, "payload": {"text": "x" * 100}} for i in range(8)]
    calls = []

    def counter(prompt):
        # Deliberately non-additive: count the complete input, including its suffix.
        return len(prompt) // 2 + int(prompt.endswith("]"))

    def boundaries(prompt):
        calls.append(prompt)
        assert counter(prompt) <= 256
        return '{"boundaries": []}', {}

    batches = segment(entries, max_tokens=256, count_tokens=counter, complete=boundaries)
    assert [row for batch in batches for row in batch] == entries
    assert calls
    assert any(len(prompt.encode("utf-8")) > 256 for prompt in calls)


@pytest.mark.parametrize("count", [True, -1, 1.5])
def test_invalid_token_counter_cannot_dispatch(count):
    calls = []
    with pytest.raises(ValueError, match="counter"):
        segment([{"payload": {}}], complete=lambda prompt: calls.append(prompt), count_tokens=lambda _: count)
    assert calls == []


def test_arena_indivisible_oversize_input_cannot_dispatch():
    from commontrace.session_learning import arena

    calls = []
    with pytest.raises(ValueError, match="budget"):
        arena([{"id": "big", "sequence": 1, "event": "PostToolUseFailure", "payload": {"text": "x" * 7000}}],
              lambda prompt: calls.append(prompt), lambda prompt: calls.append(prompt))
    assert calls == []


def test_default_durable_worker_parks_oversize_without_sealing(tmp_path, monkeypatch):
    from commontrace import jobs, llm

    root = str(tmp_path)
    journal = Journal(root)
    journal.capture("r", "UserPromptSubmit", {"prompt": "x" * 7000}, scope={"agent": "a"})
    captured(root)
    before = journal.buffer("r", scope={"agent": "a"})

    def unavailable(*args, **kwargs):
        pytest.fail("an oversize source must fail before model dispatch")

    monkeypatch.setattr(llm, "complete", unavailable)
    monkeypatch.setattr(jobs, "BACKOFF_BASE", 0.0)
    job = jobs.enqueue(root, "learn-session", {"session": "r", "scope": {"agent": "a"}}, max_attempts=2)
    assert jobs.run_pending(root, limit=1)["failed"] == 1
    failed = jobs.get(root, job.id)
    assert failed.status == "failed" and "budget" in failed.error
    assert jobs.run_pending(root, limit=1)["failed"] == 1
    dead = jobs.get(root, job.id)
    assert dead.status == "dead" and dead.attempts == 2
    assert "source work remains unsealed" in dead.error
    assert journal.buffer("r", scope={"agent": "a"}) == before


def test_case_wrapper_splits_ordinary_captures_instead_of_blocking(tmp_path):
    root = str(tmp_path)
    journal = Journal(root)
    for index, (event, payload) in enumerate([
        ("UserPromptSubmit", {"prompt": "fix parser"}),
        ("PreToolUse", {"tool_name": "pytest"}),
        ("PostToolUseFailure", {"error": "x" * 2500}),
        ("UserPromptSubmit", {"prompt": "continue fix"}),
        ("PreToolUse", {"tool_name": "pytest"}),
        ("PostToolUse", {"tool_name": "pytest", "exit_code": 0, "arguments": "x" * 2500}),
        ("PostToolUse", {"tool_name": "pytest", "exit_code": 0}),
        ("Stop", {}),
    ]):
        journal.capture("r", event, payload, scope={}, event_id=str(index))
    calls, supplied_outcomes = [], set()

    def bounded_native(prompt):
        calls.append(prompt)
        assert len(prompt.encode("utf-8")) <= 6000
        if prompt.startswith("CASE:"):
            offered = json.loads(prompt.split("\n", 1)[1])
            supplied_outcomes.update(offered["outcomes"])
            return json.dumps({"fix": "Validate parser boundaries.",
                               "outcome_message_id": next(iter(offered["outcomes"]))}), {}
        return model(prompt)

    result = learn(root, "r", scope={}, complete=bounded_native)
    assert result["operation"] == "create"
    assert supplied_outcomes == {"2", "5", "6"}
    assert sum(prompt.startswith("CASE:") for prompt in calls) == 2
    assert not journal.buffer("r", scope={})


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


def test_reflection_cursor_is_scoped_when_session_ids_interleave(tmp_path):
    root = str(tmp_path)
    journal = Journal(root)
    scope_a, scope_b = {"agent": "a"}, {"agent": "b"}
    journal.capture("shared", "UserPromptSubmit", {"prompt": "task A"}, scope=scope_a)
    event_b = journal.capture("shared", "UserPromptSubmit", {"prompt": "task B"}, scope=scope_b)
    journal.capture("shared", "Stop", {}, scope=scope_a)

    assert learn(root, "shared", scope=scope_a, complete=model)["reason"] == "thin trajectory"
    pending_b = journal.buffer("shared", scope=scope_b)
    assert [entry["id"] for entry in pending_b] == [event_b["id"]]


def test_legacy_session_cursor_migrates_to_its_capture_scope(tmp_path):
    journal = Journal(str(tmp_path))
    scope_a, scope_b = {"agent": "a"}, {"agent": "b"}
    journal.capture("shared", "UserPromptSubmit", {"prompt": "task A"}, scope=scope_a)
    event_b = journal.capture("shared", "UserPromptSubmit", {"prompt": "task B"}, scope=scope_b)
    stop_a = journal.capture("shared", "Stop", {}, scope=scope_a)
    with journal.connect(write=True) as db:
        db.execute("INSERT INTO cursors(session,through) VALUES(?,?)", ("shared", stop_a["sequence"]))

    assert journal.buffer("shared", scope=scope_a) == []
    assert [entry["id"] for entry in journal.buffer("shared", scope=scope_b)] == [event_b["id"]]
    with journal.connect() as db:
        assert db.execute("SELECT 1 FROM cursors WHERE session='shared'").fetchone() is None


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
            root,
            ["tested"],
            occasion_id=str(index),
            rate=config.rate,
            salt=config.salt,
            revisions={"tested": revision},
            durable=False,
        )
        injected = "tested" not in withheld
        holdout_io.record_outcome(root, str(index), injected == beneficial, durable=False)
    changed = reconcile(root)
    assert changed[0]["integrity"] == "SOUND"
    assert journal.history("tested")[-1].status == ("active" if beneficial else "retired")


def test_split_creates_distinct_quarantined_parts_with_pinned_target(tmp_path):
    from commontrace.memory_record import MemoryRecord

    journal = captured(str(tmp_path))
    journal.put(
        MemoryRecord(
            "broad", "lesson", 1,
            {"text": "Validate parser boundaries and test database migrations."}, scope={"agent": "a"},
        )
    )

    def split_model(prompt):
        if "WRITER" in prompt:
            return json.dumps({"accept": True, "reason": "two independent procedures"}), {}
        return json.dumps(
            {
                "operation": "split",
                "target_id": "broad",
                "parts": [
                    {
                        "kind": "procedure",
                        "text": "Validate parser boundaries before pytest.",
                        "trigger": {"path": "parser"},
                    },
                    {
                        "kind": "procedure",
                        "text": "Test migration rollback before deploying database changes.",
                        "trigger": {"path": "migration"},
                    },
                ],
            }
        ), {}

    result = learn(str(tmp_path), "r", scope={"agent": "a"}, complete=split_model)
    assert result["operation"] == "split"
    parts = journal.search("", scope={"agent": "a"}, kinds=("lesson",), statuses=("quarantined",))
    assert len(parts) == 2
    assert {part.payload["target_version"] for part in parts} == {1}
    assert all(set(part.payload["replacement_ids"]) == {p.id for p in parts} for part in parts)
    assert journal.history("broad")[-1].status == "active"


def test_invalid_split_never_seals_work(tmp_path):
    captured(str(tmp_path))
    with pytest.raises(ValueError, match="split"):
        learn(
            str(tmp_path),
            "r",
            scope={"agent": "a"},
            complete=lambda _: (json.dumps({"operation": "split", "target_id": "old", "parts": []}), {}),
        )
    assert Journal(str(tmp_path)).buffer("r", scope={"agent": "a"})


@pytest.mark.parametrize("changed_target", [False, True])
def test_split_waits_for_each_part_and_never_overwrites_changed_target(tmp_path, changed_target):
    from dataclasses import replace

    from commontrace import holdout_io
    from commontrace.measure import content_revision
    from commontrace.memory_record import MemoryRecord
    from commontrace.session_learning import reconcile

    root = str(tmp_path)
    cfg = holdout_io.configure(root, rate=0.5, salt="split-fixture")
    journal = Journal(root)
    old = journal.put(MemoryRecord("old", "lesson", 1, {"text": "Broad procedure"}))
    for identity in ("left", "right"):
        journal.put(
            MemoryRecord(
                identity,
                "lesson",
                1,
                {
                    "text": identity + " focused procedure",
                    "operation": "split",
                    "operation_id": "split-group",
                    "target_id": "old",
                    "target_version": 1,
                    "replacement_ids": ["left", "right"],
                },
                status="shadow",
            )
        )

    def evaluate(identity):
        revision = content_revision(journal.history(identity)[-1].payload["text"])
        for index in range(1000):
            occasion = identity + ":" + str(index)
            withheld = holdout_io.assign_and_log(
                root,
                [identity],
                occasion_id=occasion,
                rate=cfg.rate,
                salt=cfg.salt,
                revisions={identity: revision},
                durable=False,
            )
            holdout_io.record_outcome(root, occasion, identity not in withheld, durable=False)

    evaluate("left")
    reconcile(root)
    assert journal.history("left")[-1].status == "shadow"
    assert journal.history("old")[-1].status == "active"
    if changed_target:
        journal.put(replace(old, version=2, payload={"text": "Owner corrected the procedure"}))
    evaluate("right")
    reconcile(root)
    assert journal.history("old")[-1].status == ("active" if changed_target else "deprecated")
    assert all(
        journal.history(identity)[-1].status == ("shadow" if changed_target else "active")
        for identity in ("left", "right")
    )
