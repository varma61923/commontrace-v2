import hashlib
import json
import os
import time

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10 remains supported by the package.
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

from commontrace.bootstrap_runtime import merge_hooks, native_hooks, setup
from commontrace.hook_bridge import EVENTS
from commontrace.native_hooks import normalize


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


def test_setup_kimi_updates_the_active_config_and_preserves_host_settings(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    kimi_home = home / "kimi-data"
    config_path = kimi_home / "config.toml"
    kimi_home.mkdir(parents=True)
    config_path.write_text(
        'default_model = "kimi-code/k3"\n\n'
        '[providers."managed:kimi-code"]\ntype = "kimi"\nbase_url = "https://example.invalid"\n\n'
        '[[hooks]]\nevent = "Stop"\ncommand = "user-hook"\ntimeout = 5\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))

    result = setup(str(root), config={"agents": {"kimi": {}}, "histories": []})
    second = setup(str(root), config={"agents": {"kimi": {}}, "histories": []})

    assert result["agents"][0]["native_hooks_installed"] is True
    assert result["agents"][0]["activation"] == "kimi"
    assert second["agents"][0]["activation"] == "kimi"
    text = config_path.read_text(encoding="utf-8")
    assert text.count("# BEGIN COMMONTRACE HOOKS") == 1
    assert text.count("command = \"user-hook\"") == 1
    assert not (root / "distribution" / "kimi" / "config.toml").exists()
    if tomllib is not None:
        parsed = tomllib.loads(text)
        assert parsed["default_model"] == "kimi-code/k3"
        assert parsed["providers"]["managed:kimi-code"]["type"] == "kimi"
        assert sum(entry["command"] == "user-hook" for entry in parsed["hooks"]) == 1
        assert sum(entry["command"].endswith("--format kimi") for entry in parsed["hooks"]) == len(EVENTS)


def test_setup_kimi_uses_explicit_external_config_path_without_touching_default(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    host_config = home / ".kimi-code" / "config.toml"
    override_config = tmp_path / "custom-kimi" / "config.toml"
    host_config.parent.mkdir(parents=True)
    host_config.write_text('default_model = "host-default"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

    setup(
        str(root),
        config={"agents": {"kimi": {"config_path": str(override_config)}}, "histories": []},
    )

    assert host_config.read_text(encoding="utf-8") == 'default_model = "host-default"\n'
    assert override_config.is_file()
    assert "# BEGIN COMMONTRACE HOOKS" in override_config.read_text(encoding="utf-8")


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


def test_codex_post_tool_events_route_failures_through_supported_native_hook(tmp_path):
    manifest = {
        "agent": "codex",
        "events": {event: "python hook_bridge.py --event " + event for event in EVENTS},
    }

    generated = native_hooks(manifest)["hooks"]

    assert "PostToolUse" in generated
    assert "PostToolUseFailure" not in generated
    event, payload = normalize(
        "codex",
        "PostToolUse",
        {"session_id": "s", "tool_response": {"exit_code": 1, "stderr": "AssertionError"}},
    )
    assert event == "PostToolUseFailure"
    assert payload["error"] == {"exit_code": 1, "stderr": "AssertionError"}

    from commontrace.hook_kit import HookKit
    from commontrace.memory_journal import Journal
    from commontrace.memory_record import MemoryRecord

    root = str(tmp_path)
    Journal(root).put(
        MemoryRecord(
            "retry-after-assertion",
            "directive",
            1,
            {
                "text": "Fix the failed assertion before retrying the tool.",
                "trigger": {"event": "PostToolUseFailure", "error": "AssertionError"},
            },
            status="active",
            scope={"agent": "codex"},
        )
    )
    reply = HookKit(root, "codex", embedder="none").handle(event, payload)
    assert reply["injected"] == ["retry-after-assertion"]


@pytest.mark.parametrize(
    ("response", "expected_event"),
    [
        ({"exit_code": 0}, "PostToolUse"),
        ({"isError": False}, "PostToolUse"),
        ({"success": True}, "PostToolUse"),
        ("unstructured output", "PostToolUse"),
        ({"isError": True}, "PostToolUseFailure"),
        ({"success": False}, "PostToolUseFailure"),
    ],
)
def test_codex_post_tool_normalization_only_marks_explicit_failures(response, expected_event):
    event, _ = normalize("codex", "PostToolUse", {"tool_response": response})
    assert event == expected_event
