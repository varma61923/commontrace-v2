"""Root/store resolution — provider-agnostic, mirrors memory/attention/query.py's convention.

Priority (matches resolve_root()'s actual checks, most to least specific):
  1. --dest / --root CLI flag (explicit, passed in by callers as `explicit`)
  2. COMMONTRACE_ROOT env var
  3. Current working directory, if it looks like a commontrace store (has memory/)
  4. Current working directory (fallback — `commontrace init` will create memory/ there)
"""
from __future__ import annotations

import os

AGENT_TYPES = ["code", "support", "sales", "hr", "marketing", "ops", "custom"]

STARTER_DOMAINS = {
    "code": ["git-safety", "refactor", "testing", "subagents", "performance", "cuda-gpu", "other"],
    "support": ["escalation", "refunds", "troubleshooting", "tone", "known-issues"],
    "sales": ["objection-handling", "pricing", "qualification", "competitor", "follow-up"],
    "hr": ["screening", "compliance", "onboarding", "policy"],
    "marketing": ["messaging", "compliance", "channel", "brand-voice"],
    "ops": ["incident-response", "runbooks", "monitoring", "capacity"],
    "custom": ["other"],
}


def resolve_root(explicit: str | None = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    # JUSTDOIT_ROOT is honoured as a legacy fallback because the reference
    # scripts already do (measure_performance.py, memory/attention/*.py, all
    # documenting it as "legacy backward compatibility"). Without it here,
    # a store configured the legacy way had the CLI reading one root and the
    # reference scripts reading another -- two halves of the same product
    # silently disagreeing about which store they were operating on.
    # COMMONTRACE_ROOT wins, matching the scripts' own precedence exactly.
    env_root = os.environ.get("COMMONTRACE_ROOT") or os.environ.get("JUSTDOIT_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "memory")):
        return cwd
    return cwd


def memory_dir(root: str) -> str:
    return os.path.join(root, "memory")


def store_agent_type(root: str, default: str = "code") -> str:
    """The agent_type this store was initialized with.

    `commontrace init` stamps it into the first line of memory/INDEX.md. Reading
    it back matters because a support/sales/ops fleet otherwise has to repeat
    `--agent-type` on every single command, and the one time someone forgets,
    the record is silently written as `code` -- wrong, and invisible until a
    later filter mysteriously returns nothing.

    Falls back to `default` for a store with no index (or an index written by
    hand), so this can never be the thing that stops a capture from happening.
    """
    try:
        with open(index_path(root), encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return default
    _, sep, value = first.partition("agent_type:")
    if not sep:
        return default
    candidate = value.strip()
    return candidate if candidate in AGENT_TYPES else default


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
