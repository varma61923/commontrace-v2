"""Every route refuses an oversized request body, not only /mcp.

The MCP transport enforced HUB_MAX_REQUEST_BODY_BYTES for itself; the
signup and sign-in forms, the REST API, SCIM and the Stripe webhook read
their bodies through Starlette, which buffers without limit -- several of
them before any authentication.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from hub.server import BodySizeLimitMiddleware

pytestmark = pytest.mark.asyncio

LIMIT = 1024


def _app():
    seen = []

    async def echo(request: Request):
        body = await request.body()
        seen.append(len(body))
        return PlainTextResponse(str(len(body)))

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)
    return app, seen


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_a_body_within_the_limit_goes_through():
    app, seen = _app()
    async with _client(app) as client:
        response = await client.post("/echo", content=b"x" * LIMIT)
    assert response.status_code == 200 and response.text == str(LIMIT)
    assert seen == [LIMIT]


async def test_a_declared_oversize_is_refused_before_the_handler_reads_it():
    app, seen = _app()
    async with _client(app) as client:
        response = await client.post("/echo", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"
    assert seen == []


async def test_a_chunked_body_cannot_get_past_the_limit_without_a_content_length():
    app, seen = _app()

    async def stream():
        for _ in range(64):
            yield b"x" * 256

    async with _client(app) as client:
        response = await client.post("/echo", content=stream())
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert seen == []


async def test_an_understated_content_length_is_still_caught():
    app, seen = _app()

    async def stream():
        for _ in range(64):
            yield b"x" * 256

    async with _client(app) as client:
        response = await client.post("/echo", content=stream(), headers={"Content-Length": "10"})
    assert response.status_code == 413
    assert seen == []


async def test_an_unparseable_content_length_is_a_bad_request():
    app, _seen = _app()
    async with _client(app) as client:
        response = await client.post("/echo", content=b"x", headers={"Content-Length": "lots"})
    assert response.status_code == 400
