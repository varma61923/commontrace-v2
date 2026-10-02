"""The `/api/v1/*` REST surface the CommonTrace Claude Code plugin speaks."""

from __future__ import annotations

import json
import logging
import math

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub import audit, auth, crud, scopes
from hub.abuse import RateLimited, TraceRejected, make_named_limiter, rate_limit_key
from hub.config import HubConfig
from hub.db import session_scope
from hub.models import Organization
from hub.plans import EntitlementExceeded

logger = logging.getLogger("commontrace.hub.rest")

API_PREFIX = "/api/v1"

ACTOR_REST_API = "rest-api"
ACTOR_REST_SIGNUP = "rest-api-signup"

_DEFAULT_SEARCH_LIMIT = 3
_MAX_SEARCH_LIMIT = 50

_MAX_NAME_CHARS = 200

_DEFAULT_AGENT_TYPE = "general"


def _json_error(status: int, error: str, detail: str = "") -> JSONResponse:
    body: dict = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request) -> dict | None:
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(value: object, limit: int = 0) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text[:limit] if limit else text


def _tags(value: object) -> list[str]:
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        return [_text(t) for t in value if _text(t)]
    return []


def add_rest_routes(
    app,
    session_factory,
    *,
    config: HubConfig,
    rate_limiter,
    trusted_proxy_hops: int = 0,
    signup_enabled: bool = False,
) -> None:
    """Register the `/api/v1/*` routes. Call only when the REST API is enabled."""

    auth_limiter = make_named_limiter(
        config, config.auth_attempts_per_minute, config.auth_attempts_burst, "rest_auth"
    )
    signup_limiter = make_named_limiter(config, 1, 2, "signup")

    async def _authenticate(request: Request):
        allowed, retry_after = await auth_limiter.check(
            rate_limit_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return None, JSONResponse(
                {"error": "rate_limited", "detail": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        raw_key = request.headers.get("X-API-Key", "")
        if not raw_key:
            return None, _json_error(401, "unauthorized", "X-API-Key header is required")
        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        if authenticated is None:
            return None, _json_error(401, "unauthorized", "that key was not accepted")
        return authenticated, None

    def _require_scope(authenticated, required: str) -> Response | None:
        if not scopes.satisfies(authenticated.scopes, required):
            return _json_error(
                403,
                "forbidden",
                f"this key does not carry the '{required}' scope",
            )
        return None


    async def create_key(request: Request) -> Response:
        """`POST /api/v1/keys` -> `{"api_key", "org_id"}`."""
        allowed, retry_after = await signup_limiter.check(
            rate_limit_key(request, trusted_proxy_hops)
        )
        if not allowed:
            return JSONResponse(
                {"error": "rate_limited", "detail": "too many signups from this address"},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        payload = await _read_json(request)
        if payload is None:
            return _json_error(400, "bad_request", "body must be a JSON object")

        email = _text(payload.get("email"), _MAX_NAME_CHARS)
        display_name = _text(payload.get("display_name"), _MAX_NAME_CHARS)
        org_name = display_name or (email.split("@", 1)[0] if email else "")
        if not org_name:
            return _json_error(
                400, "bad_request", "one of display_name or email is required"
            )

        async with session_scope(session_factory) as session:
            org = Organization(name=org_name)
            session.add(org)
            await session.flush()
            issued = await auth.issue_api_key(session, org.id)
            await audit.record(
                session, actor=ACTOR_REST_SIGNUP, action="create_org",
                org_id=org.id, target_type="org", target_id=org.id,
                summary=f"name={org_name!r} via {API_PREFIX}/keys",
            )
            org_id = org.id
        return JSONResponse(
            {"api_key": issued.raw_key, "org_id": org_id},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )


    async def contribute(request: Request) -> Response:
        """`POST /api/v1/traces` -> `{"id", "quarantined", ...}`."""
        authenticated, denied = await _authenticate(request)
        if denied is not None:
            return denied
        denied = _require_scope(authenticated, scopes.SCOPE_WRITE)
        if denied is not None:
            return denied

        payload = await _read_json(request)
        if payload is None:
            return _json_error(400, "bad_request", "body must be a JSON object")

        title = _text(payload.get("title"))
        context_text = _text(payload.get("context_text"))
        solution_text = _text(payload.get("solution_text"))
        missing = [
            name for name, value in (
                ("title", title),
                ("context_text", context_text),
                ("solution_text", solution_text),
            ) if not value
        ]
        if missing:
            return _json_error(
                400, "bad_request", f"missing required field(s): {', '.join(missing)}"
            )

        idempotency_key = _text(payload.get("idempotency_key")) or None
        try:
            async with session_scope(session_factory) as session:
                result = await crud.contribute_trace(
                    session,
                    authenticated.org_id,
                    config,
                    rate_limiter,
                    title=title,
                    context_text=context_text,
                    solution_text=solution_text,
                    tags=_tags(payload.get("tags")),
                    agent_type=_text(payload.get("agent_type")) or _DEFAULT_AGENT_TYPE,
                    agent_id=_text(payload.get("agent_id")),
                    actor=ACTOR_REST_API,
                    idempotency_key=idempotency_key,
                    scopes=_tags(payload.get("scopes")),
                    valid_from=_text(payload.get("valid_from")) or None,
                    valid_until=_text(payload.get("valid_until")) or None,
                )
        except TraceRejected as exc:
            return _json_error(400, "rejected", str(exc))
        except crud.IdempotencyKeyConflict as exc:
            return _json_error(409, "conflict", str(exc))
        except EntitlementExceeded as exc:
            return _json_error(402, "entitlement_exceeded", str(exc))
        except RateLimited as exc:
            return JSONResponse(
                {"error": "rate_limited", "detail": str(exc)},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(exc.retry_after or 1)))},
            )
        return JSONResponse(result, status_code=201)

    async def search(request: Request) -> Response:
        """`POST /api/v1/traces/search` -> `{"results": [...]}`."""
        authenticated, denied = await _authenticate(request)
        if denied is not None:
            return denied
        denied = _require_scope(authenticated, scopes.SCOPE_READ)
        if denied is not None:
            return denied

        payload = await _read_json(request)
        if payload is None:
            return _json_error(400, "bad_request", "body must be a JSON object")

        query = _text(payload.get("q"))
        if not query:
            return _json_error(400, "bad_request", "q is required")
        try:
            limit = int(payload.get("limit", _DEFAULT_SEARCH_LIMIT))
        except (TypeError, ValueError, OverflowError):
            limit = _DEFAULT_SEARCH_LIMIT
        limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
        scope = _text(payload.get("scope"))
        as_of = _text(payload.get("as_of")) or None

        try:
            async with session_scope(session_factory) as session:
                found = await crud.search_traces(
                    session, authenticated.org_id, query=query, limit=limit, brief=True,
                    scope=scope, as_of=as_of,
                )
        except ValueError as exc:
            return _json_error(400, "bad_request", str(exc))
        results = []
        for trace in found.get("traces", []):
            row = dict(trace)
            row["contributor_name"] = trace.get("contributor", "")
            results.append(row)
        return JSONResponse({"results": results})


    async def telemetry(request: Request) -> Response:
        """`POST /api/v1/telemetry/{install,ping,triggers}` -> 204."""
        authenticated, denied = await _authenticate(request)
        if denied is not None:
            return denied
        return Response(status_code=204)

    app.add_route(f"{API_PREFIX}/traces", contribute, methods=["POST"])
    app.add_route(f"{API_PREFIX}/traces/search", search, methods=["POST"])
    for beacon in ("install", "ping", "triggers"):
        app.add_route(f"{API_PREFIX}/telemetry/{beacon}", telemetry, methods=["POST"])
    if signup_enabled:
        app.add_route(f"{API_PREFIX}/keys", create_key, methods=["POST"])
