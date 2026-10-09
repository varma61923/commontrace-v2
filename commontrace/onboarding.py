"""Coding-agent installer: explicit git history bootstrap and isolated SDK skill."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys

from commontrace import agent_registry, memory_authority, paths, trace_io


def install(root: str, agent_id: str, *, repo: str | None = None, commits: int = 100,
            rotate: bool = False, labels: list[str] | None = None) -> dict:
    if not isinstance(commits, int) or isinstance(commits, bool) or not 0 <= commits <= 1000:
        raise ValueError("commits must be in 0..1000")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", agent_id or ""):
        raise ValueError("invalid agent id")
    exists = agent_id in agent_registry.load(root)
    if exists and not rotate:
        raise ValueError("agent already registered; use explicit --rotate to recover/reinstall")
    # Git history is imported as raw experience, never as proven successful tasks.
    captured = 0
    history = []
    if repo and commits:
        result = subprocess.run(["git", "log", f"--max-count={commits}",
                                 "--format=%H%x00%aI%x00%s", "--no-merges"],
                                cwd=repo, capture_output=True, text=True, timeout=30, check=True)
        for line in result.stdout.splitlines():
            sha, stamp, subject = line.split("\0", 2)
            history.append((sha, stamp, subject))
    for sha, stamp, subject in history:
        with memory_authority.writer("git-history", "external"):
            imported = trace_io.write_new(root, title=subject, context="Git history bootstrap: " + subject,
                              solution=f"Recorded repository commit {sha}; task outcome was not evaluated.",
                              tags=["git-history"], agent_type="code",
                              trace_id=hashlib.sha256((agent_id + "\0" + sha).encode()).hexdigest(),
                              extra={"scopes": ["agent:" + agent_id],
                                     "extensions": {"profile": {"git": {"commit": sha, "authored_at": stamp}}}})
        if imported:
            captured += 1
    directory = os.path.join(root, "distribution", "agents", agent_id)
    filename = os.path.join(directory, "skills", "commontrace-sdk", "SKILL.md")
    paths.enforce_boundary(root, filename)
    paths.safe_prepare_output_path(filename)
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(agent_registry.SDK_SKILL)
    hook_prefix = (shlex.quote(sys.executable) + " evolve hook" if getattr(sys, "frozen", False)
                   else shlex.quote(sys.executable) + " -m commontrace.agent_hooks")
    manifest = {"name": "commontrace-" + agent_id, "agent_id": agent_id, "version": "1.0.0",
                "api": {"version": "v1", "auth": "bearer", "token_env": "COMMONTRACE_AGENT_TOKEN"},
                "skills": [{"name": "commontrace-sdk",
                "path": "skills/commontrace-sdk/SKILL.md"}],
                "hooks": {"session_start": {"module": "commontrace.agent_hooks", "operation": "start",
                    "command": hook_prefix + " start --agent-id " + shlex.quote(agent_id)
                               + " --dest " + shlex.quote(os.path.abspath(root))},
                          "session_end": {"module": "commontrace.agent_hooks", "operation": "end",
                    "command": hook_prefix + " end --agent-id " + shlex.quote(agent_id)
                               + " --dest " + shlex.quote(os.path.abspath(root))}}}
    from commontrace import _jsonl

    manifest_path = os.path.join(directory, "plugin.json")
    paths.enforce_boundary(root, manifest_path)
    paths.safe_prepare_output_path(manifest_path)
    _jsonl.write_json(manifest_path, manifest)
    # All potentially failing filesystem/git work precedes credential issuance.
    signup = agent_registry.rotate(root, agent_id) if exists else \
        agent_registry.signup(root, agent_id, labels=labels)
    # Never write the token into the plugin, git history or returned file paths.
    return {**signup, "traces_imported": captured, "skill_path": filename}
