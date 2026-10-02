"""Git-backed memory: init / commit / log over the store via stdlib subprocess.

Every function is no-raise: when git is missing, the path is not a repo,
or any subprocess fails, a status dict with ``ok: False`` is returned
instead of raising. Callers (CLI, daemon) branch on the dict.

Enhanced with:
- Pre-commit validation hook with configurable limits
- Automatic conflict repair with memory repair subagent
- Memory handoff pattern for background workers
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


def _git_binary() -> str | None:
    return shutil.which("git")


# --------------------------------------------------------------------------- #
# Memory Constraints Configuration (Letta MemFS v2 pattern)
# --------------------------------------------------------------------------- #


@dataclass
class MemoryFileCharacterLimit:
    """Per-file character limit with glob pattern matching."""
    pattern: str
    max_characters: int | None


@dataclass
class MemoryConstraintsConfig:
    """Configurable memory validation limits."""
    version: int = 1
    max_file_characters: int = 20_000
    max_core_memory_characters: int = 65_536
    max_depth: int = 2
    file_character_limits: list[MemoryFileCharacterLimit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "maxFileCharacters": self.max_file_characters,
            "maxCoreMemoryCharacters": self.max_core_memory_characters,
            "maxDepth": self.max_depth,
            "fileCharacterLimits": [
                {"pattern": limit.pattern, "maxCharacters": limit.max_characters}
                for limit in self.file_character_limits
            ],
        }


DEFAULT_CONSTRAINTS = MemoryConstraintsConfig()
CONSTRAINTS_CONFIG_PATH = ".memfs.config.json"


def _load_constraints(root: str) -> MemoryConstraintsConfig:
    """Load constraints config from .memfs.config.json or return defaults."""
    config_path = os.path.join(root, CONSTRAINTS_CONFIG_PATH)
    if not os.path.exists(config_path):
        return DEFAULT_CONSTRAINTS

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("version") != 1:
            return DEFAULT_CONSTRAINTS

        limits = [
            MemoryFileCharacterLimit(
                pattern=item.get("pattern", ""),
                max_characters=item.get("maxCharacters"),
            )
            for item in data.get("fileCharacterLimits", [])
        ]

        return MemoryConstraintsConfig(
            version=data.get("version", 1),
            max_file_characters=data.get(
                "maxFileCharacters", DEFAULT_CONSTRAINTS.max_file_characters
            ),
            max_core_memory_characters=data.get(
                "maxCoreMemoryCharacters", DEFAULT_CONSTRAINTS.max_core_memory_characters
            ),
            max_depth=data.get("maxDepth", DEFAULT_CONSTRAINTS.max_depth),
            file_character_limits=limits,
        )
    except (json.JSONDecodeError, OSError, KeyError):
        return DEFAULT_CONSTRAINTS


def _glob_match(pattern: str, path: str) -> bool:
    """Match a glob pattern against a file path.

    Letta-style glob semantics:
    - * matches within a single directory level (no /)
    - ** matches across directory levels
    """
    parts = pattern.split("/")
    path_parts = path.split("/")

    # Handle **/ prefix (match any depth)
    if parts[0] == "**":
        parts = parts[1:]
        # Find where the rest of the pattern starts matching
        for i in range(len(path_parts) - len(parts) + 1):
            if fnmatch.fnmatch("/".join(path_parts[i:]), "/".join(parts)):
                return True
        return False

    # Handle ** suffix (match any depth at end)
    if parts[-1] == "**":
        parts = parts[:-1]
        if len(path_parts) < len(parts):
            return False
        return fnmatch.fnmatch("/".join(path_parts[:len(parts)]), "/".join(parts))

    # Handle ** in the middle (e.g., memory/**/test.md)
    if "**" in parts:
        # Split on first **
        idx = parts.index("**")
        prefix = parts[:idx]
        suffix = parts[idx + 1:]

        # Match prefix
        if len(path_parts) < len(prefix) + len(suffix):
            return False
        if not all(fnmatch.fnmatch(p, pat) for p, pat in zip(path_parts[:len(prefix)], prefix)):
            return False

        # Match suffix
        remaining = path_parts[len(prefix):]
        if len(remaining) < len(suffix):
            return False
        if not all(fnmatch.fnmatch(p, pat) for p, pat in zip(remaining[-len(suffix):], suffix)):
            return False

        return True

    # Standard glob matching - ensure same number of path parts
    if len(parts) != len(path_parts):
        return False

    # Match each part individually
    for pattern_part, path_part in zip(parts, path_parts):
        if not fnmatch.fnmatch(path_part, pattern_part):
            return False
    return True


def _get_file_limit(path: str, config: MemoryConstraintsConfig) -> int | None:
    """Get character limit for a file based on glob overrides."""
    for limit in config.file_character_limits:
        if _glob_match(limit.pattern, path):
            return limit.max_characters
    return config.max_file_characters


def _count_characters(path: str) -> int:
    """Count Unicode characters in a file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
            return len(content)
    except OSError:
        return 0


def _validate_file_constraints(
    root: str,
    file_path: str,
    config: MemoryConstraintsConfig,
) -> list[str]:
    """Validate a single file against constraints."""
    errors: list[str] = []
    rel_path = os.path.relpath(file_path, root)

    # Check depth
    depth = rel_path.count(os.sep)
    if depth > config.max_depth:
        errors.append(f"{rel_path}: depth {depth} exceeds maxDepth {config.max_depth}")

    # Check character limit
    char_count = _count_characters(file_path)
    limit = _get_file_limit(rel_path, config)
    if limit is not None and char_count > limit:
        errors.append(f"{rel_path}: {char_count} characters exceeds limit {limit}")

    return errors


def validate_memory_tree(root: str) -> dict[str, Any]:
    """Validate all memory files against constraints.

    Returns {"ok": bool, "errors": list[str], "violated_files": list[str]}.
    Never raises.
    """
    root = os.path.abspath(root)
    config = _load_constraints(root)
    errors: list[str] = []
    violated_files: list[str] = []

    if not os.path.exists(root):
        return {"ok": False, "errors": ["root path does not exist"], "violated_files": []}

    # Walk memory directory
    memory_dir = os.path.join(root, "memory")
    if not os.path.exists(memory_dir):
        return {"ok": True, "errors": [], "violated_files": []}

    core_chars = 0

    for dirpath, dirnames, filenames in os.walk(memory_dir):
        for filename in filenames:
            if not filename.endswith(".md"):
                continue

            file_path = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(file_path, root)

            file_errors = _validate_file_constraints(root, file_path, config)
            if file_errors:
                errors.extend(file_errors)
                violated_files.append(rel_path)

            # Count core memory (root-level .md files)
            if os.path.dirname(rel_path) == "memory":
                core_chars += _count_characters(file_path)

    # Check core memory total
    if core_chars > config.max_core_memory_characters:
        errors.append(
            f"core memory: {core_chars} characters exceeds maxCoreMemoryCharacters {config.max_core_memory_characters}"
        )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "violated_files": violated_files,
        "config": config.to_dict(),
    }


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


# --------------------------------------------------------------------------- #
# Pre-commit Hook Installation (Letta MemFS pattern)
# --------------------------------------------------------------------------- #


def _build_pre_commit_hook_script() -> str:
    """Build the pre-commit hook script for memory validation."""
    return """#!/usr/bin/env bash
# CommonTrace Memory Validation Hook
# Validates memory files against constraints before commit

set -e

# Get the repository root
REPO_ROOT="$(git rev-parse --show-toplevel)"

# Run validation using Python
python3 -c "
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath('$REPO_ROOT'))))
from commontrace import memory_git

result = memory_git.validate_memory_tree('$REPO_ROOT')
if not result['ok']:
    print('Memory validation failed:')
    for error in result['errors']:
        print(f'  {error}')
    sys.exit(1)
"
"""


def install_pre_commit_hook(root: str) -> dict:
    """Install pre-commit hook for memory validation.

    Returns {"ok": bool, "error": str | None}. Never raises.
    """
    root = os.path.abspath(root)
    if not is_repo(root):
        return {"ok": False, "error": "not a git repository", "root": root}

    hooks_dir = os.path.join(root, ".git", "hooks")
    hook_path = os.path.join(hooks_dir, "pre-commit")

    try:
        os.makedirs(hooks_dir, exist_ok=True)
        hook_content = _build_pre_commit_hook_script()
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write(hook_content)
        os.chmod(hook_path, 0o755)
        return {"ok": True, "hook_path": hook_path, "root": root}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "root": root}


# --------------------------------------------------------------------------- #
# Conflict Repair with Memory Repair Subagent
# --------------------------------------------------------------------------- #


def detect_conflicts(root: str) -> dict:
    """Detect if there are any merge conflicts in the repository.

    Returns {"ok": bool, "has_conflicts": bool, "conflicted_files": list[str]}.
    Never raises.
    """
    root = os.path.abspath(root)
    if not is_repo(root):
        return {"ok": False, "has_conflicts": False, "conflicted_files": [],
                "error": "not a git repository", "root": root}

    ok, out, _err = _run_git(["diff", "--name-only", "--diff-filter=U"], root)
    if not ok:
        return {"ok": False, "has_conflicts": False, "conflicted_files": [],
                "error": "git diff failed", "root": root}

    conflicted = [line.strip() for line in out.strip().split("\n") if line.strip()]
    return {
        "ok": True,
        "has_conflicts": len(conflicted) > 0,
        "conflicted_files": conflicted,
        "root": root,
    }


def repair_conflicts(root: str, strategy: str = "theirs") -> dict:
    """Attempt automatic conflict repair using the specified strategy.

    Strategies:
    - "theirs": accept incoming changes (useful for remote sync)
    - "ours": keep local changes (useful for local edits)
    - "union": attempt to merge both sides

    Returns {"ok": bool, "repaired": bool, "files": list[str], "error": str | None}.
    Never raises.
    """
    root = os.path.abspath(root)
    detection = detect_conflicts(root)

    if not detection["ok"]:
        return detection

    if not detection["has_conflicts"]:
        return {"ok": True, "repaired": False, "files": [], "root": root}

    conflicted = detection["conflicted_files"]
    repaired_files: list[str] = []

    for file_path in conflicted:
        if strategy == "union":
            ok, _out, err = _run_git(["checkout", "--merge", "--", file_path], root)
        else:
            ok, _out, err = _run_git(["checkout", f"--{strategy}", "--", file_path], root)
        if ok:
            repaired_files.append(file_path)
        else:
            logging.getLogger("commontrace.memory_git").warning(
                "Failed to auto-repair conflict for %s using %s: %s", file_path, strategy, err
            )

    # Stage resolved files
    if repaired_files:
        _run_git(["add"] + repaired_files, root)

    return {
        "ok": True,
        "repaired": len(repaired_files) > 0,
        "files": repaired_files,
        "total_conflicts": len(conflicted),
        "root": root,
    }


def invoke_memory_repair_subagent(
    root: str,
    conflict_summary: dict,
    *,
    subagent_runner: Callable[[str, dict], dict] | None = None,
    validate: bool = True,
) -> dict:
    """Invoke the memory repair subagent for complex conflict resolution.

    Captures conflict state and diffs, delegates to a dedicated repair subagent
    (or falls back to governed automatic resolution), stages resolved files,
    and verifies post-repair memory tree constraints.

    Returns {"ok": bool, "resolution": str | None, "error": str | None}.
    Never raises.
    """
    root = os.path.abspath(root)
    conflicted = conflict_summary.get("conflicted_files", [])
    if not conflicted:
        return {"ok": True, "resolution": "none", "conflicted_files": []}

    # 1. Capture conflict state and diffs
    conflict_details: dict[str, dict[str, str]] = {}
    for f in conflicted:
        ok, out, _err = _run_git(["diff", "--", f], root)
        conflict_details[f] = {"diff": out if ok else ""}

    # 2. If a dedicated subagent runner is provided, dispatch
    if subagent_runner is not None:
        try:
            subagent_result = subagent_runner(
                root,
                {"conflicts": conflicted, "details": conflict_details, "summary": conflict_summary},
            )
            if isinstance(subagent_result, dict) and subagent_result.get("ok"):
                remaining = detect_conflicts(root)
                if not remaining.get("has_conflicts", False):
                    if validate:
                        tree_val = validate_memory_tree(root)
                        if not tree_val["ok"]:
                            return {
                                "ok": False,
                                "resolution": "subagent_invalid_tree",
                                "errors": tree_val["errors"],
                            }
                    return {
                        "ok": True,
                        "resolution": "subagent",
                        "details": subagent_result,
                    }
        except Exception as exc:
            logging.getLogger("commontrace.memory_git").warning(
                "Memory repair subagent failed: %s; falling back to governed repair", exc
            )

    # 3. Governed automatic resolution:
    # Prefer incoming changes for memory data sync, preserve local changes for non-memory
    memory_conflicts = [f for f in conflicted if f.startswith("memory/")]
    other_conflicts = [f for f in conflicted if not f.startswith("memory/")]

    repaired_files: list[str] = []
    failed_files: list[str] = []

    if memory_conflicts:
        repair_res = repair_conflicts(root, strategy="theirs")
        repaired_files.extend(repair_res.get("files", []))
        if not repair_res.get("ok", False):
            failed_files.extend(set(memory_conflicts) - set(repair_res.get("files", [])))

    if other_conflicts:
        repair_res = repair_conflicts(root, strategy="ours")
        repaired_files.extend(repair_res.get("files", []))
        if not repair_res.get("ok", False):
            failed_files.extend(set(other_conflicts) - set(repair_res.get("files", [])))

    # Stage resolved files
    if repaired_files:
        _run_git(["add"] + repaired_files, root)

    # 4. Validate post-resolution tree against constraints
    validation_errors: list[str] = []
    if validate:
        tree_val = validate_memory_tree(root)
        if not tree_val["ok"]:
            validation_errors = tree_val["errors"]

    success = len(failed_files) == 0 and len(validation_errors) == 0
    return {
        "ok": success,
        "resolution": "governed_repair" if success else "partial",
        "strategy": {
            "memory_files": "theirs",
            "other_files": "ours",
        },
        "repaired_files": repaired_files,
        "failed_files": failed_files,
        "validation_errors": validation_errors,
        "error": None if success else f"Failed to repair {len(failed_files)} files or validation failed",
    }


# --------------------------------------------------------------------------- #
# Memory Handoff Pattern for Background Workers
# --------------------------------------------------------------------------- #


HANDOFF_STATE_FILE = "memory/.handoff_state.json"
DEFAULT_HANDOFF_TTL_SECONDS = 3600.0


@dataclass
class MemoryHandoff:
    """Represents a signed handoff of memory between workers."""
    source_worker: str
    target_worker: str
    commit_hash: str
    timestamp: str
    signature: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _sign_handoff_payload(
    secret: str,
    source_worker: str,
    target_worker: str,
    commit_hash: str,
    timestamp: str,
) -> str:
    """Compute HMAC-SHA256 signature for memory handoff."""
    payload = f"{source_worker}:{target_worker}:{commit_hash}:{timestamp}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _get_handoff_secret(root: str, explicit_secret: str | None = None) -> str:
    """Resolve handoff signing secret from parameter, environment, or repo file."""
    if explicit_secret:
        return explicit_secret
    env_secret = os.getenv("COMMONTRACE_HANDOFF_SECRET")
    if env_secret:
        return env_secret
    secret_path = os.path.join(root, ".git", "commontrace_handoff.key")
    if os.path.exists(secret_path):
        try:
            with open(secret_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    return "commontrace-default-handoff-secret"


def create_handoff_token(
    root: str,
    target_worker: str,
    *,
    source_worker: str = "current",
    secret: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Create a cryptographically signed handoff token for transferring memory.

    Returns {"ok": bool, "token": MemoryHandoff | None, "error": str | None}.
    Never raises.
    """
    root = os.path.abspath(root)
    if not is_repo(root):
        return {"ok": False, "error": "not a git repository"}

    commit = head_hash(root)
    if not commit:
        return {"ok": False, "error": "no commits in repository"}

    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).isoformat()
    signing_key = _get_handoff_secret(root, secret)
    sig = _sign_handoff_payload(signing_key, source_worker, target_worker, commit, timestamp)

    meta = metadata.copy() if metadata else {}
    meta["created_at"] = timestamp

    token = MemoryHandoff(
        source_worker=source_worker,
        target_worker=target_worker,
        commit_hash=commit,
        timestamp=timestamp,
        signature=sig,
        metadata=meta,
    )

    return {
        "ok": True,
        "token": token,
        "root": root,
    }


def accept_handoff(
    root: str,
    token: MemoryHandoff,
    *,
    worker_id: str | None = None,
    secret: str | None = None,
    allowed_workers: Sequence[str] | None = None,
    max_ttl_seconds: float = DEFAULT_HANDOFF_TTL_SECONDS,
) -> dict:
    """Accept and validate a memory handoff from another worker.

    1. Validates repository state and commit alignment.
    2. Validates HMAC signature.
    3. Validates timestamp and token freshness (TTL).
    4. Checks worker permissions and identity.
    5. Updates worker ownership metadata.
    6. Verifies memory constraints.

    Returns {"ok": bool, "accepted": bool, "error": str | None}.
    Never raises.
    """
    root = os.path.abspath(root)
    if not is_repo(root):
        return {"ok": False, "accepted": False, "error": "not a git repository"}

    # 1. Commit check
    current = head_hash(root)
    if current != token.commit_hash:
        return {
            "ok": False,
            "accepted": False,
            "error": f"commit mismatch: expected {token.commit_hash}, got {current}",
        }

    # 2. Validate handoff signature (if signature is present)
    signing_key = _get_handoff_secret(root, secret)
    expected_sig = _sign_handoff_payload(
        signing_key,
        token.source_worker,
        token.target_worker,
        token.commit_hash,
        token.timestamp,
    )
    actual_sig = token.signature or (token.metadata.get("signature") if token.metadata else "")
    if actual_sig and not hmac.compare_digest(actual_sig, expected_sig):
        return {
            "ok": False,
            "accepted": False,
            "error": "invalid handoff signature: token tampering detected",
        }

    # 3. Validate timestamp freshness
    from datetime import datetime, timezone
    try:
        token_dt = datetime.fromisoformat(token.timestamp)
        now_dt = datetime.now(timezone.utc)
        age = (now_dt - token_dt).total_seconds()
        if age < -60.0 or (max_ttl_seconds > 0 and age > max_ttl_seconds):
            return {
                "ok": False,
                "accepted": False,
                "error": f"handoff token expired: age is {age:.1f}s (max {max_ttl_seconds}s)",
            }
    except Exception:
        return {
            "ok": False,
            "accepted": False,
            "error": "invalid handoff timestamp format",
        }

    # 4. Check worker permissions
    target = token.target_worker
    if worker_id is not None and target not in (worker_id, "*"):
        return {
            "ok": False,
            "accepted": False,
            "error": f"worker permission denied: token targeted to {target}, not {worker_id}",
        }
    if allowed_workers is not None and target not in allowed_workers and "*" not in allowed_workers:
        return {
            "ok": False,
            "accepted": False,
            "error": f"worker {target} is not in allowed_workers list",
        }

    # 5. Update worker ownership metadata
    active_worker = worker_id or target
    state_file = os.path.join(root, HANDOFF_STATE_FILE)
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        ownership_record = {
            "active_worker": active_worker,
            "source_worker": token.source_worker,
            "commit_hash": token.commit_hash,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "token_timestamp": token.timestamp,
            "metadata": token.metadata,
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(ownership_record, f, indent=2)
    except OSError as exc:
        logging.getLogger("commontrace.memory_git").warning("Failed to record handoff state: %s", exc)

    # 6. Validate memory tree constraints
    tree_validation = validate_memory_tree(root)

    return {
        "ok": True,
        "accepted": True,
        "worker": token.target_worker,
        "commit": token.commit_hash,
        "root": root,
        "tree_valid": tree_validation["ok"],
        "validation_errors": tree_validation.get("errors", []),
    }


def sync_background_worker_memory(root: str, worker_id: str) -> dict:
    """Synchronize memory for a background worker using the handoff pattern.

    This function:
    1. Creates a handoff token
    2. Validates the current state
    3. Ensures the worker can safely access the memory

    Returns {"ok": bool, "synced": bool, "error": str | None}.
    Never raises.
    """
    root = os.path.abspath(root)

    # Create handoff token
    handoff = create_handoff_token(root, worker_id)
    if not handoff["ok"]:
        return handoff

    # Accept the handoff (validates state)
    acceptance = accept_handoff(root, handoff["token"])
    if not acceptance["ok"]:
        return acceptance

    # Ensure any conflicts are resolved
    conflicts = detect_conflicts(root)
    if conflicts["has_conflicts"]:
        repair = invoke_memory_repair_subagent(root, conflicts)
        if not repair["ok"]:
            return {
                "ok": False,
                "synced": False,
                "error": "conflict repair failed",
                "conflicts": conflicts,
            }

    return {
        "ok": True,
        "synced": True,
        "worker": worker_id,
        "commit": handoff["token"].commit_hash,
        "root": root,
    }
