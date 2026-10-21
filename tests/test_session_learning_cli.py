from __future__ import annotations

from unimem import llm, session_learning, session_ledger
from unimem.cli import main
from unimem.llm_runtime import Budget, LLMRuntime


def test_runtime_learn_semantic_segmentation_is_opt_in(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_learn(root, session, *, scope, semantic_segments=False):
        calls.append((root, session, scope, semantic_segments))
        return {"operation": "none"}

    monkeypatch.setattr(session_learning, "learn", fake_learn)
    base = ["evolve", "runtime", "learn", "--session-id", "session-1", "--dest", str(tmp_path)]

    assert main(base) == 0
    assert main([*base, "--semantic-segments"]) == 0
    capsys.readouterr()

    assert [call[3] for call in calls] == [False, True]
    assert all(call[1] == "session-1" and call[2] == {"agent": "local"} for call in calls)


def test_session_segmentation_uses_its_route_and_ledger_occasion(tmp_path):
    calls = []
    fallback = llm.Config("ollama", "fallback-model", "")
    segmentation = llm.Config("ollama", "segmentation-model", "")

    def complete(_prompt, *, config):
        calls.append(config)
        return '{"boundaries": []}', {"prompt_tokens": 17, "completion_tokens": 3}

    runtime = LLMRuntime(
        str(tmp_path),
        "segmentation-session",
        routes={"default": fallback, "session-segmentation": segmentation},
        budget=Budget(calls=2, tokens=100),
        complete=complete,
    )
    entries = [
        {"sequence": 1, "event": "UserPromptSubmit", "payload": {"prompt": "first task"}},
        {"sequence": 2, "event": "PostToolUse", "payload": {"tool_name": "pytest"}},
    ]

    with runtime.scope():
        batches = session_learning.segment(entries, complete=llm.complete)

    assert batches == [entries]
    assert [config.model for config in calls] == ["segmentation-model"]
    summary = session_ledger.session_summary(str(tmp_path), "segmentation-session")
    assert summary["by_occasion"]["session-segmentation"]["calls"] == 1
    assert summary["by_occasion"]["session-segmentation"]["total_tokens"] == 20
