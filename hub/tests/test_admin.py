"""Tests for hub/admin.py -- the read-only operator console.

Two properties carry almost all the risk here, and both are asserted below
against a real request rather than by reading the code:

1. **Escaping.** The console renders content from EVERY tenant into one
   operator's browser. A trace title is customer-supplied, so interpolating
   it unescaped is stored XSS that crosses a tenant boundary into the single
   session with cross-tenant visibility.

2. **Absent unless configured.** With no operator token set, the routes must
   not exist at all -- a 404 from the router, not a 401 from a handler. A
   deployment that has not opted in should have no console to probe.

The auth and escaping tests need no database. The rendering tests do, and
use the same fixtures as the rest of hub/tests/.
"""
from __future__ import annotations

import base64

import httpx
import pytest
from starlette.applications import Starlette

from hub import admin
from hub.abuse import RateLimiter
from hub.config import HubConfig

pytestmark = pytest.mark.asyncio


def _basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _explode(*a, **kw):
    raise AssertionError("no database access should happen for an unauthenticated request")


def _app(token: str = "s3cret", session_factory=_explode) -> Starlette:
    app = Starlette()
    admin.add_admin_routes(
        app, session_factory, admin_token=token,
        # High limits: these tests assert auth and rendering, not throttling.
        rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
    )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- Absent unless configured ----------------------------------------------


class TestConsoleIsAbsentUnlessConfigured:
    async def test_no_routes_are_registered_without_a_token(self):
        """404 from the router, not 401 from a handler: a deployment that has
        not opted in has no console to find, which is a stronger property
        than one that merely refuses."""
        app = Starlette()  # add_admin_routes deliberately not called
        async with _client(app) as c:
            for path in ("/admin", "/admin/kb", "/admin/org/whatever"):
                assert (await c.get(path)).status_code == 404, path

    async def test_registering_with_an_empty_token_is_refused(self):
        """Guards against a deployment that sets HUB_ADMIN_TOKEN="" and gets
        a console anyone can read."""
        with pytest.raises(ValueError, match="non-empty admin token"):
            admin.add_admin_routes(Starlette(), _explode, admin_token="")

    async def test_the_config_default_leaves_it_off(self, monkeypatch):
        monkeypatch.delenv("HUB_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        assert HubConfig.from_env().admin_token == ""


# --- Authentication ---------------------------------------------------------


class TestConsoleAuthentication:
    async def test_no_credentials_is_challenged(self):
        async with _client(_app()) as c:
            r = await c.get("/admin")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic realm=")

    async def test_a_wrong_password_is_refused(self):
        async with _client(_app()) as c:
            r = await c.get("/admin", headers=_basic("op", "wrong"))
        assert r.status_code == 401

    async def test_a_bearer_token_is_not_accepted_as_basic(self):
        async with _client(_app()) as c:
            r = await c.get("/admin", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 401

    async def test_malformed_base64_is_refused_not_crashed(self):
        async with _client(_app()) as c:
            r = await c.get("/admin", headers={"Authorization": "Basic !!!not-base64!!!"})
        assert r.status_code == 401

    async def test_non_utf8_credentials_are_refused_not_crashed(self):
        raw = base64.b64encode(b"\xff\xfe:\xff").decode()
        async with _client(_app()) as c:
            r = await c.get("/admin", headers={"Authorization": f"Basic {raw}"})
        assert r.status_code == 401

    async def test_the_username_is_ignored(self):
        """The token is a shared operator secret, not a per-user login."""
        assert admin._authorized(_FakeRequest(_basic("anyone-at-all", "s3cret")), "s3cret")

    async def test_an_unauthenticated_request_never_touches_the_database(self):
        """_explode as the session factory: the assertion is that it is never
        called. Auth runs before any query, so a flood of unauthenticated
        probes cannot be turned into database load."""
        async with _client(_app(session_factory=_explode)) as c:
            assert (await c.get("/admin")).status_code == 401

    async def test_rate_limited_before_the_credential_is_even_checked(self):
        app = Starlette()
        admin.add_admin_routes(app, _explode, admin_token="s3cret",
                               rate_limiter=RateLimiter(per_minute=60, burst=2))
        async with _client(app) as c:
            first = [await c.get("/admin") for _ in range(2)]
            limited = await c.get("/admin")
        assert all(r.status_code == 401 for r in first)
        assert limited.status_code == 429
        assert limited.headers["retry-after"]


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = {k.lower(): v for k, v in headers.items()}


# --- Escaping ---------------------------------------------------------------


class TestEscaping:
    """The security-critical property of this module. See its docstring: the
    reader is the one browser session with visibility over every tenant."""

    @pytest.mark.parametrize("payload,must_not_appear", [
        ("<script>alert(1)</script>", "<script>alert(1)</script>"),
        ('"><img src=x onerror=alert(1)>', "<img src=x"),
        ("</td></tr><script>x</script>", "<script>x</script>"),
        ("javascript:alert(1)", None),
    ])
    async def test_hostile_text_is_neutralised(self, payload, must_not_appear):
        rendered = admin.h(payload)
        assert "<" not in rendered and ">" not in rendered
        if must_not_appear:
            assert must_not_appear not in rendered

    async def test_quotes_are_escaped_for_attribute_safety(self):
        assert '"' not in admin.h('" onmouseover="alert(1)')
        assert "'" not in admin.h("' onmouseover='alert(1)")

    async def test_none_renders_as_empty_not_the_word_none(self):
        assert admin.h(None) == ""


# --- Rendering (needs a database) -------------------------------------------


class TestRendering:
    async def _seed(self, session_factory, *, title: str = "A captured trace"):
        from hub import auth as hub_auth
        from hub.config import HubConfig as _Cfg
        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme <b>Support</b>", plan="team")
            session.add(org)
            await session.flush()
            org_id = org.id
            session.add(Trace(
                org_id=org_id, title=title, context_text="c", solution_text="s",
                tags=["refunds"], agent_type="support", agent_id="w1",
                quarantined=True, quarantine_reason="looks like <spam>",
            ))
            await hub_auth.issue_api_key(session, org_id, expires_days=90)
        return org_id, _Cfg(database_url="postgresql+asyncpg://unused/db")

    async def test_overview_lists_orgs_and_escapes_their_names(self, session_factory):
        org_id, _ = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "Acme &lt;b&gt;Support&lt;/b&gt;" in r.text
        assert "<b>Support</b>" not in r.text
        assert org_id in r.text

    async def test_a_customer_supplied_title_cannot_inject_script(self, session_factory):
        """The whole reason h() exists. A tenant that titles a trace with a
        script tag must not get code execution in the operator's browser."""
        org_id, _ = await self._seed(session_factory, title="<script>alert('pwn')</script>")
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "<script>alert('pwn')</script>" not in r.text
        assert "&lt;script&gt;" in r.text

    async def test_org_page_shows_keys_quarantine_and_audit(self, session_factory):
        org_id, _ = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "API keys" in r.text
        assert "Quarantined traces" in r.text
        assert "looks like &lt;spam&gt;" in r.text

    async def test_an_unknown_org_id_is_a_page_not_a_crash(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin/org/00000000-0000-0000-0000-000000000000",
                            headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "No such organization" in r.text

    async def test_a_malformed_org_id_is_a_page_not_a_500(self, session_factory):
        """org_id lands in a UUID column, so a mistyped URL raises at the
        driver rather than returning None -- that must be a 404 page, not a
        stack trace in the operator's face."""
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin/org/not-a-uuid", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "No such organization" in r.text

    async def test_pages_are_not_cached(self, session_factory):
        """Live operational state, and a cached copy in a shared browser is
        one more place tenant data sits at rest."""
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert r.headers["cache-control"] == "no-store"

    async def test_the_console_states_that_it_is_read_only(self, session_factory):
        """An operator must not be left wondering whether a click here
        changed something."""
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert "read-only console" in r.text
        assert "never changes state" in r.text

    async def test_no_form_or_mutating_control_is_rendered(self, session_factory):
        """The read-only guarantee, asserted structurally rather than by
        reading the templates: no forms, no POST targets, no fetch()."""
        org_id, _ = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            pages = [
                await c.get("/admin", headers=_basic("op", "s3cret")),
                await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret")),
                await c.get("/admin/kb", headers=_basic("op", "s3cret")),
            ]
        for r in pages:
            assert r.status_code == 200
            lowered = r.text.lower()
            for forbidden in ("<form", "<button", "method=\"post\"", "fetch(", "xmlhttprequest"):
                assert forbidden not in lowered, f"{forbidden} found in a read-only console"

    async def test_no_credential_material_ever_reaches_the_page(self, session_factory):
        """Only non-secret key metadata is rendered. `key_hash` is the argon2
        digest of a live credential and must never appear in a page an
        operator might screenshot or a browser might cache."""
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import ApiKey

        org_id, _ = await self._seed(session_factory)
        async with session_scope(session_factory) as session:
            hashes = (await session.execute(select(ApiKey.key_hash))).scalars().all()
        assert hashes, "the fixture should have issued a key"

        async with _client(_app(session_factory=session_factory)) as c:
            pages = [
                await c.get("/admin", headers=_basic("op", "s3cret")),
                await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret")),
            ]
        for r in pages:
            for digest in hashes:
                assert digest not in r.text
            assert "$argon2" not in r.text

    async def test_only_get_is_routed(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post("/admin", headers=_basic("op", "s3cret"))
        assert r.status_code == 405
