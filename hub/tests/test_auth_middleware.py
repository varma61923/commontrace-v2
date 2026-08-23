"""Regression tests for ApiKeyAuthMiddleware's path matching.

Uses httpx's ASGI transport driven from within the test's own running event
loop (not Starlette's synchronous TestClient, which spins up its own loop --
see hub/tests/test_observability.py's docstring for why that matters when a
fixture's DB pool is bound to a specific loop). None of these tests touch a
real database: unprotected paths must never even attempt to resolve a
session_factory, and protected paths without a token are rejected before
any lookup happens.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hub.abuse import RateLimiter
from hub.server import ApiKeyAuthMiddleware

pytestmark = pytest.mark.asyncio


def _explode():
    raise AssertionError("must not be called for a path outside the protected prefix")


async def _ok(request):
    return PlainTextResponse("ok")


def _build_app():
    app = Starlette(routes=[Route(p, _ok) for p in ("/mcp", "/mcp/foo", "/healthz", "/mcpadmin", "/mcpx")])
    # High limits: these tests assert path-matching behavior, not rate
    # limiting -- a tight bucket would make them flaky depending on how many
    # requests a given test fires.
    app.add_middleware(
        ApiKeyAuthMiddleware,
        session_factory=_explode,
        protected_path="/mcp",
        auth_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
        read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
    )
    return app


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=_build_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestProtectedPathMatching:
    async def test_exact_protected_path_requires_auth(self, client):
        async with client as c:
            r = await c.get("/mcp")
        assert r.status_code == 401

    async def test_path_segment_under_protected_path_requires_auth(self, client):
        async with client as c:
            r = await c.get("/mcp/foo")
        assert r.status_code == 401

    async def test_unrelated_path_is_not_gated(self, client):
        async with client as c:
            r = await c.get("/healthz")
        assert r.status_code == 200

    async def test_path_that_merely_starts_with_the_same_prefix_is_not_treated_as_protected(self, client):
        """/mcpadmin and /mcpx are NOT under /mcp -- a plain startswith()
        check would wrongly gate them (harmless only because no such routes
        exist in the real app today, per the audit; this pins the correct,
        not-merely-coincidental, behavior)."""
        async with client as c:
            admin = await c.get("/mcpadmin")
            x = await c.get("/mcpx")
        assert admin.status_code == 200
        assert x.status_code == 200


def _build_app_with_tight_auth_limiter():
    app = Starlette(routes=[Route(p, _ok) for p in ("/mcp",)])
    app.add_middleware(
        ApiKeyAuthMiddleware,
        session_factory=_explode,
        protected_path="/mcp",
        auth_rate_limiter=RateLimiter(per_minute=0, burst=1),
        read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
    )
    return app


class TestAuthAttemptRateLimiting:
    """search_traces/get_trace/vote_trace/list_tags/commons_overlap had no
    rate limiting at all, and Argon2id verification (hub/auth.py) is
    deliberately expensive CPU work performed on every request carrying an
    Authorization header, valid or not -- so both a read-endpoint DoS and a
    CPU-amplification DoS were reachable with a single API key or even none
    at all. auth_rate_limiter gates requests before verify_api_key ever
    runs, so it must reject purely on request volume, with no session
    factory access (i.e. no DB lookup) needed to do it."""

    async def test_auth_attempt_limiter_rejects_before_touching_the_session_factory(self):
        transport = httpx.ASGITransport(app=_build_app_with_tight_auth_limiter())
        # No Authorization header on either request: session_factory
        # (`_explode`, which raises if actually called) is only ever
        # reached AFTER the header check, so a request that never gets that
        # far proves the auth_rate_limiter -- not a header/DB failure --
        # produced whichever response it got.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp")
            second = await c.get("/mcp")
        assert first.status_code == 401  # burst of 1: allowed through to the header check, which fails
        assert second.status_code == 429  # no refill (per_minute=0): the limiter itself now rejects
