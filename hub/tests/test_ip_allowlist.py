"""hub/server.py:IpAllowlistMiddleware -- the code-only half of audit
1.6's "no IP allowlisting / private networking" (see HubConfig.ip_allowlist's
own docstring for why the other half, actual private networking, stays out
of scope here).

What these tests defend:
1. Off by default -- a request from anywhere reaches the app when
   HUB_IP_ALLOWLIST is unset, unchanged from before this existed.
2. A source outside the configured CIDR set is refused with 403; one
   inside it is let through.
3. /healthz and /readyz are exempt regardless -- an orchestrator's own
   probes must never be blocked by this. /disclosure is exempt for the
   opposite reason: it exists specifically for reach from OUTSIDE this
   deployment's own network.
4. trusted_proxy_hops is honored: behind a trusted proxy, the real
   client address (not the proxy's own) is what gets checked.
"""
from __future__ import annotations

import ipaddress

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from hub.server import IpAllowlistMiddleware

pytestmark = pytest.mark.asyncio


def _app(networks, trusted_proxy_hops: int = 0) -> Starlette:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[])
    app.add_route("/healthz", ok, methods=["GET"])
    app.add_route("/readyz", ok, methods=["GET"])
    app.add_route("/disclosure", ok, methods=["GET"])
    app.add_route("/mcp", ok, methods=["GET"])
    app.add_middleware(IpAllowlistMiddleware, networks=networks, trusted_proxy_hops=trusted_proxy_hops)
    return app


def _networks(*cidrs: str):
    return tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)


def _client(app: Starlette, client_ip: str = "203.0.113.9") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(client_ip, 12345))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestNoAllowlistConfigured:
    async def test_every_source_is_let_through_when_the_list_is_empty(self):
        async with _client(_app(())) as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 200


class TestSourceRestriction:
    async def test_a_source_inside_the_allowlist_is_let_through(self):
        async with _client(_app(_networks("203.0.113.0/24")), client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 200

    async def test_a_source_outside_the_allowlist_is_refused(self):
        async with _client(_app(_networks("10.0.0.0/8")), client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    async def test_multiple_cidrs_are_all_checked(self):
        networks = _networks("10.0.0.0/8", "203.0.113.0/24")
        async with _client(_app(networks), client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 200

    async def test_an_exact_single_host_cidr_matches_only_that_host(self):
        async with _client(_app(_networks("203.0.113.9/32")), client_ip="203.0.113.10") as client:
            resp = await client.get("/mcp")
        assert resp.status_code == 403


class TestHealthAndReadinessAreAlwaysExempt:
    async def test_healthz_is_reachable_from_outside_the_allowlist(self):
        async with _client(_app(_networks("10.0.0.0/8")), client_ip="203.0.113.9") as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200

    async def test_readyz_is_reachable_from_outside_the_allowlist(self):
        async with _client(_app(_networks("10.0.0.0/8")), client_ip="203.0.113.9") as client:
            resp = await client.get("/readyz")
        assert resp.status_code == 200

    async def test_disclosure_is_reachable_from_outside_the_allowlist(self):
        async with _client(_app(_networks("10.0.0.0/8")), client_ip="203.0.113.9") as client:
            resp = await client.get("/disclosure")
        assert resp.status_code == 200


class TestTrustedProxyHops:
    async def test_the_real_client_behind_a_trusted_proxy_is_checked(self):
        """The proxy's own address (203.0.113.9, request.client.host) is
        NOT in the allowlist; the real origin it appended to
        X-Forwarded-For (10.1.2.3) IS -- proves trusted_proxy_hops is
        honored, not just request.client.host."""
        app = _app(_networks("10.0.0.0/8"), trusted_proxy_hops=1)
        async with _client(app, client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp", headers={"X-Forwarded-For": "10.1.2.3"})
        assert resp.status_code == 200

    async def test_a_client_forged_header_cannot_bypass_the_allowlist(self):
        """trusted_proxy_hops=0 (the default): X-Forwarded-For is entirely
        client-supplied and must be ignored, exactly as
        resolve_client_key's own docstring requires."""
        app = _app(_networks("10.0.0.0/8"), trusted_proxy_hops=0)
        async with _client(app, client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp", headers={"X-Forwarded-For": "10.1.2.3"})
        assert resp.status_code == 403
