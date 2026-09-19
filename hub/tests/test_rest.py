"""Tests for hub/rest.py -- the `/api/v1/*` surface the CommonTrace Claude
Code plugin speaks.

The wire format here is not this codebase's to choose: it is dictated by a
client that already exists and already ships (`commontrace/skill`'s hooks).
So the property that matters most, and the one these tests are built
around, is FIDELITY TO THAT CLIENT -- the exact paths, the `X-API-Key`
header, `q` rather than `query`, `contributor_name` rather than
`contributor`, a `{"results": [...]}` envelope, and a 403 (not a 401 or a
500) on the one status the plugin branches on specifically. A change here
that still passes hub/tests/test_crud-level checks but renames a field
breaks every installed plugin silently, which is exactly what
`TestThePluginsOwnRequestShapes` exists to catch.

Everything else is the same posture the rest of this Hub's routes are held
to: absent unless configured, no tenant boundary crossable, a malformed
body is a 400 rather than a 500, and nothing reaches Postgres before the
credential does.
"""
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
    """A real org and a real raw key, issued the way every other caller
    gets one -- so a test that authenticates here proves the same path a
    deployed plugin walks, not a fixture-shaped approximation of it."""
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
        """The same property /admin, /app and /signup each hold: a
        deployment that has not opted in has nothing here to probe."""
        async with _client(_app(session_factory, config, enabled=False)) as client:
            for path in ("/api/v1/traces", "/api/v1/traces/search", "/api/v1/keys"):
                assert (await client.post(path, json={})).status_code == 404, path

    async def test_key_provisioning_is_absent_when_signup_is_disabled(
        self, session_factory, config
    ):
        """`/api/v1/keys` mints a credential with no caller identity at all --
        the same capability /signup's form offers, so it must not become a
        second door that opens when that one is shut."""
        app = _app(session_factory, config, signup_enabled=False)
        async with _client(app) as client:
            provision = await client.post("/api/v1/keys", json={"display_name": "x"})
            # The authenticated routes are still mounted; only this one is gone.
            search = await client.post("/api/v1/traces/search", json={"q": "x"})
        assert provision.status_code == 404
        assert search.status_code == 401


class TestKeyProvisioning:
    async def test_it_creates_an_org_and_returns_a_key_that_actually_works(
        self, session_factory, config
    ):
        """The round trip, not just the 201: a key that comes back from
        this endpoint but cannot then authenticate against the Hub is the
        failure mode worth a test, and it is invisible to a status check."""
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

            # The key is immediately usable on an authenticated route.
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
        """An operator reading `list-orgs` should see something meaningful
        rather than a wall of UUIDs."""
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
        """It is shown exactly once and this Hub stores only its hash --
        a cached copy in a proxy would outlive the only chance to read it."""
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
        """Unknown, revoked and expired all get one message -- telling them
        apart tells an attacker which of those a guessed key was."""
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
        """403, not 401: the key is real, the capability is not there. The
        plugin branches on exactly this status ("publishing restricted for
        this account") and prints the body, so it must not be conflated
        with an authentication failure."""
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
        # Named rather than left empty, so fleet reporting can group traces
        # that arrived from the coding-agent plugin.
        assert trace.agent_type == "code"
        assert entry is not None
        assert entry.actor == rest.ACTOR_REST_API

    async def test_tags_are_accepted_as_a_comma_separated_string_too(
        self, session_factory, config
    ):
        """The plugin's directive tells an LLM to send "tags"; composing
        that body by hand produces a CSV string about as often as a list,
        and losing the tags of an otherwise-good trace is a worse outcome
        than accepting both shapes."""
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
        """`{"title": 123}` must land on the same clean rejection a missing
        title gets -- never a TypeError deep inside validation, which would
        surface as a server fault for what is a client mistake."""
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
        """Valid JSON, wrong shape -- `payload.get` on a list is an
        AttributeError, so this is a distinct path from malformed bytes."""
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                "/api/v1/traces", json=["not", "an", "object"], headers=_key(raw_key))
        assert response.status_code == 400

    async def test_metadata_json_is_accepted_and_ignored(self, session_factory, config):
        """The plugin has always sent it and nothing here stores it.
        Rejecting the field would break a shipped client over a value this
        Hub has no column for."""
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
        """The boundary this whole Hub is built around. A new surface is
        exactly where it would be lost, so it is asserted here directly
        rather than assumed from crud.search_traces' own tests."""
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
        """Matching crud.search_traces' own tolerant handling: a bad limit
        is a client slip, not a reason to return nothing."""
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
    """The fidelity tests. Each one mirrors a call the shipped plugin
    actually makes, byte-for-byte in the fields that matter -- so renaming
    a key here fails CI instead of silently breaking every install."""

    async def test_search_returns_the_four_fields_format_results_reads(
        self, session_factory, config
    ):
        """`retrieval.py:format_results` reads id, title, solution_text and
        contributor_name off each result. `contributor_name` is this Hub's
        `contributor` under the client's name for it -- the one field that
        exists purely to match the wire format."""
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
        """`retrieval.py` adds `context` to the body when it has one. This
        Hub has nowhere to apply it, but a 400 over an extra key would
        disable retrieval for every plugin session that sends one."""
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
        """`session_start.provision_api_key` reads `data["api_key"]` and
        writes it to ~/.commontrace/config.json. Any other name and every
        install silently never provisions."""
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
        """The plugin's contribution directive ends "take the returned id"
        and prints it in the receipt banner."""
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
        """A documented no-op, not an accident: the plugin only checks for
        a 2xx, and answering 404 would make every session log a swallowed
        error for a beacon nothing here reads."""
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
        """Unauthenticated beacons would be an open write-shaped endpoint
        for anyone who finds the path."""
        async with _client(_app(session_factory, config)) as client:
            response = await client.post("/api/v1/telemetry/ping", json={})
        assert response.status_code == 401
