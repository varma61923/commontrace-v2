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

from hub import auth, crud
from hub.abuse import RateLimited, RateLimiter, TraceRejected, make_rate_limiter
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
    tenant credentials)."""

    def __init__(self, app: ASGIApp, session_factory: async_sessionmaker, protected_path: str):
        super().__init__(app)
        self._session_factory = session_factory
        self._protected_path = protected_path

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self._protected_path):
            return await call_next(request)

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
    if isinstance(exc, (TraceRejected, SchemaValidationError, ValueError)):
        return {"error": "invalid_request", "detail": str(exc)}
    logger.exception("unexpected error in Hub tool")
    return {"error": "internal_error", "detail": "an unexpected error occurred"}


def build_mcp_server(config: HubConfig, session_factory: async_sessionmaker, rate_limiter: RateLimiter):
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(
        name="commontrace",
        version="0.1.0",
        instructions=(
            "CommonTrace Hub: a cross-org, cross-fleet shared trace store. "
            "search_traces/get_trace/list_tags read; contribute_trace writes a new "
            "trace; vote_trace/amend_trace act on an existing one. All operations "
            "are scoped to your organization's own traces (see hub/README.md "
            "'Tenant isolation' for why cross-org sharing is not yet automatic)."
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
    ) -> dict:
        """Contribute a new trace. Returns its id and quarantine status."""
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
                    actor=auth.get_current_actor(),
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
        """Cast (or update) this org's vote ('up'/'down') on a trace."""
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

    return mcp


def build_app(config: HubConfig, session_factory: async_sessionmaker) -> Starlette:
    rate_limiter = make_rate_limiter(config)
    mcp = build_mcp_server(config, session_factory, rate_limiter)
    inner_app = mcp.streamable_http_app(streamable_http_path=config.streamable_http_path, host=config.host)

    add_health_routes(inner_app, session_factory)
    inner_app.add_middleware(
        ApiKeyAuthMiddleware, session_factory=session_factory, protected_path=config.streamable_http_path
    )
    # Added last => outermost: a request id exists (and the request gets
    # logged) even for calls the auth middleware rejects with a 401.
    inner_app.add_middleware(RequestContextMiddleware)
    return inner_app
