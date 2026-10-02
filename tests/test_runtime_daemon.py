"""T3 runtime tests: git-backed memory, watch cascade, consolidation daemon."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time

import pytest

from commontrace import daemon as daemon_mod
from commontrace import memory_git
from commontrace import watch as watch_mod
from commontrace.commands import daemon_cmd, watch_cmd

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git not on PATH")


# ---------------------------------------------------------------- git


@needs_git
def test_git_init_commit_log(tmp_path):
    root = str(tmp_path)
    init = memory_git.init_repo(root)
    assert init["ok"] and init["git_available"]
    # Second init is idempotent.
    again = memory_git.init_repo(root)
    assert again["ok"] and again.get("already") is True

    os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
    with open(os.path.join(root, "memory", "lessons", "lesson_alpha.md"), "w",
              encoding="utf-8") as fh:
        fh.write("# alpha\n")
    first = memory_git.commit_all(root, "first snapshot")
    assert first["ok"] and first["committed"] is True
    assert first.get("commit")

    clean = memory_git.commit_all(root, "nothing new")
    assert clean["ok"] and clean["committed"] is False

    logged = memory_git.log(root, 5)
    assert logged["ok"] and len(logged["entries"]) >= 1
    assert logged["entries"][0]["subject"] == "first snapshot"
    assert logged["entries"][0]["hash"] == first["commit"]


@needs_git
def test_git_log_empty_repo_is_ok_with_no_entries(tmp_path):
    assert memory_git.init_repo(str(tmp_path))["ok"]
    logged = memory_git.log(str(tmp_path), 5)
    assert logged["ok"] and logged["entries"] == []


def test_git_graceful_when_not_a_repo(tmp_path):
    root = str(tmp_path / "plain")
    os.makedirs(root)
    assert memory_git.is_repo(root) is False
    committed = memory_git.commit_all(root, "msg")
    assert committed["ok"] is False and committed["committed"] is False
    logged = memory_git.log(root, 3)
    assert logged["ok"] is False and logged["entries"] == []


def test_git_graceful_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_git, "_git_binary", lambda: None)
    assert memory_git.init_repo(str(tmp_path))["ok"] is False
    assert memory_git.commit_all(str(tmp_path), "x")["committed"] is False
    assert memory_git.log(str(tmp_path))["entries"] == []


# ---------------------------------------------------------------- watch


def _seed_store(root: str) -> tuple[str, str]:
    lpath = os.path.join(root, "memory", "lessons", "lesson_beta.md")
    gpath = os.path.join(root, "memory", "graph", "edges.jsonl")
    os.makedirs(os.path.dirname(lpath), exist_ok=True)
    os.makedirs(os.path.dirname(gpath), exist_ok=True)
    with open(lpath, "w", encoding="utf-8") as fh:
        fh.write("---\nname: beta\nstatus: active\n---\n\nBody.\n")
    with open(gpath, "w", encoding="utf-8") as fh:
        fh.write('{"source": "a", "target": "b"}\n')
    return lpath, gpath


def test_watch_detects_changed_file(tmp_path):
    root = str(tmp_path)
    lpath, _gpath = _seed_store(root)
    state = os.path.join(root, "state.json")

    first = watch_mod.scan(root, state)
    assert len(first) == 2  # baseline: everything present counts as changed

    second = watch_mod.scan(root, state)
    assert second == []

    time.sleep(0.02)
    with open(lpath, "a", encoding="utf-8") as fh:
        fh.write("\nMore.\n")
    third = watch_mod.scan(root, state)
    assert third == [os.path.join("memory", "lessons", "lesson_beta.md")]

    os.unlink(lpath)
    fourth = watch_mod.scan(root, state)
    assert os.path.join("memory", "lessons", "lesson_beta.md") in fourth


def test_reconcile_rebuilds_lesson_cache(tmp_path):
    root = str(tmp_path)
    _seed_store(root)
    state = os.path.join(root, "watch.json")
    result = watch_mod.reconcile(root, state)
    assert result["changed"], "baseline pass must report files"
    assert result["rebuilt"] is True
    assert result["cache"]["ok"] is True
    # Second pass: quiet.
    quiet = watch_mod.reconcile(root, state)
    assert quiet["changed"] == [] and quiet["rebuilt"] is False

    # Touch the graph file only: still detected, cache refresh attempted.
    time.sleep(0.02)
    with open(os.path.join(root, "memory", "graph", "edges.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write('{"source": "b", "target": "c"}\n')
    again = watch_mod.reconcile(root, state)
    assert again["changed"] == [os.path.join("memory", "graph", "edges.jsonl")]


# ---------------------------------------------------------------- daemon


def test_should_run_semantics():
    assert daemon_mod.should_run("cron", {}) is True
    assert daemon_mod.should_run("once", {}) is True
    assert daemon_mod.should_run({"type": "event"}, {"pending_changes": 0}) is False
    assert daemon_mod.should_run({"type": "event"}, {"pending_changes": 2}) is True
    assert daemon_mod.should_run({"type": "idle", "threshold_s": 60}, {"idle_s": 61}) is True
    assert daemon_mod.should_run({"type": "idle", "threshold_s": 60}, {"idle_s": 5}) is False


def test_daemon_lock_is_exclusive(tmp_path):
    root = str(tmp_path)
    first = daemon_mod.acquire_lock(root)
    assert first is not None
    try:
        assert daemon_mod.acquire_lock(root) is None
    finally:
        first.release()
    retaken = daemon_mod.acquire_lock(root)
    assert retaken is not None
    retaken.release()


def test_daemon_run_once_invokes_hooks(tmp_path, monkeypatch):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
    calls: list[str] = []

    from commontrace.commands import dream_cmd
    monkeypatch.setattr(dream_cmd, "main_dream",
                        lambda r, draft=False: calls.append(r) or 0)
    result = daemon_mod.run_once(root, "once")
    assert result["ok"] and result["ran"] is True
    assert calls == [os.path.abspath(root)]
    assert result["hooks"]["dream"] == 0
    # Crash marker cleaned up; daemon state records last_run.
    assert not os.path.exists(daemon_mod.marker_path(root))
    with open(daemon_mod.state_path(root), encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["last_run"] == result["last_run"]


def test_daemon_run_once_recovers_crash_marker(tmp_path, monkeypatch):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
    from commontrace.commands import dream_cmd
    monkeypatch.setattr(dream_cmd, "main_dream", lambda r, draft=False: 0)
    mpath = daemon_mod.marker_path(root)
    os.makedirs(os.path.dirname(mpath), exist_ok=True)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write('{"pid": 999999, "started": 0}')
    result = daemon_mod.run_once(root, [{"type": "once"}])
    assert result["ok"] and result.get("recovered") is True
    assert not os.path.exists(mpath)


def test_daemon_run_once_respects_lock(tmp_path):
    root = str(tmp_path)
    lock = daemon_mod.acquire_lock(root)
    try:
        result = daemon_mod.run_once(root, "once")
    finally:
        lock.release()
    assert result["ran"] is False and result["reason"] == "locked"


def test_daemon_run_once_real_hooks_on_empty_store(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "memory", "lessons"), exist_ok=True)
    result = daemon_mod.run_once(root, "once")
    assert result["ok"] and result["ran"] is True
    assert "dream" in result["hooks"] and "consolidate" in result["hooks"]


# ---------------------------------------------------------------- CLI parsers


def _parser_with(mod) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="commontrace")
    sub = parser.add_subparsers(dest="command")
    mod.add_parser(sub)
    return parser


def test_watch_cli_parsers_exist(capsys):
    parser = _parser_with(watch_cmd)
    args = parser.parse_args(["watch", "--once"])
    assert args.once is True
    assert callable(args.func)
    root_dir = os.getcwd()
    assert watch_cmd.run(args) == 0
    assert "watch" in capsys.readouterr().out.lower()
    assert root_dir  # silence linters about unused


def test_watch_cli_reports_changes(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_store(str(tmp_path))
    parser = _parser_with(watch_cmd)
    args = parser.parse_args(["watch", "--once", "--dest", str(tmp_path)])
    assert watch_cmd.run(args) == 0
    assert "changed file" in capsys.readouterr().out


def test_daemon_cli_parsers_exist(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    os.makedirs(os.path.join(str(tmp_path), "memory", "lessons"), exist_ok=True)
    parser = _parser_with(daemon_cmd)
    args = parser.parse_args(["daemon", "--once", "--trigger", "cron",
                              "--dest", str(tmp_path)])
    assert args.once is True and args.trigger == "cron"
    assert callable(args.func)
    assert daemon_cmd.run(args) == 0
    assert "daemon" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------- memory constraints


@needs_git
def test_memory_constraints_validation(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Create memory directory with a file
    mem_dir = os.path.join(root, "memory", "lessons")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "lesson_test.md"), "w", encoding="utf-8") as f:
        f.write("# Test\n" * 10)  # Small file

    # Validation should pass
    result = memory_git.validate_memory_tree(root)
    assert result["ok"] is True
    assert len(result["errors"]) == 0

    # Create a file that exceeds default limit
    with open(os.path.join(mem_dir, "lesson_large.md"), "w", encoding="utf-8") as f:
        f.write("# Large\n" * 10000)  # Exceeds 20k chars

    result = memory_git.validate_memory_tree(root)
    assert result["ok"] is False
    assert len(result["errors"]) > 0
    assert "exceeds limit" in str(result["errors"])


@needs_git
def test_memory_constraints_config_loading(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Test default constraints
    config = memory_git._load_constraints(root)
    assert config.max_file_characters == 20_000
    assert config.max_core_memory_characters == 65_536
    assert config.max_depth == 2

    # Create custom config
    config_path = os.path.join(root, memory_git.CONSTRAINTS_CONFIG_PATH)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "version": 1,
            "maxFileCharacters": 50000,
            "maxCoreMemoryCharacters": 100000,
            "maxDepth": 5,
            "fileCharacterLimits": [
                {"pattern": "memory/lessons/*.md", "maxCharacters": 100000}
            ]
        }, f)

    # Load custom config
    config = memory_git._load_constraints(root)
    assert config.max_file_characters == 50000
    assert config.max_core_memory_characters == 100000
    assert config.max_depth == 5
    assert len(config.file_character_limits) == 1


@needs_git
def test_glob_pattern_matching(tmp_path):
    # Test glob matching for file limits
    assert memory_git._glob_match("memory/lessons/*.md", "memory/lessons/test.md")
    # * does NOT match across directory levels
    assert memory_git._glob_match("memory/lessons/*.md", "memory/lessons/sub/test.md") is False
    # ** matches across directory levels
    assert memory_git._glob_match("**/test.md", "memory/lessons/test.md")
    assert memory_git._glob_match("memory/**/test.md", "memory/lessons/sub/test.md")
    assert memory_git._glob_match("memory/**", "memory/lessons/sub/test.md")


# ---------------------------------------------------------------- pre-commit hooks


@needs_git
def test_pre_commit_hook_installation(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    result = memory_git.install_pre_commit_hook(root)
    assert result["ok"] is True
    assert "hook_path" in result

    hook_path = result["hook_path"]
    assert os.path.exists(hook_path)
    assert os.access(hook_path, os.X_OK)  # Executable

    # Check hook content
    with open(hook_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "CommonTrace Memory Validation Hook" in content
    assert "validate_memory_tree" in content


# ---------------------------------------------------------------- conflict detection and repair


@needs_git
def test_conflict_detection(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # No conflicts initially
    result = memory_git.detect_conflicts(root)
    assert result["ok"] is True
    assert result["has_conflicts"] is False
    assert len(result["conflicted_files"]) == 0

    # Test graceful handling of non-repo (use a sibling directory outside the repo)
    import tempfile
    with tempfile.TemporaryDirectory() as plain_dir:
        result = memory_git.detect_conflicts(plain_dir)
        assert result["ok"] is False
        assert result["has_conflicts"] is False
        assert "error" in result


@needs_git
def test_conflict_repair(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # No conflicts to repair
    result = memory_git.repair_conflicts(root, strategy="theirs")
    assert result["ok"] is True
    assert result["repaired"] is False
    assert len(result["files"]) == 0


@needs_git
def test_memory_repair_subagent_invocation(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Create a mock conflict summary
    conflict_summary = {
        "has_conflicts": True,
        "conflicted_files": ["memory/lessons/test.md", "config.json"]
    }

    result = memory_git.invoke_memory_repair_subagent(root, conflict_summary)
    assert result["ok"] is True
    assert "resolution" in result
    assert "strategy" in result
    assert result["strategy"]["memory_files"] == "theirs"
    assert result["strategy"]["other_files"] == "ours"


# ---------------------------------------------------------------- memory handoff pattern


@needs_git
def test_memory_handoff_token_creation(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Create initial commit
    mem_dir = os.path.join(root, "memory", "lessons")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# Test\n")
    memory_git.commit_all(root, "initial")

    # Create handoff token
    result = memory_git.create_handoff_token(root, "worker-1")
    assert result["ok"] is True
    assert "token" in result
    assert result["token"].target_worker == "worker-1"
    assert result["token"].commit_hash is not None


@needs_git
def test_memory_handoff_acceptance(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Create initial commit
    mem_dir = os.path.join(root, "memory", "lessons")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# Test\n")
    memory_git.commit_all(root, "initial")

    # Create and accept handoff
    token_result = memory_git.create_handoff_token(root, "worker-1")
    token = token_result["token"]

    result = memory_git.accept_handoff(root, token)
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["worker"] == "worker-1"

    # Test mismatched commit
    token.commit_hash = "badhash123"
    result = memory_git.accept_handoff(root, token)
    assert result["ok"] is False
    assert result["accepted"] is False
    assert "commit mismatch" in result["error"]


@needs_git
def test_background_worker_sync(tmp_path):
    root = str(tmp_path)
    memory_git.init_repo(root)

    # Create initial commit
    mem_dir = os.path.join(root, "memory", "lessons")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# Test\n")
    memory_git.commit_all(root, "initial")

    # Sync worker
    result = memory_git.sync_background_worker_memory(root, "worker-1")
    assert result["ok"] is True
    assert result["synced"] is True
    assert result["worker"] == "worker-1"
    assert result["commit"] is not None
