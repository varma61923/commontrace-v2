"""SCIM 2.0 user provisioning (RFC 7643/7644), a narrow, deliberate subset --
audit 1.2's own remaining line: "SCIM auto-provisioning" was named as still
open in hub/sso.py's module docstring and hub/README.md's "Auth follow-ups".

WHAT THIS DOES
--------------
An IdP (Okta, Azure AD, OneLogin, ...) pushes user lifecycle events --
someone joined, someone left -- to `POST/GET/PUT/PATCH/DELETE
/scim/v2/Users[/{id}]`, and this Hub creates or deactivates the matching
`hub/models.py` `User` row automatically, instead of an operator running
`hub.manage create-user`/`disable-user` by hand for every hire and every
termination. That last part -- automated, IMMEDIATE deprovisioning when an
IdP marks someone inactive -- is the security-critical direction: an
account a leaver still holds a live credential for is a bigger and more
common real-world exposure than a new hire's account showing up a day
late.

WHAT THIS DELIBERATELY DOES NOT DO
------------------------------------
**SCIM manages the ROW; it does not grant a login.** hub/sso.py's own
docstring is explicit that OIDC linking is "always an explicit operator
action (`hub.manage link-sso`), never automatic just-in-time
provisioning" -- and this module does not change that. A SCIM-created
`User` row has no `issuer`/`external_subject` populated by this code path
at all, so it cannot authenticate via `hub/auth.py:verify_user_token`
until an operator separately links it. Auto-populating those columns from
whatever `externalId` an IdP happens to push would silently reintroduce
exactly the auto-provisioning-grants-access risk sso.py declined -- SCIM
and SSO are, on purpose, two different subsystems here even though the
same IdP often drives both. What this module DOES automate is the row's
existence and its `active` state, which is what a real termination event
needs handled instantly, whether or not that row is linked yet.

**A provisioned account starts as `viewer`, never anything higher**
(`DEFAULT_PROVISIONED_ROLE`). Proof an IdP vouches someone should have SOME
account is not proof of what they should be able to DO with it; an
operator still runs `hub.manage set-user-role` for anything beyond
read-only access.

**Not the full RFC 7644 grammar.** `filter` supports exactly one shape --
`userName eq "<value>"`, the one every real IdP integration actually sends
(checking whether an account exists before creating one) -- and PATCH
applies only `active` (and, leniently, `displayName`) replace operations;
anything else in a filter or a PATCH operation is either rejected (an
unsupported filter) or left untouched rather than guessed at (an
unsupported PATCH attribute), so an IdP that bundles an unsupported
attribute into the same request still gets its deprovisioning applied
instead of the whole call failing.

**A dedicated credential, not a wider door on an existing one.**
`scopes.SCOPE_SCIM` is its own scope, checked here and NOWHERE an MCP tool
is (see hub/scopes.py's own docstring): a SCIM-scoped key can manage this
org's User rows and cannot touch a single trace, and a key scoped for
ordinary trace access cannot reach this endpoint at all. Issue one with
`hub.manage issue-key <org_id> [days] scim`.

DELETE DEACTIVATES; IT NEVER REMOVES THE ROW
---------------------------------------------
Same reasoning as everywhere else in this Hub (hub/models.py:User's own
docstring): the row is exactly what an auditor asks about later -- who had
access, with what role, and when it was revoked -- and a SCIM client
sending `DELETE` when an IdP simply means "this person is no longer here"
must not erase that history to say so.

DISCOVERY ROUTES ARE UNAUTHENTICATED, ON PURPOSE
--------------------------------------------------
`ServiceProviderConfig`/`ResourceTypes`/`Schemas` return the same static,
non-tenant document to anyone -- no `User` row, no org data, nothing a
`scim`-scoped key gates elsewhere. Some IdP setup flows probe these before
a token is even entered, and there is nothing here for an unauthenticated
caller to learn beyond "this server implements SCIM 2.0 for Users".

GROUPS (`/scim/v2/Groups`) ARE MEMBERSHIP METADATA, NOT PERMISSIONS
---------------------------------------------------------------------
Audit 1.2 named "SCIM Groups" as a declined gap: a real Groups API needs
many-to-many membership, and `hub/rbac.py` gives one `User` exactly one
`role` -- no additive permission surface anywhere in this Hub for a group
to plug into. `hub/models.py:ScimGroup`/`ScimGroupMembership` close the
data-model half honestly, tracking an IdP's group roster faithfully,
WITHOUT inventing a second authorization system alongside `role`:
`hub/rbac.py` and `hub/server.py`'s tool gating never read either table.
A group here is a label plus a membership list an IdP can keep in sync --
adding or removing someone from a group changes nothing about what they
can do.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from hub import audit, auth, rbac, scopes
from hub.abuse import RateLimiter, rate_limit_key
from hub.crud import _is_uuid
from hub.db import session_scope
from hub.models import Organization, ScimGroup, ScimGroupMembership, User

logger = logging.getLogger("commontrace.hub")

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_CONTENT_TYPE = "application/scim+json"

#: Role a SCIM-created account starts with -- see the module docstring's
#: "not proof of what they should be able to DO" paragraph.
DEFAULT_PROVISIONED_ROLE = rbac.ROLE_VIEWER

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200

_FILTER_RE = re.compile(r'^\s*userName\s+eq\s+"(.*)"\s*$', re.IGNORECASE)
_GROUP_FILTER_RE = re.compile(r'^\s*displayName\s+eq\s+"(.*)"\s*$', re.IGNORECASE)
_MEMBER_FILTER_RE = re.compile(r'^\s*members\[value\s+eq\s+"(.*)"\]\s*$', re.IGNORECASE)


class ScimError(Exception):
    """A well-formed SCIM error this module refuses on its own terms.
    `status`/`scim_type` map onto RFC 7644 §3.12's error body."""

    def __init__(self, detail: str, *, status: int = 400, scim_type: str | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.scim_type = scim_type

    def to_body(self) -> dict:
        body = {"schemas": [SCIM_ERROR_SCHEMA], "detail": self.detail, "status": str(self.status)}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


def user_to_scim(user: User) -> dict:
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": user.id,
        "userName": user.email,
        "displayName": user.display_name,
        "active": user.disabled_at is None,
        "meta": {"resourceType": "User", "created": user.created_at.isoformat()},
    }


async def create_user(session: AsyncSession, org_id: str, body: dict, *, actor: str) -> User:
    user_name = str(body.get("userName") or "").strip()
    if not user_name:
        raise ScimError("userName is required", scim_type="invalidValue")
    org = await session.get(Organization, org_id)
    if org is None:
        raise ScimError(f"no such organization: {org_id}", status=404)
    user = User(
        org_id=org_id, email=user_name, role=DEFAULT_PROVISIONED_ROLE,
        display_name=str(body.get("displayName") or ""), created_by=actor,
    )
    if body.get("active") is False:
        user.disabled_at = datetime.now(timezone.utc)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ScimError(
            f"{org_id} already has a user with userName {user_name!r}",
            status=409, scim_type="uniqueness",
        ) from None
    await audit.record(
        session, actor=actor, action="scim.create_user", org_id=org_id,
        target_type="user", target_id=user.id, summary=f"userName={user_name}",
    )
    return user


async def get_user(session: AsyncSession, org_id: str, user_id: str) -> User | None:
    # A non-UUID-shaped id would otherwise raise asyncpg.DataError against
    # User.id (a UUID column) instead of resolving to the same not-found a
    # well-formed-but-nonexistent id already produces -- see
    # hub/crud.py:_is_uuid's own docstring for why this is checked before
    # the query runs, not caught after.
    if not _is_uuid(user_id):
        return None
    user = await session.get(User, user_id)
    if user is None or user.org_id != org_id:
        return None
    return user


async def list_users(
    session: AsyncSession, org_id: str, *, user_name: str | None = None,
    start_index: int = 1, count: int = DEFAULT_PAGE_SIZE,
) -> tuple[int, list[User]]:
    """SCIM's own 1-based `startIndex` pagination. `user_name`, when given,
    is the ONLY filter shape the route layer accepts -- see the module
    docstring."""
    start_index = max(1, start_index)
    count = max(0, min(count, MAX_PAGE_SIZE))
    query = select(User).where(User.org_id == org_id)
    if user_name is not None:
        query = query.where(User.email == user_name)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.execute(
            query.order_by(User.created_at).offset(start_index - 1).limit(count)
        )
    ).scalars().all()
    return int(total or 0), list(rows)


_ACTIVE_TRUE = {"true", "1"}
_ACTIVE_FALSE = {"false", "0"}


def _coerce_active(value: object) -> bool | None:
    """A real JSON boolean is the common case; at least one real IdP
    integration has been observed sending the STRING `"True"`/`"False"`
    instead. Accept both; anything else is "no change", never a guess."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _ACTIVE_TRUE:
            return True
        if lowered in _ACTIVE_FALSE:
            return False
    return None


def _apply_active(user: User, active: bool) -> bool:
    """Returns whether anything actually changed, so callers only
    audit-log a real transition, not a same-state PUT/PATCH."""
    was_disabled = user.disabled_at is not None
    if active and was_disabled:
        user.disabled_at = None
        return True
    if not active and not was_disabled:
        user.disabled_at = datetime.now(timezone.utc)
        return True
    return False


async def _record_active_change(session: AsyncSession, org_id: str, user_id: str, active: bool, *, actor: str) -> None:
    action = "scim.enable_user" if active else "scim.disable_user"
    await audit.record(
        session, actor=actor, action=action, org_id=org_id,
        target_type="user", target_id=user_id,
    )


async def replace_user(session: AsyncSession, org_id: str, user_id: str, body: dict, *, actor: str) -> User | None:
    """PUT: full replace of the attributes this Hub actually models.
    `userName` is deliberately NOT reassignable here -- it is this row's
    own uniqueness key, and silently repointing which person an existing
    row's history belongs to is not what "replace" means."""
    user = await get_user(session, org_id, user_id)
    if user is None:
        return None
    if "displayName" in body:
        user.display_name = str(body.get("displayName") or "")
    active = _coerce_active(body.get("active"))
    if active is not None and _apply_active(user, active):
        await _record_active_change(session, org_id, user_id, active, actor=actor)
    return user


async def patch_user(session: AsyncSession, org_id: str, user_id: str, body: dict, *, actor: str) -> User | None:
    """PATCH (RFC 7644 §3.5.2), narrowly: only `active` replace/add
    operations are applied -- by far the dominant real use (an IdP
    deprovisioning a leaver) -- see the module docstring for why anything
    else in the operations array is left untouched rather than rejected."""
    user = await get_user(session, org_id, user_id)
    if user is None:
        return None
    operations = body.get("Operations") or body.get("operations") or []
    if not isinstance(operations, list):
        operations = []
    changed_to: bool | None = None
    for op in operations:
        if not isinstance(op, dict):
            continue
        verb = str(op.get("op") or "").strip().lower()
        if verb not in ("replace", "add"):
            continue
        path = str(op.get("path") or "").strip().lower()
        value = op.get("value")
        if path == "active":
            active = _coerce_active(value)
        elif not path and isinstance(value, dict) and "active" in value:
            active = _coerce_active(value.get("active"))
        else:
            continue
        if active is not None and _apply_active(user, active):
            changed_to = active
    # A bare {"active": false} with no Operations array -- non-compliant,
    # but observed in practice, and costs nothing extra to also accept.
    if not operations and "active" in body:
        active = _coerce_active(body.get("active"))
        if active is not None and _apply_active(user, active):
            changed_to = active
    if changed_to is not None:
        await _record_active_change(session, org_id, user_id, changed_to, actor=actor)
    return user


async def deactivate_user(session: AsyncSession, org_id: str, user_id: str, *, actor: str) -> User | None:
    """DELETE. Deactivates, never removes -- see the module docstring."""
    user = await get_user(session, org_id, user_id)
    if user is None:
        return None
    if user.disabled_at is None:
        user.disabled_at = datetime.now(timezone.utc)
        await audit.record(
            session, actor=actor, action="scim.disable_user", org_id=org_id,
            target_type="user", target_id=user_id,
        )
    return user


async def _member_ids(session: AsyncSession, group_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(ScimGroupMembership.user_id)
            .where(ScimGroupMembership.group_id == group_id)
            .order_by(ScimGroupMembership.created_at)
        )
    ).scalars().all()
    return list(rows)


async def group_to_scim(session: AsyncSession, group: ScimGroup) -> dict:
    member_ids = await _member_ids(session, group.id)
    members = []
    if member_ids:
        users = (
            await session.execute(select(User).where(User.id.in_(member_ids)))
        ).scalars().all()
        by_id = {u.id: u for u in users}
        for user_id in member_ids:
            user = by_id.get(user_id)
            members.append({"value": user_id, "display": user.email if user else ""})
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": group.id,
        "displayName": group.display_name,
        "members": members,
        "meta": {"resourceType": "Group", "created": group.created_at.isoformat()},
    }


async def _existing_org_user_ids(session: AsyncSession, org_id: str, candidate_ids: set[str]) -> set[str]:
    """Which of `candidate_ids` are real, well-formed `User` ids in THIS
    org. A member reference to a nonexistent or cross-org id is silently
    dropped rather than failing the whole request -- the same "leave the
    unsupported part alone rather than guess" stance `patch_user` already
    takes, and the common real cause is an IdP's own group roster having
    drifted from this Hub's, which a provisioning call cannot fix by
    refusing outright."""
    uuid_candidates = {uid for uid in candidate_ids if _is_uuid(uid)}
    if not uuid_candidates:
        return set()
    rows = (
        await session.execute(
            select(User.id).where(User.id.in_(uuid_candidates), User.org_id == org_id)
        )
    ).scalars().all()
    return set(rows)


async def _set_members(session: AsyncSession, org_id: str, group_id: str, user_ids: set[str]) -> None:
    valid_ids = await _existing_org_user_ids(session, org_id, user_ids)
    current = set(await _member_ids(session, group_id))
    to_remove = current - valid_ids
    if to_remove:
        await session.execute(
            delete(ScimGroupMembership).where(
                ScimGroupMembership.group_id == group_id,
                ScimGroupMembership.user_id.in_(to_remove),
            )
        )
    for user_id in valid_ids - current:
        session.add(ScimGroupMembership(org_id=org_id, group_id=group_id, user_id=user_id))


async def _add_members(session: AsyncSession, org_id: str, group_id: str, user_ids: set[str]) -> None:
    valid_ids = await _existing_org_user_ids(session, org_id, user_ids)
    current = set(await _member_ids(session, group_id))
    for user_id in valid_ids - current:
        session.add(ScimGroupMembership(org_id=org_id, group_id=group_id, user_id=user_id))


async def _remove_members(session: AsyncSession, group_id: str, user_ids: set[str]) -> None:
    if not user_ids:
        return
    await session.execute(
        delete(ScimGroupMembership).where(
            ScimGroupMembership.group_id == group_id, ScimGroupMembership.user_id.in_(user_ids),
        )
    )


def _member_values(raw) -> set[str]:
    """`members` is a list of `{"value": "<user_id>", ...}` objects per
    RFC 7643 -- anything else (a bare string, a malformed entry) is
    dropped rather than guessed at."""
    if not isinstance(raw, list):
        return set()
    out = set()
    for entry in raw:
        if isinstance(entry, dict) and entry.get("value"):
            out.add(str(entry["value"]))
    return out


async def create_group(session: AsyncSession, org_id: str, body: dict, *, actor: str) -> ScimGroup:
    display_name = str(body.get("displayName") or "").strip()
    if not display_name:
        raise ScimError("displayName is required", scim_type="invalidValue")
    org = await session.get(Organization, org_id)
    if org is None:
        raise ScimError(f"no such organization: {org_id}", status=404)
    group = ScimGroup(
        org_id=org_id, display_name=display_name,
        external_id=str(body.get("externalId") or ""),
    )
    session.add(group)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ScimError(
            f"{org_id} already has a group named {display_name!r}",
            status=409, scim_type="uniqueness",
        ) from None
    member_ids = _member_values(body.get("members"))
    if member_ids:
        await _add_members(session, org_id, group.id, member_ids)
    await audit.record(
        session, actor=actor, action="scim.create_group", org_id=org_id,
        target_type="scim_group", target_id=group.id,
        summary=f"displayName={display_name}",
    )
    return group


async def get_group(session: AsyncSession, org_id: str, group_id: str) -> ScimGroup | None:
    if not _is_uuid(group_id):
        return None
    group = await session.get(ScimGroup, group_id)
    if group is None or group.org_id != org_id:
        return None
    return group


async def list_groups(
    session: AsyncSession, org_id: str, *, display_name: str | None = None,
    start_index: int = 1, count: int = DEFAULT_PAGE_SIZE,
) -> tuple[int, list[ScimGroup]]:
    start_index = max(1, start_index)
    count = max(0, min(count, MAX_PAGE_SIZE))
    query = select(ScimGroup).where(ScimGroup.org_id == org_id)
    if display_name is not None:
        query = query.where(ScimGroup.display_name == display_name)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.execute(
            query.order_by(ScimGroup.created_at).offset(start_index - 1).limit(count)
        )
    ).scalars().all()
    return int(total or 0), list(rows)


async def replace_group(
    session: AsyncSession, org_id: str, group_id: str, body: dict, *, actor: str,
) -> ScimGroup | None:
    """PUT: full replace. `members`, when present, becomes the group's
    ENTIRE membership -- anyone not listed is removed, matching PUT's own
    replace semantics rather than PATCH's incremental add/remove."""
    group = await get_group(session, org_id, group_id)
    if group is None:
        return None
    if "displayName" in body:
        new_name = str(body.get("displayName") or "").strip()
        if new_name:
            group.display_name = new_name
    if "members" in body:
        await _set_members(session, org_id, group_id, _member_values(body.get("members")))
    group.updated_at = datetime.now(timezone.utc)
    await audit.record(
        session, actor=actor, action="scim.replace_group", org_id=org_id,
        target_type="scim_group", target_id=group_id,
    )
    return group


async def patch_group(session: AsyncSession, org_id: str, group_id: str, body: dict, *, actor: str) -> ScimGroup | None:
    """PATCH (RFC 7644 §3.5.2), narrowly: `displayName` replace, and
    `members` add/remove -- including the single-member
    `members[value eq "<id>"]` filtered-path remove shape real IdPs (Okta
    among them) actually send. Anything else in the operations array is
    left untouched rather than guessed at, same stance as `patch_user`."""
    group = await get_group(session, org_id, group_id)
    if group is None:
        return None
    operations = body.get("Operations") or body.get("operations") or []
    if not isinstance(operations, list):
        operations = []
    changed = False
    for op in operations:
        if not isinstance(op, dict):
            continue
        verb = str(op.get("op") or "").strip().lower()
        path = str(op.get("path") or "").strip()
        value = op.get("value")

        if verb in ("replace", "add") and path.lower() == "displayname":
            new_name = str(value or "").strip()
            if new_name and new_name != group.display_name:
                group.display_name = new_name
                changed = True
            continue

        member_filter = _MEMBER_FILTER_RE.match(path)
        if verb == "remove" and member_filter:
            await _remove_members(session, group_id, {member_filter.group(1)})
            changed = True
            continue

        if path.lower() == "members":
            if verb == "add":
                ids = _member_values(value)
                if ids:
                    await _add_members(session, org_id, group_id, ids)
                    changed = True
            elif verb == "remove":
                if value is None:
                    # {"op": "remove", "path": "members"} with no value:
                    # RFC 7644's own "remove the whole attribute" shape.
                    current = set(await _member_ids(session, group_id))
                    if current:
                        await _remove_members(session, group_id, current)
                        changed = True
                else:
                    ids = _member_values(value)
                    if ids:
                        await _remove_members(session, group_id, ids)
                        changed = True
    if changed:
        group.updated_at = datetime.now(timezone.utc)
        await audit.record(
            session, actor=actor, action="scim.patch_group", org_id=org_id,
            target_type="scim_group", target_id=group_id,
        )
    return group


async def delete_group(session: AsyncSession, org_id: str, group_id: str, *, actor: str) -> bool:
    """A real delete, unlike `deactivate_user` -- a group confers no
    access, so unlike a `User` row there is no deprovisioning history that
    removing it could falsify. `ScimGroupMembership` rows cascade with it."""
    group = await get_group(session, org_id, group_id)
    if group is None:
        return False
    await audit.record(
        session, actor=actor, action="scim.delete_group", org_id=org_id,
        target_type="scim_group", target_id=group_id,
        summary=f"displayName={group.display_name}",
    )
    await session.delete(group)
    return True


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

_SERVICE_PROVIDER_CONFIG = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
    "patch": {"supported": True},
    "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
    "filter": {"supported": True, "maxResults": MAX_PAGE_SIZE},
    "changePassword": {"supported": False},
    "sort": {"supported": False},
    "etag": {"supported": False},
    "authenticationSchemes": [{
        "type": "oauthbearertoken", "name": "Bearer",
        "description": "hub.manage issue-key <org_id> [days] scim",
    }],
}

_RESOURCE_TYPES = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
    "totalResults": 2,
    "Resources": [
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": SCIM_USER_SCHEMA,
        },
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": SCIM_GROUP_SCHEMA,
        },
    ],
}

_SCHEMAS = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
    "totalResults": 2,
    "Resources": [
        {
            "id": SCIM_USER_SCHEMA,
            "name": "User",
            "description": "hub/models.py User -- a person, not a workload API key",
            "attributes": [
                {"name": "userName", "type": "string", "required": True, "uniqueness": "server"},
                {"name": "displayName", "type": "string", "required": False},
                {"name": "active", "type": "boolean", "required": False},
            ],
        },
        {
            "id": SCIM_GROUP_SCHEMA,
            "name": "Group",
            "description": (
                "hub/models.py ScimGroup -- membership metadata only. "
                "Membership confers no capability; hub/rbac.py's role is "
                "the only thing this Hub checks."
            ),
            "attributes": [
                {"name": "displayName", "type": "string", "required": True, "uniqueness": "server"},
                {"name": "members", "type": "complex", "multiValued": True, "required": False},
            ],
        },
    ],
}


def _response(body: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, media_type=SCIM_CONTENT_TYPE)


def _scim_error_response(detail: str, *, status: int = 400, scim_type: str | None = None) -> JSONResponse:
    return _response(ScimError(detail, status=status, scim_type=scim_type).to_body(), status)


def _rate_limited(retry_after: float) -> JSONResponse:
    seconds = max(1, math.ceil(retry_after))
    return JSONResponse(
        {"schemas": [SCIM_ERROR_SCHEMA], "detail": "too many auth attempts", "status": "429"},
        status_code=429, media_type=SCIM_CONTENT_TYPE, headers={"Retry-After": str(seconds)},
    )


async def _authenticate(
    request: Request, session_factory: async_sessionmaker,
    auth_rate_limiter: RateLimiter, trusted_proxy_hops: int,
) -> tuple[str, str] | JSONResponse:
    """Returns `(org_id, actor)` on success, or a ready-to-return
    `JSONResponse` on failure. Mirrors hub/server.py's
    `ApiKeyAuthMiddleware` auth-attempt rate limiting exactly, against a
    dedicated limiter so SCIM traffic and MCP traffic never share (or
    starve) the same bucket."""
    client_key = rate_limit_key(request, trusted_proxy_hops)
    allowed, retry_after = await auth_rate_limiter.check(client_key)
    if not allowed:
        return _rate_limited(retry_after)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return _scim_error_response(
            "missing or malformed Authorization: Bearer <token> header", status=401
        )
    raw_token = header[len("bearer "):].strip()
    async with session_scope(session_factory) as session:
        key = await auth.verify_api_key(session, raw_token)
    if key is None or not scopes.satisfies(key.scopes, scopes.SCOPE_SCIM):
        # One message either way -- an invalid token and a valid-but-
        # unscoped one are not distinguishable at this layer, the same
        # collapsing hub/auth.py's own verify_api_key already does for
        # invalid/revoked/expired.
        return _scim_error_response(
            "invalid, expired, revoked, or insufficiently-scoped token", status=401
        )
    auth_rate_limiter.refund(client_key)
    return key.org_id, audit.actor_for_api_key(key.key_prefix)


def add_scim_routes(
    app, session_factory: async_sessionmaker, *,
    auth_rate_limiter: RateLimiter, trusted_proxy_hops: int = 0,
) -> None:
    """Mount `/scim/v2/*`. Always registered, unlike `/admin`/`/app`: there
    is no shared deployment-wide secret gating this the way there is for
    those (HubConfig.admin_token/console_secret) -- access is per-ORG,
    through the same argon2id-hashed, revocable `ApiKey` machinery the MCP
    endpoint already relies on, via `scopes.SCOPE_SCIM`. A deployment that
    never issues a `scim`-scoped key has a mounted endpoint that accepts
    zero requests -- indistinguishable in practice from not being mounted
    at all."""

    async def service_provider_config(request: Request) -> JSONResponse:
        return _response(_SERVICE_PROVIDER_CONFIG)

    async def resource_types(request: Request) -> JSONResponse:
        return _response(_RESOURCE_TYPES)

    async def schemas(request: Request) -> JSONResponse:
        return _response(_SCHEMAS)

    async def users_collection(request: Request) -> JSONResponse:
        result = await _authenticate(request, session_factory, auth_rate_limiter, trusted_proxy_hops)
        if isinstance(result, JSONResponse):
            return result
        org_id, actor = result

        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return _scim_error_response("request body is not valid JSON")
            if not isinstance(body, dict):
                return _scim_error_response("request body must be a JSON object")
            async with session_scope(session_factory) as session:
                try:
                    user = await create_user(session, org_id, body, actor=actor)
                except ScimError as exc:
                    # create_user already rolled back internally on the one
                    # path that mutates before failing (a uniqueness
                    # conflict); every other ScimError here is raised before
                    # anything was added, so there is nothing pending to
                    # discard -- no second rollback needed.
                    return _response(exc.to_body(), exc.status)
                scim_user = user_to_scim(user)
            return _response(scim_user, 201)

        # GET: list, with SCIM's own startIndex/count pagination and
        # exactly the one filter shape the module docstring names.
        query = request.query_params
        user_name = None
        raw_filter = query.get("filter")
        if raw_filter:
            match = _FILTER_RE.match(raw_filter)
            if match is None:
                return _scim_error_response(
                    f'unsupported filter (only `userName eq "<value>"` is supported): {raw_filter!r}',
                    scim_type="invalidFilter",
                )
            user_name = match.group(1)
        try:
            start_index = int(query.get("startIndex", "1"))
            count = int(query.get("count", str(DEFAULT_PAGE_SIZE)))
        except ValueError:
            return _scim_error_response("startIndex and count must be integers")
        async with session_scope(session_factory) as session:
            total, rows = await list_users(
                session, org_id, user_name=user_name, start_index=start_index, count=count,
            )
            resources = [user_to_scim(u) for u in rows]
        return _response({
            "schemas": [SCIM_LIST_RESPONSE_SCHEMA],
            "totalResults": total,
            "startIndex": max(1, start_index),
            "itemsPerPage": len(resources),
            "Resources": resources,
        })

    async def user_item(request: Request) -> JSONResponse:
        result = await _authenticate(request, session_factory, auth_rate_limiter, trusted_proxy_hops)
        if isinstance(result, JSONResponse):
            return result
        org_id, actor = result
        user_id = request.path_params["user_id"]

        if request.method == "GET":
            async with session_scope(session_factory) as session:
                user = await get_user(session, org_id, user_id)
            if user is None:
                return _scim_error_response(f"no such user: {user_id}", status=404)
            return _response(user_to_scim(user))

        if request.method == "DELETE":
            async with session_scope(session_factory) as session:
                user = await deactivate_user(session, org_id, user_id, actor=actor)
            if user is None:
                return _scim_error_response(f"no such user: {user_id}", status=404)
            return JSONResponse(None, status_code=204)

        try:
            body = await request.json()
        except ValueError:
            return _scim_error_response("request body is not valid JSON")
        if not isinstance(body, dict):
            return _scim_error_response("request body must be a JSON object")

        async with session_scope(session_factory) as session:
            if request.method == "PUT":
                user = await replace_user(session, org_id, user_id, body, actor=actor)
            else:  # PATCH
                user = await patch_user(session, org_id, user_id, body, actor=actor)
            if user is None:
                # Nothing was mutated on a not-found lookup -- no rollback
                # needed, session_scope's commit-on-exit has nothing to do.
                return _scim_error_response(f"no such user: {user_id}", status=404)
            scim_user = user_to_scim(user)
        return _response(scim_user)

    async def groups_collection(request: Request) -> JSONResponse:
        result = await _authenticate(request, session_factory, auth_rate_limiter, trusted_proxy_hops)
        if isinstance(result, JSONResponse):
            return result
        org_id, actor = result

        if request.method == "POST":
            try:
                body = await request.json()
            except ValueError:
                return _scim_error_response("request body is not valid JSON")
            if not isinstance(body, dict):
                return _scim_error_response("request body must be a JSON object")
            async with session_scope(session_factory) as session:
                try:
                    group = await create_group(session, org_id, body, actor=actor)
                except ScimError as exc:
                    return _response(exc.to_body(), exc.status)
                scim_group = await group_to_scim(session, group)
            return _response(scim_group, 201)

        # GET: list, with SCIM's own startIndex/count pagination and
        # exactly the one filter shape supported.
        query = request.query_params
        display_name = None
        raw_filter = query.get("filter")
        if raw_filter:
            match = _GROUP_FILTER_RE.match(raw_filter)
            if match is None:
                return _scim_error_response(
                    f'unsupported filter (only `displayName eq "<value>"` is supported): {raw_filter!r}',
                    scim_type="invalidFilter",
                )
            display_name = match.group(1)
        try:
            start_index = int(query.get("startIndex", "1"))
            count = int(query.get("count", str(DEFAULT_PAGE_SIZE)))
        except ValueError:
            return _scim_error_response("startIndex and count must be integers")
        async with session_scope(session_factory) as session:
            total, rows = await list_groups(
                session, org_id, display_name=display_name, start_index=start_index, count=count,
            )
            resources = [await group_to_scim(session, g) for g in rows]
        return _response({
            "schemas": [SCIM_LIST_RESPONSE_SCHEMA],
            "totalResults": total,
            "startIndex": max(1, start_index),
            "itemsPerPage": len(resources),
            "Resources": resources,
        })

    async def group_item(request: Request) -> JSONResponse:
        result = await _authenticate(request, session_factory, auth_rate_limiter, trusted_proxy_hops)
        if isinstance(result, JSONResponse):
            return result
        org_id, actor = result
        group_id = request.path_params["group_id"]

        if request.method == "GET":
            async with session_scope(session_factory) as session:
                group = await get_group(session, org_id, group_id)
                if group is None:
                    return _scim_error_response(f"no such group: {group_id}", status=404)
                scim_group = await group_to_scim(session, group)
            return _response(scim_group)

        if request.method == "DELETE":
            async with session_scope(session_factory) as session:
                deleted = await delete_group(session, org_id, group_id, actor=actor)
            if not deleted:
                return _scim_error_response(f"no such group: {group_id}", status=404)
            return JSONResponse(None, status_code=204)

        try:
            body = await request.json()
        except ValueError:
            return _scim_error_response("request body is not valid JSON")
        if not isinstance(body, dict):
            return _scim_error_response("request body must be a JSON object")

        async with session_scope(session_factory) as session:
            if request.method == "PUT":
                group = await replace_group(session, org_id, group_id, body, actor=actor)
            else:  # PATCH
                group = await patch_group(session, org_id, group_id, body, actor=actor)
            if group is None:
                return _scim_error_response(f"no such group: {group_id}", status=404)
            scim_group = await group_to_scim(session, group)
        return _response(scim_group)

    app.add_route("/scim/v2/ServiceProviderConfig", service_provider_config, methods=["GET"])
    app.add_route("/scim/v2/ResourceTypes", resource_types, methods=["GET"])
    app.add_route("/scim/v2/Schemas", schemas, methods=["GET"])
    app.add_route("/scim/v2/Users", users_collection, methods=["GET", "POST"])
    app.add_route("/scim/v2/Users/{user_id}", user_item, methods=["GET", "PUT", "PATCH", "DELETE"])
    app.add_route("/scim/v2/Groups", groups_collection, methods=["GET", "POST"])
    app.add_route("/scim/v2/Groups/{group_id}", group_item, methods=["GET", "PUT", "PATCH", "DELETE"])
