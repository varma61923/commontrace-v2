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

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from commontrace import __version__ as _COMMONTRACE_VERSION
from hub import auth, commons, crud, plans
from hub.abuse import (
    RateLimited,
    RateLimiter,
    TraceRejected,
    make_auth_rate_limiter,
    make_rate_limiter,
    make_read_rate_limiter,
)
from hub.config import DEFAULT_SEARCH_LIMIT, HubConfig
from hub.db import session_scope
from hub.observability import RequestContextMiddleware, add_health_routes
from hub.schema_validation import SchemaValidationError

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
        Argon2 verification runs. Verification is deliberately expensive
        CPU work (hub/auth.py), performed for every candidate key sharing a
        presented key's prefix, on every request carrying an Authorization
        header regardless of whether it turns out valid -- without this, a
        remote attacker can flood the endpoint with credentials sharing a
        known/guessed prefix to exhaust the process's to_thread worker
        pool. This bounds CPU spent per source rather than only reacting
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
    ):
        super().__init__(app)
        self._session_factory = session_factory
        self._protected_path = protected_path
        self._auth_rate_limiter = auth_rate_limiter
        self._read_rate_limiter = read_rate_limiter

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

        client_key = request.client.host if request.client else "unknown"
        if not self._auth_rate_limiter.allow(client_key):
            return JSONResponse({"error": "rate_limited", "detail": "too many auth attempts"}, status_code=429)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing or malformed Authorization: Bearer <api-key> header"}, status_code=401
            )
        raw_key = header[len("bearer ") :].strip()

        async with self._session_factory() as session:
            authenticated = await auth.verify_api_key(session, raw_key)
            await session.commit()  # persists last_used_at touch

        if authenticated is None:
            # One message for invalid / revoked / expired alike -- see
            # hub/auth.py:verify_api_key for why they must be indistinguishable.
            return JSONResponse({"error": "invalid, revoked, or expired API key"}, status_code=401)

        if not self._read_rate_limiter.allow(authenticated.org_id):
            return JSONResponse({"error": "rate_limited", "detail": "too many requests"}, status_code=429)

        org_token = auth.current_org_id.set(authenticated.org_id)
        actor_token = auth.current_actor.set(authenticated.key_prefix)
        try:
            return await call_next(request)
        finally:
            auth.current_org_id.reset(org_token)
            auth.current_actor.reset(actor_token)


def _error_response(exc: Exception) -> dict:
    if isinstance(exc, PermissionError):
        return {"error": "unauthorized", "detail": str(exc)}
    if isinstance(exc, RateLimited):
        return {"error": "rate_limited", "detail": str(exc)}
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
    if isinstance(exc, (TraceRejected, SchemaValidationError, ValueError)):
        return {"error": "invalid_request", "detail": str(exc)}
    logger.exception("unexpected error in Hub tool")
    return {"error": "internal_error", "detail": "an unexpected error occurred"}


def build_mcp_server(config: HubConfig, session_factory: async_sessionmaker, rate_limiter: RateLimiter):
    from mcp.server.mcpserver import MCPServer

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
            "account_usage reports your plan and usage. All operations are scoped to "
            "your organization's own traces (hub/README.md 'Tenant isolation'). "
            + ("share_trace/unshare_trace/commons_overlap/commons_search add opt-in "
               "cross-org sharing on top of that: commons_search looks up ranked "
               "candidate answers to one failure, commons_overlap reports the "
               "conservative coverage fraction across many."
               if config.commons_enabled else
               "This deployment has HUB_COMMONS_ENABLED=false: no cross-org sharing "
               "tools exist on this server.")
        ),
    )

    @mcp.tool()
    async def search_traces(
        query: str = "",
        tags: list[str] | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> dict:
        """Search this org's traces by full-text query and/or tags.

        Returns {"traces": [...], "limit", "offset", "has_more"}. Page by
        re-calling with offset += limit while has_more is true.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.search_traces(
                    session, org_id, query=query, tags=tags, limit=limit, offset=offset
                )
        except Exception as exc:  # noqa: BLE001 - converted to a structured tool error below
            return _error_response(exc)

    @mcp.tool()
    async def contribute_trace(
        title: str,
        context_text: str,
        solution_text: str,
        tags: list[str] | None = None,
        agent_type: str = "",
        agent_id: str = "",
        idempotency_key: str | None = None,
    ) -> dict:
        """Contribute a new trace. Returns its id and quarantine status.

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
                    actor=auth.get_current_actor(),
                    idempotency_key=idempotency_key,
                )
            return result
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    async def amend_trace(
        id: str,
        title: str | None = None,
        context_text: str | None = None,
        solution_text: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create a new trace that supersedes `id`, carrying forward any
        field not explicitly overridden."""
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
                    actor=auth.get_current_actor(),
                )
            if amended is None:
                return {"error": "not_found", "detail": f"no trace with id {id}"}
            return amended
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    @mcp.tool()
    async def list_tags() -> dict:
        """List every distinct tag used across this org's non-quarantined traces."""
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                tags = await crud.list_tags(session, org_id)
            return {"tags": tags}
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # --- Cross-org commons (opt-in), gated by HUB_COMMONS_ENABLED --------
    #
    # `if config.commons_enabled:` around the @mcp.tool() registrations
    # themselves, not a check inside each handler -- a disabled deployment
    # must not even LIST these tools. A client that tries gets the MCP
    # framework's own "unknown tool" error, which holds even if an org
    # forgets the commons exists; a per-call refusal only holds if every
    # caller remembers to check first. account_usage is intentionally
    # outside this block: it reports an org's own plan and its own usage,
    # never another org's data, so disabling the commons does not disable it.
    if config.commons_enabled:

        @mcp.tool()
        async def share_trace(id: str, rationale: str = "") -> dict:
            """Contribute one of your own traces to the cross-org commons.

            Opt-in and revocable. Only share SUBSTRATE failures -- things like
            "this API needs an idempotency key" or "this library changed its
            default" -- that every fleet rediscovers at full cost and nobody
            considers proprietary. Do NOT share business logic: pricing rules,
            escalation policy, qualification criteria. The Hub cannot make that
            judgment for you, so `rationale` records why you decided this trace
            is substrate.

            Once shared, this trace's full content becomes visible to other orgs
            whose recurring failures it matches. Withdraw it with unshare_trace.
            """
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    result = await crud.share_trace(
                        session, org_id, id, rationale=rationale, actor=auth.get_current_actor()
                    )
                if result is None:
                    return {"error": "not_found", "detail": f"no trace with id {id}"}
                return result
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

        @mcp.tool()
        async def unshare_trace(id: str) -> dict:
            """Withdraw one of your traces from the cross-org commons. It stops
            matching other orgs' queries immediately."""
            try:
                org_id = auth.get_current_org_id()
                async with session_scope(session_factory) as session:
                    result = await crud.unshare_trace(session, org_id, id, actor=auth.get_current_actor())
                if result is None:
                    return {"error": "not_found", "detail": f"no trace with id {id}"}
                return result
            except Exception as exc:  # noqa: BLE001
                return _error_response(exc)

        @mcp.tool()
        async def commons_overlap(
            failures: list[dict] | None = None,
            threshold: float = commons.DEFAULT_COMMONS_THRESHOLD,
            include_matches: bool = True,
            agent_type: str = "",
        ) -> dict:
            """Of the recurring failures your fleet keeps hitting, what fraction
            has some *other* fleet already solved?

            Send MinHash signatures of your own failures -- generated locally by
            `commontrace commons sign`, so no failure text ever leaves your
            machine. Each entry is {"label": str, "signature": [int, ...]}.

            You do not have to contribute anything to ask this. What comes back
            is drawn only from traces whose owners explicitly shared them, and
            your own traces are excluded from the corpus so the number reflects
            what you'd actually *gain* rather than counting your own work.
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

        @mcp.tool()
        async def commons_search(
            query_signature: list[int] | None = None,
            limit: int = commons.DEFAULT_SEARCH_CANDIDATES,
            agent_type: str = "",
        ) -> dict:
            """Ask the commons what it already knows about ONE failure, and get
            back ranked candidate answers with their solutions.

            This is the knowledge-base lookup: "has anyone solved this?".
            `commons_overlap` answers the different, quotable question "what
            FRACTION of my failures are solved" and buys 0% false positives
            with a threshold that discards about nine of every ten real
            answers. This tool ranks instead, and finds the right record
            89.1% of the time at rank 1 and 100% within the top 10 on the
            held-out evaluation (commons/eval/RESULTS.md).

            Send one MinHash signature, generated locally by `commontrace
            commons sign` -- no failure text leaves your machine, exactly as
            with commons_overlap. Your own traces are excluded from the
            corpus, and what comes back is drawn only from traces whose
            owners explicitly shared them.

            Results are CANDIDATES TO JUDGE, never coverage: a failure the
            commons does not contain still returns a non-empty list every
            time. Do not derive a percentage from this tool -- that is what
            commons_overlap is for.
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

    @mcp.tool()
    async def account_usage() -> dict:
        """What your plan entitles you to, and what you have used this period.

        Free to call and does not consume a commons query -- a meter that
        charges you for reading the meter is a support ticket waiting to
        happen. `commons_queries.earned` is allowance you did not pay for:
        every time a trace you shared covers another fleet's failure, your
        allowance grows. That is the whole reason contributing is worth
        doing rather than a favour you do for strangers.
        """
        try:
            org_id = auth.get_current_org_id()
            async with session_scope(session_factory) as session:
                return await crud.entitlements(session, org_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    return mcp


def build_app(config: HubConfig, session_factory: async_sessionmaker) -> Starlette:
    rate_limiter = make_rate_limiter(config)
    mcp = build_mcp_server(config, session_factory, rate_limiter)
    inner_app = mcp.streamable_http_app(
        streamable_http_path=config.streamable_http_path,
        host=config.host,
        max_request_body_size=config.max_request_body_bytes,
    )

    add_health_routes(
        inner_app,
        session_factory,
        readyz_rate_limiter=RateLimiter(
            per_minute=config.readyz_rate_limit_per_minute, burst=config.readyz_rate_limit_burst
        ),
    )
    inner_app.add_middleware(
        ApiKeyAuthMiddleware,
        session_factory=session_factory,
        protected_path=config.streamable_http_path,
        auth_rate_limiter=make_auth_rate_limiter(config),
        read_rate_limiter=make_read_rate_limiter(config),
    )
    # Added last => outermost: a request id exists (and the request gets
    # logged) even for calls the auth middleware rejects with a 401.
    inner_app.add_middleware(RequestContextMiddleware)
    return inner_app
