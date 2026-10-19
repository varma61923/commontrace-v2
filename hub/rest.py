"""The `/api/v1/*` REST surface the CommonTrace Claude Code plugin speaks."""

from __future__ import annotations

import binascii
import json
import logging
import math

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub import audit, auth, crud, scopes
from hub.abuse import RateLimited, TraceRejected, make_named_limiter, rate_limit_key, reject_unstorable_text
from hub.config import HubConfig
from hub.db import session_scope
from hub.market import add_market_routes
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
        from commontrace.frontier_protocol import validate_json_tree

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object key")
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError("non-finite JSON number")

        payload = json.loads(
            await request.body(),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        validate_json_tree(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
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
    oblivious_replica_resolver=None,
    oblivious_clearance_resolver=None,
) -> None:
    """Register the `/api/v1/*` routes. Call only when the REST API is enabled."""

    from hub import delegation

    delegation.install(app, config)
    auth_limiter = make_named_limiter(
        config, config.auth_attempts_per_minute, config.auth_attempts_burst, "rest_auth"
    )
    identity_provider = config.identity_provider()
    jwks_cache = None
    if identity_provider is not None and identity_provider.jwks_uri:
        from hub.sso import JWKSCache

        def fetch_jwks(uri: str) -> dict:
            import httpx

            response = httpx.get(uri, timeout=5.0, follow_redirects=False)
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict):
                raise ValueError("OIDC JWKS response must be a JSON object")
            return document

        jwks_cache = JWKSCache(fetch_jwks)
    signup_limiter = make_named_limiter(config, 1, 2, "signup")
    # Per-org ceiling on authenticated calls, the REST twin of the MCP
    # middleware's read limiter: the auth limiter above is refunded for a
    # valid key, so without this one a single key could drive unmetered
    # ranked search and memory queries through /api/v1.
    org_limiter = make_named_limiter(
        config, config.read_rate_limit_per_minute, config.read_rate_limit_burst, "rest_read"
    )

    async def _admit(request: Request, authenticated):
        async def delegated():
            denied = await delegation.admit_request(request, authenticated, config, session_factory)
            return (None, denied) if denied is not None else (authenticated, None)

        if request.method == "DELETE":
            return await delegated()
        allowed, retry_after = await org_limiter.check(authenticated.org_id)
        if allowed:
            return await delegated()
        return None, JSONResponse(
            {"error": "rate_limited", "detail": "too many requests"},
            status_code=429,
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    async def _authenticate(request: Request):
        client_key = rate_limit_key(request, trusted_proxy_hops)
        allowed, retry_after = await auth_limiter.check(client_key)
        if not allowed:
            return None, JSONResponse(
                {"error": "rate_limited", "detail": "too many requests"},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        raw_key = request.headers.get("X-API-Key", "")
        authorization = request.headers.get("authorization", "")
        if not raw_key and authorization.lower().startswith("bearer "):
            bearer = authorization[len("bearer "):].strip()
            from hub.sso import looks_like_jwt

            if identity_provider is not None and looks_like_jwt(bearer):
                async with session_scope(session_factory) as session:
                    person = await auth.verify_user_token(
                        session, bearer, identity_provider, jwks_cache=jwks_cache,
                    )
                if person is not None:
                    from hub import rbac

                    auth_limiter.refund(client_key)
                    return await _admit(request, auth.AuthenticatedKey(
                        person.org_id, f"user:{person.id}", rbac.scopes_of(person.role),
                    ))
            return None, _json_error(401, "unauthorized", "OIDC bearer token was rejected or is not linked")
        if not raw_key:
            return None, _json_error(401, "unauthorized", "X-API-Key or linked OIDC bearer token is required")
        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        if authenticated is None:
            return None, _json_error(401, "unauthorized", "that key was not accepted")
        # The limiter bounds failed guesses; a valid key must not spend the budget.
        auth_limiter.refund(client_key)
        return await _admit(request, authenticated)

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
        try:
            reject_unstorable_text(org_name, "display_name")
        except ValueError as exc:
            return _json_error(400, "bad_request", str(exc))

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
            async with session_scope(session_factory, org_id=authenticated.org_id) as session:
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
                    actor=authenticated.key_prefix if authenticated.key_prefix.startswith("user:") else ACTOR_REST_API,
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
        except ValueError as exc:
            return _json_error(400, "bad_request", str(exc))
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
            async with session_scope(session_factory, org_id=authenticated.org_id) as session:
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

    async def oblivious_pir(request: Request) -> Response:
        """One replica of the two-server XOR-PIR protocol; clearance is key-derived."""
        import base64

        if oblivious_replica_resolver is None or oblivious_clearance_resolver is None:
            return _json_error(503, "unavailable", "oblivious recall is not configured on this Hub replica")
        authenticated, denied = await _authenticate(request)
        if denied is not None:
            return denied
        denied = _require_scope(authenticated, scopes.SCOPE_READ)
        if denied is not None:
            return denied
        try:
            from commontrace import oblivious

            ceiling = oblivious_clearance_resolver(authenticated)
            replica = oblivious_replica_resolver(authenticated.org_id, ceiling)
            if (not callable(getattr(replica, "handle", None))
                    or not callable(getattr(replica, "handle_many", None))):
                raise TypeError("oblivious resolver must return a two-server PIR endpoint adapter")
            payload = await _read_json(request)
            if payload is None:
                raise ValueError("body must be a JSON object")
            cluster = payload.get("cluster")
            if not isinstance(cluster, str) or not cluster or len(cluster.encode()) > 96:
                raise ValueError("cluster must be a string of 1..96 bytes")
            encoded_many = payload.get("shares")
            encoded = payload.get("share")
            if encoded_many is not None and encoded is not None:
                raise ValueError("supply share or shares, never both")

            def decode_share(value: object) -> bytes:
                if not isinstance(value, str) or len(value) > 172:
                    raise ValueError("PIR share must be a base64 string of at most 172 characters")
                return base64.b64decode(value, validate=True)

            if encoded_many is not None:
                if (not isinstance(encoded_many, list)
                        or len(encoded_many) > oblivious.MAX_BLOCKS_PER_CLUSTER):
                    raise ValueError("PIR shares must be a bounded array")
                shares = [decode_share(item) for item in encoded_many]
                result = await run_in_threadpool(replica.handle_many, cluster, shares)
            else:
                share = None if encoded is None else decode_share(encoded)
                result = await run_in_threadpool(replica.handle, cluster, share)
            if isinstance(result, bytes):
                return JSONResponse({"answer": base64.b64encode(result).decode("ascii")})
            if isinstance(result, list):
                return JSONResponse({"answers": [base64.b64encode(item).decode("ascii") for item in result]})
            return JSONResponse(result)
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            return _json_error(400, "bad_request", str(exc))

    from hub.memory_embedding import ProjectionFactory
    from hub.memory_records import add_memory_routes

    if not hasattr(app.state, "memory_projection_factory"):
        app.state.memory_projection_factory = ProjectionFactory(session_factory, config.database_url)
    add_memory_routes(app, session_factory, authenticate=_authenticate, require_scope=_require_scope)
    add_market_routes(app, session_factory, config=config, authenticate=_authenticate,
                      require_scope=_require_scope)
    from hub import transparency

    transparency.add_routes(app, session_factory, authenticate=_authenticate,
                            require_scope=_require_scope, config=config)
    from hub import incidents

    incidents.add_routes(app, session_factory, authenticate=_authenticate,
                         require_scope=_require_scope, config=config)
    from hub import pooled

    pooled.add_routes(app, session_factory, authenticate=_authenticate,
                      require_scope=_require_scope, config=config)
    from hub import policies

    policies.add_routes(app, session_factory, authenticate=_authenticate,
                        require_scope=_require_scope, config=config)
    from hub import attestation

    attestation.add_routes(app, session_factory, authenticate=_authenticate,
                          require_scope=_require_scope, config=config)
    app.add_route(f"{API_PREFIX}/traces", contribute, methods=["POST"])
    app.add_route(f"{API_PREFIX}/traces/search", search, methods=["POST"])
    app.add_route(f"{API_PREFIX}/oblivious/pir", oblivious_pir, methods=["POST"])
    for beacon in ("install", "ping", "triggers"):
        app.add_route(f"{API_PREFIX}/telemetry/{beacon}", telemetry, methods=["POST"])
    if signup_enabled:
        app.add_route(f"{API_PREFIX}/keys", create_key, methods=["POST"])
