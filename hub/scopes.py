"""What one API key is allowed to do, as distinct from which org it speaks for.

WHY THIS EXISTS
---------------
Until this module a Hub API key was a single, undifferentiated capability:
hold one and you could search the org's corpus, contribute to it, amend it,
delete a trace outright, and schedule the org's own deletion. There was
exactly one identity per org and exactly one privilege level, so the answer
to "what can this credential do if it leaks out of the CI job it was minted
for" was "everything that org can do".

That is the gap an enterprise security review names first: no scoped
workload tokens, so no way to hand a production agent a credential that can
read and contribute but cannot destroy, or to hand a dashboard a credential
that can only read. Least privilege is not a posture you can adopt with a
credential that has no notion of less.

THREE SCOPES, AND WHY NOT MORE
------------------------------
    read    every read path -- search, get, tags, the Knowledge Base,
            fleet outcomes, the value report, the working set
    write   the paths that add to the corpus or record evidence --
            contribute, amend, vote, KB submission, holdout assignment,
            occasion outcomes
    admin   the destructive ones -- deleting a trace, and the account
            deletion request/cancel/confirm trio

These map onto the three jobs a credential actually holds in a fleet: a
dashboard reads, a production agent reads and writes, an operator's key
does the rest. A finer lattice is easy to add later and hard to configure
correctly today: eight scopes an operator sets wrong are worse than three
they reason about.

SCOPES DO NOT IMPLY EACH OTHER
------------------------------
`admin` does not confer `read`; `write` does not confer `read`. The grant
list says exactly what a key may do, with no implication chain to reason
about, which is the property that makes "this key cannot escalate" checkable
by reading one row instead of by simulating a hierarchy. The cost is that a
key meant to do everything must say `read,write,admin` -- which is what
`hub.manage issue-key` grants by default, so the ordinary path is unchanged.

BACKWARD COMPATIBILITY
----------------------
Every key issued before this column existed is treated as holding all three
scopes (the column's server default), because that is what it could do when
it was issued. Narrowing an existing credential silently would break running
fleets on upgrade, which is a decision for the operator, not for a
migration.
"""

from __future__ import annotations

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
#: Not a finer slice of the three above -- a wholly separate capability
#: class. read/write/admin gate MCP tool calls over an org's trace corpus;
#: `scim` gates hub/scim.py's own HTTP endpoint, which never touches trace
#: data at all and only creates/deactivates `User` rows (see that module's
#: own docstring for why "manages accounts" and "manages the corpus" are
#: deliberately never the same credential).
SCOPE_SCIM = "scim"

#: Every scope this Hub understands, in the order they are displayed.
ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN, SCOPE_SCIM)

#: What a key issued before the `scopes` column existed could do -- fixed
#: at exactly these three, forever, independent of whatever ALL_SCOPES
#: grows to later. `satisfies` uses this, not ALL_SCOPES, for the "legacy
#: key" (granted=None) case: a scope added after the fact (like `scim`,
#: a materially more sensitive capability than any of the original three)
#: must never be retroactively granted to every already-issued full-access
#: key just because it joined ALL_SCOPES. Growing ALL_SCOPES having
#: silently widened every legacy key in production is exactly the "quiet
#: widening" this module's own docstring calls worse than a quiet
#: narrowing.
_LEGACY_IMPLIED_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)

#: What `hub.manage issue-key` grants when the operator does not say --
#: everything a key could do before scopes existed. Deliberately NOT
#: ALL_SCOPES: `scim` is sensitive enough (see SCOPE_SCIM above) that
#: issuing it must always be an explicit ask (`issue-key <org> [days]
#: read,write,admin,scim`), never a side effect of omitting `--scopes`.
DEFAULT_SCOPES: tuple[str, ...] = _LEGACY_IMPLIED_SCOPES


class ScopeError(ValueError):
    """An unknown or malformed scope. A ValueError so hub/server.py's
    `_error_response` renders it as a structured invalid-request rather
    than a 500."""


def parse(raw: str | list | tuple | None) -> tuple[str, ...]:
    """Normalize operator input ("read,write", ["read", "write"], None) into
    a canonical, deduplicated, ALL_SCOPES-ordered tuple.

    Raises on an unknown scope rather than dropping it. A typo'd
    `--scopes reed,write` that silently produced a write-only key would be a
    quiet privilege change in the direction nobody checks -- and a typo'd
    `--scopes read,wrote` that silently produced a read-only key would page
    somebody at 3am instead.
    """
    if raw is None:
        return DEFAULT_SCOPES
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    requested = {str(item).strip().lower() for item in items if str(item).strip()}
    if not requested:
        raise ScopeError(
            "no scopes given. Pass at least one of "
            f"{', '.join(ALL_SCOPES)} -- a key with no scopes can do nothing at all."
        )
    unknown = sorted(requested - set(ALL_SCOPES))
    if unknown:
        raise ScopeError(
            f"unknown scope(s): {', '.join(unknown)}. "
            f"Valid scopes are {', '.join(ALL_SCOPES)}."
        )
    return tuple(scope for scope in ALL_SCOPES if scope in requested)


def satisfies(granted: object, required: str) -> bool:
    """Whether a key holding `granted` may perform an action needing
    `required`.

    A missing/None grant list means a key that predates the column, which
    held every capability that existed AT ISSUANCE TIME -- see BACKWARD
    COMPATIBILITY above and `_LEGACY_IMPLIED_SCOPES`'s own docstring for why
    that is `_LEGACY_IMPLIED_SCOPES`, not today's `ALL_SCOPES`: a scope
    added later (like `scim`) must never be retroactively granted to a key
    that could not have asked for it. An EMPTY list is not the same thing
    as None and is not treated the same: it means somebody deliberately
    narrowed this key to nothing.
    """
    if granted is None:
        return required in _LEGACY_IMPLIED_SCOPES
    return required in set(granted)


def describe(granted: object) -> str:
    """Short human-readable rendering for CLI output and audit lines."""
    if granted is None:
        return (
            f"{','.join(_LEGACY_IMPLIED_SCOPES)} (legacy key, issued before "
            "scopes existed)"
        )
    scopes = tuple(granted)
    if not scopes:
        return "none"
    return ",".join(scope for scope in ALL_SCOPES if scope in set(scopes))
