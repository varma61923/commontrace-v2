from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from sqlalchemy.exc import IntegrityError

from hub import auth, rbac
from hub.abuse import RateLimiter, make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, User
from hub.server import ApiKeyAuthMiddleware, build_app, build_mcp_server
from hub.sso import IdentityProvider

pytestmark = pytest.mark.asyncio

ISSUER = "https://idp.example.test/"
AUDIENCE = "commontrace-hub"


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _jwks_for(pub, kid="k1"):
    raw = json.loads(RSAAlgorithm.to_jwk(pub))
    raw["kid"] = kid
    raw["alg"] = "RS256"
    return {"keys": [raw]}


def _token(key, kid="k1", *, sub="subject-1", **claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 300}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def payload(result) -> dict:
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


@pytest_asyncio.fixture
async def org(session_factory) -> str:
    async with session_scope(session_factory) as session:
        organization = Organization(name="identity-test-org")
        session.add(organization)
        await session.flush()
        return str(organization.id)


@pytest.fixture
def signing_key():
    return _keypair()


@pytest.fixture
def provider(signing_key):
    _, pub = signing_key
    return IdentityProvider(issuer=ISSUER, audience=AUDIENCE, jwks=_jwks_for(pub))


async def _linked_user(session_factory, org_id, role=rbac.ROLE_CURATOR, subject="subject-1"):
    async with session_scope(session_factory) as session:
        user = User(
            org_id=org_id, email="person@example.test", role=role,
            issuer=ISSUER, external_subject=subject, created_by="test",
        )
        session.add(user)
        await session.flush()
        return user.id


class _AsUser:
    def __init__(self, org_id: str, person: auth.AuthenticatedUser):
        self._org_id = org_id
        self._person = person

    def __enter__(self):
        self._org = auth.current_org_id.set(self._org_id)
        self._actor = auth.current_actor.set(f"user:{self._person.id}")
        self._scopes = auth.current_scopes.set(rbac.scopes_of(self._person.role))
        self._user = auth.current_user.set(self._person)
        return self

    def __exit__(self, *exc):
        auth.current_user.reset(self._user)
        auth.current_scopes.reset(self._scopes)
        auth.current_actor.reset(self._actor)
        auth.current_org_id.reset(self._org)
        return False


class TestUserModel:
    async def test_one_email_per_org(self, session_factory, org):
        async with session_scope(session_factory) as session:
            session.add(User(org_id=org, email="a@x.test", role=rbac.ROLE_VIEWER))
            await session.flush()
        with pytest.raises(IntegrityError):
            async with session_scope(session_factory) as session:
                session.add(User(org_id=org, email="a@x.test", role=rbac.ROLE_CURATOR))
                await session.flush()

    async def test_the_same_email_in_two_different_orgs_is_fine(self, session_factory, org):
        async with session_scope(session_factory) as session:
            other = Organization(name="another-org")
            session.add(other)
            await session.flush()
            other_id = other.id
        async with session_scope(session_factory) as session:
            session.add(User(org_id=org, email="a@x.test", role=rbac.ROLE_VIEWER))
            session.add(User(org_id=other_id, email="a@x.test", role=rbac.ROLE_VIEWER))
            await session.flush()

    async def test_one_user_per_linked_identity_globally(self, session_factory, org):
        async with session_scope(session_factory) as session:
            other = Organization(name="another-org-2")
            session.add(other)
            await session.flush()
            other_id = other.id
        async with session_scope(session_factory) as session:
            session.add(User(
                org_id=org, email="a@x.test", role=rbac.ROLE_VIEWER,
                issuer=ISSUER, external_subject="dupe-subject",
            ))
            await session.flush()
        with pytest.raises(IntegrityError):
            async with session_scope(session_factory) as session:
                session.add(User(
                    org_id=other_id, email="b@x.test", role=rbac.ROLE_VIEWER,
                    issuer=ISSUER, external_subject="dupe-subject",
                ))
                await session.flush()

    async def test_multiple_unlinked_users_do_not_collide(self, session_factory, org):
        async with session_scope(session_factory) as session:
            session.add(User(org_id=org, email="a@x.test", role=rbac.ROLE_VIEWER))
            session.add(User(org_id=org, email="b@x.test", role=rbac.ROLE_VIEWER))
            await session.flush()


class TestVerifyUserToken:
    async def test_a_valid_token_for_a_linked_user_authenticates(
        self, session_factory, org, signing_key, provider
    ):
        key, _ = signing_key
        await _linked_user(session_factory, org)
        token = _token(key)
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token, provider)
        assert person is not None
        assert person.org_id == org
        assert person.role == rbac.ROLE_CURATOR

    async def test_login_updates_last_login_at(self, session_factory, org, signing_key, provider):
        key, _ = signing_key
        user_id = await _linked_user(session_factory, org)
        token = _token(key)
        async with session_scope(session_factory) as session:
            await auth.verify_user_token(session, token, provider)
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            assert row.last_login_at is not None

    async def test_an_unverifiable_token_authenticates_no_one(
        self, session_factory, org, provider
    ):
        garbage_key, _ = _keypair()
        await _linked_user(session_factory, org)
        token = _token(garbage_key)
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token, provider)
        assert person is None

    async def test_a_verified_subject_with_no_linked_user_authenticates_no_one(
        self, session_factory, signing_key, provider
    ):
        key, _ = signing_key
        token = _token(key, sub="nobody-has-linked-this-subject")
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token, provider)
        assert person is None

    async def test_a_disabled_user_is_refused_even_with_a_fresh_valid_token(
        self, session_factory, org, signing_key, provider
    ):
        from datetime import datetime, timezone

        key, _ = signing_key
        user_id = await _linked_user(session_factory, org)
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            row.disabled_at = datetime.now(timezone.utc)
        token = _token(key)
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token, provider)
        assert person is None

    async def test_re_enabling_restores_access(self, session_factory, org, signing_key, provider):
        from datetime import datetime, timezone

        key, _ = signing_key
        user_id = await _linked_user(session_factory, org)
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            row.disabled_at = datetime.now(timezone.utc)
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
            row.disabled_at = None
        token = _token(key)
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token, provider)
        assert person is not None

    async def test_a_token_for_a_different_issuer_does_not_match_a_user_linked_elsewhere(
        self, session_factory, org, signing_key
    ):
        key, _ = signing_key
        await _linked_user(session_factory, org)
        other_provider = IdentityProvider(
            issuer="https://different-idp.test/", audience=AUDIENCE,
            jwks=_jwks_for(signing_key[1]),
        )
        token_for_other_issuer = jwt.encode(
            {"iss": "https://different-idp.test/", "aud": AUDIENCE, "sub": "subject-1",
             "iat": int(time.time()), "exp": int(time.time()) + 300},
            key, algorithm="RS256", headers={"kid": "k1"},
        )
        async with session_scope(session_factory) as session:
            person = await auth.verify_user_token(session, token_for_other_issuer, other_provider)
        assert person is None


@pytest_asyncio.fixture
async def mcp(config, session_factory):
    return build_mcp_server(config, session_factory, make_rate_limiter(config))


class TestCapabilityEnforcementThroughARealTool:
    async def test_a_curator_can_contribute(self, mcp, org):
        person = auth.AuthenticatedUser(id="u1", org_id=org, role=rbac.ROLE_CURATOR, email="c@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("contribute_trace", {
                "title": "t", "context_text": "c", "solution_text": "s",
                "agent_type": "support",
            }))
        assert result.get("ok", True) is not False
        assert "error" not in result or result["error"] != "forbidden"

    async def test_a_viewer_cannot_contribute(self, mcp, org):
        person = auth.AuthenticatedUser(id="u2", org_id=org, role=rbac.ROLE_VIEWER, email="v@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("contribute_trace", {
                "title": "t", "context_text": "c", "solution_text": "s",
                "agent_type": "support",
            }))
        assert result["error"] == "forbidden"

    async def test_a_role_with_write_scope_but_no_curate_capability_is_refused(self, mcp, org):
        person = auth.AuthenticatedUser(id="u2b", org_id=org, role=rbac.ROLE_ANALYST, email="an@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("contribute_trace", {
                "title": "t", "context_text": "c", "solution_text": "s",
                "agent_type": "support",
            }))
        assert result["error"] == "forbidden"
        assert result["required_capability"] == rbac.CAP_CURATE
        assert result["role"] == rbac.ROLE_ANALYST

    async def test_a_viewer_can_still_read(self, mcp, org):
        person = auth.AuthenticatedUser(id="u3", org_id=org, role=rbac.ROLE_VIEWER, email="v@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("search_traces", {"query": "x"}))
        assert result.get("error") != "forbidden"

    async def test_a_deployer_holds_write_scope_but_still_cannot_curate(self, mcp, org):
        person = auth.AuthenticatedUser(id="u4", org_id=org, role=rbac.ROLE_DEPLOYER, email="d@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("contribute_trace", {
                "title": "t", "context_text": "c", "solution_text": "s",
                "agent_type": "support",
            }))
        assert result["error"] == "forbidden"
        assert result["required_capability"] == rbac.CAP_CURATE
        assert result["role"] == rbac.ROLE_DEPLOYER

    async def test_a_curator_holds_write_scope_but_cannot_deploy(self, mcp, org):
        person = auth.AuthenticatedUser(id="u4b", org_id=org, role=rbac.ROLE_CURATOR, email="c2@x.test")
        with _AsUser(org, person):
            result = payload(await mcp.call_tool("holdout_assign", {
                "trace_ids": [], "occasion_id": "occ-1",
            }))
        assert result["error"] == "forbidden"
        assert result["required_capability"] == rbac.CAP_DEPLOY

    async def test_a_viewer_cannot_delete_a_trace(self, mcp, org):
        viewer = auth.AuthenticatedUser(id="u5", org_id=org, role=rbac.ROLE_VIEWER, email="v2@x.test")
        with _AsUser(org, viewer):
            result = payload(await mcp.call_tool("delete_trace", {"id": "nonexistent"}))
        assert result["error"] == "forbidden"

    async def test_no_user_in_context_is_unaffected_bare_api_key_path(self, mcp, org):
        from hub import scopes

        org_token = auth.current_org_id.set(org)
        actor_token = auth.current_actor.set("ct_live_test")
        scope_token = auth.current_scopes.set(scopes.ALL_SCOPES)
        try:
            result = payload(await mcp.call_tool("search_traces", {"query": "x"}))
        finally:
            auth.current_scopes.reset(scope_token)
            auth.current_actor.reset(actor_token)
            auth.current_org_id.reset(org_token)
        assert result.get("error") != "forbidden"


class TestFullStackJwtAuthentication:
    async def _app_client(self, config, session_factory, provider):
        app = build_app(config, session_factory)
        inner = app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=inner), base_url="http://test")

    async def test_a_jwt_shaped_token_with_no_configured_provider_is_refused(
        self, config, session_factory
    ):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        async def _ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", _ok)])
        app.add_middleware(
            ApiKeyAuthMiddleware,
            session_factory=session_factory,
            protected_path="/mcp",
            auth_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
            read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
            identity_provider=None,
        )
        key, _ = _keypair()
        token = _token(key)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/mcp", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 401

    async def test_a_deprovisioned_user_is_refused_at_the_http_layer(
        self, session_factory, org, signing_key, provider
    ):
        from datetime import datetime, timezone

        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        key, _ = signing_key
        user_id = await _linked_user(session_factory, org)

        async def _ok(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/mcp", _ok)])
        app.add_middleware(
            ApiKeyAuthMiddleware,
            session_factory=session_factory,
            protected_path="/mcp",
            auth_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
            read_rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
            identity_provider=provider,
        )
        token = _token(key)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            before = await client.get("/mcp", headers={"authorization": f"Bearer {token}"})
            assert before.status_code == 200

            async with session_scope(session_factory) as session:
                row = await session.get(User, user_id)
                row.disabled_at = datetime.now(timezone.utc)

            after = await client.get("/mcp", headers={"authorization": f"Bearer {token}"})
        assert after.status_code == 401


class TestGetCurrentActor:
    async def test_a_person_actor_is_not_double_wrapped(self):
        token = auth.current_actor.set("user:abc-123")
        try:
            assert auth.get_current_actor() == "user:abc-123"
        finally:
            auth.current_actor.reset(token)

    async def test_an_api_key_prefix_is_still_wrapped(self):
        token = auth.current_actor.set("ct_live_test")
        try:
            assert auth.get_current_actor() == "api-key:ct_live_test"
        finally:
            auth.current_actor.reset(token)

    async def test_no_actor_in_context_is_unknown(self):
        assert auth.get_current_actor() == "unknown"
