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
    # per_minute=60/burst=1, not per_minute=0: a burst-of-1 bucket refilling
    # at 1/sec still lets exactly one request through immediately (proving
    # the limiter, not per_minute=0's own "always deny" floor -- see
    # TestZeroPerMinuteAlwaysDenies below -- is what let the first request
    # reach the header check).
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
        assert second.status_code == 429  # bucket drained, negligible refill within the test: rejected


class TestTrustedProxyHops:
    """[SEC-HUB-01]: with no proxy awareness, every request behind a
    reverse proxy shares the proxy's own request.client.host -- collapsing
    the auth-attempt limiter into one bucket across every real client
    behind it. trusted_proxy_hops=1 must let two distinct clients (as seen
    through X-Forwarded-For) get independent buckets, while trusted_proxy_hops=0
    (the default, and every other test in this file) must still ignore the
    header entirely -- unaudited trust in a client-settable header would be
    a worse bug than the one being fixed."""

    def _app(self, trusted_proxy_hops):
        app = Starlette(routes=[Route(p, _ok) for p in ("/mcp",)])
        app.add_middleware(
            ApiKeyAuthMiddleware,
            session_factory=_explode,
            protected_path="/mcp",
            # per_minute=60/burst=1, not per_minute=0 (which denies
            # unconditionally regardless of key -- see
            # TestZeroPerMinuteAlwaysDenies): burst=1 with a slow refill
            # still lets exactly one request through per distinct key, so
            # the SECOND request against the same resolved key is what
            # reveals whether two requests shared a bucket.
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
        assert first.status_code == 401  # burst of 1: reaches the header check, which fails
        assert second.status_code == 401, "a distinct forwarded client must not inherit an exhausted bucket"

    async def test_the_same_forwarded_client_shares_one_bucket(self):
        transport = httpx.ASGITransport(app=self._app(trusted_proxy_hops=1))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
            second = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
        assert first.status_code == 401
        assert second.status_code == 429  # same resolved key, bucket exhausted

    async def test_default_zero_hops_ignores_the_header_entirely(self):
        """The exact pre-fix behavior, and the default for every deployment
        that has not explicitly opted in: two requests differing only by a
        client-supplied X-Forwarded-For must still collide into the one
        request.client.host bucket ASGITransport gives every call here."""
        transport = httpx.ASGITransport(app=self._app(trusted_proxy_hops=0))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.1"})
            second = await c.get("/mcp", headers={"X-Forwarded-For": "203.0.113.2"})
        assert first.status_code == 401
        assert second.status_code == 429, "hops=0 must never be swayed by a client-supplied header"


class TestZeroPerMinuteAlwaysDenies:
    """per_minute<=0 must mean "deny every request", not "allow an initial
    burst of `burst` free requests per distinct key forever" -- a token
    bucket's capacity floor (so a configured burst of 0 doesn't deadlock
    every caller) previously applied even when the configured rate was 0,
    so a bucket for any NEW key started pre-filled with `burst` tokens and
    let that many calls through before ever hitting the "no refill" wall an
    operator setting per_minute=0 obviously intends."""

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
