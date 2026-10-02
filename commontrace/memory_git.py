"""Git-backed memory: init / commit / log over the store via stdlib subprocess.

Every function is no-raise: when git is missing, the path is not a repo,
or any subprocess fails, a status dict with ``ok: False`` is returned
instead of raising. Callers (CLI, daemon) branch on the dict.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def _git_binary() -> str | None:
    return shutil.which("git")


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> tuple[bool, str, str]:
    """Run ``git <args>`` in *cwd*. Returns (ok, stdout, stderr); never raises."""
    binary = _git_binary()
    if binary is None:
        return False, "", "git executable not found on PATH"
    try:
        proc = subprocess.run(
            [binary] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "", str(exc)
    return proc.returncode == 0, proc.stdout, proc.stderr


def is_repo(root: str) -> bool:
    """True when *root* (or an ancestor) is inside a git work tree."""
    ok, _out, _err = _run_git(["rev-parse", "--git-dir"], root)
    return ok


def init_repo(root: str) -> dict:
    """``git init`` *root* unless it already is a repo.

    Also sets local ``user.name``/``user.email`` fallbacks when missing so
    that ``commit_all`` works on fresh machines. Never raises.
    """
    root = os.path.abspath(root)
    if _git_binary() is None:
        return {"ok": False, "git_available": False, "error": "git not found on PATH"}
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "git_available": True, "error": str(exc), "root": root}
    if is_repo(root):
        return {"ok": True, "git_available": True, "already": True, "root": root}
    ok, _out, err = _run_git(["init"], root)
    if not ok:
        return {"ok": False, "git_available": True, "error": err.strip() or "git init failed", "root": root}
    # Best-effort identity so commits do not fail on bare machines.
    _run_git(["config", "user.name", "commontrace"], root)
    _run_git(["config", "user.email", "commontrace@localhost"], root)
    return {"ok": True, "git_available": True, "already": False, "root": root}


def _status_porcelain(root: str) -> tuple[bool, str, str]:
    ok, out, err = _run_git(["status", "--porcelain"], root)
    if not ok:
        return False, out, err
    return True, out, err


def commit_all(root: str, message: str) -> dict:
    """Stage everything under *root* and commit with *message*.

    Returns a status dict; ``committed`` is False when the tree is clean
    (nothing to commit) or when git/repo is unavailable. Never raises.
    """
    root = os.path.abspath(root)
    if _git_binary() is None:
        return {"ok": False, "git_available": False, "committed": False,
                "error": "git not found on PATH"}
    if not is_repo(root):
        return {"ok": False, "git_available": True, "committed": False,
                "error": "not a git repository", "root": root}
    message = (message or "").strip() or "commontrace: snapshot"
    ok, _out, err = _run_git(["add", "-A"], root)
    if not ok:
        return {"ok": False, "git_available": True, "committed": False,
                "error": (err.strip() or "git add failed"), "root": root}
    ok, out, err = _status_porcelain(root)
    if not ok:
        return {"ok": False, "git_available": True, "committed": False,
                "error": (err.strip() or "git status failed"), "root": root}
    # After `git add -A`, staged changes show in `status --porcelain` too.
    if not out.strip():
        return {"ok": True, "git_available": True, "committed": False,
                "reason": "clean", "root": root}
    ok, _out, err = _run_git(["commit", "-m", message], root)
    if not ok:
        return {"ok": False, "git_available": True, "committed": False,
                "error": (err.strip() or "git commit failed"), "root": root}
    commit = head_hash(root)
    return {"ok": True, "git_available": True, "committed": True,
            "message": message, "commit": commit, "root": root}


def head_hash(root: str) -> str | None:
    ok, out, _err = _run_git(["rev-parse", "HEAD"], root)
    if not ok:
        return None
    return out.strip() or None


def log(root: str, n: int = 10) -> dict:
    """Last *n* commits, newest first. Never raises.

    Returns ``{"ok": bool, "entries": [{"hash":..., "subject":...}], ...}``.
    Empty/non-repo/git-missing yields ``ok: False`` (or ``ok: True`` with
    zero entries when the repo simply has no commits yet — distinguished
    via the ``reason`` key).
    """
    root = os.path.abspath(root)
    if _git_binary() is None:
        return {"ok": False, "git_available": False, "entries": [],
                "error": "git not found on PATH"}
    if not is_repo(root):
        return {"ok": False, "git_available": True, "entries": [],
                "error": "not a git repository", "root": root}
    try:
        count = max(1, int(n))
    except (TypeError, ValueError):
        count = 10
    sep_unit, sep_rec = "\x1f", "\x1e"
    ok, out, err = _run_git(
        ["log", f"-n{count}", f"--pretty=format:%H{sep_unit}%s{sep_unit}%aI{sep_rec}"],
        root,
    )
    if not ok:
        text = (err.strip() or out.strip()).lower()
        # A repo with zero commits reports an error; normalize to empty.
        if "does not have any commits yet" in text or "bad default revision" in text:
            return {"ok": True, "git_available": True, "entries": [],
                    "reason": "no commits yet", "root": root}
        return {"ok": False, "git_available": True, "entries": [],
                "error": (err.strip() or "git log failed"), "root": root}
    entries: list[dict] = []
    for chunk in out.split(sep_rec):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(sep_unit)
        if len(parts) < 2:
            continue
        entries.append({
            "hash": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
            "date": parts[2].strip() if len(parts) > 2 else "",
        })
    return {"ok": True, "git_available": True, "entries": entries, "root": root}
