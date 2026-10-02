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
        app = _app(_networks("10.0.0.0/8"), trusted_proxy_hops=1)
        async with _client(app, client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp", headers={"X-Forwarded-For": "10.1.2.3"})
        assert resp.status_code == 200

    async def test_a_client_forged_header_cannot_bypass_the_allowlist(self):
        app = _app(_networks("10.0.0.0/8"), trusted_proxy_hops=0)
        async with _client(app, client_ip="203.0.113.9") as client:
            resp = await client.get("/mcp", headers={"X-Forwarded-For": "10.1.2.3"})
        assert resp.status_code == 403
