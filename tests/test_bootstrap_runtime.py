import hashlib
import json
import os
import time

import pytest

from commontrace.bootstrap_runtime import merge_hooks, setup


def test_setup_is_idempotent_and_merges_native_hooks(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "existing"}]}]}}))
    config = {"agents": {"claude": {"config_path": "hooks.json"}}, "histories": []}
    first = setup(str(tmp_path), config=config)
    second = setup(str(tmp_path), config=config)
    assert first["agents"][0]["native_hooks_installed"]
    assert len(json.loads(path.read_text())["hooks"]["SessionStart"]) == 2
    assert second["elapsed_seconds"] < 120
    assert first["storage"] == "sqlite-wal"


def test_up_first_reviewed_lesson_reaches_first_prompt_hook(tmp_path, capsys):
    """A reviewed, scoped lesson is available on the first prompt after setup."""
    from types import SimpleNamespace

    from commontrace import holdout_io
    from commontrace.commands.up_cmd import run as run_up
    from commontrace.hook_kit import HookKit
    from commontrace.memory_journal import Journal
    from commontrace.memory_record import MemoryRecord

    root = str(tmp_path)
    repo = hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:24]
    agent_id = "claude-" + repo
    Journal(root).put(
        MemoryRecord(
            "reviewed-parser-rule",
            "lesson",
            1,
            {"text": "Run pytest before editing parser boundaries.", "lesson_kind": "procedure", "trigger": {}},
            status="active",
            scope={"agent": agent_id, "repo": repo},
            created_at="2026-10-12T00:00:00Z",
        )
    )

    started = time.monotonic()
    assert run_up(
        SimpleNamespace(dest=root, host=None, profile="local", setup_only=True, agent=["claude"], port=None)
    ) == 0
    output = capsys.readouterr().out
    json_start = output.rfind('{\n  "first_lesson"')
    assert json_start >= 0
    result = json.loads(output[json_start:])
    assert result["first_value_status"] == "lesson ready"
    assert result["first_lesson"]["id"] == "reviewed-parser-rule"

    class FixtureEmbedder:
        def embed(self, texts, *, query=False):
            return [[1.0, 0.0] if any(term in text.lower() for term in ("pytest", "parser", "boundary"))
                    else [0.0, 1.0] for text in texts]

    holdout_io.configure(root, rate=0)
    injected = HookKit(root, agent_id, scope={"repo": repo}, embedder=FixtureEmbedder()).handle(
        "UserPromptSubmit", {"session_id": "first-prompt", "prompt": "What should I check before changing parser boundaries?"}
    )
    assert injected["injected"] == ["reviewed-parser-rule"]
    assert "Run pytest before editing parser boundaries." in injected["context"]
    assert time.monotonic() - started < 120


def test_up_empty_store_does_not_claim_or_fabricate_a_first_lesson(tmp_path, capsys):
    from types import SimpleNamespace

    from commontrace.commands.up_cmd import run as run_up

    assert run_up(
        SimpleNamespace(dest=str(tmp_path), host=None, profile="local", setup_only=True, agent=["claude"], port=None)
    ) == 0
    output = capsys.readouterr().out
    json_start = output.rfind('{\n  "first_lesson"')
    assert json_start >= 0
    result = json.loads(output[json_start:])
    assert result["first_lesson"] is None
    assert result["first_value_status"] == "waiting for an evaluated run or reviewed import"
    assert result["fabricated_lessons"] == 0


@pytest.mark.parametrize("previous", ["python -m commontrace.hook_kit", "node hook-bridge.mjs", "python hook_bridge.py"])
def test_reinstall_replaces_previous_transport_and_preserves_other_hooks(tmp_path, previous):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"command": previous}, {"command": "other-tool"}]}}))
    merge_hooks(str(path), {"hooks": {"SessionStart": [{"command": "python hook_bridge.py --event SessionStart"}]}})
    assert json.loads(path.read_text())["hooks"]["SessionStart"] == [
        {"command": "other-tool"}, {"command": "python hook_bridge.py --event SessionStart"},
    ]
