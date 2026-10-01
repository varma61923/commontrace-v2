"""Route-driven cross-tenant fuzz of the customer console.

Hand-written isolation tests cover the routes someone remembered. This one
ENUMERATES every console route that takes a resource id in its path, drives
each as org A against org B's real ids and against hostile id strings, and
fails when:

* a route with a path parameter has no resource mapping here (so a new route
  cannot ship without being fuzzed);
* any request returns a 5xx;
* anything of org B's changes, or org B's identifying text appears in a
  response to org A.

SCIM's tenant boundary is the bearer token, not a session, and is covered in
test_scim.py; the admin console is cross-tenant by design.
"""
from __future__ import annotations

import urllib.parse
import uuid

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from hub import alerts as alerts_module
from hub import auth, console, events, rbac
from hub.db import session_scope
from hub.models import AlertRule, ApiKey, Organization, User, WebhookEndpoint

pytestmark = pytest.mark.asyncio

SECRET = "fuzz-console-secret"
B_EMAIL = "victim-b@example.org"
B_WEBHOOK = "https://hooks.org-b.example.org/secret-path"

HOSTILE_IDS = [
    str(uuid.uuid4()),
    "../../etc/passwd",
    "' OR '1'='1",
    "1; DROP TABLE users;--",
    "%00",
    "‮​",
    "-1",
    "{{7*7}}",
    "A" * 1500,
    "",
]


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def resolve(hostname):
        import socket
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]
    monkeypatch.setattr(events, "_default_resolve", resolve)


def _app(session_factory, config) -> Starlette:
    app = Starlette()
    console.add_console_routes(
        app, session_factory, console_secret=SECRET, signing_key="fuzz-signing", config=config,
    )
    return app


@pytest_asyncio.fixture
async def tenants(session_factory):
    async with session_scope(session_factory) as session:
        a, b = Organization(name="Org A"), Organization(name="Org B")
        session.add_all([a, b])
        await session.flush()
        a_key = await auth.issue_api_key(session, a.id)
        b_key = await auth.issue_api_key(session, b.id, scopes=["read"])
        user = User(org_id=b.id, email=B_EMAIL, role=rbac.ROLE_VIEWER)
        session.add(user)
        rule = await alerts_module.create_rule(
            session, b.id, alerts_module.METRIC_QUARANTINE_RATE, alerts_module.COMPARATOR_GT, 0.5,
        )
        endpoint, _secret = await events.add_endpoint(session, b.id, B_WEBHOOK, signing_key="fuzz-signing")
        await session.flush()
        return {
            "a_raw_key": a_key.raw_key, "b_org": b.id,
            "ids": {
                "user_id": user.id, "key_id": b_key.key_id,
                "rule_id": rule.id, "endpoint_id": endpoint.id,
            },
        }


async def _snapshot(session_factory, tenants) -> dict:
    org, ids = tenants["b_org"], tenants["ids"]
    async with session_scope(session_factory) as session:
        user = await session.get(User, ids["user_id"])
        key = await session.get(ApiKey, ids["key_id"])
        rule = await session.get(AlertRule, ids["rule_id"])
        endpoint = await session.get(WebhookEndpoint, ids["endpoint_id"])
        return {
            "user": (user.role, user.disabled_at),
            "key": (key.revoked_at, key.key_hash if hasattr(key, "key_hash") else None),
            "rule": rule is not None and (rule.threshold, rule.org_id),
            "endpoint": (endpoint.enabled, endpoint.key_version, endpoint.url),
            "org": org,
        }


def _param_routes(app: Starlette) -> list[tuple[str, str, list[str]]]:
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        params = [p for p in path.replace("}", "{").split("{")[1::2]]
        if not params:
            continue
        for method in sorted(m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS")):
            out.append((method, path, params))
    return out


async def test_every_console_route_with_an_id_is_mapped(session_factory, config):
    mapped = {"user_id", "key_id", "rule_id", "endpoint_id", "token"}
    unmapped = {
        (m, p) for m, p, params in _param_routes(_app(session_factory, config))
        if set(params) - mapped
    }
    assert not unmapped, f"console routes with an unmapped path parameter: {sorted(unmapped)}"
    assert _param_routes(_app(session_factory, config)), "found no parameterised routes to fuzz"


async def test_org_a_cannot_touch_org_b_through_any_id_route(session_factory, config, tenants):
    app = _app(session_factory, config)
    before = await _snapshot(session_factory, tenants)
    attempted = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        signed = await client.post(f"{console.CONSOLE_PATH}/signin", data={"api_key": tenants["a_raw_key"]})
        assert signed.status_code in (200, 303)
        for method, template, params in _param_routes(app):
            candidates = [tenants["ids"].get(params[0], "")] + HOSTILE_IDS
            for value in candidates:
                path = template.replace("{" + params[0] + "}", urllib.parse.quote(value, safe=""))
                response = await client.request(
                    method, path, data={"role": rbac.ROLE_OWNER}, follow_redirects=True,
                )
                attempted += 1
                assert response.status_code < 500, (method, template, value[:40], response.status_code)
                body = response.text
                for marker in (B_EMAIL, B_WEBHOOK, tenants["b_org"]):
                    assert marker not in body, (method, template, "leaked", marker)
    assert attempted >= 10 * len(_param_routes(app)) - 10
    assert await _snapshot(session_factory, tenants) == before


async def test_no_admin_route_returns_5xx_for_a_malformed_id(session_factory, config):
    """The operator console is cross-tenant by design, so the property here is
    narrower: a value that cannot be an id is a 404, never a driver error."""
    import base64

    from hub import admin
    from hub.abuse import RateLimiter

    app = Starlette()
    admin.add_admin_routes(
        app, session_factory, admin_token="s3cret",
        rate_limiter=RateLimiter(per_minute=100_000, burst=100_000), config=config,
    )
    headers = {"Authorization": "Basic " + base64.b64encode(b"op:s3cret").decode()}
    routes = _param_routes(app)
    assert routes
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, template, params in routes:
            for value in HOSTILE_IDS[1:]:
                path = template.replace("{" + params[0] + "}", urllib.parse.quote(value, safe=""))
                response = await client.request(
                    method, path, headers=headers, data={"role": "owner"}, follow_redirects=True,
                )
                assert response.status_code < 500, (method, template, value[:40], response.status_code)


async def test_rotating_a_revoked_key_from_the_console_changes_nothing(session_factory, config, tenants):
    """The rotate route answers an already-revoked key like an unknown one
    (no new key is shown, none is minted) rather than a 500."""
    from sqlalchemy import func, select

    async with session_scope(session_factory) as session:
        a_org = (await session.execute(select(ApiKey.org_id).limit(1))).scalar_one()
        issued = await auth.issue_api_key(session, a_org)
        await auth.revoke_api_key(session, issued.key_id)
    app = _app(session_factory, config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(f"{console.CONSOLE_PATH}/signin", data={"api_key": tenants["a_raw_key"]})
        async with session_scope(session_factory) as session:
            before = (await session.execute(select(func.count()).select_from(ApiKey))).scalar_one()
        response = await client.post(f"{console.CONSOLE_PATH}/keys/{issued.key_id}/rotate")
        assert response.status_code < 500 and "shown once" not in response.text
    async with session_scope(session_factory) as session:
        after = (await session.execute(select(func.count()).select_from(ApiKey))).scalar_one()
    assert after == before


async def test_no_scim_route_returns_5xx_for_a_malformed_id(session_factory, config, tenants):
    """SCIM's tenant boundary is its own bearer scope (test_scim.py); here, only
    that a value that cannot be an id answers 4xx, on every verb of every id route."""
    from hub import scim
    from hub.abuse import RateLimiter

    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, tenants["b_org"], scopes=["scim"])
    app = Starlette()
    scim.add_scim_routes(
        app, session_factory, auth_rate_limiter=RateLimiter(per_minute=1_000_000, burst=1_000_000),
    )
    headers = {"Authorization": f"Bearer {issued.raw_key}", "Content-Type": "application/scim+json"}
    routes = _param_routes(app)
    assert {t for _m, t, _p in routes} == {"/scim/v2/Users/{user_id}", "/scim/v2/Groups/{group_id}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for method, template, params in routes:
            for value in HOSTILE_IDS[1:]:
                path = template.replace("{" + params[0] + "}", urllib.parse.quote(value, safe=""))
                response = await client.request(method, path, headers=headers, content=b"{}")
                assert response.status_code < 500, (method, template, value[:40], response.status_code)
