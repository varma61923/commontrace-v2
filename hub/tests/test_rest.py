from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from starlette.applications import Starlette

from hub import auth, rest
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import AuditLogEntry, Organization, Trace

pytestmark = pytest.mark.asyncio


def _app(session_factory, config, *, enabled: bool = True, signup_enabled: bool = True) -> Starlette:
    app = Starlette()
    if enabled:
        rest.add_rest_routes(
            app,
            session_factory,
            config=config,
            rate_limiter=make_rate_limiter(config),
            signup_enabled=signup_enabled,
        )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _org_with_key(session_factory, name="Acme", scopes=None):
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id, scopes=scopes)
        return org.id, issued.raw_key


def _key(raw_key: str) -> dict[str, str]:
    return {"X-API-Key": raw_key}


class TestAbsentUnlessEnabled:
    async def test_no_routes_when_not_mounted(self, session_factory, config):
        async with _client(_app(session_factory, config, enabled=False)) as client:
            for path in ("/api/v1/traces", "/api/v1/traces/search", "/api/v1/keys"):
                assert (await client.post(path, json={})).status_code == 404, path

    async def test_key_provisioning_is_absent_when_signup_is_disabled(
        self, session_factory, config
    ):
        app = _app(session_factory, config, signup_enabled=False)
        async with _client(app) as client:
            provision = await client.post("/api/v1/keys", json={"display_name": "x"})
            search = await client.post("/api/v1/traces/search", json={"q": "x"})
        assert provision.status_code == 404
        assert search.status_code == 401


class TestKeyProvisioning:
    async def test_it_creates_an_org_and_returns_a_key_that_actually_works(
        self, session_factory, config
    ):
        async with _client(_app(session_factory, config)) as client:
            provision = await client.post(
                "/api/v1/keys",
                json={"email": "agent-ab12cd34@commontrace.auto",
                      "display_name": "Claude Code Agent"},
            )
            assert provision.status_code == 201
            body = provision.json()
            assert body["api_key"]
            assert body["org_id"]

            search = await client.post(
                "/api/v1/traces/search", json={"q": "anything"}, headers=_key(body["api_key"]),
            )
        assert search.status_code == 200

        async with session_scope(session_factory) as session:
            org = await session.get(Organization, body["org_id"])
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.org_id == body["org_id"])
                )
            ).scalars().first()
        assert org.name == "Claude Code Agent"
        assert entry is not None
        assert entry.actor == rest.ACTOR_REST_SIGNUP

    async def test_the_org_is_named_from_the_email_when_no_display_name_is_sent(
        self, session_factory, config
    ):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/keys", json={"email": "agent-99ff@commontrace.auto"}
            )
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, response.json()["org_id"])
        assert org.name == "agent-99ff"

    async def test_a_body_with_neither_field_is_refused(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post("/api/v1/keys", json={})
        assert response.status_code == 400
        assert response.json()["error"] == "bad_request"

    async def test_the_raw_key_is_never_cached(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post("/api/v1/keys", json={"display_name": "x"})
        assert response.headers["cache-control"] == "no-store"


class TestAuthentication:
    async def test_a_missing_key_is_refused_before_any_work(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post("/api/v1/traces/search", json={"q": "x"})
        assert response.status_code == 401

    async def test_an_unknown_key_is_refused(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search", json={"q": "x"}, headers=_key("ct_not_a_real_key"),
            )
        assert response.status_code == 401

    async def test_the_refusal_does_not_say_which_failure_mode_it_was(
        self, session_factory, config
    ):
        org_id, raw_key = await _org_with_key(session_factory)
        async with session_scope(session_factory) as session:
            key_row = (
                await session.execute(select(auth.ApiKey).where(auth.ApiKey.org_id == org_id))
            ).scalars().one()
            await auth.revoke_api_key(session, key_row.id)

        async with _client(_app(session_factory, config)) as client:
            revoked = await client.post(
                "/api/v1/traces/search", json={"q": "x"}, headers=_key(raw_key))
            unknown = await client.post(
                "/api/v1/traces/search", json={"q": "x"}, headers=_key("ct_nope"))
        assert revoked.status_code == unknown.status_code == 401
        assert revoked.json() == unknown.json()


class TestScopes:
    async def test_a_read_only_key_cannot_contribute(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory, scopes=["read"])
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={"title": "t", "context_text": "c", "solution_text": "s"},
                headers=_key(raw_key),
            )
        assert response.status_code == 403
        assert "write" in response.json()["detail"]

    async def test_a_write_only_key_cannot_search(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory, scopes=["write"])
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search", json={"q": "x"}, headers=_key(raw_key))
        assert response.status_code == 403


class TestContributing:
    async def test_it_stores_a_real_trace_attributed_to_this_surface(
        self, session_factory, config
    ):
        org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={
                    "title": "Flaky asyncpg pool under load",
                    "context_text": "Connections exhausted at 200 rps",
                    "solution_text": "Raised pool_size and added a timeout",
                    "tags": ["asyncpg", "pool"],
                },
                headers=_key(raw_key),
            )
        assert response.status_code == 201
        trace_id = response.json()["id"]

        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.target_id == trace_id)
                )
            ).scalars().first()
        assert trace.org_id == org_id
        assert trace.title == "Flaky asyncpg pool under load"
        assert sorted(trace.tags) == ["asyncpg", "pool"]
        assert trace.agent_type == "general"
        assert entry is not None
        assert entry.actor == rest.ACTOR_REST_API

    async def test_tags_are_accepted_as_a_comma_separated_string_too(
        self, session_factory, config
    ):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={"title": "t", "context_text": "c", "solution_text": "s",
                      "tags": "alpha, beta"},
                headers=_key(raw_key),
            )
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, response.json()["id"])
        assert sorted(trace.tags) == ["alpha", "beta"]

    async def test_a_missing_required_field_is_a_400_naming_it(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces", json={"title": "t"}, headers=_key(raw_key))
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "context_text" in detail and "solution_text" in detail

    async def test_a_wrong_typed_field_is_a_400_not_a_500(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={"title": {"nested": "object"}, "context_text": None, "solution_text": []},
                headers=_key(raw_key),
            )
        assert response.status_code == 400

    async def test_a_malformed_body_is_a_400_not_a_500(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                content=b"{not json at all",
                headers={**_key(raw_key), "Content-Type": "application/json"},
            )
        assert response.status_code == 400

    async def test_a_json_array_body_is_a_400_not_a_500(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces", json=["not", "an", "object"], headers=_key(raw_key))
        assert response.status_code == 400

    async def test_metadata_json_is_accepted_and_ignored(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={"title": "t", "context_text": "c", "solution_text": "s",
                      "metadata_json": {"pattern": "debug_loop", "minutes": 12}},
                headers=_key(raw_key),
            )
        assert response.status_code == 201


class TestSearching:
    async def _seed(self, session_factory, config, org_id, title="Postgres deadlock on upsert"):
        from hub import crud
        async with session_scope(session_factory) as session:
            return await crud.contribute_trace(
                session, org_id, config, make_rate_limiter(config),
                title=title, context_text="two writers, same row",
                solution_text="ordered the writes", tags=["postgres"], agent_type="code",
            )

    async def test_it_finds_this_orgs_trace(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        await self._seed(session_factory, config, org_id)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search", json={"q": "deadlock"}, headers=_key(raw_key))
        assert response.status_code == 200
        results = response.json()["results"]
        assert results
        assert results[0]["title"] == "Postgres deadlock on upsert"

    async def test_it_never_returns_another_orgs_trace(self, session_factory, config):
        owner_id, _owner_key = await _org_with_key(session_factory, name="Owner")
        _other_id, other_key = await _org_with_key(session_factory, name="Other")
        await self._seed(session_factory, config, owner_id)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search", json={"q": "deadlock"}, headers=_key(other_key))
        assert response.status_code == 200
        assert response.json()["results"] == []

    async def test_a_missing_query_is_refused(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search", json={}, headers=_key(raw_key))
        assert response.status_code == 400

    async def test_an_absurd_limit_is_clamped_rather_than_rejected(
        self, session_factory, config
    ):
        org_id, raw_key = await _org_with_key(session_factory)
        await self._seed(session_factory, config, org_id)
        async with _client(_app(session_factory, config)) as client:
            huge = await client.post(
                "/api/v1/traces/search", json={"q": "deadlock", "limit": 10_000},
                headers=_key(raw_key))
            junk = await client.post(
                "/api/v1/traces/search", json={"q": "deadlock", "limit": "lots"},
                headers=_key(raw_key))
        assert huge.status_code == 200 and huge.json()["results"]
        assert junk.status_code == 200 and junk.json()["results"]


class TestThePluginsOwnRequestShapes:
    async def test_search_returns_the_four_fields_format_results_reads(
        self, session_factory, config
    ):
        org_id, raw_key = await _org_with_key(session_factory)
        await TestSearching()._seed(session_factory, config, org_id)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search",
                json={"q": "deadlock", "limit": 3},
                headers=_key(raw_key),
            )
        result = response.json()["results"][0]
        for field in ("id", "title", "solution_text", "contributor_name"):
            assert field in result, field

    async def test_search_accepts_the_context_object_the_plugin_sends(
        self, session_factory, config
    ):
        org_id, raw_key = await _org_with_key(session_factory)
        await TestSearching()._seed(session_factory, config, org_id)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces/search",
                json={"q": "deadlock", "limit": 3,
                      "context": {"cwd": "/repo", "tool": "Bash"}},
                headers=_key(raw_key),
            )
        assert response.status_code == 200
        assert response.json()["results"]

    async def test_provisioning_returns_the_api_key_field_the_plugin_stores(
        self, session_factory, config
    ):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/keys",
                json={"email": "agent-ab12cd34@commontrace.auto",
                      "display_name": "Claude Code Agent"},
            )
        assert "api_key" in response.json()

    async def test_contribute_returns_an_id_the_receipt_can_print(
        self, session_factory, config
    ):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces",
                json={"title": "t", "context_text": "c", "solution_text": "s",
                      "tags": ["x"], "metadata_json": {}},
                headers=_key(raw_key),
            )
        assert response.json()["id"]


class TestTelemetry:
    async def test_the_beacons_accept_and_drop(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            for beacon, payload in (
                ("install", {"platform": "Claude Code", "skill_version": "1.0",
                             "install_source": "plugin"}),
                ("ping", {}),
                ("triggers", {"triggers": ["domain_entry"]}),
            ):
                response = await client.post(
                    f"/api/v1/telemetry/{beacon}", json=payload, headers=_key(raw_key))
                assert response.status_code == 204, beacon

    async def test_they_still_require_a_valid_key(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post("/api/v1/telemetry/ping", json={})
        assert response.status_code == 401
