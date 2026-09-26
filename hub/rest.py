"""The `/api/v1/*` REST surface the CommonTrace Claude Code plugin speaks.

WHY THIS EXISTS
---------------
This Hub's only programmatic surface was MCP (hub/server.py, mounted at
`/mcp`). The client that actually captures traces in the field -- the
`commontrace/skill` Claude Code plugin -- does not speak MCP at all. Its
hooks are plain `urllib` calls against a small JSON API:

    POST /api/v1/keys                  provision an account + key (no auth)
    POST /api/v1/traces                contribute one trace
    POST /api/v1/traces/search         retrieve relevant traces
    POST /api/v1/telemetry/install     one-shot install beacon
    POST /api/v1/telemetry/ping        daily heartbeat
    POST /api/v1/telemetry/triggers    which hooks fired this session

So the plugin and this Hub could not be pointed at each other, despite
storing the same objects and meaning the same things by them. This module
closes that gap: with `HUB_REST_API_ENABLED=true`, a deployment of this Hub
is a drop-in backend for the existing plugin --

    COMMONTRACE_API_BASE_URL=https://your-hub.example.com

-- with no plugin change at all. The endpoint paths, header name
(`X-API-Key`), request bodies and response shapes here are dictated by that
client, not chosen freshly; where a name reads oddly for this codebase
(`q` rather than `query`, `contributor_name` rather than `contributor`) it
is because the wire format is the plugin's, and the adapting happens here.

WHAT THIS IS NOT
----------------
Not a second implementation of anything. Every endpoint below is a thin
translation in front of the SAME `hub/crud.py` and `hub/auth.py` functions
the MCP tools call, so tenant isolation, plan entitlements, quarantine,
rate limits and audit rows behave identically whichever surface a caller
arrives through. A trace contributed here is indistinguishable afterwards
from one contributed over MCP, except for its audit `actor`.

Not a replacement for MCP. MCP gives an agent tool discovery, typed
arguments and the full twenty-tool surface; this gives an HTTP client six
endpoints. Deployments that need neither can leave both off.

AUTHENTICATION
--------------
`X-API-Key`, verified per-request with `auth.verify_api_key` -- deliberately
NOT by reusing `ApiKeyAuthMiddleware`, which is mounted on the MCP path and
carries MCP-specific context plumbing. Scope checks (hub/scopes.py) mirror
the MCP tools': search requires `read`, contribute requires `write`. A key
with neither gets 403, which the plugin already handles specifically (see
its `candidate.py`: "403 means publishing restricted for this account").

THE UNAUTHENTICATED ROUTE
-------------------------
`POST /api/v1/keys` mints a usable credential with no caller identity at
all, exactly as `hub/signup.py`'s form does -- so it is gated on the SAME
`HUB_SIGNUP_ENABLED` flag, rate limited per client address on the same
conservative budget, and audited under its own actor. With signup disabled
the route is not registered at all (404 from the router, not 403 from a
handler), matching how `/admin`, `/app` and `/signup` each stay absent
until configured.
"""

from __future__ import annotations

import json
import logging
import math

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hub import audit, auth, crud, scopes
from hub.abuse import RateLimited, RateLimiter, TraceRejected, rate_limit_key
from hub.config import HubConfig
from hub.db import session_scope
from hub.models import Organization
from hub.plans import EntitlementExceeded

logger = logging.getLogger("commontrace.hub.rest")

API_PREFIX = "/api/v1"

#: Audit actor for anything arriving over this surface. Distinct from the
#: MCP path's actor so "where did this trace come from" stays answerable
#: after the fact, the same reason hub/admin.py stamps `operator-console`.
ACTOR_REST_API = "rest-api"
ACTOR_REST_SIGNUP = "rest-api-signup"

#: The plugin asks for 3 and this Hub's own MAX_SEARCH_LIMIT is 200; clamp
#: rather than reject, matching `crud.search_traces`' own tolerant handling
#: of an out-of-range limit.
_DEFAULT_SEARCH_LIMIT = 3
_MAX_SEARCH_LIMIT = 50

#: An org name is all `POST /api/v1/keys` gets to work with, and the plugin
#: sends a synthesized `agent-<hex>@commontrace.auto` address rather than a
#: real one. Bounded for the same reason hub/signup.py bounds its form field.
_MAX_NAME_CHARS = 200

#: Traces arriving from a coding-agent plugin with no `agent_type` of their
#: own. Named rather than left empty so fleet reporting can group them.
_DEFAULT_AGENT_TYPE = "code"


def _json_error(status: int, error: str, detail: str = "") -> JSONResponse:
    """One error shape for every endpoint here.

    `error` is a stable machine-readable slug, `detail` the human sentence.
    The plugin only ever reads the status code and (on 403) prints the body,
    so the body stays short and free of anything tenant-specific.
    """
    body: dict = {"error": error}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status)


async def _read_json(request: Request) -> dict | None:
    """Parse a JSON object body, or None if it is malformed or not an object.

    Returns None rather than raising for BOTH failure modes on purpose: a
    truncated body and a JSON array are equally "the client sent something
    this endpoint cannot act on", and every caller below turns that into the
    same 400. Letting json.JSONDecodeError escape would surface as a 500,
    which is a server fault and this is not one.
    """
    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(value: object, limit: int = 0) -> str:
    """Coerce a JSON value to a stripped string.

    Defensive about type, not just absence: a client sending
    `{"title": 123}` or `{"title": null}` must get the same clean 400 a
    missing field gets, never a TypeError deep inside validation. `limit`
    truncates rather than rejects where the field is advisory (a display
    name), and is left 0 where `crud` does its own bounded validation.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    return text[:limit] if limit else text


def _tags(value: object) -> list[str]:
    """Normalize the `tags` field to a list of non-empty strings.

    Accepts both a JSON array and a comma-separated string: the plugin's
    directive text tells the agent to send "tags", and an LLM composing that
    body by hand produces either shape in practice. Anything else (a dict, a
    number) yields no tags rather than an error -- tags are advisory, and
    refusing an otherwise-valid trace over them would lose the trace.
    """
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
    """Register the `/api/v1/*` routes. Call only when the REST API is enabled.

    `rate_limiter` is the SAME write-bucket limiter `build_app` hands the MCP
    tools (`abuse.make_rate_limiter`), passed in rather than constructed here
    so a burst of contributions spends one shared per-org budget no matter
    which surface it arrives through -- two independent buckets would mean an
    org could double its real write rate simply by using both.
    """

    # Keyed by client address and checked before any credential work, for the
    # reason hub/server.py's auth limiter exists: an unauthenticated endpoint
    # that reaches Postgres on every request is a lever without one.
    auth_limiter = RateLimiter(
        per_minute=config.auth_attempts_per_minute,
        burst=config.auth_attempts_burst,
    )
    # Deliberately tight and deliberately not CAPTCHA-strength -- the same
    # budget and the same reasoning as hub/signup.py's form limiter, because
    # this is the same capability behind a different content type.
    signup_limiter = RateLimiter(per_minute=1, burst=2)

    async def _authenticate(request: Request):
        """(AuthenticatedKey, None) on success, or (None, error response).

        Rate limited before verification rather than after, so a flood of
        invalid keys costs one bucket check each instead of one Argon2
        verification each.
        """
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
            # One message for every failure mode -- unknown, revoked,
            # expired -- for the reason hub/console.py's sign-in gives:
            # distinguishing them tells an attacker which of those a
            # guessed key was.
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

    # --- Account provisioning ------------------------------------------

    async def create_key(request: Request) -> Response:
        """`POST /api/v1/keys` -> `{"api_key", "org_id"}`.

        The plugin calls this once per install with a synthesized address
        (`agent-<hex>@commontrace.auto`) and a display name, then stores the
        returned key in `~/.commontrace/config.json`. Neither field is
        verified -- there is no outbound email here to verify one with, the
        same v1 position hub/signup.py takes and documents.

        The org is named from `display_name` when one is sent, falling back
        to the local part of `email`, so an operator reading `list-orgs`
        sees something meaningful rather than a UUID wall.
        """
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
        # Shown exactly once, like every other issuance path here: this Hub
        # stores only an Argon2 hash and cannot reproduce the raw key later.
        return JSONResponse(
            {"api_key": issued.raw_key, "org_id": org_id},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    # --- Traces ---------------------------------------------------------

    async def contribute(request: Request) -> Response:
        """`POST /api/v1/traces` -> `{"id", "quarantined", ...}`.

        Body: `title`, `context_text`, `solution_text` (required), plus
        optional `tags`, `agent_type`, `agent_id`, `idempotency_key`.

        `metadata_json` is accepted and ignored. The plugin sends it (its
        session pattern, elapsed minutes, error count), but `Trace` has no
        column that means the same thing -- `extensions` is the profile
        schema's, not a scratch pad -- and inventing a home for it here
        would bake a shape into storage that nothing else reads. Accepting
        it silently is deliberate: rejecting the field would break a client
        that has always sent it, and the trace itself is the thing worth
        keeping.
        """
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
                )
        except TraceRejected as exc:
            return _json_error(400, "rejected", str(exc))
        except crud.IdempotencyKeyConflict as exc:
            return _json_error(409, "conflict", str(exc))
        except EntitlementExceeded as exc:
            # 402, not 403: the key is valid and permitted, the PLAN is the
            # thing in the way, and conflating the two would send the plugin
            # down its "publishing restricted for this account" branch for
            # what is really "you are at your trace cap".
            return _json_error(402, "entitlement_exceeded", str(exc))
        except RateLimited as exc:
            return JSONResponse(
                {"error": "rate_limited", "detail": str(exc)},
                status_code=429,
                headers={"Retry-After": str(max(1, math.ceil(exc.retry_after or 1)))},
            )
        return JSONResponse(result, status_code=201)

    async def search(request: Request) -> Response:
        """`POST /api/v1/traces/search` -> `{"results": [...]}`.

        Body: `q` (the query), optional `limit`, optional `context` (which
        the plugin sends and this Hub has nowhere to apply -- accepted and
        ignored, same reasoning as `metadata_json` above).

        Results are shaped for the plugin's `format_results`: it reads
        `id`, `title`, `solution_text` and `contributor_name`. The first
        three come straight off `crud.search_traces`; `contributor_name`
        is this Hub's `contributor` under the name the client expects.
        `brief=True` because the plugin only ever renders a 200-character
        preview -- sending untruncated bodies would be pure waste on a path
        whose whole job is injecting a short hint into a live session.
        """
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

        async with session_scope(session_factory) as session:
            found = await crud.search_traces(
                session, authenticated.org_id, query=query, limit=limit, brief=True,
            )
        results = []
        for trace in found.get("traces", []):
            row = dict(trace)
            # The client's name for it. Added alongside `contributor` rather
            # than renaming, so a non-plugin consumer of this endpoint still
            # sees the field this codebase calls it everywhere else.
            row["contributor_name"] = trace.get("contributor", "")
            results.append(row)
        return JSONResponse({"results": results})

    # --- Telemetry ------------------------------------------------------

    async def telemetry(request: Request) -> Response:
        """`POST /api/v1/telemetry/{install,ping,triggers}` -> 204.

        Accepted and dropped. The plugin fires these on a best-effort path
        that already ignores the response body and only checks for a 2xx, so
        the contract it actually depends on is "does not fail" -- and this
        Hub has no product-analytics store to land them in. Answering 404
        instead would make every plugin session log a swallowed error for a
        beacon nobody reads.

        Registered rather than omitted for exactly that reason: the useful
        behavior here is a deliberate, documented no-op, not an accident.
        Counting installs would need a store, a retention position and a
        privacy note; none of those exist yet, and inventing them silently
        inside a telemetry sink is how a product acquires data it never
        decided to collect.
        """
        authenticated, denied = await _authenticate(request)
        if denied is not None:
            return denied
        return Response(status_code=204)

    app.add_route(f"{API_PREFIX}/traces", contribute, methods=["POST"])
    app.add_route(f"{API_PREFIX}/traces/search", search, methods=["POST"])
    for beacon in ("install", "ping", "triggers"):
        app.add_route(f"{API_PREFIX}/telemetry/{beacon}", telemetry, methods=["POST"])
    # Absent, not merely refused, when self-serve account creation is off --
    # see this module's docstring.
    if signup_enabled:
        app.add_route(f"{API_PREFIX}/keys", create_key, methods=["POST"])
