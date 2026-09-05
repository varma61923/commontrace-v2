"""Tests for hub/console.py -- the customer-facing console.

This is the first HTML the Hub serves to anyone but the operator, and the
risk profile is different from `hub/admin.py`'s. There, one trusted employee
sees every tenant. Here, many untrusted browsers each see exactly one tenant,
and the properties that matter are:

1. **A session is scoped to one org and cannot be moved to another.** The
   org id lives inside a signed cookie, so forging one is the whole tenant
   boundary for this surface.
2. **Revoking a key ends the sessions it opened.** Otherwise an operator
   revoking a compromised key is told the problem is handled while the
   console keeps serving that org's data.
3. **Nothing changes state.** The console is read-only, which is what makes
   the absence of CSRF tokens correct rather than an oversight.
4. **Absent unless configured**, like /admin: no secret, no routes to probe.
5. **Escaping**, because a trace title is customer-supplied and lands in
   that customer's own page -- self-XSS is less severe than the operator
   console's cross-tenant case, but a session cookie is right there.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import update as sa_update
from starlette.applications import Starlette

from hub import auth, console
from hub.db import session_scope
from hub.models import ApiKey, Organization, Trace

pytestmark = pytest.mark.asyncio

SECRET = "console-signing-secret"


def _explode(*a, **kw):
    raise AssertionError("no database access should happen for this request")


def _app(secret: str = SECRET, session_factory=_explode) -> Starlette:
    app = Starlette()
    if secret:
        console.add_console_routes(app, session_factory, console_secret=secret)
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def org_and_key(session_factory):
    async with session_scope(session_factory) as session:
        org = Organization(name="Northwind")
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id)
        return org.id, issued.raw_key


@pytest_asyncio.fixture
async def other_org_and_key(session_factory):
    async with session_scope(session_factory) as session:
        org = Organization(name="Acme")
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id)
        return org.id, issued.raw_key


async def _revoke_keys(session_factory, org_id: str) -> None:
    async with session_scope(session_factory) as session:
        await session.execute(
            sa_update(ApiKey).where(ApiKey.org_id == org_id)
            .values(revoked_at=datetime.now(timezone.utc))
        )


async def _signed_in(client: httpx.AsyncClient, raw_key: str) -> httpx.Response:
    return await client.post(f"{console.CONSOLE_PATH}/signin", data={"api_key": raw_key})


# --- Absent unless configured ----------------------------------------------


class TestAbsentUnlessConfigured:
    async def test_no_routes_without_a_secret(self):
        """A deployment that has not opted in should have no console to
        probe: a 404 from the router, not a redirect from a handler."""
        async with _client(_app(secret="")) as client:
            for path in ("", "/signin", "/proof", "/memory", "/kb"):
                response = await client.get(f"{console.CONSOLE_PATH}{path}")
                assert response.status_code == 404, path

    async def test_the_secret_is_not_the_operator_token(self):
        """Separate settings on purpose: one value that both authenticates
        the vendor and signs customer sessions means one leak compromises
        both surfaces."""
        from hub.config import HubConfig

        config = HubConfig(database_url="postgresql+asyncpg://x/y")
        assert config.console_secret == ""
        assert config.admin_token == ""


# --- The session is the tenant boundary ------------------------------------


class TestTheSessionCannotBeForged:
    async def test_a_valid_session_round_trips(self):
        token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        claims = console.read_session(SECRET, token)
        assert claims and claims["org"] == "org-1" and claims["key"] == "ct_live_ab"

    async def test_a_different_secret_is_rejected(self):
        token = console.issue_session("attacker-guess", "org-1", "ct_live_ab")
        assert console.read_session(SECRET, token) is None

    @pytest.mark.parametrize("mangle", [
        lambda t: t[:-4] + "AAAA",                       # tampered signature
        lambda t: t.split(".")[0],                        # signature removed
        lambda t: "." + t.split(".")[1],                  # payload removed
        lambda t: t.replace(".", "", 1),                  # separator removed
        lambda t: "",                                     # no cookie
        lambda t: "not-a-token",
    ])
    async def test_a_mangled_token_is_rejected(self, mangle):
        token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        assert console.read_session(SECRET, mangle(token)) is None

    async def test_the_org_id_cannot_be_edited_by_its_holder(self):
        """The whole tenant boundary: an attacker with a legitimate session
        for their own org must not be able to point it at another."""
        import base64
        import json

        token = console.issue_session(SECRET, "org-mine", "ct_live_ab")
        body, _, signature = token.partition(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        claims["org"] = "org-theirs"
        forged_body = base64.urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
        ).decode().rstrip("=")
        assert console.read_session(SECRET, f"{forged_body}.{signature}") is None

    async def test_an_expired_session_is_rejected(self, monkeypatch):
        monkeypatch.setattr(console, "SESSION_TTL_SECONDS", -1)
        assert console.read_session(SECRET, console.issue_session(SECRET, "o", "k")) is None

    async def test_the_api_key_is_not_in_the_token(self):
        """A cookie is long-lived and widely copied. A full-scope credential
        inside one turns every browser misconfiguration into a key
        disclosure, so only the non-secret prefix goes in."""
        raw_key = "ct_live_SUPERSECRETVALUE1234567890"
        token = console.issue_session(SECRET, "org-1", raw_key[:10])
        assert "SUPERSECRETVALUE" not in token
        import base64
        body = token.split(".")[0]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        assert "SUPERSECRETVALUE" not in decoded


# --- Sign-in ----------------------------------------------------------------


class TestSignIn:
    async def test_an_unauthenticated_visit_redirects_rather_than_erroring(self):
        async with _client(_app()) as client:
            for path in ("", "/proof", "/memory", "/kb"):
                response = await client.get(f"{console.CONSOLE_PATH}{path}")
                assert response.status_code == 303
                assert response.headers["location"].endswith("/signin")

    async def test_a_valid_key_opens_a_session(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            response = await _signed_in(client, raw_key)
            assert response.status_code == 303
            claims = console.read_session(SECRET, client.cookies[console.SESSION_COOKIE])
            assert claims["org"] == org_id

    async def test_an_unknown_key_is_refused(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await _signed_in(client, "ct_live_nope")
            assert response.status_code == 200
            assert "not accepted" in response.text
            assert console.SESSION_COOKIE not in client.cookies

    async def test_every_failure_gives_the_same_message(self, session_factory, org_and_key):
        """Unknown, revoked and expired must be indistinguishable, or the
        response tells an attacker which of those a guessed key was."""
        org_id, raw_key = org_and_key
        await _revoke_keys(session_factory, org_id)

        async with _client(_app(session_factory=session_factory)) as client:
            revoked = await _signed_in(client, raw_key)
            unknown = await _signed_in(client, "ct_live_nope")
        assert "not accepted" in revoked.text
        assert revoked.text == unknown.text

    async def test_the_cookie_is_hardened(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            response = await _signed_in(client, raw_key)
        header = response.headers["set-cookie"]
        # httponly: XSS cannot lift the session.
        # samesite=strict: the cookie is never sent cross-site, which is why
        #   a read-only console needs no CSRF token.
        # path=/app: never sent to /mcp, /admin or /metrics.
        assert "HttpOnly" in header
        assert "samesite=strict" in header.lower()
        assert "Path=/app" in header

    async def test_sign_out_clears_the_session(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.get(f"{console.CONSOLE_PATH}/signout")
            response = await client.get(console.CONSOLE_PATH)
            assert response.status_code == 303

    async def test_sign_in_is_rate_limited(self, session_factory):
        """Without this the console is an unauthenticated, unthrottled oracle
        for testing API keys, reachable from a browser -- a strictly easier
        target than the MCP transport, which is limited."""
        app = Starlette()
        console.add_console_routes(app, session_factory, console_secret=SECRET)
        async with _client(app) as client:
            texts = [(await _signed_in(client, "ct_live_nope")).text for _ in range(20)]
        assert any("Too many attempts" in t for t in texts)


# --- Revocation must actually revoke ---------------------------------------


class TestRevocationEndsTheSession:
    async def test_revoking_the_key_locks_the_browser_out(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            assert (await client.get(console.CONSOLE_PATH)).status_code == 200

            await _revoke_keys(session_factory, org_id)

            # Signature still valid, session not expired -- and refused
            # anyway. Revocation that does not revoke is worse than none: it
            # is a false belief about the state of a credential.
            response = await client.get(console.CONSOLE_PATH)
            assert response.status_code == 303
            assert response.headers["location"].endswith("/signin")

    async def test_an_expired_key_locks_the_browser_out(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            async with session_scope(session_factory) as session:
                await session.execute(
                    sa_update(ApiKey).where(ApiKey.org_id == org_id)
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
                )
            assert (await client.get(console.CONSOLE_PATH)).status_code == 303

    async def test_another_orgs_live_key_does_not_revive_the_session(
        self, session_factory, org_and_key, other_org_and_key
    ):
        """The liveness lookup is scoped to the session's own org. A key with
        the same prefix under a different org must not satisfy it."""
        org_id, raw_key = org_and_key
        other_id, _ = other_org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            async with session_scope(session_factory) as session:
                prefix = (await session.execute(
                    select(ApiKey.key_prefix).where(ApiKey.org_id == org_id)
                )).scalars().first()
                await session.execute(
                    sa_update(ApiKey).where(ApiKey.org_id == org_id)
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                session.add(ApiKey(org_id=other_id, key_prefix=prefix, key_hash="x"))
            assert (await client.get(console.CONSOLE_PATH)).status_code == 303


# --- Tenant isolation -------------------------------------------------------


class TestOneSessionSeesOneOrg:
    async def test_a_session_never_shows_another_orgs_traces(
        self, session_factory, org_and_key, other_org_and_key
    ):
        org_id, raw_key = org_and_key
        other_id, other_key = other_org_and_key
        async with session_scope(session_factory) as session:
            session.add(Trace(org_id=org_id, title="MINE-northwind-secret",
                              context_text="c", solution_text="s", tags=["mine"],
                              agent_type="support"))
            session.add(Trace(org_id=other_id, title="THEIRS-acme-secret",
                              context_text="c", solution_text="s", tags=["theirs"],
                              agent_type="support"))

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            for path in ("", "/memory", "/proof", "/kb"):
                text = (await client.get(f"{console.CONSOLE_PATH}{path}")).text
                assert "THEIRS-acme-secret" not in text, path

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, other_key)
            for path in ("", "/memory", "/proof", "/kb"):
                text = (await client.get(f"{console.CONSOLE_PATH}{path}")).text
                assert "MINE-northwind-secret" not in text, path

    async def test_search_cannot_reach_across_orgs(
        self, session_factory, org_and_key, other_org_and_key
    ):
        org_id, raw_key = org_and_key
        other_id, _ = other_org_and_key
        async with session_scope(session_factory) as session:
            session.add(Trace(org_id=other_id, title="THEIRS-acme-secret",
                              context_text="a distinctive phrase about widgets",
                              solution_text="s", tags=[], agent_type="support"))

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(
                f"{console.CONSOLE_PATH}/memory", params={"q": "distinctive widgets"})
        assert "THEIRS-acme-secret" not in response.text


class TestMemoryPagination:
    """search_traces has supported `offset`/`has_more` since the fix for
    the product's own core retrieval defect (hub/crud.py:search_traces's
    docstring) -- but this page used to call it with no offset and never
    read `has_more` back, so a corpus with more than 50 matches showed
    exactly 50 rows with no indication more existed and no link to reach
    them."""

    async def test_more_than_a_page_of_matches_offers_a_next_link(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            for i in range(55):
                session.add(Trace(
                    org_id=org_id, title=f"widget failure {i}",
                    context_text="a distinctive phrase about widgets", solution_text="s",
                    tags=[], agent_type="support",
                ))

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            first_page = await client.get(
                f"{console.CONSOLE_PATH}/memory", params={"q": "distinctive widgets"})
            assert "Older" in first_page.text
            assert "Newer" not in first_page.text

            second_page = await client.get(
                f"{console.CONSOLE_PATH}/memory",
                params={"q": "distinctive widgets", "offset": 50},
            )
        assert "Newer" in second_page.text
        # 55 rows at 50/page: the second page holds the remaining 5, so
        # there is nothing further to page to.
        assert "Older" not in second_page.text

    async def test_a_single_page_of_matches_offers_no_pagination_links(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            session.add(Trace(
                org_id=org_id, title="one widget failure",
                context_text="a distinctive phrase about widgets", solution_text="s",
                tags=[], agent_type="support",
            ))

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(
                f"{console.CONSOLE_PATH}/memory", params={"q": "distinctive widgets"})
        assert "Older" not in response.text
        assert "Newer" not in response.text


# --- Read-only --------------------------------------------------------------


class TestItChangesNothing:
    async def test_no_route_accepts_a_state_changing_method(self, session_factory, org_and_key):
        """Read-only is what makes the absence of CSRF tokens correct rather
        than an oversight: there is no state-changing request for a forged
        one to trigger. Sign-in and sign-out are the exceptions, and neither
        touches tenant data."""
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            for path in ("", "/proof", "/memory", "/kb"):
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    response = await client.request(
                        method, f"{console.CONSOLE_PATH}{path}")
                    assert response.status_code == 405, (method, path)


# --- Escaping ---------------------------------------------------------------


class TestCustomerContentIsEscaped:
    async def test_a_trace_title_cannot_inject_script(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        payload = '<script>alert("xss")</script>'
        async with session_scope(session_factory) as session:
            session.add(Trace(org_id=org_id, title=payload, context_text="c",
                              solution_text="s", tags=[payload], agent_type="support"))

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            for path in ("", "/memory"):
                text = (await client.get(f"{console.CONSOLE_PATH}{path}")).text
                assert "<script>alert" not in text, path
                assert "&lt;script&gt;" in text, path

    async def test_a_search_query_is_escaped_back_into_the_form(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(
                f"{console.CONSOLE_PATH}/memory", params={"q": '"><script>alert(1)</script>'})
        assert "<script>alert(1)" not in response.text


class TestTheKBPageShowsWhyASubmissionWasDeclined:
    async def test_a_rejection_reason_reaches_the_page(self, session_factory, org_and_key, config):
        """`_render_kb` used to read `s.get('reviewer_note')`, a key
        `_submission_to_wire` never produces -- the field is named
        `rejection_reason` there. The lookup always came back `None`, so
        this column silently rendered '—' for every declined proposal,
        no matter what an operator actually typed as the reason."""
        from hub import crud
        from hub.abuse import make_rate_limiter
        from hub.models import KnowledgeBaseSubmission

        org_id, raw_key = org_and_key
        rate_limiter = make_rate_limiter(config)
        async with session_scope(session_factory) as session:
            submission = await crud.submit_kb_entry(
                session, org_id, config, rate_limiter,
                title="t", context_text="c", solution_text="s",
                tags=[], agent_type="code", rationale="substrate knowledge",
            )
        async with session_scope(session_factory) as session:
            other_org = Organization(name="Operator Org")
            session.add(other_org)
            await session.flush()
            operator_org_id = other_org.id
        async with session_scope(session_factory) as session:
            await crud.review_kb_submission(
                session, submission["id"], "reject", operator_org_id, reviewer="op",
                rejection_reason="this is your own business logic, not substrate knowledge",
            )
        # Sanity check the row actually holds what the test expects, under
        # the field name production code actually uses -- so a future rename
        # of BOTH sides together can't make this test pass for the wrong reason.
        async with session_scope(session_factory) as session:
            row = await session.get(KnowledgeBaseSubmission, submission["id"])
            assert row.rejection_reason == "this is your own business logic, not substrate knowledge"

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            text = (await client.get(f"{console.CONSOLE_PATH}/kb")).text
        assert "this is your own business logic, not substrate knowledge" in text


# --- The pages themselves ---------------------------------------------------


class TestTheProofPageLeadsWithValidity:
    async def test_a_compromised_verdict_appears_above_the_effects(self):
        """Placement is the whole point of this page. A number on screen in a
        renewal conversation gets quoted; a caveat below it does not travel
        with it."""
        html = console._render_proof(
            {"headline": "Resolution is up.", "metrics": []},
            {
                "experiment_running": True, "n_observations": 300, "n_occasions": 300,
                "integrity": {
                    "verdict": "COMPROMISED", "effects_readable": False,
                    "n_assignments": 400, "n_resolved": 300,
                    "findings": [{"check": "differential_attrition", "severity": "INVALIDATES",
                                  "headline": "The arms are not equally observed.",
                                  "detail": "The withheld arm is losing occasions faster."}],
                    "projections": [],
                },
                "effects": [{"trace_id": "t1", "title": "A memory", "verdict": "HURTS",
                             "n_injected": 150, "n_withheld": 150, "rate_injected": 0.48,
                             "rate_withheld": 0.65, "effect": -0.17, "ci_95": [-0.28, -0.07],
                             "p_value": 0.002, "significant": True}],
            },
        )
        assert "COMPROMISED" in html
        # Withheld entirely, not shown with a caveat.
        assert "HURTS" not in html
        assert "Effect sizes are withheld" in html

    async def test_a_sound_verdict_shows_the_effects(self):
        html = console._render_proof(
            {"headline": "", "metrics": []},
            {
                "experiment_running": True, "n_observations": 300, "n_occasions": 300,
                "integrity": {"verdict": "SOUND", "effects_readable": True,
                              "n_assignments": 300, "n_resolved": 300,
                              "findings": [], "projections": []},
                "effects": [{"trace_id": "t1", "title": "A memory", "verdict": "HELPS",
                             "n_injected": 150, "n_withheld": 150, "rate_injected": 0.72,
                             "rate_withheld": 0.55, "effect": 0.17, "ci_95": [0.07, 0.28],
                             "p_value": 0.002, "significant": True}],
            },
        )
        assert "SOUND" in html and "HELPS" in html
        assert html.index("Can this be trusted?") < html.index("HELPS")

    async def test_it_states_what_cannot_be_checked(self):
        html = console._render_proof(
            {"headline": "", "metrics": []},
            {"experiment_running": True, "n_observations": 1, "n_occasions": 1,
             "integrity": {"verdict": "SOUND", "effects_readable": True, "findings": [],
                           "projections": [], "n_assignments": 1, "n_resolved": 1},
             "effects": []},
        )
        assert "told to withhold" in html

    async def test_the_observational_half_says_it_is_not_causal(self):
        html = console._render_proof(
            {"headline": "Resolution is up.",
             "metrics": [{"metric": "resolution_rate",
                          "baseline": {"rate": 0.5, "n": 100},
                          "current": {"rate": 0.6, "n": 120},
                          "delta": 0.1, "verdict": "improved"}]},
            {"experiment_running": False},
        )
        assert "OBSERVED change, not a causal effect" in html
        # n beside every rate: 100% of two and 100% of two thousand are the
        # same number on a slide and different facts.
        assert "n=100" in html and "n=120" in html


class TestTheOverviewIsHonestAboutMissingData:
    async def test_no_searches_yet_is_not_rendered_as_a_zero_miss_rate(self):
        """A fleet that has never searched would otherwise read as a fleet
        whose every search succeeds."""
        assert "no searches yet" in console._miss({"miss_rate": None, "searches": 0})
        assert "33%" in console._miss({"miss_rate": 1 / 3, "searches_with_terms": 9})

    async def test_no_experiment_says_nothing_here_is_causal(self):
        html = console._render_overview(
            {"entitlements": {"plan": "free", "traces": {}, "agents": {},
                              "commons_queries": {}},
             "search": {}, "recent": {"traces": []}},
            {"experiment_running": False},
        )
        assert "nothing here is causal yet" in html

    async def test_an_experiment_with_no_outcomes_says_what_is_missing(self):
        html = console._render_overview(
            {"entitlements": {"plan": "free", "traces": {}, "agents": {},
                              "commons_queries": {}},
             "search": {}, "recent": {"traces": []}},
            {"experiment_running": True, "n_observations": 0},
        )
        assert "record_occasion_outcome" in html
