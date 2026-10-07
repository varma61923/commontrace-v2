"""Authenticated HTTP transports for a single fleet's local MCP store.

The SDK owns JSON-RPC negotiation, cancellation, progress, session framing,
SSE and Streamable HTTP. This module adds the local store's rotating credential
boundary and bounded admission. A token grants access to one entire store;
multi-tenant deployments use the Hub's organization-scoped authorization.
"""
from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

Transport = Literal["sse", "streamable-http"]


class BearerBoundary:
    """Check the current credential on every HTTP request, including sessions.

    Rotation and revocation apply to subsequent requests. An already accepted
    streaming request may finish. No bearer token is accepted in URL parameters.
    Admission never allocates an unbounded queue of waiting requests.
    """

    def __init__(self, app: ASGIApp, token_provider: Callable[[], str | None], *, max_connections: int = 128):
        if isinstance(max_connections, bool) or not isinstance(max_connections, int) or max_connections < 1:
            raise ValueError("max_connections must be a positive integer")
        self.app = app
        self.token_provider = token_provider
        self.max_connections = max_connections
        self._active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        if self._active >= self.max_connections:
            await self._deny(send, 503, b'{"error":"server_busy"}')
            return
        self._active += 1
        try:
            values = [value for key, value in scope.get("headers", ()) if key.lower() == b"authorization"]
            supplied: bytes | None = None
            if len(values) == 1 and len(values[0]) <= 8192:
                parts = values[0].split()
                if len(parts) == 2 and parts[0].lower() == b"bearer":
                    supplied = parts[1]
            # FileTokenProvider rereads changed files. Keep disk I/O outside the
            # event loop, and fail closed if the credential source is unavailable.
            expected: str | None = None
            if supplied is not None:
                try:
                    expected = await asyncio.to_thread(self.token_provider)
                except Exception:
                    expected = None
            if not expected or supplied is None or not secrets.compare_digest(supplied, expected.encode("utf-8")):
                await self._deny(send, 401, b'{"error":"unauthorized"}')
                return
            await self.app(scope, receive, send)
        finally:
            self._active -= 1

    @staticmethod
    async def _deny(send: Send, status: int, body: bytes) -> None:
        headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                   (b"content-length", str(len(body)).encode("ascii"))]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="commontrace-local"'))
        else:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def build_http_app(
    root: str,
    *,
    transport: Transport = "streamable-http",
    token_provider: Callable[[], str | None] | None = None,
    host: str = "127.0.0.1",
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
    allow_approval: bool = True,
    max_request_body_size: int = 4 * 1024 * 1024,
    max_sessions: int = 128,
    session_idle_timeout: float = 300,
    max_connections: int = 128,
) -> ASGIApp:
    """Build an embeddable SDK transport with authentication and DNS protection.

    SSE uses ``/sse`` and ``/messages/``; Streamable HTTP uses ``/mcp`` and
    supports both POST responses and a server-to-client GET stream. A remote
    listener must declare allowed Host values explicitly. Origins are denied
    unless they match the allowlist; clients without Origin remain supported.
    Missing ``token_provider`` uses the same private, rotatable file credential
    as ``commontrace gateway``. Deploy behind TLS for non-loopback use.
    """
    import math

    from mcp.server.transport_security import TransportSecuritySettings

    from commontrace import gateway_tokens, mcp_server

    if transport not in ("sse", "streamable-http"):
        raise ValueError("transport must be sse or streamable-http")
    for name, value in (("max_request_body_size", max_request_body_size), ("max_sessions", max_sessions)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not math.isfinite(session_idle_timeout) or session_idle_timeout <= 0:
        raise ValueError("session_idle_timeout must be finite and positive")
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not allowed_hosts:
        raise ValueError("a remote listener requires explicit allowed_hosts")
    hosts = list(allowed_hosts) or ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = list(allowed_origins) or (
        ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"] if loopback else [])
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins,
    )
    server = mcp_server.build_server(root, allow_approval=allow_approval)
    if transport == "sse":
        app = server.sse_app(host=host, transport_security=security, max_request_body_size=max_request_body_size)
    else:
        app = server.streamable_http_app(
            host=host, transport_security=security, max_request_body_size=max_request_body_size,
            max_sessions=max_sessions, session_idle_timeout=session_idle_timeout,
        )
    if token_provider is None:
        gateway_tokens.load_or_create_token(root)
        token_provider = gateway_tokens.FileTokenProvider(root)
    return BearerBoundary(app, token_provider, max_connections=max_connections)
