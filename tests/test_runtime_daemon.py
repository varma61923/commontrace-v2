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


@needs_git
def test_git_init_commit_log(tmp_path):
    root = str(tmp_path)
    init = memory_git.init_repo(root)
    assert init["ok"] and init["git_available"]
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


@needs_git
def test_a_store_inside_another_repo_is_never_committed(tmp_path):
    project = tmp_path / "project"
    store = project / "fleet"
    store.mkdir(parents=True)
    assert memory_git.init_repo(str(project))["ok"]
    (project / "work.txt").write_text("unrelated work in progress\n")
    (store / "memory").mkdir()
    (store / "memory" / "facts.jsonl").write_text("{}\n")

    assert memory_git.is_repo(str(store)) and not memory_git.owns_repo(str(store))
    result = memory_git.commit_all(str(store), "commontrace: fact forget x")
    assert result["committed"] is False
    assert memory_git.log(str(project))["entries"] == []


@needs_git
def test_commits_stay_inside_the_store(tmp_path):
    store = tmp_path / "fleet"
    assert memory_git.init_repo(str(store))["ok"]
    assert memory_git.owns_repo(str(store))
    (store / "a.txt").write_text("a\n")
    first = memory_git.commit_all(str(store), "snapshot")
    assert first["committed"] and first["commit"] == memory_git.head_hash(str(store))


def test_git_graceful_when_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_git, "_git_binary", lambda: None)
    assert memory_git.init_repo(str(tmp_path))["ok"] is False
    assert memory_git.commit_all(str(tmp_path), "x")["committed"] is False
    assert memory_git.log(str(tmp_path))["entries"] == []


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
    assert len(first) == 2

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
    quiet = watch_mod.reconcile(root, state)
    assert quiet["changed"] == [] and quiet["rebuilt"] is False

    time.sleep(0.02)
    with open(os.path.join(root, "memory", "graph", "edges.jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write('{"source": "b", "target": "c"}\n')
    again = watch_mod.reconcile(root, state)
    assert again["changed"] == [os.path.join("memory", "graph", "edges.jsonl")]


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
    assert root_dir


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
