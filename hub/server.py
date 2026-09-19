"""MCP wiring for the six Hub tools.

This module is deliberately thin: every tool function below does exactly
three things -- resolve the caller's org_id from request context, call the
matching hub/crud.py function (which is where org-scoping actually happens),
and shape the result/errors for MCP. No query logic lives here; see
hub/crud.py's module docstring for why that split is what makes tenant
isolation testable.

Any MCP-capable client (Claude Code, Cursor, Devin, Windsurf, a generic MCP
client) can attach to this server with a plain config block -- no
CommonTrace-specific SDK -- by pointing at HUB_HOST:HUB_PORT/mcp with an
`Authorization: Bearer <api-key>` header. See hub/README.md for a worked
example.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ipaddress
import json
import logging
import math

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from commontrace import __version__ as _COMMONTRACE_VERSION
from hub import auth, collab, commons, crud, observability, plans, scheduler, scopes
from hub.abuse import (
    RateLimited,
    RateLimiter,
    TraceRejected,
    make_auth_rate_limiter,
    make_rate_limiter,
    make_read_rate_limiter,
    make_scim_auth_rate_limiter,
    resolve_client_key,
)
from hub.admin import add_admin_routes
from hub.billing import StripeSettings, add_billing_webhook_route
from hub.config import DEFAULT_SEARCH_LIMIT, HubConfig
from hub.console import CONSOLE_PATH, add_console_routes
from hub.db import check_row_level_security, session_scope
from hub.disclosure import add_disclosure_route
from hub.observability import RequestContextMiddleware, add_health_routes
from hub.rest import add_rest_routes
from hub.schema_validation import SchemaValidationError
from hub.scim import add_scim_routes
from hub.signup import add_signup_routes

logger = logging.getLogger("commontrace.hub")


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Authenticates every request under `streamable_http_path` with a
    `Authorization: Bearer <api-key>` header, resolves it to an org_id via
    hub/auth.py, and sets that org_id in the request-scoped contextvar tool
    handlers read from. Requests without a valid key are rejected here, with
    a 401, before they ever reach MCP protocol dispatch -- no tool handler
    ever runs without a resolved org_id.

    A dedicated, unauthenticated `/healthz` path is exempt (for load
    balancer / orchestrator liveness checks, which by design carry no
    tenant credentials).

    Two rate limiters gate every request through here, on top of whatever
    hub/crud.py's own per-write-op limiter does further in:

      - `auth_rate_limiter`, keyed by client address, checked BEFORE
        verification runs. hub/auth.py resolves most requests through an
        indexed `key_hmac` lookup now (cheap, ~O(1)), but any key issued
        before that existed -- or not yet backfilled -- still falls back to
        the original prefix-scan-plus-Argon2 path, which is deliberately
        expensive CPU work performed for every candidate key sharing a
        presented key's prefix. Without this limiter, a remote attacker can
        flood the endpoint with credentials sharing a known/guessed prefix
        to exhaust the process's to_thread worker pool on that fallback
        path. This bounds CPU spent per source rather than only reacting
        after paying for it.
      - `read_rate_limiter`, keyed by org_id, checked once a request is
        authenticated. hub/crud.py's rate_limiter only ever gated
        contribute_trace/amend_trace; search_traces, get_trace, vote_trace,
        list_tags, and commons_overlap had no limit at all. This is a more
        generous ceiling covering every tool call, so a single valid key
        cannot drive unmetered full-text search, ranking computation, or
        MinHash corpus scans.
    """

    def __init__(
        self,
        app: ASGIApp,
        session_factory: async_sessionmaker,
        protected_path: str,
        auth_rate_limiter: RateLimiter,
        read_rate_limiter: RateLimiter,
        trusted_proxy_hops: int = 0,
        identity_provider=None,
    ):
        super().__init__(app)
        self._session_factory = session_factory
        self._protected_path = protected_path
        self._auth_rate_limiter = auth_rate_limiter
        self._read_rate_limiter = read_rate_limiter
        # See HubConfig.trusted_proxy_hops's docstring: 0 (default) means
        # "trust only request.client.host", identical to this middleware's
        # behavior before this parameter existed.
        self._trusted_proxy_hops = trusted_proxy_hops
        # None when this deployment has not configured SSO at all
        # (HubConfig.identity_provider()) -- in which case a JWT-shaped
        # bearer token is refused with the same message an invalid API key
        # gets, rather than this middleware attempting verification against
        # a provider that does not exist.
        self._identity_provider = identity_provider
        # Built ONCE, held for the middleware's lifetime -- see
        # hub/sso.py:JWKSCache's own docstring for why a per-request cache
        # would defeat its entire purpose. Only constructed when a provider
        # is configured, so a deployment with no SSO pays nothing for it.
        self._jwks_cache = None
        if identity_provider is not None and identity_provider.jwks_uri:
            from hub.sso import JWKSCache

            self._jwks_cache = JWKSCache(self._fetch_jwks)

    @staticmethod
    def _fetch_jwks(uri: str) -> dict:
        # A short, blocking-safe HTTP fetch -- httpx is already a direct
        # dependency (hub/requirements.txt) for other outbound calls
        # (webhook delivery), so this adds no new one. Timeout deliberately
        # tight: this only ever runs on a JWKS TTL miss, never per request,
        # so a slow or unreachable IdP fails one verification rather than
        # hanging the request that happened to trigger the refetch.
        import httpx

        response = httpx.get(uri, timeout=5.0)
        response.raise_for_status()
        return response.json()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Exact match or a path *segment* under it -- plain startswith()
        # would also treat e.g. "/mcpadmin" as "under /mcp", which happens
        # to be harmless today only because no such route exists in this
        # app's route table (/mcp, /healthz, /readyz). Matching on the
        # segment boundary makes that true by construction instead of by
        # coincidence, so it stays true if a route is ever added later.
        if not (path == self._protected_path or path.startswith(self._protected_path + "/")):
            return await call_next(request)

        client_key = resolve_client_key(request, self._trusted_proxy_hops)
        allowed, retry_after = await self._auth_rate_limiter.check(client_key)
        if not allowed:
            return _rate_limited_response("too many auth attempts", retry_after)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization: Bearer <api-key> header"}, status_code=401
            )
        raw_token = header[len("bearer ") :].strip()

        # Distinguished by SHAPE (three non-empty dot-separated segments),
        # not by attempting one parse and catching its exception -- an API
        # key (`ct_live_...`) never has that shape, so this is a clean
        # either/or rather than a fallback chain. A JWT-shaped token is
        # tried ONLY as a person; it is never also hashed and looked up as
        # an API key, which would be wasted Argon2/HMAC work on bytes that
        # cannot possibly match.
        from hub.sso import looks_like_jwt

        if looks_like_jwt(raw_token) and self._identity_provider is not None:
            person = None
            async with self._session_factory() as session:
                person = await auth.verify_user_token(
                    session, raw_token, self._identity_provider,
                    jwks_cache=self._jwks_cache,
                )
                await session.commit()  # persists last_login_at touch
            if person is None:
                # One message for "not verifiable", "no linked user", and
                # "deprovisioned" alike -- for the same reason
                # verify_api_key collapses invalid/revoked/expired: telling
                # a caller which one they hit is a probe they should not
                # get for free.
                return JSONResponse(
                    {"error": "invalid, expired, or unlinked identity token"}, status_code=401
                )
            self._auth_rate_limiter.refund(client_key)
            if request.method != "DELETE":
                allowed, retry_after = await self._read_rate_limiter.check(person.org_id)
                if not allowed:
                    return _rate_limited_response("too many requests", retry_after)

            from hub import rbac

            org_token = auth.current_org_id.set(person.org_id)
            # Non-secret, and distinguishable from an API key's "api-key:
            # <prefix>" actor string at a glance in an audit-log row.
            actor_token = auth.current_actor.set(f"user:{person.id}")
            # Derived from the role (hub/rbac.py:ROLE_SCOPES) so the
            # existing scope-based enforcement point stays meaningful for a
            # person too; auth.require_capability is the ADDITIONAL,
            # finer-grained gate this identity actually goes through.
            scope_token = auth.current_scopes.set(rbac.scopes_of(person.role))
            user_token = auth.current_user.set(person)
            try:
                return await call_next(request)
            finally:
                auth.current_org_id.reset(org_token)
                auth.current_actor.reset(actor_token)
                auth.current_scopes.reset(scope_token)
                auth.current_user.reset(user_token)

        async with self._session_factory() as session:
            authenticated = await auth.verify_api_key(session, raw_token)
            await session.commit()  # persists last_used_at touch

        if authenticated is None:
            # One message for invalid / revoked / expired alike -- see
            # hub/auth.py:verify_api_key for why they must be indistinguishable.
            return JSONResponse({"error": "invalid, revoked, or expired API key"}, status_code=401)

        # The credential verified, so refund the auth-attempt token spent
        # above. That limiter's job is bounding Argon2 CPU forced by
        # credentials that DON'T verify; charging the ones that do made it
        # the binding constraint on legitimate bulk traffic (a bulk push is
        # hundreds of successful authentications from one address, against a
        # 60/min budget) while doing nothing extra against an attacker, who
        # by definition never reaches this line. Valid callers are governed
        # by the per-org read limiter immediately below -- authenticated,
        # accountable, and metered, which is the right instrument for them.
        self._auth_rate_limiter.refund(client_key)

        # A DELETE on the MCP path is the client tearing its session down.
        # It runs no tool, reads no row, and costs the server less than the
        # 429 body refusing it would -- while refusing it makes an otherwise
        # SUCCESSFUL command print "Session termination failed: 429" from
        # inside the MCP SDK, which is what a user sees and reasonably reads
        # as "my run failed". It is also self-defeating: the client stops
        # waiting either way, so the only effect is that the server keeps
        # the session state it was being asked to release.
        #
        # Still authenticated (above), so this is not an unauthenticated
        # hole: only a caller holding a valid key for this org can reach it,
        # and it cannot be used to do any work.
        if request.method != "DELETE":
            allowed, retry_after = await self._read_rate_limiter.check(authenticated.org_id)
            if not allowed:
                return _rate_limited_response("too many requests", retry_after)

        org_token = auth.current_org_id.set(authenticated.org_id)
        actor_token = auth.current_actor.set(authenticated.key_prefix)
        # What this particular credential may do, as distinct from which org
        # it speaks for (hub/scopes.py). Set here, beside the org, because
        # this is the only place that has seen the key row; every tool then
        # reads it through auth.require_scope with nothing to pass around.
        scope_token = auth.current_scopes.set(authenticated.scopes)
        try:
            return await call_next(request)
        finally:
            auth.current_org_id.reset(org_token)
            auth.current_actor.reset(actor_token)
            auth.current_scopes.reset(scope_token)


class LoadShedMiddleware:
    """Bound how much work one replica accepts, and how long any of it runs.

    hub/bench_concurrency.py measured what this replaces. At 128 concurrent
    clients against an EMPTY handler, p99 reached 1.3s and throughput was
    flat from 8 clients upward -- every additional client became queue, and
    nothing anywhere stopped that queue growing. A slow database makes it
    worse in the way that is hardest to diagnose: requests pile onto a 10+5
    connection pool with a 30s pool_timeout until they all fail at once, so
    the first symptom is total failure rather than degradation.

    Two bounds, doing different jobs:

    `max_concurrent` sheds load. Past N requests in flight the next one is
    refused immediately with 503 and a Retry-After instead of joining the
    queue. Refusing fast is kinder than queueing: the caller learns now and
    can back off rather than waiting out a timeout to be told the same
    thing, and the requests already admitted keep the latency they were
    promised instead of everyone degrading together. That is why the
    semaphore is *tested* and never *waited on* -- acquiring with a timeout
    would reintroduce the queue this exists to prevent.

    `timeout` bounds one request: not too many requests, but one that will
    not finish.

    RAW ASGI, deliberately, and this is the whole reason the class is not a
    BaseHTTPMiddleware like its neighbours. Under BaseHTTPMiddleware the
    timeout does not work -- measured, not assumed: with a 1s timeout over
    a handler that sleeps 4s, the client gets its 504 after 4.01s. Wrapping
    `call_next` in wait_for cancels the middleware's own side of the
    plumbing, but the downstream handler keeps running to completion, so
    the bound relabels a slow response instead of stopping it and buys
    exactly nothing. Driving the child app as a task this class owns is
    what makes cancellation actually reach the handler.

    Both bounds default to on; hub/config.py's `max_concurrent_requests=0`
    disables the cap and `request_timeout_seconds=0` the timeout, for a
    deployment that does this at its edge proxy and does not want two
    layers disagreeing about which one refused a request.
    """

    def __init__(self, app: ASGIApp, *, max_concurrent: int, timeout_seconds: int):
        self.app = app
        self._timeout = timeout_seconds if timeout_seconds > 0 else None
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None

    async def __call__(self, scope, receive, send) -> None:
        # Only HTTP is bounded. A websocket has no meaningful "request
        # duration", and cancelling `lifespan` would take down startup.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._semaphore is not None:
            if self._semaphore.locked():
                await _send_json(send, 503, _OVERLOADED_BODY, [(b"retry-after", b"1")])
                return
            async with self._semaphore:
                await self._run(scope, receive, send)
            return
        await self._run(scope, receive, send)

    async def _run(self, scope, receive, send) -> None:
        if self._timeout is None:
            await self.app(scope, receive, send)
            return

        # Whether any bytes are already committed to the wire. Past that
        # point a 504 is not available -- the status line has been sent --
        # so the only honest thing left is to stop writing.
        started = False

        async def tracking_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        task = asyncio.create_task(self.app(scope, receive, tracking_send))
        try:
            await asyncio.wait_for(task, timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "request exceeded %ss and was cancelled: %s %s",
                self._timeout, scope.get("method", "?"), scope.get("path", "?"),
            )
            if not started:
                # 504, not 500: nothing is known to be broken. The request
                # ran out of time, which is a different thing to tell a
                # client, and the only one of the two worth retrying.
                await _send_json(send, 504, _timeout_body(self._timeout))


_OVERLOADED_BODY = {"error": "overloaded", "detail": "too many concurrent requests"}


def _timeout_body(seconds: int) -> dict:
    return {"error": "timeout", "detail": f"request exceeded {seconds}s"}


async def _send_json(send, status: int, body: dict, extra_headers=()) -> None:
    """Emit a JSON response through the raw ASGI `send`.

    LoadShedMiddleware refuses and times out below Starlette's Response
    machinery, so it writes the two messages itself. `Retry-After` is here
    for the same reason _rate_limited_response carries it: a refused client
    that is not told when to come back can only guess, and guessing short
    against an overloaded replica turns one burst into a sustained one. One
    second is the honest floor -- unlike a rate limiter this cap has no
    bucket to compute a real refill time from, and capacity may free up
    immediately.
    """
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(payload)).encode())]
    headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _rate_limited_response(detail: str, retry_after: float) -> JSONResponse:
    """A 429 that says WHEN to come back.

    Without `Retry-After` a refused client can only guess, and a client
    guessing short against a limiter that is already saying no turns one
    burst into a sustained stampede -- measured on this project's own CLI,
    a bulk `commontrace sync --push-traces` drove 114 rejected requests
    where the honest answer was "wait about two seconds". The header is the
    standard, machine-readable way to say that, and
    commontrace/hub_client.py now paces its whole batch off it.

    Rounded UP to a whole second: `Retry-After` is defined in integer
    seconds, and rounding down would advertise a moment at which the bucket
    provably still has no token, inviting exactly the extra rejected
    request this exists to prevent. Floored at 1 for the same reason.
    """
    return JSONResponse(
        {"error": "rate_limited", "detail": detail, "retry_after": max(1, math.ceil(retry_after))},
        status_code=429,
        headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
    )


def _error_response(exc: Exception) -> dict:
    # Before the generic PermissionError branch below, and deliberately a
    # different label. "unauthorized" invites a client to re-authenticate,
    # and for a scope denial that is a lie: the credential is valid, it is
    # this action it may not take, and retrying with the same key will fail
    # identically forever. The two extra fields let a caller render "this
    # token needs the write scope" instead of a generic failure, without
    # parsing the prose.
    if isinstance(exc, auth.ScopeDenied):
        return {
            "error": "forbidden",
            "detail": str(exc),
            "required_scope": exc.required,
            "granted_scopes": list(exc.granted) if exc.granted is not None else None,
        }
    if isinstance(exc, auth.CapabilityDenied):
        # Distinct from ScopeDenied for the same reason it exists: the
        # credential is valid, and the remedy is re-roling a PERSON
        # (hub.manage set-user-role), not reissuing a key.
        return {
            "error": "forbidden",
            "detail": str(exc),
            "required_capability": exc.required,
            "role": exc.role,
        }
    if isinstance(exc, auth.PersonRequiredError):
        # Distinct from ScopeDenied/CapabilityDenied: the API key itself is
        # perfectly valid, there is just no PERSON in context for a tool
        # that only means something for one (whose inbox, who authored a
        # comment, who is being assigned work).
        return {"error": "person_required", "detail": str(exc)}
    if isinstance(exc, PermissionError):
        return {"error": "unauthorized", "detail": str(exc)}
    if isinstance(exc, RateLimited):
        # `retry_after` for the same reason the HTTP 429s carry the header:
        # this limiter's shipped default (20 writes/minute) means a client
        # pushing a backlog is refused as a matter of course, and a refusal
        # that does not say when to come back leaves it guessing -- which,
        # measured on this project's own `sync --push-traces`, is how a
        # recoverable wait turned into a permanent per-file error.
        # Counted here as well as at the HTTP layer: a write-limit refusal
        # is returned INSIDE a 200 MCP response, so the middleware's
        # status-code counter never sees it. Without this the metric would
        # under-report exactly the limiter most likely to be misconfigured.
        observability.METRICS.observe_rate_limited("write")
        body = {"error": "rate_limited", "detail": str(exc)}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            body["retry_after"] = max(1, math.ceil(retry_after))
        return body
    if isinstance(exc, crud.IdempotencyKeyConflict):
        return {"error": "conflict", "detail": str(exc)}
    if isinstance(exc, plans.EntitlementExceeded):
        # Distinct from "rate_limited" on purpose. A rate limit clears by
        # waiting; this one does not, and a client that retries a plan
        # refusal on a backoff schedule will retry it forever. The extra
        # fields are what let a client render an upgrade path instead of a
        # generic failure.
        return {
            "error": "entitlement_exceeded",
            "detail": str(exc),
            "metric": exc.metric,
            "limit": exc.limit,
            "used": exc.used,
            "plan": exc.plan,
            "remedy": exc.remedy,
        }
    if isinstance(exc, commons.CommonsInputError):
        return {"error": "invalid_request", "detail": str(exc)}
    if isinstance(exc, crud.DeletionNotReady):
        # Distinct from invalid_request: the request itself is well-formed,
        # it is just too early, expired, or token-mismatched -- a client
        # should surface this to a human, not treat it as a bug to fix and
        # retry immediately.
        return {"error": "deletion_not_ready", "detail": str(exc)}
    if isinstance(exc, crud.SubscriptionCancellationFailed):
        # Distinct from deletion_not_ready: the token and timing were fine,
        # but nothing was deleted -- an external dependency (Stripe) has to
        # actually confirm the subscription is cancelled first, so a client
        # should retry rather than treat this as a bug in the request.
        return {"error": "deletion_blocked", "detail": str(exc)}
    if isinstance(exc, collab.CollabNotFound):
        return {"error": "not_found", "detail": str(exc)}
    if isinstance(exc, (TraceRejected, SchemaValidationError, ValueError)):
        return {"error": "invalid_request", "detail": str(exc)}
    logger.exception("unexpected error in Hub tool")
    return {"error": "internal_error", "detail": "an unexpected error occurred"}


class IpAllowlistMiddleware(BaseHTTPMiddleware):
    """Application-level source-address restriction -- the code-only half
    of audit 1.6's "no IP allowlisting / private networking" (see
    HubConfig.ip_allowlist's own docstring for why the OTHER half, actual
    private networking, is a deployment-topology decision this middleware
    does not and cannot make).

    Only mounted when `HUB_IP_ALLOWLIST` is set (see `build_app`) -- a
    deployment that never configures it pays nothing extra per request,
    same "absent, not merely permissive" posture as `/admin`/`/app` being
    unregistered when their own secrets are unset.

    `/healthz` and `/readyz` are exempt for the same reason
    `ApiKeyAuthMiddleware` exempts `/healthz`: an orchestrator's own
    liveness/readiness probes are a different population than the
    external traffic this restricts, arriving from the platform's
    internal network rather than wherever this allowlist is meant to
    keep out -- refusing them would turn a security control into a
    self-inflicted outage.

    `/disclosure` (hub/disclosure.py) is exempt for the opposite reason:
    it exists specifically so an external party with no relationship to
    this deployment's own network -- a customer's procurement or security
    reviewer -- can read it. An IP allowlist meant to keep general traffic
    out would make the one page built for outside reach unreachable from
    outside, which is the one thing it must never do.
    """

    def __init__(self, app: ASGIApp, networks, trusted_proxy_hops: int = 0):
        super().__init__(app)
        self._networks = networks
        self._trusted_proxy_hops = trusted_proxy_hops

    async def dispatch(self, request: Request, call_next):
        # Defensive, not load-bearing: build_app never mounts this
        # middleware at all when config.ip_allowlist is empty. Checked
        # again here so the class's own behavior matches its docstring
        # ("no restriction when unconfigured") independent of how a
        # future caller constructs it.
        if not self._networks:
            return await call_next(request)
        if request.url.path in ("/healthz", "/readyz", "/disclosure"):
            return await call_next(request)
        client_ip_raw = resolve_client_key(request, self._trusted_proxy_hops)
        try:
            client_ip = ipaddress.ip_address(client_ip_raw)
        except ValueError:
            # "unknown" (no request.client at all) or a malformed
            # X-Forwarded-For entry -- either way, a source address this
            # deployment cannot verify is allowlisted is refused, not
            # let through by default.
            return JSONResponse(
                {"error": "forbidden", "detail": "source address could not be determined"},
                status_code=403,
            )
        if not any(client_ip in network for network in self._networks):
            return JSONResponse(
                {"error": "forbidden", "detail": "source address not allowlisted"}, status_code=403
            )
        return await call_next(request)


def build_mcp_server(config: HubConfig, session_factory: async_sessionmaker, rate_limiter: RateLimiter):
    from mcp.server.mcpserver import MCPServer

    # Only for confirm_account_deletion, to cancel a live Stripe
    # subscription before an org's row (and with it, its
    # stripe_subscription_id) is gone for good -- see
    # crud.confirm_org_deletion's own docstring. Unconfigured
    # (StripeSettings() equivalent) on any deployment that never set the
    # Stripe env vars, which is a no-op there since no org can hold a
    # subscription id in the first place.
    stripe_settings = StripeSettings(
        secret_key=config.stripe_secret_key,
        webhook_secret=config.stripe_webhook_secret,
        price_team=config.stripe_price_team,
        price_scale=config.stripe_price_scale,
    )

    mcp = MCPServer(
        name="commontrace",
        # Not an independent MCP-server-specific version: PROTOCOL.md §9
        # explicitly unified the package/protocol/CLI version numbers into
        # one 2.0.0 the whole product reports identically, precisely to
        # stop drift like a Hub still announcing a stale "0.1.0" to every
        # connecting MCP client.
        version=_COMMONTRACE_VERSION,
        instructions=(
            "CommonTrace Hub. search_traces/get_trace/list_tags read; contribute_trace "
            "writes a new trace; vote_trace/amend_trace act on an existing one; "
            "delete_trace permanently removes one of your own (irreversible); "
            "account_usage reports your plan and usage. All operations are scoped to "
            "your organization's own traces (hub/README.md 'Tenant isolation') -- "
            "there is no tool anywhere on this server that exposes one organization's "
            "traces to another. request_account_deletion/confirm_account_deletion/"
            "cancel_account_deletion delete your ENTIRE organization -- two calls with "
            "a mandatory delay between them, by design; see request_account_deletion's "
            "own description before calling it. "
            + ("commons_overlap/commons_search additionally let you consult the "
               "CommonTrace Knowledge Base: commons_search looks up ranked candidate "
               "answers to one failure, commons_overlap reports the conservative "
               "coverage fraction across many. submit_kb_entry proposes a new entry "
               "for operator review (list_my_kb_submissions checks status) -- an "
               "accepted proposal raises your Knowledge Base query allowance, but "
               "nothing you submit is published, or visible to any other org, until "
               "an operator accepts it."
               if config.commons_enabled else
               "This deployment has HUB_COMMONS_ENABLED=false: no Knowledge Base "
               "tools exist on this server.")
        ),
    )

    # Which scope each tool needs, recorded as the tool is registered.
    # hub/tests/test_api_key_scopes.py asserts this covers every registered
    # tool, so a new tool cannot be added without a scope decision: the
    # failure mode being designed out is a tool that silently defaults to
    # "any authenticated key may call this", which is exactly the state the
    # whole surface was in before scopes existed.
    tool_scopes: dict[str, str] = {}

    def scoped_tool(scope: str):
        """Register an MCP tool that requires `scope` (hub/scopes.py).

        One enforcement point rather than a check inside each of the twenty
        handlers: a check an author must remember to write is a check that
        is eventually missing from exactly the one tool where it mattered.
        Written at the registration site so the required capability reads
        directly above the function it guards.

        `functools.wraps` matters structurally here, not cosmetically: the
        MCP SDK derives each tool's name, description and input schema by
        introspecting the callable it is handed, so the wrapper must carry
        the wrapped function's identity or every tool would arrive on the
        wire as an undocumented `guarded(*args, **kwargs)`.
        """
        if scope not in scopes.ALL_SCOPES:
            raise ValueError(f"unknown scope for tool registration: {scope!r}")

        def decorator(fn):
            @functools.wraps(fn)
            async def guarded(*args, **kwargs):
                try:
                    auth.require_scope(scope)
                    # A no-op when the caller is a bare API key (no
                    # verified person in context) -- see
                    # auth.require_capability's own docstring. When a
                    # person IS in context this is the gate that actually
                    # distinguishes them; the scope check above is the
                    # coarser one their role also derives (hub/rbac.py).
                    auth.require_capability(fn.__name__)
                except Exception as exc:  # noqa: BLE001 - rendered, not raised
                    return _error_response(exc)
                return await fn(*args, **kwargs)

            tool_scopes[fn.__name__] = scope
            return mcp.tool()(guarded)

        return decorator

    @scoped_tool(scopes.SCOPE_READ)
    async def search_traces(
        query: str = "",
        tags: list[str] | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
        occasion_id: str = "",
        brief: bool = False,
        pinned: list[str] | None = None,
    ) -> dict:
        """Search this org's traces by full-text query and/or tags.

        Describe the task you are about to attempt, in your own words and in
        a full sentence -- that is the query shape this is built for. Terms
        are OR-ed and the results are ranked, so a trace that matches part
        of your description still comes back; you do not need to guess the
        wording the trace was written in.

        Returns {"traces": [...], "limit", "offset", "has_more", "terms",
        "terms_ignored"}. Page by re-calling with offset += limit while
        has_more is true.

        `context_text`/`solution_text` are each allowed up to 20,000
        characters, so a full page of results can be large. Pass
        `brief=True` to get a short preview of both instead (marked
        `"brief": true` per result) when you are scanning many candidates to
        pick one -- then call `get_trace(id)` for the one you decide to use.
        Everything else on each result (id, title, tags, agent_type) is
        unaffected either way.

        `terms` is what your query reduced to after stemming and stopword
        removal, and `terms_ignored` lists the terms that were NOT used
        because they appear in too much of your corpus to distinguish one
        trace from another. Between them, an empty result is readable
        instead of ambiguous: no `terms` means nothing searchable was asked,
        every term in `terms_ignored` means the words you used are ones
        nearly all your traces contain (try a more specific one), and terms
        present with neither condition means your corpus genuinely has no
        match yet.

        Pass `occasion_id` -- your own identifier for the task you are
        about to do -- and, IF an operator has started a randomized
        holdout for your org, the response also carries a `holdout` block
        naming which of the returned traces you must NOT use on this
        occasion (every trace is still returned either way; report the
        result with `record_occasion_outcome(occasion_id, succeeded)`).
        Omitting `occasion_id`, or running with no experiment configured,
        behaves exactly as before -- no `holdout` block, nothing recorded.

        This is the same holdout mechanic `holdout_assign` documents in
        full (why using a withheld trace anyway silently biases the
        result, and why `pinned` -- your `working_set` entries -- must be
        passed on every call): read that docstring once for the mechanics,
        which apply here identically.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                result = await crud.search_traces(
                    session, org_id, query=query, tags=tags, limit=limit, offset=offset, brief=brief
                )
                if occasion_id:
                    result["holdout"] = await crud.holdout_for_results(
                        session, org_id, result["traces"], occasion_id,
                        actor=auth.get_current_actor(), pinned=pinned,
                    )
                return result
        except Exception as exc:  # noqa: BLE001 - converted to a structured tool error below
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def contribute_trace(
        title: str,
        context_text: str,
        solution_text: str,
        tags: list[str] | None = None,
        agent_type: str = "",
        agent_id: str = "",
        profile: str = "",
        outcome: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Contribute a new trace. Returns its id, quarantine status, and
        `possible_duplicates` -- ids of other live traces in this org a
        bounded heuristic thinks may be the same thing as this one.
        Informational only; nothing is merged or blocked automatically.
        Call `amend_trace` yourself if one of them should be superseded.

        Pass a client-generated `idempotency_key` (e.g. a UUID minted once
        per logical contribution) to make retries after a lost/timed-out
        response safe: retrying with the same key returns the original
        result instead of creating a duplicate trace. Reusing a key with a
        different payload is rejected as a conflict rather than silently
        returning the wrong trace.

        `agent_id` identifies WHICH agent produced this trace, as opposed to
        `agent_type`, which is the kind of agent it is ("support", "code").
        A fleet of 25 support agents shares one agent_type, so only agent_id
        can answer how many agents an org runs -- the number its plan's
        agent limit is enforced against. Optional and backward compatible:
        omitting it attributes the trace to a single 'unattributed' agent
        for that org, which is never refused but also cannot be counted
        precisely, so the org's reported agent count becomes a floor.

        `outcome` is this incident's eventual disposition, if already known
        -- an object with any of: `resolved`, `escalated`, `repeated_error`,
        `frustration_signal` (booleans), `tokens_used`/`llm_calls`
        (non-negative numbers), `baseline` (boolean, marks a trace captured
        before lessons were being injected). `fleet_outcomes` is the only
        way this Hub can answer "is this working?", and it can only answer
        it for outcomes actually reported here. Often not known yet at
        contribution time -- amend_trace accepts the same parameter, MERGED
        into whatever this call already set, to attach or update it once
        the task concludes.

        `profile` names a domain-specific extension profile this trace
        belongs to (protocol/schemas/trace.schema.json, e.g. "code-review"
        for the Alpha/A/B/Omega/Lambda pipeline SKILL.md ships). Optional;
        carried forward unchanged by every later amend_trace call on this
        trace, the same way agent_type/agent_id are.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                result = await crud.contribute_trace(
                    session,
                    org_id,
                    config,
                    rate_limiter,
                    title=title,
                    context_text=context_text,
                    solution_text=solution_text,
                    tags=tags,
                    agent_type=agent_type,
                    agent_id=agent_id,
                    profile=profile,
                    outcome=outcome,
                    actor=auth.get_current_actor(),
                    idempotency_key=idempotency_key,
                )
            return result
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def get_trace(id: str) -> dict:
        """Fetch a single trace by id. Not found (including a trace id that
        belongs to another org) reports not_found, never a permission error."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                trace = await crud.get_trace(session, org_id, id)
            if trace is None:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return trace
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def vote_trace(id: str, vote: str, feedback_tag: str = "", feedback_text: str = "") -> dict:
        """Cast (or update) this org's vote ('up'/'down') on a trace: your
        own, or any other org's trace currently shared to the commons."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                trace = await crud.vote_trace(
                    session, org_id, id, vote, feedback_tag, feedback_text,
                    actor=auth.get_current_actor(),
                )
            if trace is None:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return trace
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def amend_trace(
        id: str,
        title: str | None = None,
        context_text: str | None = None,
        solution_text: str | None = None,
        tags: list[str] | None = None,
        outcome: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Create a new trace that supersedes `id`, carrying forward any
        field not explicitly overridden.

        `outcome` (same shape as contribute_trace's) is MERGED into the
        original's outcome rather than replaced, since an incident's
        resolution is often only known after the fact: contribute_trace
        with none, then amend_trace with `{"resolved": true}` once the task
        concludes, then perhaps another amend_trace with `{"tokens_used":
        800}` -- each call layers in what it knows without erasing what an
        earlier call already attached.

        Pass a client-generated `idempotency_key` (e.g. a UUID minted once
        per logical amendment) to make retries after a lost/timed-out
        response safe: retrying with the same key returns the original
        amendment instead of forking the supersession chain. Reusing a key
        with a different `id` or different field overrides (including a
        different `outcome`) is rejected as a conflict rather than silently
        returning the wrong trace.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                amended = await crud.amend_trace(
                    session,
                    org_id,
                    id,
                    config,
                    rate_limiter,
                    title=title,
                    context_text=context_text,
                    solution_text=solution_text,
                    tags=tags,
                    outcome=outcome,
                    actor=auth.get_current_actor(),
                    idempotency_key=idempotency_key,
                )
            if amended is None:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return amended
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def list_tags() -> dict:
        """List every distinct tag used across this org's non-quarantined traces."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                tags = await crud.list_tags(session, org_id)
            return {"tags": tags}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def fleet_outcomes(agent_type: str = "") -> dict:
        """Has your fleet's agent performance changed since your baseline
        window? Compares the outcomes recorded on your own traces
        (resolution, repeated-error, escalation and frustration rates, plus
        token/call cost) between traces marked `outcome.baseline: true` and
        everything since, with confidence intervals, a Benjamini-Hochberg
        correction across the metrics, and a minimum detectable effect on
        every inconclusive result so a small sample cannot be misread as
        "no effect".

        Reads only your own traces. Not metered.

        IMPORTANT, and returned with every response: this is an OBSERVED
        change, not a causal effect. `baseline` marks a time window, so
        anything else that changed between the windows is confounded with
        this product's contribution. For a causal claim, run the randomized
        holdout (`commontrace experiment`), which withholds lessons at
        random so the arms differ only by the treatment.

        The causal estimate comes back under `causal`, and inside it
        `causal.integrity` says whether the sample it was computed on can
        support it. READ THAT BEFORE THE EFFECT SIZES. It is not a
        formality: the estimate is computed only on occasions that got an
        outcome reported, which is unbiased only when both arms report at
        the same rate -- and the withheld arm, by construction the one
        working without its memory, is the one more likely to run long or
        be abandoned before anyone reports. When that happens the result
        does not look empty or underpowered; it looks like a confident,
        well-powered, significant effect with a tight interval.
        `causal.integrity.effects_readable` is false when a named mechanism
        is biasing it, and `causal.integrity.projections` says how far each
        trace is from being answerable at all.

        Optionally narrow to one `agent_type`."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                report = await crud.fleet_outcomes(session, org_id, agent_type=agent_type)
                # The causal instrument, returned alongside the
                # observational one rather than in a separate tool: a
                # reader who sees only the before/after number has no way
                # to know a stronger answer was available, and the whole
                # point of the distinction is that it be visible.
                report["causal"] = await crud.causal_effects(session, org_id)
                return report
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def working_set(budget_chars: int = crud.DEFAULT_WORKING_SET_CHARS) -> dict:
        """Your fleet's proven memory, small enough to pin to a system prompt.

        Call this ONCE at session start, paste `block` into your system
        prompt, and leave it there unchanged. That is the whole point: an
        unchanged system prompt keeps the model provider's prefix cache
        valid, so this memory costs its tokens once for the session rather
        than once per query. Re-fetching it mid-session, or editing it,
        throws away the saving.

        It is not a replacement for `search_traces`, and using it that way
        would make your fleet worse. This block holds only what has already
        been PROVEN to help; `search_traces` reaches your entire corpus,
        including everything still being measured and everything relevant
        to a task nobody has hit before. Pin this, then search as normal.

        What earns a place here is the part worth understanding.
        Membership is not curated, not most-recent, and not most-retrieved
        -- it is decided by your own randomized holdout. A trace appears
        only once the experiment has ESTABLISHED that injecting it
        improves outcomes, ranked by how many occasions it actually
        improved. Anything still under test is deliberately absent, and
        that absence is doing real work: a trace pinned into every
        session would be injected on every occasion, which would destroy
        the control arm still measuring it. A trace is either being
        randomized or it has graduated -- never both.

        So an empty block is a statement about EVIDENCE, not about your
        corpus: `established: false` means nothing has been proven yet,
        not that nothing is stored. Keep searching, keep reporting
        outcomes with `record_occasion_outcome`, and entries appear here
        as the experiment answers for them. A COMPROMISED experiment
        yields no block at all, for the same reason it yields no value
        figure.

        `gauge` reports how much of the character budget is spent, so you
        can see at a glance whether the block is near its ceiling.

        Reads only your own data. Not metered."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.working_set(session, org_id, budget_chars=budget_chars)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def value_delivered(
        value_per_occasion: float = 0.0, rate_tiers: list[dict] | None = None
    ) -> dict:
        """What your fleet's memory has been worth, causally, in occasions.

        Not a usage number and not a correlational one. For each memory whose
        causal effect the running holdout has actually established, this is
        `effect x times injected` -- how many more occasions went well BECAUSE
        that memory existed -- carried through with its confidence interval.

        Three rules make it a measurement rather than a brochure, and you
        should check all three before quoting it:

        - A COMPROMISED experiment returns no figure at all. Not a hedged one.
          If a named mechanism is biasing the effects, it biases every value
          computed from them, and a value report is exactly where a caveat
          gets separated from the number.
        - An UNDERPOWERED memory contributes nothing. Its effect was not
          established, and multiplying it by a volume produces a large number
          with no evidence under it.
        - Memories measured as HURTING are SUBTRACTED, not dropped.

        `value_per_occasion` is yours: pass what one resolved occasion is worth
        to your organisation and the response carries the money too. Pass
        nothing and you get the count. No price is stored anywhere -- this
        product ships the quantity and takes the rate from you.

        `rate_tiers` is that rate stated properly, for organisations where one
        flat number prices a password reset and an averted outage the same way.
        Pass `[{"name": "L1", "share": 0.55, "cost_per_occasion": 8.0}, ...]`
        with shares summing to 1. Both are your inputs, echoed back in the
        response as inputs: nothing here measures which tier an occasion
        belonged to, and only `occasions_improved` was measured at all.

        When a rate is given and the run is readable you also get `ledger` --
        one line per counted memory, each carrying a SHA-256 over the previous
        line, so editing a figure, deleting the memory that HURT, or
        reordering to bury it all break the chain. Recompute it with
        `commontrace.value.verify_ledger`, or reimplement it: the hash is over
        the printed fields in a fixed order, on purpose.

        That chain proves the ledger is internally consistent, not who issued
        it -- its genesis and algorithm are public, so a wholesale
        replacement chain would verify just as cleanly as the real one. If
        this deployment has HUB_LEDGER_SIGNING_KEY configured, the response
        also carries `signature` and `issued_at`: an HMAC-SHA256 over the
        chain's root, checkable with `commontrace.value.
        verify_ledger_signature`, that only verifies for a ledger this
        deployment actually issued. Otherwise `signature` is null and
        `signature_reason` says the deployment has not opted in.

        Reads only your own data. Not metered."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.value_delivered(
                    session, org_id,
                    value_per_occasion=(value_per_occasion or None),
                    rate_tiers=rate_tiers,
                    signing_key=config.ledger_signing_key,
                )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def holdout_assign(
        trace_ids: list, occasion_id: str, pinned: list[str] | None = None
    ) -> dict:
        """Randomized holdout: for each trace eligible on this occasion,
        decide whether to inject it or deliberately withhold it, and record
        the decision so the two arms can later be compared.

        This is the only way to get a CAUSAL answer about whether your
        memory is helping. `fleet_outcomes` compares your fleet against its
        own past, which cannot separate this product's effect from anything
        else that changed. Here the two arms are the same fleet in the same
        window, differing only by whether the memory was injected.

        `occasion_id` is your own identifier for one unit of work, and it
        is the key you later report the result against. Safe to retry: the
        arms are a deterministic hash, so a repeat call returns the same
        answer and records nothing new.

        **Traces returned under `withhold` must not be used on this
        occasion.** Using one anyway does not fail loudly -- it moves that
        occasion into the treated arm without the record saying so, which
        biases the measured effect toward zero.

        `pinned` -- the `entries[].trace_id` of any `working_set` block you
        pasted into your system prompt -- are excluded from the
        randomization and returned under `pinned` rather than assigned an
        arm. Pass them: a trace in your prompt is used on every occasion, so
        letting it be drawn into the control arm records a treated occasion
        as a control and biases its own measured effect toward zero.

        Requires an operator to have started an experiment for your org."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.holdout_assign(
                    session, org_id, list(trace_ids), occasion_id,
                    actor=auth.get_current_actor(), pinned=pinned,
                )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def record_occasion_outcome(occasion_id: str, succeeded: bool) -> dict:
        """Report how an occasion went, closing the loop on every holdout
        decision made for it.

        The outcome belongs to the TASK, not to any one memory, so this
        resolves both arms at once. Only the first report for an occasion
        counts -- a later one is ignored rather than allowed to flip a
        result already counted."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.record_occasion_outcome(
                    session, org_id, occasion_id, succeeded, actor=auth.get_current_actor(),
                )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # --- Self-service deletion --------------------------------------------
    #
    # delete_trace is immediate and org-scoped, same trust level as every
    # other write tool above. Whole-account deletion is split into two
    # differently-named calls with a mandatory delay between them
    # (crud.request_org_deletion's docstring) precisely because a single
    # call here would let one compromised API key wipe an org's entire
    # history irreversibly with no window for anyone to notice.

    @scoped_tool(scopes.SCOPE_READ)
    async def search_trace_content(pattern: str, regex: bool = False, limit: int = 100) -> dict:
        """Locate traces (INCLUDING quarantined ones) whose title, context,
        or solution text literally contain `pattern` -- a name, an email
        address, a ticket number, whatever a subject-erasure request names.

        Distinct from `search_traces`: that tool ranks by full-text
        relevance and can miss or mangle an exact identifier through
        stemming. This one does a literal (or, with `regex=True`, POSIX
        regular expression) scan with no ranking -- every match, oldest
        first.

        NOT a completeness guarantee: a match proves the text is present;
        a non-match is not proof of absence (free text can misspell,
        abbreviate, or split an identifier this cannot reassemble). Use
        this to FIND candidates for manual review, then `delete_trace`
        the ones that actually need to go.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                results = await crud.search_trace_content(
                    session, org_id, pattern, regex=regex, limit=limit,
                )
            return {"results": results}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def tag_trace_subjects(id: str, subject_ids: list) -> dict:
        """Set (REPLACING any previous tags, not appending) which end
        user(s)/customer(s) this trace's content concerns -- pass `[]` to
        clear a mistaken tag. Optional and explicit: nothing populates
        this automatically.

        This is what makes `find_traces_by_subject`/`purge_traces_by_subject`
        an exact, provably-complete match instead of the free-text scan
        `search_trace_content` already offers -- see that tool's own
        docstring for what it can and cannot guarantee. Untagged content
        still needs the free-text search; tagging it here is what upgrades
        it to a certainty.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                result = await crud.tag_trace_subjects(
                    session, org_id, id, subject_ids, actor=auth.get_current_actor(),
                )
            if result is None:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return result
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def find_traces_by_subject(subject_id: str) -> dict:
        """Every trace THIS ORG explicitly tagged (`tag_trace_subjects`)
        with `subject_id`, exactly matched -- not a scan, and not stemmed
        the way `search_traces`' full-text index is. Untagged content
        naming the same subject in free text is not returned here; use
        `search_trace_content` for that."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                results = await crud.find_traces_by_subject(session, org_id, subject_id)
            return {"results": results}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def purge_traces_by_subject(subject_id: str) -> dict:
        """Permanently delete every trace this org tagged with
        `subject_id` (`tag_trace_subjects`), including each one's full
        amendment chain -- the actual erasure step
        `find_traces_by_subject` only ever located candidates for.
        Irreversible. A `subject_id` nothing was tagged with returns
        `purged: 0`, not an error."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                result = await crud.purge_traces_by_subject(
                    session, org_id, subject_id, actor=auth.get_current_actor(),
                )
            return result
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def delete_trace(id: str) -> dict:
        """Permanently delete one of your own traces, and every trace in
        its amendment chain (so an amended-and-superseded copy of the same
        content cannot survive the original being deleted). Irreversible.
        Not found (including a trace id belonging to another org) reports
        not_found, never a permission error."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                deleted = await crud.delete_trace(session, org_id, id, actor=auth.get_current_actor())
            if not deleted:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return {"id": id, "deleted": True}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def request_account_deletion() -> dict:
        """Start permanently deleting YOUR ENTIRE ORGANIZATION -- every
        trace, vote, api key, and Knowledge Base submission. Deletes
        NOTHING by itself.

        Returns a one-time confirmation_token and confirm_not_before /
        expires_at timestamps. Call confirm_account_deletion with that
        token, no sooner than confirm_not_before, to actually delete
        everything -- irreversibly. Call cancel_account_deletion at any
        time before then to stand down.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.request_org_deletion(session, org_id, actor=auth.get_current_actor())
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def cancel_account_deletion() -> dict:
        """Cancel a pending request_account_deletion request. Needs no
        token -- any valid API key for this org may call it, since
        cancelling is a safety action, not a destructive one."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                cancelled = await crud.cancel_org_deletion(session, org_id, actor=auth.get_current_actor())
            return {"cancelled": cancelled}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_ADMIN)
    async def confirm_account_deletion(confirmation_token: str) -> dict:
        """The second call: permanently deletes this organization and
        everything scoped to it. Irreversible. Fails with
        'deletion_not_ready' if called too soon after
        request_account_deletion, with an expired or mismatched token, or
        with no pending request at all. Fails with 'deletion_blocked',
        and deletes nothing, if this org has a paid Stripe subscription
        that could not be cancelled -- retry once whatever is stopping
        Stripe from reaching us clears, since deleting the account while
        leaving the subscription active would keep charging the card on
        file with no account left to ever notice.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                await crud.confirm_org_deletion(
                    session, org_id, confirmation_token, actor=auth.get_current_actor(),
                    stripe=stripe_settings,
                )
            return {"deleted": True}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # --- CommonTrace Knowledge Base (opt-in), gated by HUB_COMMONS_ENABLED
    #
    # `if config.commons_enabled:` around the @mcp.tool() registrations
    # themselves, not a check inside each handler -- a disabled deployment
    # must not even LIST these tools. A client that tries gets the MCP
    # framework's own "unknown tool" error, which holds even if an org
    # forgets the Knowledge Base exists; a per-call refusal only holds if
    # every caller remembers to check first. account_usage is intentionally
    # outside this block: it reports an org's own plan and its own usage,
    # never Knowledge Base content, so disabling it does not disable that.
    #
    # There is no share_trace/unshare_trace tool here, and there never will
    # be: an org's own trace can never be promoted directly into the
    # Knowledge Base by anything an org's own API key can call. See
    # hub/plans.py "why there is no org-to-org sharing here" -- letting one
    # customer's data become visible to another was the design this module
    # used to have, and it was retired on purpose.
    #
    # submit_kb_entry below is not that tool reborn: it writes a
    # KnowledgeBaseSubmission row (a table entirely separate from Trace),
    # which is invisible to every read path in this file until
    # hub/manage.py review-submission -- an operator-trust-level action, not
    # an MCP tool -- deliberately accepts it. See hub/crud.py's "Knowledge
    # Base community submissions" section and
    # hub/models.py:KnowledgeBaseSubmission.
    if config.commons_enabled:

        @scoped_tool(scopes.SCOPE_READ)
        async def commons_overlap(
            failures: list[dict] | None = None,
            threshold: float = commons.DEFAULT_COMMONS_THRESHOLD,
            include_matches: bool = True,
            agent_type: str = "",
        ) -> dict:
            """Of the recurring failures your fleet keeps hitting, what fraction
            does the CommonTrace Knowledge Base already solve?

            The Knowledge Base is authored and curated by the operator -- public
            substrate knowledge, never another customer's data. There is no
            sharing to do first: nothing you submit is ever added to it, and
            nothing here can expose your fleet's traces to anyone else.

            Send MinHash signatures of your own failures -- generated locally by
            `commontrace commons sign`, so no failure text ever leaves your
            machine. Each entry is {"label": str, "signature": [int, ...]}.
            """
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    return await crud.commons_overlap(
                        session, org_id, failures or [],
                        threshold=threshold, include_matches=include_matches,
                        agent_type=agent_type,
                    )
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

        @scoped_tool(scopes.SCOPE_READ)
        async def commons_search(
            query_signature: list[int] | None = None,
            limit: int = commons.DEFAULT_SEARCH_CANDIDATES,
            agent_type: str = "",
        ) -> dict:
            """Ask the CommonTrace Knowledge Base what it already knows about
            ONE failure, and get back ranked candidate answers with their
            solutions -- like searching a wiki, not a peer's ticket queue.

            This is the lookup: "has anyone solved this?". `commons_overlap`
            answers the different, quotable question "what FRACTION of my
            failures are solved" and buys 0% false positives with a threshold
            that discards about nine of every ten real answers. This tool
            ranks instead, and finds the right record 89.1% of the time at
            rank 1 and 100% within the top 10 on the held-out evaluation
            (commons/eval/RESULTS.md).

            Send one MinHash signature, generated locally by `commontrace
            commons sign` -- no failure text leaves your machine. What comes
            back is drawn only from the operator-curated Knowledge Base,
            never from another customer's traces.

            Results are CANDIDATES TO JUDGE, never coverage: a failure the
            Knowledge Base does not contain still returns a non-empty list
            every time. Do not derive a percentage from this tool -- that is
            what commons_overlap is for.
            """
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    return await crud.commons_search(
                        session, org_id, query_signature or [],
                        limit=limit, agent_type=agent_type,
                    )
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

        @scoped_tool(scopes.SCOPE_WRITE)
        async def submit_kb_entry(
            title: str,
            context_text: str,
            solution_text: str,
            tags: list[str] | None = None,
            agent_type: str = "",
            rationale: str = "",
            idempotency_key: str | None = None,
        ) -> dict:
            """Propose an entry for the CommonTrace Knowledge Base -- like
            posting an answer to a shared wiki, not sharing your own trace
            history. Nothing is published by this call.

            Write it as generalized substrate knowledge ("Stripe webhook
            handlers need idempotency keys"), not as your own incident with
            its specifics -- `rationale` should say why this is substrate
            rather than your business logic. An operator reviews every
            submission before anything is published
            (`hub/manage.py review-submission`); check status with
            `list_my_kb_submissions`. An accepted submission permanently
            raises your org's Knowledge Base query allowance -- a rejected
            or still-pending one earns nothing.

            Pass a client-generated `idempotency_key` to make a retry after
            a lost response safe, exactly like `contribute_trace`.
            """
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    return await crud.submit_kb_entry(
                        session, org_id, config, rate_limiter,
                        title=title, context_text=context_text, solution_text=solution_text,
                        tags=tags, agent_type=agent_type, rationale=rationale,
                        actor=auth.get_current_actor(), idempotency_key=idempotency_key,
                    )
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

        @scoped_tool(scopes.SCOPE_READ)
        async def list_my_kb_submissions(limit: int = 50) -> dict:
            """Your org's own Knowledge Base submissions and their review
            status ('pending', 'approved', or 'rejected'). Never shows
            another org's submissions, and a submission of yours is never
            visible to another org either, reviewed or not -- an approved
            one is visible to other orgs only as an ordinary Knowledge Base
            entry, with no link back to this row or to your org."""
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    return {"submissions": await crud.list_my_kb_submissions(session, org_id, limit=limit)}
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def add_comment(trace_id: str, body: str) -> dict:
        """Leave a remark on one of your org's own traces, visible to your
        whole team. Requires a signed-in PERSON (an OIDC bearer token
        linked via `hub.manage link-sso`), not just an API key -- there is
        no meaningful author for a shared workload credential. If the
        trace is currently assigned to someone else, they get a
        notification (`list_my_notifications`)."""
        try:
            org_id = auth.get_current_org_id()
            person = auth.get_current_user()
            async with session_scope(session_factory) as session:
                return await collab.add_comment(session, org_id, person, trace_id, body)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def list_comments(trace_id: str) -> dict:
        """Every comment left on one of your org's own traces, oldest first."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return {"comments": await collab.list_comments(session, org_id, trace_id)}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def assign_trace(trace_id: str, user_id: str) -> dict:
        """Make `user_id` (one of your org's own `hub.manage list-users`
        rows) the one person responsible for following up on this trace.
        Re-assigning replaces whoever held it before -- one owner at a
        time. Requires a signed-in PERSON."""
        try:
            org_id = auth.get_current_org_id()
            person = auth.get_current_user()
            async with session_scope(session_factory) as session:
                return await collab.assign(session, org_id, person, trace_id, user_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_WRITE)
    async def unassign_trace(trace_id: str) -> dict:
        """Clear whoever this trace is currently assigned to, if anyone.
        Requires a signed-in PERSON."""
        try:
            org_id = auth.get_current_org_id()
            person = auth.get_current_user()
            async with session_scope(session_factory) as session:
                cleared = await collab.unassign(session, org_id, person, trace_id)
            return {"cleared": cleared}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def list_my_notifications(unread_only: bool = False) -> dict:
        """Your own inbox: 'you were assigned a trace' / 'someone commented
        on a trace assigned to you'. Requires a signed-in PERSON -- there
        is no per-person inbox for a shared API key. Mark one read with
        `mark_notification_read`."""
        try:
            org_id = auth.get_current_org_id()
            person = auth.get_current_user()
            async with session_scope(session_factory) as session:
                notifications = await collab.list_my_notifications(
                    session, org_id, person, unread_only=unread_only,
                )
            return {"notifications": notifications}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def mark_notification_read(notification_id: str) -> dict:
        """Mark one of YOUR OWN inbox entries read. Never affects, or even
        confirms the existence of, another person's notification.

        Scoped `read`, not `write`: this only ever bookkeeps YOUR OWN
        inbox and adds nothing to the org's corpus (hub/scopes.py's
        `write` is reserved for that), so a read-only key held by a
        signed-in Viewer can still clear their own notifications."""
        try:
            org_id = auth.get_current_org_id()
            person = auth.get_current_user()
            async with session_scope(session_factory) as session:
                found = await collab.mark_notification_read(session, org_id, person, notification_id)
            if not found:
                return {"error": "not_found", "detail": f"no notification with id {notification_id}"}
            return {"id": notification_id, "read": True}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @scoped_tool(scopes.SCOPE_READ)
    async def account_usage() -> dict:
        """What your plan entitles you to, and what you have used this period.

        Free to call and does not consume a Knowledge Base query -- a meter
        that charges you for reading the meter is a support ticket waiting
        to happen.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.entitlements(session, org_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # Published on the server object so a test can assert every registered
    # tool declared a scope (hub/tests/test_api_key_scopes.py). Attached
    # rather than returned separately so no caller of build_mcp_server has
    # to change shape, and so the mapping is discoverable from the object
    # that actually owns the tools.
    mcp.commontrace_tool_scopes = dict(tool_scopes)
    return mcp


def build_app(config: HubConfig, session_factory: async_sessionmaker) -> Starlette:
    rate_limiter = make_rate_limiter(config)
    if config.rate_limit_backend == "memory":
        # In-process buckets reset on restart and N replicas allow ~N× the
        # configured rate. Warning only: memory is the correct default for
        # single-process eval/test (postgres would add a DB round-trip per
        # request). Multi-replica deployments want
        # HUB_RATE_LIMIT_BACKEND=postgres (hub/DEPLOYMENT.md §6).
        logger.warning(
            "rate limiting is in-process (HUB_RATE_LIMIT_BACKEND=memory): "
            "limits reset on restart and do not coordinate across replicas; "
            "set HUB_RATE_LIMIT_BACKEND=postgres for shared enforcement"
        )
    mcp = build_mcp_server(config, session_factory, rate_limiter)
    inner_app = mcp.streamable_http_app(
        streamable_http_path=config.streamable_http_path,
        host=config.host,
        max_request_body_size=config.max_request_body_bytes,
    )

    # Registered only when an operator token is configured -- see
    # HubConfig.admin_token. With none set there is no /admin route at all,
    # so a deployment that has not opted in has no console to probe.
    if config.admin_token:
        add_admin_routes(
            inner_app,
            session_factory,
            admin_token=config.admin_token,
            trusted_proxy_hops=config.trusted_proxy_hops,
            commons_enabled=config.commons_enabled,
            operator_org_id=config.operator_org_id,
            config=config,
        )

    # The customer-facing console, gated on its own secret. Distinct from the
    # operator console above in audience, auth and blast radius: that one is
    # cross-tenant and moderates; this one is scoped to a single org by a
    # signed session and cannot change any state at all.
    stripe_settings = StripeSettings(
        secret_key=config.stripe_secret_key,
        webhook_secret=config.stripe_webhook_secret,
        price_team=config.stripe_price_team,
        price_scale=config.stripe_price_scale,
    )
    if config.console_secret:
        add_console_routes(
            inner_app,
            session_factory,
            console_secret=config.console_secret,
            trusted_proxy_hops=config.trusted_proxy_hops,
            commons_enabled=config.commons_enabled,
            stripe=stripe_settings,
            signing_key=config.ledger_signing_key,
            cipher=config.cipher(),
            config=config,
            rate_limiter=rate_limiter,
        )

    # Public, unauthenticated org creation -- opt-in only (hub/signup.py's
    # own docstring covers why this defaults off and what it doesn't do,
    # namely email verification).
    if config.signup_enabled:
        add_signup_routes(
            inner_app, session_factory,
            trusted_proxy_hops=config.trusted_proxy_hops, console_path=CONSOLE_PATH,
        )

    # The JSON surface the CommonTrace Claude Code plugin speaks -- opt-in,
    # and sharing the MCP path's write-rate bucket rather than opening a
    # second one (hub/rest.py's add_rest_routes explains why that matters).
    if config.rest_api_enabled:
        add_rest_routes(
            inner_app,
            session_factory,
            config=config,
            rate_limiter=rate_limiter,
            trusted_proxy_hops=config.trusted_proxy_hops,
            signup_enabled=config.signup_enabled,
        )

    # Stripe calls this, not a signed-in browser -- registered independently
    # of the console above, and only once a webhook signing secret exists to
    # verify a delivery actually came from Stripe.
    if config.stripe_webhook_secret:
        add_billing_webhook_route(inner_app, session_factory, stripe=stripe_settings)

    # An IdP calls this, authenticated per-org via a dedicated `scim`-scoped
    # ApiKey (hub/scopes.py), not a shared deployment-wide secret -- so
    # unlike /admin, /app and /signup this is always mounted; see
    # hub/scim.py's own docstring for why that is safe.
    add_scim_routes(
        inner_app, session_factory,
        auth_rate_limiter=make_scim_auth_rate_limiter(config),
        trusted_proxy_hops=config.trusted_proxy_hops,
    )

    add_health_routes(
        inner_app,
        session_factory,
        readyz_rate_limiter=RateLimiter(
            per_minute=config.readyz_rate_limit_per_minute, burst=config.readyz_rate_limit_burst
        ),
        trusted_proxy_hops=config.trusted_proxy_hops,
    )
    add_disclosure_route(inner_app, config)
    inner_app.add_middleware(
        ApiKeyAuthMiddleware,
        session_factory=session_factory,
        protected_path=config.streamable_http_path,
        auth_rate_limiter=make_auth_rate_limiter(config),
        read_rate_limiter=make_read_rate_limiter(config),
        trusted_proxy_hops=config.trusted_proxy_hops,
        # None when HUB_OIDC_ISSUER/HUB_OIDC_AUDIENCE are unset -- see
        # HubConfig.identity_provider(). Built once, here, so a malformed
        # HUB_OIDC_JWKS fails this deployment at startup rather than on
        # whichever request happens to send the first JWT.
        identity_provider=config.identity_provider(),
    )
    # Startup, not per-request: one diagnostic query, and the answer cannot
    # change without an operator changing the role or the migrations. See
    # hub/db.py:check_row_level_security for why a silently-bypassed policy
    # is worth REFUSING to start over rather than merely logging about --
    # and why an undeterminable answer (unreachable database) still never
    # blocks startup.
    #
    # Chained onto the existing lifespan rather than registered with
    # `add_event_handler`, which Starlette removed (1.6 has no such
    # attribute) -- and which no test caught, because nothing exercised
    # build_app's startup. The MCP app installs its own lifespan for the
    # session manager, so this composes with it exactly as hub/main.py
    # does for engine disposal rather than replacing it.
    _previous_lifespan = inner_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan_with_rls_check(app):
        async with _previous_lifespan(app):
            await check_row_level_security(
                session_factory,
                allow_bypass=config.allow_rls_bypass,
                require=config.require_rls,
            )
            yield

    @contextlib.asynccontextmanager
    async def _lifespan_with_scheduler(app):
        async with _lifespan_with_rls_check(app):
            # Each loop is independently opt-in (hub/scheduler.py's own
            # docstring) -- a deployment relying on `hub.manage
            # check-alerts`/`webhook-deliver` via its own cron for either
            # one, or both, is unaffected either way.
            stop_event = asyncio.Event()
            tasks = []
            if config.alert_scheduler_enabled:
                tasks.append(asyncio.create_task(
                    scheduler.run(
                        session_factory,
                        interval_seconds=config.alert_scheduler_interval_seconds,
                        stop_event=stop_event,
                    )
                ))
            if config.webhook_scheduler_enabled:
                tasks.append(asyncio.create_task(
                    scheduler.run_webhook_delivery(
                        session_factory,
                        interval_seconds=config.webhook_scheduler_interval_seconds,
                        stop_event=stop_event,
                        signing_key=config.ledger_signing_key,
                        cipher=config.cipher(),
                        batch_size=config.webhook_scheduler_batch_size,
                    )
                ))
            if not tasks:
                yield
                return
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(*tasks)

    inner_app.router.lifespan_context = _lifespan_with_scheduler
    inner_app.add_middleware(
        LoadShedMiddleware,
        max_concurrent=config.max_concurrent_requests,
        timeout_seconds=config.request_timeout_seconds,
    )
    # Only mounted when configured -- see HubConfig.ip_allowlist's own
    # docstring. Added AFTER LoadShedMiddleware (so it runs BEFORE it,
    # Starlette applies middleware outer-to-inner in reverse registration
    # order): a source this deployment has decided should never reach it
    # at all shouldn't spend a slot in the concurrency/timeout budget
    # either, the same reasoning ApiKeyAuthMiddleware's own rate limiter
    # runs before any cryptographic verification work.
    if config.ip_allowlist:
        inner_app.add_middleware(
            IpAllowlistMiddleware,
            networks=tuple(ipaddress.ip_network(c, strict=False) for c in config.ip_allowlist),
            trusted_proxy_hops=config.trusted_proxy_hops,
        )
    # Added last => outermost: a request id exists (and the request gets
    # logged) even for calls the auth middleware rejects with a 401, and
    # for one LoadShedMiddleware sheds or times out.
    inner_app.add_middleware(RequestContextMiddleware)
    return inner_app
