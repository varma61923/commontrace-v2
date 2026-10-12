import json

import pytest

from benchmarks.injector_bench import SEMANTIC_EVENTS, poison_check
from commontrace.hook_kit import HookKit
from commontrace.memory_record import MemoryRecord
from commontrace.native_hooks import format_reply, normalize


def test_poison_and_unreviewed_directives_never_reach_native_hook_context():
    report = poison_check()
    assert report["successful_injections"] == 0
    assert report["clean_utility"]
    assert all(report["clean_utility_by_event"].values())
    assert {row["event"] for row in report["rows"] if row.get("event")} == set(SEMANTIC_EVENTS)
    assert report["passed"]


@pytest.mark.parametrize("event", SEMANTIC_EVENTS)
def test_shared_hook_core_admits_event_scoped_directive(event, tmp_path):
    kit = HookKit(str(tmp_path), "fixture", embedder="none")
    kit.journal.put(
        MemoryRecord(
            "event-rule",
            "directive",
            1,
            {"text": "Keep the capture cursor until lesson writing is persisted.", "trigger": {"event": event}},
            scope={"agent": "fixture"},
        )
    )
    payload = {
        "session_id": "event-admission",
        "prompt": "Preserve the cursor until the lesson writer persists.",
        "tool_name": "Bash",
        "tool_input": {"command": "persist the lesson"},
        "error": "tool failed after persistence check",
    }

    result = kit.handle(event, payload, deadline_ms=1000)

    assert result["injected"] == ["event-rule"]
    assert "capture cursor" in result["context"]


def test_pretool_retrieval_requires_topical_overlap_beyond_generic_timing_words(tmp_path):
    class MatchingEmbeddings:
        def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    kit = HookKit(str(tmp_path), "fixture", embedder=MatchingEmbeddings())
    for identity, text in (
        ("tests", "Run pytest before committing changes to catch test failures."),
        ("validation", "Validate request fields at the API boundary before parsing JSON."),
    ):
        kit.journal.put(
            MemoryRecord(
                identity,
                "lesson",
                1,
                {"text": text, "trigger": {"event": "PreToolUse"}},
                scope={"agent": "fixture"},
            )
        )

    validation = kit.handle(
        "PreToolUse",
        {
            "session_id": "validation-command",
            "tool_name": "Bash",
            "tool_input": {"command": "check request fields before JSON parsing"},
        },
        deadline_ms=1000,
    )
    tests = kit.handle(
        "PreToolUse",
        {
            "session_id": "test-command",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/test_cli.py before commit"},
        },
        deadline_ms=1000,
    )

    assert validation["injected"] == ["validation"]
    assert tests["injected"] == ["tests"]


def test_posttool_timeout_phrase_matches_timeout_lesson_without_bypassing_lexical_gate(tmp_path):
    class MatchingEmbeddings:
        def embed(self, texts, *, query=False):
            return [[1.0, 0.0] for _ in texts]

    kit = HookKit(str(tmp_path), "fixture", embedder=MatchingEmbeddings())
    for identity, text, trigger in (
        (
            "timeout",
            "Handle network timeouts by returning a safe empty memory context.",
            {"event": "PostToolUseFailure", "error": "timed out"},
        ),
        (
            "unrelated",
            "Keep the report nearby for the next planning meeting.",
            {"event": "PostToolUseFailure"},
        ),
    ):
        kit.journal.put(
            MemoryRecord(
                identity,
                "lesson",
                1,
                {
                    "text": text,
                    "trigger": trigger,
                },
                scope={"agent": "fixture"},
            )
        )

    result = kit.handle(
        "PostToolUseFailure",
        {
            "session_id": "timeout-error",
            "tool_name": "Bash",
            "tool_input": {"command": "download package index"},
            "error": "Read timed out while fetching package index",
        },
        deadline_ms=1000,
    )

    assert result["injected"] == ["timeout"]


@pytest.mark.parametrize("event", SEMANTIC_EVENTS)
def test_native_event_normalization_and_claude_context_serializer(event, tmp_path):
    manifest = HookKit(str(tmp_path), "fixture", embedder="none").manifest("claude")
    assert event in manifest["events"]

    payload = {"session_id": "serializer", "prompt": "keep this context"}
    normalized_event, normalized = normalize("claude", event, payload)
    if event == "PostToolUseFailure":
        # The host provides this event directly; Codex can also promote a failed PostToolUse.
        assert normalized_event == event
    assert normalized["session_id"] == "serializer"
    serialized = format_reply("claude", normalized_event, {"context": "fixture context"})
    assert serialized == {
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": "fixture context"}
    }


def test_codex_failed_tool_result_is_admitted_as_post_tool_failure():
    event, payload = normalize(
        "codex",
        "PostToolUse",
        {"session_id": "failure", "tool_response": {"exit_code": 1, "stderr": "request timed out"}},
    )

    assert event == "PostToolUseFailure"
    assert payload["error"] == {"exit_code": 1, "stderr": "request timed out"}


def test_event_fixture_keeps_cases_and_relevance_labels_separate_by_event():
    with open("benchmarks/fixtures/injector-labels.json", encoding="utf-8") as stream:
        fixture = json.load(stream)

    assert set(fixture["event_cases"]) == set(SEMANTIC_EVENTS) - {"UserPromptSubmit"}
    for event, cases in fixture["event_cases"].items():
        assert cases and all("payload" in case and "relevant" in case for case in cases)
        assert all(isinstance(case["relevant"], list) for case in cases)
