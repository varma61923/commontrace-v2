from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from starlette.applications import Starlette

from hub import auth, console, signup
from hub.db import session_scope
from hub.models import ApiKey, AuditLogEntry, Organization

pytestmark = pytest.mark.asyncio


def _app(session_factory, *, enabled: bool = True) -> Starlette:
    app = Starlette()
    if enabled:
        signup.add_signup_routes(app, session_factory)
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestAbsentUnlessEnabled:
    async def test_no_routes_when_not_mounted(self, session_factory):
        async with _client(_app(session_factory, enabled=False)) as client:
            get_response = await client.get(signup.SIGNUP_PATH)
            post_response = await client.post(signup.SIGNUP_PATH, data={"org_name": "Acme"})
        assert get_response.status_code == 404
        assert post_response.status_code == 404


class TestSuccessfulSignup:
    async def test_it_creates_an_org_and_returns_a_working_key(self, session_factory):
        async with _client(_app(session_factory)) as client:
            response = await client.post(signup.SIGNUP_PATH, data={"org_name": "Northwind Robotics"})
        assert response.status_code == 200
        assert "Account created" in response.text

        async with session_scope(session_factory) as session:
            org = (
                await session.execute(select(Organization).where(Organization.name == "Northwind Robotics"))
            ).scalar_one()
            assert org.plan == "free"
            keys = (await session.execute(select(ApiKey).where(ApiKey.org_id == org.id))).scalars().all()
            assert len(keys) == 1

    async def test_the_raw_key_in_the_response_actually_authenticates(self, session_factory):
        import re

        async with _client(_app(session_factory)) as client:
            response = await client.post(signup.SIGNUP_PATH, data={"org_name": "Acme"})
        match = re.search(r'value="(ct_live_[^"]+)"', response.text)
        assert match, response.text
        raw_key = match.group(1)

        async with session_scope(session_factory) as session:
            authenticated = await auth.verify_api_key(session, raw_key)
        assert authenticated is not None
        assert authenticated.org_id

    async def test_the_issued_key_signs_in_to_the_console(self, session_factory):
        import re

        async with _client(_app(session_factory)) as client:
            response = await client.post(signup.SIGNUP_PATH, data={"org_name": "Acme"})
        match = re.search(r'value="(ct_live_[^"]+)"', response.text)
        raw_key = match.group(1)

        console_app = Starlette()
        console.add_console_routes(console_app, session_factory, console_secret="test-secret")
        async with _client(console_app) as client:
            signin_response = await client.post(
                f"{console.CONSOLE_PATH}/signin", data={"api_key": raw_key}
            )
        assert signin_response.status_code == 303
        assert signin_response.headers["location"] == console.CONSOLE_PATH

    async def test_it_writes_an_audit_entry(self, session_factory):
        async with _client(_app(session_factory)) as client:
            await client.post(signup.SIGNUP_PATH, data={"org_name": "Acme"})
        async with session_scope(session_factory) as session:
            entries = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.actor == signup.ACTOR_SELF_SERVE_SIGNUP)
                )
            ).scalars().all()
        assert len(entries) == 1
        assert entries[0].action == "create_org"

    async def test_two_signups_from_the_same_address_get_two_distinct_orgs(self, session_factory):
        async with _client(_app(session_factory)) as client:
            first = await client.post(signup.SIGNUP_PATH, data={"org_name": "Team One"})
            second = await client.post(signup.SIGNUP_PATH, data={"org_name": "Team Two"})
        assert first.status_code == 200 and second.status_code == 200
        async with session_scope(session_factory) as session:
            count = len((await session.execute(select(Organization.id))).scalars().all())
        assert count == 2


class TestValidation:
    async def test_a_blank_name_is_rejected_without_touching_the_database(self, session_factory):
        async with _client(_app(session_factory)) as client:
            response = await client.post(signup.SIGNUP_PATH, data={"org_name": "  "})
        assert response.status_code == 200
        assert "must be" in response.text
        async with session_scope(session_factory) as session:
            assert (await session.execute(select(Organization.id))).first() is None

    async def test_an_overlong_name_is_rejected(self, session_factory):
        async with _client(_app(session_factory)) as client:
            response = await client.post(signup.SIGNUP_PATH, data={"org_name": "x" * 300})
        assert "must be" in response.text
        async with session_scope(session_factory) as session:
            assert (await session.execute(select(Organization.id))).first() is None


class TestHoneypot:
    async def test_a_filled_honeypot_field_creates_no_org(self, session_factory):
        async with _client(_app(session_factory)) as client:
            response = await client.post(
                signup.SIGNUP_PATH, data={"org_name": "Bot Co", "website": "http://spam.example"}
            )
        assert response.status_code == 200
        assert "Account created" not in response.text
        async with session_scope(session_factory) as session:
            assert (await session.execute(select(Organization.id))).first() is None

    async def test_an_empty_honeypot_field_is_a_normal_signup(self, session_factory):
        async with _client(_app(session_factory)) as client:
            response = await client.post(
                signup.SIGNUP_PATH, data={"org_name": "Real Co", "website": ""}
            )
        assert "Account created" in response.text


class TestSignupIsRateLimited:
    async def test_a_flood_from_one_address_eventually_gets_refused(self, session_factory):
        async with _client(_app(session_factory)) as client:
            texts = [
                (await client.post(signup.SIGNUP_PATH, data={"org_name": f"Org {i}"})).text
                for i in range(10)
            ]
        assert any("Too many signups" in t for t in texts)

    async def test_a_refused_attempt_still_creates_no_org(self, session_factory):
        async with _client(_app(session_factory)) as client:
            for i in range(10):
                await client.post(signup.SIGNUP_PATH, data={"org_name": f"Org {i}"})
        async with session_scope(session_factory) as session:
            count = len((await session.execute(select(Organization.id))).scalars().all())
        assert count <= 2
