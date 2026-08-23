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

from hub.server import ApiKeyAuthMiddleware

pytestmark = pytest.mark.asyncio


def _explode():
    raise AssertionError("must not be called for a path outside the protected prefix")


async def _ok(request):
    return PlainTextResponse("ok")


def _build_app():
    app = Starlette(routes=[Route(p, _ok) for p in ("/mcp", "/mcp/foo", "/healthz", "/mcpadmin", "/mcpx")])
    app.add_middleware(ApiKeyAuthMiddleware, session_factory=_explode, protected_path="/mcp")
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
