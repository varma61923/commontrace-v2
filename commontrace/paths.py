"""Root/store resolution — provider-agnostic, mirrors memory/attention/query.py's convention.

Priority (matches resolve_root()'s actual checks, most to least specific):
  1. --dest / --root CLI flag (explicit, passed in by callers as `explicit`)
  2. COMMONTRACE_ROOT env var
  3. Current working directory, if it looks like a commontrace store (has memory/)
  4. Current working directory (fallback — `commontrace init` will create memory/ there)
"""
from __future__ import annotations

import os
import re
import sys

# Suggestions, not a closed set. protocol/PROTOCOL.md#7-taxonomy-open-not-closed
# defines the taxonomy as open, the JSON schemas declare agent_type as a plain
# string with no enum, and the Hub stores it as free text -- so a robotics or
# legal fleet is a first-class citizen of the protocol. These names are what
# `--help` offers as examples and what STARTER_DOMAINS below has starter
# vocabularies for; nothing validates against membership in this list.
SUGGESTED_AGENT_TYPES = ["code", "support", "sales", "hr", "marketing", "ops", "custom"]

# Deprecated alias. Kept for one release so an external caller importing
# `paths.AGENT_TYPES` does not break on upgrade; prefer SUGGESTED_AGENT_TYPES.
AGENT_TYPES = SUGGESTED_AGENT_TYPES

# What a valid agent_type looks like, rather than which ones exist. Lowercase
# slug, <= 64 chars: safe unquoted in a filename, in memory/INDEX.md's first
# line, and in the Hub's String(64) agent_type column (hub/models.py).
AGENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

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

    Validated by SHAPE, not by membership in SUGGESTED_AGENT_TYPES. Checking
    membership caused the exact failure the paragraph above warns about, one
    level down: a store initialized as `robotics` or `legal` -- both valid
    under the open taxonomy in protocol/PROTOCOL.md#7 -- read back as `code`,
    silently, so every trace it captured was stamped with the wrong fleet and
    `--agent-type robotics` then matched nothing. A store's own declared type
    is the authority here; this function's job is to reject what cannot be
    written down safely, not to have opinions about which fields exist.
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
    if not candidate:
        return default
    if AGENT_TYPE_RE.match(candidate):
        return candidate
    # Present but unusable. Silence here is what made the original bug
    # invisible, so say so once rather than quietly substituting `default`.
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
