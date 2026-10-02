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
        auth_rate_limiter=RateLimiter(per_minute=60, burst=1),
        read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
    )
    return app


class TestAuthAttemptRateLimiting:
    async def test_auth_attempt_limiter_rejects_before_touching_the_session_factory(self):
        transport = httpx.ASGITransport(app=_build_app_with_tight_auth_limiter())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp")
            second = await c.get("/mcp")
        assert first.status_code == 401
        assert second.status_code == 429


class TestTrustedProxyHops:
    def _app(self, trusted_proxy_hops):
        app = Starlette(routes=[Route(p, _ok) for p in ("/mcp",)])
        app.add_middleware(
            ApiKeyAuthMiddleware,
            session_factory=_explode,
            protected_path="/mcp",
            auth_rate_limiter=RateLimiter(per_minute=60, burst=1),
            read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
            trusted_proxy_hops=trusted_proxy_hops,
        )
        return app

    async def test_two_distinct_forwarded_clients_get_independent_buckets(self):
        transport = httpx.ASGITransport(app=self._app(trusted_proxy_hops=1))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
            second = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.2"})
        assert first.status_code == 401
        assert second.status_code == 401, "a distinct forwarded client must not inherit an exhausted bucket"

    async def test_the_same_forwarded_client_shares_one_bucket(self):
        transport = httpx.ASGITransport(app=self._app(trusted_proxy_hops=1))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
            second = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
        assert first.status_code == 401
        assert second.status_code == 429

    async def test_default_zero_hops_ignores_the_header_entirely(self):
        transport = httpx.ASGITransport(app=self._app(trusted_proxy_hops=0))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
            second = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.2"})
        assert first.status_code == 401
        assert second.status_code == 429, "hops=0 must never be swayed by a client-supplied header"


class TestZeroPerMinuteAlwaysDenies:
    async def test_the_very_first_request_is_rejected_not_just_the_second(self):
        app = Starlette(routes=[Route(p, _ok) for p in ("/mcp",)])
        app.add_middleware(
            ApiKeyAuthMiddleware,
            session_factory=_explode,
            protected_path="/mcp",
            auth_rate_limiter=RateLimiter(per_minute=0, burst=5),
            read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp")
        assert first.status_code == 429
