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
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse

from hub import audit, auth, rbac, scopes
from hub.abuse import RateLimiter, resolve_client_key
from hub.crud import _is_uuid
from hub.db import session_scope
from hub.models import Organization, User

logger = logging.getLogger("commontrace.hub")

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_CONTENT_TYPE = "application/scim+json"

#: Role a SCIM-created account starts with -- see the module docstring's
#: "not proof of what they should be able to DO" paragraph.
DEFAULT_PROVISIONED_ROLE = rbac.ROLE_VIEWER

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 200

_FILTER_RE = re.compile(r'^\s*userName\s+eq\s+"(.*)"\s*$', re.IGNORECASE)


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
    "totalResults": 1,
    "Resources": [{
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": SCIM_USER_SCHEMA,
    }],
}

_SCHEMAS = {
    "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
    "totalResults": 1,
    "Resources": [{
        "id": SCIM_USER_SCHEMA,
        "name": "User",
        "description": "hub/models.py User -- a person, not a workload API key",
        "attributes": [
            {"name": "userName", "type": "string", "required": True, "uniqueness": "server"},
            {"name": "displayName", "type": "string", "required": False},
            {"name": "active", "type": "boolean", "required": False},
        ],
    }],
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
    client_key = resolve_client_key(request, trusted_proxy_hops)
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

    app.add_route("/scim/v2/ServiceProviderConfig", service_provider_config, methods=["GET"])
    app.add_route("/scim/v2/ResourceTypes", resource_types, methods=["GET"])
    app.add_route("/scim/v2/Schemas", schemas, methods=["GET"])
    app.add_route("/scim/v2/Users", users_collection, methods=["GET", "POST"])
    app.add_route("/scim/v2/Users/{user_id}", user_item, methods=["GET", "PUT", "PATCH", "DELETE"])
