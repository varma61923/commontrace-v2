"""Named roles for a PERSON, checked in addition to an org's API-key scope.

WHY THIS EXISTS
---------------
`hub/scopes.py` answers "what may this CREDENTIAL do" -- read, write, admin,
on the org's one shared workload key. It has no idea who is holding that
key, because nothing before `hub/models.py:User` did either. That is the
right question for an agent's own key and the wrong one for a human: an
API key scoped `read,write` says nothing about whether the specific person
who curates lessons should also be the one who deploys them, or whether
someone who left the team still can.

`hub/rbac.py` answers the second question. A `User` (hub/models.py) carries
one `Role`, and a role grants a fixed, EXPLICIT set of `Capability` values --
not a position on a ladder. See WHY NOT A HIERARCHY below for why that
distinction is load-bearing, not a style choice.

WHY NOT A HIERARCHY
--------------------
The obvious design ranks roles Viewer < Analyst < Curator < ... < Owner and
grants everything at or below a role's rank. That is wrong for the same
reason `hub/scopes.py` rejected it for API keys: `BILLING_ADMIN` is not
"better at curating" than `CURATOR`, and ranking them anyway means the day
someone inserts a role in the middle of the list, every existing grant
silently shifts. `ROLE_CAPABILITIES` below is instead an explicit set per
role. `VIEW` is included in every non-empty role as a stated BASELINE (a
role that can write but not read would be unusable, not more restrictive),
not as evidence of an implied order among the rest.

TWO GATES, NOT ONE
-------------------
A person authenticating with a verified identity (hub/sso.py) is checked
against BOTH this module's capability for the tool they called AND the
scope `ROLE_SCOPES` derives for their role, through the exact same
`hub/scopes.py` mechanism an API-key-only caller goes through
(hub/server.py wires both). The capability check is the one that actually
distinguishes people; the derived scope exists so the two authorization
paths share one enforcement point (`auth.require_scope`) instead of the
capability check becoming a second, independently-fallible gate that could
diverge from the first. `hub/tests/test_rbac.py` asserts the derived scope
for every role is never narrower than what its granted capabilities need,
so scope can never become the more restrictive -- and more surprising --
gate for a person who already holds the right capability.

WHAT THIS DOES NOT DO
----------------------
Authenticate anyone. `hub/sso.py` verifies a token came from a trusted
issuer; `hub/auth.py` looks up the `User` row it names. This module only
ever answers "given a role, may it do X" -- a pure function of two strings,
deliberately free of the database and the JWT library, so it can be tested
(and read) without either.
"""

from __future__ import annotations

from dataclasses import dataclass

from hub import scopes

# --- roles -------------------------------------------------------------------

ROLE_VIEWER = "viewer"
ROLE_ANALYST = "analyst"
ROLE_CURATOR = "curator"
ROLE_VALIDATOR = "validator"
ROLE_DEPLOYER = "deployer"
ROLE_SECURITY_ADMIN = "security_admin"
ROLE_BILLING_ADMIN = "billing_admin"
ROLE_OWNER = "owner"

ROLES = (
    ROLE_VIEWER, ROLE_ANALYST, ROLE_CURATOR, ROLE_VALIDATOR, ROLE_DEPLOYER,
    ROLE_SECURITY_ADMIN, ROLE_BILLING_ADMIN, ROLE_OWNER,
)

# --- capabilities --------------------------------------------------------------

#: Read traces, tags, working set, reports -- anything a Viewer may see.
CAP_VIEW = "view"
#: Run/read analytics: outcomes, value delivered, commons overlap, usage,
#: record what happened to an occasion, vote on a trace's usefulness.
CAP_ANALYZE = "analyze"
#: Author or amend content: contribute a trace, submit a Knowledge Base
#: candidate.
CAP_CURATE = "curate"
#: The second, independent judgement on curated content. No MCP tool needs
#: this today -- Knowledge Base review (`hub.manage kb-review` /
#: `approve-submission`) is an operator action, not a per-org member one.
#: Granted to VALIDATOR and OWNER anyway, so the role exists ready for the
#: day an org-facing review tool is added, rather than needing a migration
#: to retrofit it onto users created before that tool existed.
CAP_VALIDATE = "validate"
#: Decide what is actually served: assign a holdout arm.
CAP_DEPLOY = "deploy"
#: Destructive or trust-boundary actions: delete a trace, the whole-account
#: deletion request/cancel/confirm sequence.
CAP_SECURITY = "security"
#: Change what an org is billed or its plan. No MCP tool needs this today
#: either -- `hub.manage set-plan` is operator-only -- kept for the same
#: forward-compatibility reason as CAP_VALIDATE.
CAP_BILLING = "billing"
#: Create, disable, or re-role another user. Owner only, and never granted
#: through the same role grant that lets someone hold it AND be curated by
#: it -- see hub/manage.py's user commands for why this stays a separate,
#: harder-to-reach action even for Owners.
CAP_MANAGE_USERS = "manage_users"

CAPABILITIES = (
    CAP_VIEW, CAP_ANALYZE, CAP_CURATE, CAP_VALIDATE, CAP_DEPLOY,
    CAP_SECURITY, CAP_BILLING, CAP_MANAGE_USERS,
)

# Explicit per role -- see WHY NOT A HIERARCHY above. Every entry lists VIEW
# even though it is "implied" in the sense that a roleless grant makes no
# sense without it; nothing here is computed by inheritance.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ROLE_VIEWER: frozenset({CAP_VIEW}),
    ROLE_ANALYST: frozenset({CAP_VIEW, CAP_ANALYZE}),
    ROLE_CURATOR: frozenset({CAP_VIEW, CAP_ANALYZE, CAP_CURATE}),
    ROLE_VALIDATOR: frozenset({CAP_VIEW, CAP_ANALYZE, CAP_CURATE, CAP_VALIDATE}),
    ROLE_DEPLOYER: frozenset({CAP_VIEW, CAP_ANALYZE, CAP_DEPLOY}),
    ROLE_SECURITY_ADMIN: frozenset({CAP_VIEW, CAP_ANALYZE, CAP_SECURITY}),
    ROLE_BILLING_ADMIN: frozenset({CAP_VIEW, CAP_BILLING}),
    ROLE_OWNER: frozenset(CAPABILITIES),
}

# The scope (hub/scopes.py) a person's role maps to, so the ONE existing
# enforcement point (auth.require_scope) stays meaningful for a
# JWT-authenticated caller too, rather than this module needing a second,
# parallel gate that could disagree with the first about the same call. Each
# entry is chosen to be a SUPERSET of every scope this role's own
# capabilities need -- test_rbac.py asserts that, so scope can never become
# the more restrictive, more surprising gate for someone who already holds
# the matching capability.
ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    ROLE_VIEWER: (scopes.SCOPE_READ,),
    # Not read-only: recording an occasion's outcome and voting on a trace's
    # usefulness (record_occasion_outcome, vote_trace) are both WRITE-scoped
    # tools mapped to CAP_ANALYZE below, because reporting a result IS
    # analysis work, not curation of content. Without write here, an Analyst
    # holding CAP_ANALYZE would still be refused by the coarser scope check
    # underneath it -- test_rbac.py's cross-check against the live registry
    # is what caught this the first time. CAP_CURATE is what actually keeps
    # an Analyst from contributing or amending a trace despite holding write
    # scope; the two gates are independent by design (see module docstring).
    ROLE_ANALYST: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_CURATOR: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_VALIDATOR: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_DEPLOYER: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_SECURITY_ADMIN: (scopes.SCOPE_READ, scopes.SCOPE_WRITE, scopes.SCOPE_ADMIN),
    ROLE_BILLING_ADMIN: (scopes.SCOPE_READ,),
    ROLE_OWNER: (scopes.SCOPE_READ, scopes.SCOPE_WRITE, scopes.SCOPE_ADMIN),
}

#: Which capability each of the Hub's 20 org-scoped MCP tools needs. One
#: entry per tool, exactly like hub/scopes.py's own tool_scopes: the failure
#: mode being designed out is a new tool silently reachable by anyone with a
#: verified identity because nobody assigned it a capability.
TOOL_CAPABILITY: dict[str, str] = {
    "search_traces": CAP_VIEW,
    "get_trace": CAP_VIEW,
    "list_tags": CAP_VIEW,
    "fleet_outcomes": CAP_ANALYZE,
    "working_set": CAP_VIEW,
    "value_delivered": CAP_ANALYZE,
    "commons_overlap": CAP_ANALYZE,
    "commons_search": CAP_VIEW,
    "list_my_kb_submissions": CAP_VIEW,
    "account_usage": CAP_ANALYZE,
    "contribute_trace": CAP_CURATE,
    "vote_trace": CAP_ANALYZE,
    "amend_trace": CAP_CURATE,
    "holdout_assign": CAP_DEPLOY,
    "record_occasion_outcome": CAP_ANALYZE,
    "submit_kb_entry": CAP_CURATE,
    "delete_trace": CAP_SECURITY,
    "request_account_deletion": CAP_SECURITY,
    "cancel_account_deletion": CAP_SECURITY,
    "confirm_account_deletion": CAP_SECURITY,
    "add_comment": CAP_CURATE,
    "list_comments": CAP_VIEW,
    "assign_trace": CAP_CURATE,
    "unassign_trace": CAP_CURATE,
    "list_my_notifications": CAP_VIEW,
    "mark_notification_read": CAP_VIEW,
}


class RoleError(Exception):
    """A role name or capability name this module does not recognise."""


class CapabilityDenied(PermissionError):
    """A user's role does not grant the capability a tool requires."""

    def __init__(self, required: str, role: str):
        super().__init__(
            f"role {role!r} does not grant the {required!r} capability"
        )
        self.required = required
        self.role = role


def check_role(role: str) -> str:
    if role not in ROLES:
        raise RoleError(f"unknown role {role!r}; known roles: {', '.join(ROLES)}")
    return role


def capabilities_of(role: str) -> frozenset[str]:
    check_role(role)
    return ROLE_CAPABILITIES[role]


def scopes_of(role: str) -> tuple[str, ...]:
    check_role(role)
    return ROLE_SCOPES[role]


def has_capability(role: str, capability: str) -> bool:
    return capability in capabilities_of(role)


def capability_for_tool(tool_name: str) -> str:
    try:
        return TOOL_CAPABILITY[tool_name]
    except KeyError:
        # Deny by construction rather than by omission: a tool with no
        # mapping is not "ungated", it is unreachable for any role, which is
        # the safe failure and the one that gets noticed in a smoke test
        # rather than in a security review.
        raise RoleError(
            f"tool {tool_name!r} has no capability mapping in "
            "hub/rbac.py:TOOL_CAPABILITY -- it cannot be called with a "
            "user (as opposed to a bare API key) identity until it does"
        ) from None


def require_capability(role: str, tool_name: str) -> None:
    """Raise unless `role` may call `tool_name`."""
    required = capability_for_tool(tool_name)
    if not has_capability(role, required):
        raise CapabilityDenied(required, role)


@dataclass(frozen=True)
class RoleDescription:
    role: str
    capabilities: frozenset[str]
    scopes: tuple[str, ...]


def describe(role: str) -> RoleDescription:
    return RoleDescription(role, capabilities_of(role), scopes_of(role))
