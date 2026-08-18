"""Root/store resolution — provider-agnostic, mirrors memory/attention/query.py's convention.

Priority:
  1. COMMONTRACE_ROOT env var (explicit override)
  2. --dest / --root CLI flag (passed in by callers)
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
    env_root = os.environ.get("COMMONTRACE_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "memory")):
        return cwd
    return cwd


def memory_dir(root: str) -> str:
    return os.path.join(root, "memory")


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
