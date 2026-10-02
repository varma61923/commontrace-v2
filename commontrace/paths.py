"""Root/store resolution — provider-agnostic, mirrors memory/attention/query.py's convention."""
from __future__ import annotations

import os
import re
import sys

GENERAL_AGENT_TYPE = "general"

SUGGESTED_AGENT_TYPES = ["code", "support", "sales", "hr", "marketing", "ops", "custom"]

AGENT_TYPES = SUGGESTED_AGENT_TYPES

AGENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

STARTER_DOMAINS = {
    "code": ["git-safety", "refactor", "testing", "subagents", "performance", "cuda-gpu", "other"],
    "support": ["escalation", "refunds", "troubleshooting", "tone", "known-issues"],
    "sales": ["objection-handling", "pricing", "qualification", "competitor", "follow-up"],
    "hr": ["screening", "compliance", "onboarding", "policy"],
    "marketing": ["messaging", "compliance", "channel", "brand-voice"],
    "ops": ["incident-response", "runbooks", "monitoring", "capacity"],
    "custom": ["other"],
    GENERAL_AGENT_TYPE: ["other"],
}


def resolve_root(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    env_root = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "memory")):
        return cwd
    return cwd


def warn_if_implicit_cwd_store(explicit: str | None) -> None:
    """Warn when a write is about to materialize a new store in the cwd."""
    if explicit:
        return
    if os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT"):
        return
    if os.path.isdir(os.path.join(os.getcwd(), "memory")):
        return
    print(
        "[commontrace] warning: no store found (--dest not given, "
        "$COMMONTRACE_ROOT unset, no ./memory/ in cwd); "
        f"creating a new store at {os.getcwd()}. "
        "To use an existing store, pass --dest or set $COMMONTRACE_ROOT. "
        "To silence (intentional init here), run `commontrace init` first.",
        file=sys.stderr,
    )


def memory_dir(root: str) -> str:
    return os.path.join(root, "memory")


def store_agent_type(root: str, default: str = GENERAL_AGENT_TYPE) -> str:
    """The agent_type this store was initialized with."""
    try:
        with open(index_path(root), encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return default
    _, sep, value = first.partition("agent_type:")
    if not sep:
        return default
    candidate = value.strip()
    if not candidate:
        return default
    if AGENT_TYPE_RE.match(candidate):
        return candidate
    print(
        f"[commontrace] warning: memory/INDEX.md declares agent_type "
        f"{candidate!r}, which is not a valid slug ({AGENT_TYPE_RE.pattern}). "
        f"Using {default!r}. Fix the first line of {index_path(root)}.",
        file=sys.stderr,
    )
    return default


def lessons_dir(root: str) -> str:
    return os.path.join(memory_dir(root), "lessons")


def episodes_dir(root: str) -> str:
    return os.path.join(memory_dir(root), "episodes")


def traces_dir(root: str) -> str:
    """Generic (non code-review-profile) Trace capture directory. See protocol/PROTOCOL.md."""
    return os.path.join(memory_dir(root), "traces")


def index_path(root: str) -> str:
    return os.path.join(memory_dir(root), "INDEX.md")


def schemas_dir() -> str:
    """The schemas bundled with the installed package (works even without a repo checkout)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas")


class PathTraversalError(ValueError):
    """Raised when a candidate path resolves outside the allowed base directory boundary."""
    pass


def is_within_directory(base_dir: str, candidate_path: str) -> bool:
    """Return True if candidate_path strictly resides within base_dir (resolving symlinks)."""
    base_real = os.path.realpath(os.path.abspath(str(base_dir)))
    if not os.path.isabs(str(candidate_path)):
        candidate_full = os.path.join(base_real, str(candidate_path))
    else:
        candidate_full = str(candidate_path)
    candidate_real = os.path.realpath(os.path.abspath(candidate_full))
    base_prefix = base_real if base_real.endswith(os.sep) else base_real + os.sep
    return candidate_real == base_real or candidate_real.startswith(base_prefix)


def enforce_boundary(base_dir: str, candidate_path: str, allow_within: bool = True) -> str:
    if not base_dir:
        raise ValueError("base_dir must not be empty")
    if not candidate_path:
        raise ValueError("cannot read: candidate_path must not be empty")

    base_abs = os.path.abspath(str(base_dir))
    base_real = os.path.realpath(base_abs)

    if not os.path.isabs(str(candidate_path)):
        candidate_full = os.path.join(base_real, str(candidate_path))
    else:
        candidate_full = str(candidate_path)
    candidate_abs = os.path.abspath(candidate_full)
    candidate_real = os.path.realpath(candidate_abs)

    base_prefix = base_real if base_real.endswith(os.sep) else base_real + os.sep
    if allow_within:
        is_safe = (candidate_real == base_real) or candidate_real.startswith(base_prefix)
    else:
        is_safe = (candidate_real == base_real)

    if not is_safe:
        raise PathTraversalError(
            f"cannot read: path traversal detected: {candidate_path!r} resolves outside boundary {base_dir!r}"
        )
    return candidate_real


def safe_prepare_output_path(out_path: str, allow_unlink_leaf: bool = True) -> str:
    """Validate and prepare a file path for safe writing without symlink write-through."""
    if not out_path:
        raise ValueError("Output path must not be empty")

    norm_path = os.path.abspath(str(out_path))
    parent_dir = os.path.dirname(norm_path)

    parts = []
    curr = parent_dir
    while True:
        parts.append(curr)
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    parts.reverse()

    for comp in parts:
        if comp == os.sep:
            continue
        if os.path.islink(comp):
            raise PathTraversalError(
                f"refusing to write through symlinked directory: {comp!r}"
            )

    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)
        if os.path.islink(parent_dir):
            raise PathTraversalError(
                f"refusing to write through symlinked directory: {parent_dir!r}"
            )

    if os.path.islink(norm_path) or os.path.islink(str(out_path)):
        if allow_unlink_leaf:
            target_to_unlink = norm_path if os.path.islink(norm_path) else str(out_path)
            try:
                os.unlink(target_to_unlink)
            except OSError as exc:
                raise PathTraversalError(
                    f"cannot remove leaf symlink at {out_path!r}: {exc}"
                ) from exc
        else:
            raise PathTraversalError(
                f"refusing to write through leaf symlink: {out_path!r}"
            )

    return norm_path

