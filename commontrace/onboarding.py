"""Coding-agent installer: explicit git history bootstrap and isolated SDK skill."""
from __future__ import annotations

import json
import os
import subprocess

from commontrace import agent_registry, paths, trace_io


def install(root: str, agent_id: str, *, repo: str | None = None, commits: int = 100) -> dict:
    if not isinstance(commits, int) or isinstance(commits, bool) or not 0 <= commits <= 1000:
        raise ValueError("commits must be in 0..1000")
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
    signup = agent_registry.signup(root, agent_id)
    for sha, stamp, subject in history:
        if trace_io.write_new(root, title=subject, context="Git history bootstrap: " + subject,
                              solution=f"Recorded repository commit {sha}; task outcome was not evaluated.",
                              tags=["git-history"], agent_type="code", trace_id=sha,
                              extra={"scopes": ["agent:" + agent_id],
                                     "extensions": {"profile": {"git": {"commit": sha, "authored_at": stamp}}}}):
            captured += 1
    directory = os.path.join(root, "distribution", "agents", agent_id)
    filename = os.path.join(directory, "skills", "commontrace-sdk", "SKILL.md")
    paths.enforce_boundary(root, filename)
    paths.safe_prepare_output_path(filename)
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(agent_registry.SDK_SKILL)
    manifest = {k: v for k, v in signup["plugin"].items() if k != "skills"}
    with open(os.path.join(directory, "plugin.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    # Never write the token into the plugin, git history or returned file paths.
    return {**signup, "traces_imported": captured, "skill_path": filename}
