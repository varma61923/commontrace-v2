from __future__ import annotations

import shlex

import pytest

from commontrace.hook_bridge import EVENTS
from commontrace.native_hooks import (
    CLINE,
    KIMI_HOOK_EVENTS,
    install_special,
    normalize,
    resolve_kimi_config_target,
)

try:
    import tomllib
except ImportError:  # Python 3.10 remains supported by the package.
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


def test_cline_task_complete_installs_session_end_without_inventing_outcome(tmp_path):
    events = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SessionEnd")
    manifest = {"events": {event: f"/usr/bin/python /repo/hook_bridge.py --event {event}" for event in events}}

    result = install_special(str(tmp_path), "cline", str(tmp_path / "hooks"), manifest)

    assert "TaskComplete" in CLINE
    command = (tmp_path / "hooks" / "TaskComplete").read_text(encoding="utf-8")
    assert shlex.split(command.splitlines()[-1]) == [
        "exec",
        "/usr/bin/python",
        "/repo/hook_bridge.py",
        "--event",
        "SessionEnd",
        "--format",
        "cline",
    ]
    event, payload = normalize(
        "cline",
        "SessionEnd",
        {"hookName": "TaskComplete", "taskId": "task-42", "task": "Implemented the fix."},
    )
    assert event == "SessionEnd"
    assert payload["session_id"] == "task-42"
    assert "succeeded" not in payload
    assert result["context_native"] is True


def test_cline_task_cancel_does_not_claim_success():
    event, payload = normalize(
        "cline",
        "SessionEnd",
        {"hookName": "TaskCancel", "taskId": "task-42", "task": "Partial work."},
    )
    assert event == "SessionEnd"
    assert "succeeded" not in payload


def _kimi_manifest(events=EVENTS):
    return {
        "events": {event: f"/usr/bin/python /repo/hook_bridge.py --event {event}" for event in events}
    }


def _parse_toml(text):
    if tomllib is None:
        return None
    return tomllib.loads(text)


def test_kimi_preserves_existing_toml_and_is_idempotent(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        'default_model = "kimi-code/k3"\n\n'
        "[loop_control]\nmax_attempts_per_step = 8\n\n"
        '[[hooks]]\nevent = "Stop"\ncommand = "user-hook"\ntimeout = 5\n',
        encoding="utf-8",
    )
    original = target.read_text(encoding="utf-8")
    manifest = _kimi_manifest()

    first = install_special(str(tmp_path), "kimi", str(target), manifest)
    once = target.read_text(encoding="utf-8")
    second = install_special(str(tmp_path), "kimi", str(target), manifest)
    twice = target.read_text(encoding="utf-8")

    assert first == second
    assert twice == once
    assert once.startswith(original)
    assert twice.count("# BEGIN COMMONTRACE HOOKS") == 1
    assert twice.count("# END COMMONTRACE HOOKS") == 1
    assert twice.count("hook_bridge.py --event UserPromptSubmit --format kimi") == 1
    assert 'default_model = "kimi-code/k3"' in twice
    assert "max_attempts_per_step = 8" in twice
    assert 'command = "user-hook"' in twice

    parsed = _parse_toml(twice)
    if parsed is not None:
        assert parsed["default_model"] == "kimi-code/k3"
        assert parsed["loop_control"]["max_attempts_per_step"] == 8
        entries = parsed["hooks"]
        assert sum(entry["command"] == "user-hook" for entry in entries) == 1
        commontrace_entries = [entry for entry in entries if entry["command"].endswith("--format kimi")]
        assert [entry["event"] for entry in commontrace_entries] == list(EVENTS)
        assert all(set(entry) == {"event", "command", "timeout"} for entry in commontrace_entries)
        assert all(entry["timeout"] == 1 for entry in commontrace_entries)


def test_kimi_event_mapping_and_activation_use_active_host_config(tmp_path):
    target = tmp_path / "project with spaces" / "config.toml"
    manifest = _kimi_manifest()

    result = install_special(str(tmp_path), "kimi", str(target), manifest)

    assert result["events"] == list(EVENTS)
    assert result["native_hooks_installed"] is True
    assert result["context_native"] is True
    assert result["activation"] == "kimi"
    parsed = _parse_toml(target.read_text(encoding="utf-8"))
    if parsed is not None:
        entries = parsed["hooks"]
        assert [entry["event"] for entry in entries] == list(EVENTS)
        for event, entry in zip(EVENTS, entries, strict=True):
            assert entry["command"] == manifest["events"][event] + " --format kimi"
            assert set(entry) == {"event", "command", "timeout"}
            assert 1 <= entry["timeout"] <= 600


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ({"ImaginaryEvent": "/usr/bin/hook"}, "unsupported Kimi hook event"),
        ({"Stop": ""}, "non-empty strings"),
    ],
)
def test_kimi_rejects_hook_entries_outside_the_host_contract_before_writing(tmp_path, events, message):
    target = tmp_path / "config.toml"
    target.write_text('default_model = "keep"\n', encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        install_special(str(tmp_path), "kimi", str(target), {"events": events})

    assert target.read_text(encoding="utf-8") == before
    assert set(EVENTS) <= KIMI_HOOK_EVENTS


def test_kimi_external_target_requires_explicit_allowance_and_rejects_symlinks(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    external = tmp_path / "host" / "config.toml"
    resolved, allow_external = resolve_kimi_config_target(str(root), str(external))
    assert resolved == str(external)
    assert allow_external is True
    with pytest.raises(ValueError, match="path traversal"):
        resolve_kimi_config_target(str(root), "../outside.toml")
    with pytest.raises(ValueError, match="path traversal"):
        install_special(str(root), "kimi", str(external), _kimi_manifest())

    link = tmp_path / "config-link.toml"
    external.parent.mkdir()
    external.write_text('default_model = "keep"\n', encoding="utf-8")
    link.symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        install_special(str(root), "kimi", str(link), _kimi_manifest(), allow_external=True)

    lock_target = tmp_path / "separate-host-config.toml"
    lock_target.write_text('default_model = "keep"\n', encoding="utf-8")
    lock_link = tmp_path / "separate-host-config.toml.lock"
    lock_link.symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        install_special(str(root), "kimi", str(lock_target), _kimi_manifest(), allow_external=True)


def test_kimi_rejects_malformed_managed_region_without_modifying_config(tmp_path):
    target = tmp_path / "config.toml"
    original = 'default_model = "keep"\n# BEGIN COMMONTRACE HOOKS\n[[hooks]]\n'
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete or duplicate"):
        install_special(str(tmp_path), "kimi", str(target), _kimi_manifest())

    assert target.read_text(encoding="utf-8") == original


def test_kimi_default_config_path_uses_home_and_kimi_code_home_override(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    default, default_external = resolve_kimi_config_target(str(root))
    assert default == str(home / ".kimi-code" / "config.toml")
    assert default_external is True

    override_home = tmp_path / "kimi-override"
    monkeypatch.setenv("KIMI_CODE_HOME", str(override_home))
    override, override_external = resolve_kimi_config_target(str(root))
    assert override == str(override_home / "config.toml")
    assert override_external is True
