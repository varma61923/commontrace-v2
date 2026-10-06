"""Regressions for regional authentication and recipient-controlled egress."""
from __future__ import annotations

import asyncio
import json
import socket
import time

import httpcore
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text
from starlette.applications import Starlette

from hub import auth, crud, events, otlp, rest
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, User
from hub.sso import IdentityProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned,accepted", [("eu", False), ("us", True), ("", True)])
async def test_oidc_respects_the_same_region_boundary_as_api_keys(
    session_factory, monkeypatch, pinned, accepted,
):
    monkeypatch.setattr(auth, "_DEPLOYMENT_REGION", "us")
    issuer = "https://idp.example.test/"
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update(kid="region-key", alg="RS256")
    provider = IdentityProvider(issuer=issuer, audience="hub", jwks={"keys": [jwk]})
    now = int(time.time())
    token = jwt.encode(
        {"iss": issuer, "aud": "hub", "sub": "region-user", "iat": now, "exp": now + 300},
        signing_key, algorithm="RS256", headers={"kid": "region-key"},
    )
    async with session_scope(session_factory) as session:
        org = Organization(name="regional-auth", data_region=pinned)
        session.add(org)
        await session.flush()
        user = User(
            org_id=org.id, email="user@example.test", role="viewer",
            issuer=issuer, external_subject="region-user",
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        issued = await auth.issue_api_key(session, org.id)

    async with session_scope(session_factory) as session:
        person = await auth.verify_user_token(session, token, provider)
        api_key = await auth.verify_api_key(session, issued.raw_key)
    assert (person is not None) == accepted
    assert (api_key is not None) == accepted
    async with session_scope(session_factory) as session:
        user = await session.get(User, user_id)
        assert (user.last_login_at is not None) == accepted


async def _public_resolve(host):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]


class _UnreadBody(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        # A malicious receiver can stream an unlimited or compressed body.
        # Delivery consumes no response content, even when status is an error.
        raise AssertionError("webhook response body must never be read")
        yield b""  # pragma: no cover - defines an async generator

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [204, 302, 500])
async def test_webhook_ignores_response_bodies_and_closes_streams(monkeypatch, status):
    stream = _UnreadBody()
    requests = []

    async def response(transport, request):
        requests.append(request)
        return httpx.Response(
            status, stream=stream,
            headers={"location": "http://127.0.0.1/private", "content-encoding": "gzip"},
        )

    monkeypatch.setattr(events, "_default_resolve", _public_resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", response)
    send = events.http_transport()
    if status >= 300:
        with pytest.raises(httpx.HTTPStatusError):
            await send("https://example.test/hook", "{}", {"X-CommonTrace-Signature": "signed"})
    else:
        await send("https://example.test/hook", "{}", {"X-CommonTrace-Signature": "signed"})
    assert stream.closed
    assert len(requests) == 1
    assert str(requests[0].url) == "https://example.test/hook"


@pytest.mark.asyncio
async def test_webhook_deadline_includes_dns_resolution(monkeypatch):
    cancelled = asyncio.Event()

    async def stuck_dns(host):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(events, "_default_resolve", stuck_dns)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            events.http_transport(timeout=0.02)("https://example.test/hook", "{}", {}),
            timeout=1.0,
        )
    assert cancelled.is_set()


async def _db_org(session):
    return await session.scalar(text("SELECT current_setting('app.org_id', true)"))


@pytest.mark.asyncio
async def test_explicit_session_org_scopes_nested_sessions_and_restores_context(session_factory):
    outer = "00000000-0000-0000-0000-000000000001"
    inner = "00000000-0000-0000-0000-000000000002"
    token = auth.current_org_id.set(None)
    try:
        async with session_scope(session_factory, org_id=outer) as session:
            assert auth.current_org_id.get() == outer
            assert await _db_org(session) == outer
            async with session_scope(session_factory) as nested:
                assert await _db_org(nested) == outer
            async with session_scope(session_factory, org_id=inner) as nested:
                assert auth.current_org_id.get() == inner
                assert await _db_org(nested) == inner
            assert auth.current_org_id.get() == outer
            assert await _db_org(session) == outer
        assert auth.current_org_id.get() is None
        async with session_scope(session_factory) as session:
            assert not await _db_org(session)
    finally:
        auth.current_org_id.reset(token)


@pytest.mark.asyncio
async def test_explicit_session_org_is_restored_after_rollback_and_cancellation(session_factory):
    org_id = "00000000-0000-0000-0000-000000000001"
    original = auth.current_org_id.get()
    with pytest.raises(ValueError, match="rollback"):
        async with session_scope(session_factory, org_id=org_id) as session:
            assert await _db_org(session) == org_id
            session.add(Organization(name="must-rollback"))
            await session.flush()
            raise ValueError("rollback")
    assert auth.current_org_id.get() == original
    async with session_scope(session_factory) as session:
        assert not await session.scalar(text("SELECT count(*) FROM organizations WHERE name = 'must-rollback'"))

    entered = asyncio.Event()
    restored = []

    async def cancelled_session():
        try:
            async with session_scope(session_factory, org_id=org_id) as session:
                assert await _db_org(session) == org_id
                entered.set()
                await asyncio.Event().wait()
        finally:
            restored.append(auth.current_org_id.get())

    task = asyncio.create_task(cancelled_session())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored == [original]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["rest-write", "rest-read", "otlp-write"])
async def test_authenticated_ingestion_sessions_set_the_tenant_guc(
    session_factory, config, monkeypatch, surface,
):
    keys = []
    async with session_scope(session_factory) as session:
        for name in ("one", "two"):
            org = Organization(name=name)
            session.add(org)
            await session.flush()
            issued = await auth.issue_api_key(session, org.id)
            keys.append((org.id, issued.raw_key))

    name = "search_traces" if surface == "rest-read" else "contribute_trace"
    operation = getattr(crud, name)
    seen = []

    async def scoped_operation(session, org_id, *args, **kwargs):
        assert auth.current_org_id.get() == org_id
        assert await _db_org(session) == org_id
        seen.append(org_id)
        return await operation(session, org_id, *args, **kwargs)

    monkeypatch.setattr(crud, name, scoped_operation)
    app = Starlette()
    if surface == "otlp-write":
        otlp.add_otlp_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
        path = "/v1/traces"
        payload = {"resourceSpans": [{"scopeSpans": [{"spans": [{
            "spanId": "a" * 16, "name": "work",
            "attributes": [
                {"key": "gen_ai.prompt", "value": {"stringValue": "context"}},
                {"key": "gen_ai.completion", "value": {"stringValue": "solution"}},
            ],
        }]}]}]}
    else:
        rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
        if surface == "rest-read":
            path, payload = "/api/v1/traces/search", {"q": "context"}
        else:
            path = "/api/v1/traces"
            payload = {"title": "work", "context_text": "context", "solution_text": "solution"}
    original = auth.current_org_id.get()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post(path, json=payload, headers={"X-API-Key": raw}) for _, raw in keys
        ])
    assert all(response.status_code in (200, 201) for response in responses)
    assert sorted(seen) == sorted(org for org, _ in keys)
    assert auth.current_org_id.get() == original


@pytest.mark.asyncio
async def test_webhook_address_fallbacks_share_one_deadline(monkeypatch):
    attempted = []
    cancelled = asyncio.Event()

    async def many_addresses(host):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"8.8.8.{n}", 0)) for n in range(1, 21)]

    async def slow_connect(backend, host, port, **kwargs):
        attempted.append(host)
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise OSError("connection refused")

    monkeypatch.setattr(events, "_default_resolve", many_addresses)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", slow_connect)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            events.http_transport(timeout=0.15)("https://example.test/hook", "{}", {}),
            timeout=1.0,
        )
    assert 1 <= len(attempted) < 20
    assert cancelled.is_set()
