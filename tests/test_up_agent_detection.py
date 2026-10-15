"""`up` discovers supported hosts without writing host-global configuration."""

import json
from pathlib import Path

import pytest

from commontrace.bootstrap_runtime import detect_installed_agents, setup
from commontrace.native_hooks import install_special


def _executable(path: Path):
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)


def test_detection_uses_path_and_known_config_roots_in_stable_order(tmp_path):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    root.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    _executable(bin_dir / "claude")
    _executable(bin_dir / "cursor-agent")
    (home / ".codex").mkdir()
    (root / ".gemini").mkdir()
    (home / ".kimi-code").mkdir()

    result = detect_installed_agents(str(root), path=str(bin_dir), home=str(home))

    assert result["detected"] == ["claude", "codex", "cursor", "gemini", "kimi"]
    assert result["project_scoped"] == ["claude", "codex", "cursor", "gemini"]
    assert result["requires_global_opt_in"] == ["kimi"]
    assert result["evidence"]["codex"] == ["host-config:.codex"]


def test_detection_respects_kimi_config_home_override(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    custom = tmp_path / "kimi-profile"
    root.mkdir()
    home.mkdir()
    custom.mkdir()
    (custom / "config.toml").write_text('default_model = "fixture"\n', encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(custom))

    result = detect_installed_agents(str(root), path="", home=str(home))

    assert result["detected"] == ["kimi"]
    assert result["evidence"]["kimi"] == ["host-config:KIMI_CODE_HOME"]


def test_setup_auto_detection_writes_project_hooks_and_skips_kimi_global_config(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    root.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    _executable(bin_dir / "codex")
    _executable(bin_dir / "kimi")
    (home / ".kimi-code").mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

    # Existing project exports remain discoverable. A detected host's private
    # home history is never scanned as a side effect of hook setup.
    history = root / ".commontrace" / "history" / "codex" / "session.jsonl"
    history.parent.mkdir(parents=True)
    row = json.dumps({
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"text": "Use parser.py for parsing."}]},
    })
    history.write_text(row, encoding="utf-8")
    private_history = home / ".codex" / "sessions" / "private.jsonl"
    private_history.parent.mkdir(parents=True)
    private_history.write_text(row, encoding="utf-8")

    result = setup(str(root), config={"agents": {}, "histories": []})

    assert [agent["agent"] for agent in result["agents"]] == ["codex"]
    assert result["agent_detection"]["selection_source"] == "auto-detected"
    assert result["agent_detection"]["detected"] == ["codex", "kimi"]
    assert result["agent_detection"]["selected"] == ["codex"]
    assert result["agent_detection"]["requires_global_opt_in"] == ["kimi"]
    assert result["history"] == [{"agent": "codex", "messages": 1}]
    assert (root / ".codex" / "hooks.json").is_file()
    assert not (home / ".kimi-code" / "config.toml").exists()


def test_explicit_agent_name_preserves_selection_and_kimi_is_an_opt_in(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    root.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    _executable(bin_dir / "kimi")
    (home / ".kimi-code").mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

    # A named host is explicit and retains the established Kimi integration.
    result = setup(str(root), config={"agents": {}, "histories": []}, agents=["kimi"])

    assert [agent["agent"] for agent in result["agents"]] == ["kimi"]
    assert result["agents"][0]["activation"] == "kimi"
    assert (home / ".kimi-code" / "config.toml").is_file()


def test_global_config_flag_can_opt_in_to_detected_kimi(tmp_path, monkeypatch, capsys):
    from commontrace.cli import main

    root = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    root.mkdir()
    home.mkdir()
    bin_dir.mkdir()
    _executable(bin_dir / "kimi")
    (home / ".kimi-code").mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

    assert main([
        "up", "--dest", str(root), "--setup-only", "--allow-global-agent-config",
    ]) == 0
    output = capsys.readouterr().out
    result_start = output.rfind('{\n  "first_lesson"')
    assert result_start >= 0
    result = json.loads(output[result_start:])

    assert [agent["agent"] for agent in result["agents"]] == ["kimi"]
    assert (home / ".kimi-code" / "config.toml").is_file()


def test_kimi_global_config_reader_rejects_files_over_64_kib(tmp_path):
    root = tmp_path / "repo"
    target = tmp_path / "home" / ".kimi-code" / "config.toml"
    root.mkdir()
    target.parent.mkdir(parents=True)
    original = b"# user configuration\n" + b"x" * 65537
    target.write_bytes(original)
    manifest = {"events": {"Stop": "commontrace-hook --event Stop"}}

    with pytest.raises(ValueError, match="Kimi config exceeds 64 KiB"):
        install_special(str(root), "kimi", str(target), manifest, allow_external=True)

    assert target.read_bytes() == original
