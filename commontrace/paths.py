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


def warn_if_implicit_cwd_store(explicit: str | None) -> None:
    """Warn when a write is about to materialize a new store in the cwd.

    `resolve_root` silently falls back to the cwd when no `--dest`, no
    `$COMMONTRACE_ROOT`/`$JUSTDOIT_ROOT`, and no `./memory/` exist -- and
    write paths (`capture`, `lesson new`) then `makedirs` a store wherever
    the user happened to be. Read-only paths stay silent (a warning there
    would be noise); call this only before creating store directories.

    Truthiness matches `resolve_root`'s own `if explicit:` check, not just
    `is not None`: an empty string (e.g. `--dest ""` from an unset shell
    variable) is falsy to `resolve_root`, which falls through to the same
    env/cwd fallback as no `--dest` at all -- this function must fall
    through with it, or the one caller relying on `--dest ""` behaving like
    "no --dest" gets silently different warning behavior than resolution.
    """
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
    """Resolves candidate_path relative to base_dir, normalizes both paths (resolving symlinks),
    and strictly verifies that candidate_path resides within base_dir.
    Raises ValueError or PathTraversalError if candidate_path escapes base_dir.
    Returns the sanitized, canonical absolute path.
    """
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
    """Validate and prepare a file path for safe writing without symlink write-through.

    1. Checks all existing directory components leading to out_path. If any existing
       intermediate directory is a symlink, rejects it by raising PathTraversalError.
    2. If the leaf file itself is a symlink:
       - If allow_unlink_leaf is True, unlinks the leaf symlink so the subsequent open()
         writes to a new regular file rather than through the symlink.
       - If allow_unlink_leaf is False, raises PathTraversalError.
    3. Safely creates parent directories (exist_ok=True) and verifies that the created
       parent directory is not a symlink.
    Returns the normalized, safe target path.
    """
    if not out_path:
        raise ValueError("Output path must not be empty")

    norm_path = os.path.abspath(str(out_path))
    parent_dir = os.path.dirname(norm_path)

    # Check intermediate directory components from root down to parent_dir
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

