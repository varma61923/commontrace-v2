"""Named roles for a PERSON, checked in addition to an org's API-key scope."""

from __future__ import annotations

from dataclasses import dataclass

from hub import scopes

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

PRIVILEGED_ROLES = (ROLE_SECURITY_ADMIN, ROLE_OWNER)


CAP_VIEW = "view"
CAP_ANALYZE = "analyze"
CAP_CURATE = "curate"
CAP_VALIDATE = "validate"
CAP_DEPLOY = "deploy"
CAP_SECURITY = "security"
CAP_BILLING = "billing"
CAP_MANAGE_USERS = "manage_users"

CAPABILITIES = (
    CAP_VIEW, CAP_ANALYZE, CAP_CURATE, CAP_VALIDATE, CAP_DEPLOY,
    CAP_SECURITY, CAP_BILLING, CAP_MANAGE_USERS,
)

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

ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    ROLE_VIEWER: (scopes.SCOPE_READ,),
    ROLE_ANALYST: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_CURATOR: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_VALIDATOR: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_DEPLOYER: (scopes.SCOPE_READ, scopes.SCOPE_WRITE),
    ROLE_SECURITY_ADMIN: (scopes.SCOPE_READ, scopes.SCOPE_WRITE, scopes.SCOPE_ADMIN),
    ROLE_BILLING_ADMIN: (scopes.SCOPE_READ,),
    ROLE_OWNER: (scopes.SCOPE_READ, scopes.SCOPE_WRITE, scopes.SCOPE_ADMIN),
}

TOOL_CAPABILITY: dict[str, str] = {
    "get_traces_batch": CAP_VIEW,
    "contribute_traces_batch": CAP_CURATE,
    "delete_traces_batch": CAP_SECURITY,
    "search_traces": CAP_VIEW,
    "get_trace": CAP_VIEW,
    "list_tags": CAP_VIEW,
    "fleet_outcomes": CAP_ANALYZE,
    "working_set": CAP_VIEW,
    "value_delivered": CAP_ANALYZE,
    "commons_overlap": CAP_ANALYZE,
    "commons_search": CAP_VIEW,
    "commons_export": CAP_VIEW,
    "list_my_kb_submissions": CAP_VIEW,
    "account_usage": CAP_ANALYZE,
    "contribute_trace": CAP_CURATE,
    "vote_trace": CAP_ANALYZE,
    "amend_trace": CAP_CURATE,
    "holdout_assign": CAP_DEPLOY,
    "record_occasion_outcome": CAP_ANALYZE,
    "submit_kb_entry": CAP_CURATE,
    "search_trace_content": CAP_SECURITY,
    "tag_trace_subjects": CAP_SECURITY,
    "find_traces_by_subject": CAP_SECURITY,
    "purge_traces_by_subject": CAP_SECURITY,
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
