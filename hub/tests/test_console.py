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
3. **Overview/Proof/Memory/Knowledge Base change nothing** -- read-only,
   which is what makes the absence of CSRF tokens correct there rather than
   an oversight. Users & API Keys are the one deliberate exception, gated
   behind `admin` scope (checked live on every request, not baked into the
   session cookie) plus an explicit org-ownership check on every
   id-addressed mutation.
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

from hub import alerts as alerts_module
from hub import auth, console, rbac
from hub.billing import StripeSettings
from hub.db import session_scope
from hub.models import ApiKey, Organization, Trace, User

pytestmark = pytest.mark.asyncio

SECRET = "console-signing-secret"


def _explode(*a, **kw):
    raise AssertionError("no database access should happen for this request")


def _app(secret: str = SECRET, session_factory=_explode, stripe: StripeSettings | None = None) -> Starlette:
    app = Starlette()
    if secret:
        console.add_console_routes(app, session_factory, console_secret=secret, stripe=stripe)
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


@pytest_asyncio.fixture
async def org_and_readonly_key(session_factory):
    """A key scoped read-only -- satisfies() with `read` never satisfies
    `admin`, which is exactly the property the new mutating console
    routes below are gated on."""
    async with session_scope(session_factory) as session:
        org = Organization(name="Readonly Co")
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id, scopes=["read"])
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
            for path in ("", "/signin", "/proof", "/memory", "/kb", "/users", "/keys", "/alerts"):
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


# --- Shareable, read-only Proof links ---------------------------------------


class TestShareTokensAreDistinctFromSessions:
    """issue_share_token/read_share_token in isolation -- no database, no
    routes. The property that matters most: a share token and a session
    token are signed the same way but must never be interchangeable, or a
    link handed to an outsider becomes a full-scope session for that org."""

    async def test_a_valid_share_token_round_trips(self):
        token = console.issue_share_token(SECRET, "org-1")
        claims = console.read_share_token(SECRET, token)
        assert claims and claims["org"] == "org-1" and claims["kind"] == "share_proof"

    async def test_a_different_secret_is_rejected(self):
        token = console.issue_share_token("attacker-guess", "org-1")
        assert console.read_share_token(SECRET, token) is None

    @pytest.mark.parametrize("mangle", [
        lambda t: t[:-4] + "AAAA",
        lambda t: t.split(".")[0],
        lambda t: "." + t.split(".")[1],
        lambda t: t.replace(".", "", 1),
        lambda t: "",
        lambda t: "not-a-token",
    ])
    async def test_a_mangled_token_is_rejected(self, mangle):
        token = console.issue_share_token(SECRET, "org-1")
        assert console.read_share_token(SECRET, mangle(token)) is None

    async def test_an_expired_share_token_is_rejected(self):
        token = console.issue_share_token(SECRET, "org-1", ttl_seconds=-1)
        assert console.read_share_token(SECRET, token) is None

    async def test_a_session_token_is_not_a_valid_share_token(self):
        """The other half of the guard read_session already carries: a full
        session, presented at the public share route, must not resolve."""
        session_token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        assert console.read_share_token(SECRET, session_token) is None

    async def test_a_share_token_is_not_a_valid_session(self):
        """Forward direction: a share link must never be upgradeable into a
        mutating-scope console session just by pasting it into the cookie."""
        share_token = console.issue_share_token(SECRET, "org-1")
        assert console.read_session(SECRET, share_token) is None


class TestProofSharing:
    """End-to-end: minting a link from an authenticated session, and
    resolving it back with none at all."""

    async def test_the_proof_page_offers_a_share_form_when_signed_in(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/proof")
        assert response.status_code == 200
        assert "Generate shareable link" in response.text

    async def test_sharing_without_a_session_redirects_to_signin(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(f"{console.CONSOLE_PATH}/proof/share")
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_a_signed_in_org_can_mint_and_then_view_its_own_link(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            share_response = await client.post(f"{console.CONSOLE_PATH}/proof/share")
            assert share_response.status_code == 303
            location = share_response.headers["location"]
            assert location.startswith(f"{console.CONSOLE_PATH}/proof?share_url=")

            proof_response = await client.get(location)
            assert "Shareable link generated." in proof_response.text

        # The minted URL resolves with NO cookies at all -- a fresh, bare
        # client, exactly like an outsider who was only handed the link.
        import re
        match = re.search(r'value="([^"]+)"', proof_response.text)
        assert match, proof_response.text
        share_url = match.group(1).replace("&amp;", "&")
        token = share_url.rsplit("/", 1)[-1]

        async with _client(_app(session_factory=session_factory)) as anon_client:
            shared_response = await anon_client.get(
                f"{console.CONSOLE_PATH}/proof/shared/{token}"
            )
        assert shared_response.status_code == 200
        assert "Shared, read-only report" in shared_response.text
        assert "Proof" in shared_response.text
        # No session cookie, no nav -- an outsider gets exactly the report,
        # nothing that grants access to anything else.
        assert console.SESSION_COOKIE not in shared_response.cookies
        assert f"{console.CONSOLE_PATH}/memory" not in shared_response.text

    async def test_a_share_link_only_ever_resolves_its_own_org(
        self, session_factory, org_and_key, other_org_and_key
    ):
        org_id, _raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        token = console.issue_share_token(SECRET, org_id)
        assert console.read_share_token(SECRET, token)["org"] == org_id
        assert console.read_share_token(SECRET, token)["org"] != other_org_id

    async def test_an_expired_link_is_a_plain_404(self, session_factory):
        token = console.issue_share_token(SECRET, "org-1", ttl_seconds=-1)
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")
        assert response.status_code == 404

    async def test_a_forged_link_is_a_plain_404(self, session_factory):
        """Not 401/403 -- a share link is handed to people with no other
        relationship to this Hub, and the response must not distinguish
        'tampered' from 'expired' from 'never existed'."""
        token = console.issue_share_token(SECRET, "org-1")
        forged = token[:-4] + "AAAA"
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/proof/shared/{forged}")
        assert response.status_code == 404

    async def test_a_full_session_token_is_refused_at_the_shared_route(
        self, session_factory, org_and_key
    ):
        """The narrower half of the kind-separation guard exercised against
        the real route, not just the pure function above."""
        org_id, _raw_key = org_and_key
        session_token = console.issue_session(SECRET, org_id, "ct_live_ab")
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(
                f"{console.CONSOLE_PATH}/proof/shared/{session_token}"
            )
        assert response.status_code == 404

    async def test_a_share_link_cannot_be_used_to_sign_in(
        self, session_factory, org_and_key
    ):
        """The forward direction, exercised through cookies: pasting a share
        token in as the session cookie must not open the authenticated
        console -- read_session's explicit kind check is what stops it."""
        org_id, _raw_key = org_and_key
        share_token = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            client.cookies.set(console.SESSION_COOKIE, share_token)
            response = await client.get(console.CONSOLE_PATH)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_shared_view_is_rate_limited_per_org(self, session_factory, org_and_key):
        """Keyed by org, not by client address: the threat is one link being
        hit hard by whoever holds it, from however many addresses."""
        org_id, _raw_key = org_and_key
        token = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            statuses = [
                (await client.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")).status_code
                for _ in range(85)
            ]
        assert 429 in statuses

    async def test_a_rate_limited_view_names_when_to_come_back(
        self, session_factory, org_and_key
    ):
        org_id, _raw_key = org_and_key
        token = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            responses = [
                await client.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")
                for _ in range(85)
            ]
        limited = [r for r in responses if r.status_code == 429]
        assert limited
        assert "Retry-After" in limited[0].headers


# --- Billing (hub/billing.py) ------------------------------------------------


class TestBillingCheckoutAndPortal:
    """The console's own Stripe surface: outbound Stripe API calls are
    monkeypatched at the names console.py imports them under (the same
    seam hub/tests/test_billing.py patches at billing._post), so none of
    this touches the real network or needs a Stripe account."""

    async def test_checkout_is_absent_without_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_checkout_refuses_to_run_with_no_webhook_secret_configured(
        self, session_factory, org_and_key, monkeypatch
    ):
        """The severe partial-configuration case: secret_key + a price with
        NO webhook_secret must never reach Stripe. If it did, a customer
        could complete a real payment while add_billing_webhook_route
        (hub/server.py, gated on webhook_secret) stays unregistered --
        Organization.plan would never learn the payment happened. Charged
        and never upgraded is worse than the button not existing."""
        _org_id, raw_key = org_and_key

        async def must_not_be_called(*a, **kw):
            raise AssertionError("checkout must not run without a working webhook")

        monkeypatch.setattr(console, "create_checkout_session", must_not_be_called)
        stripe = StripeSettings(secret_key="sk_test", price_team="price_t")  # no webhook_secret
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_checkout_redirects_home_when_stripe_is_not_configured(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_checkout_redirects_to_stripe_when_configured(
        self, session_factory, org_and_key, monkeypatch
    ):
        org_id, raw_key = org_and_key
        captured = {}

        async def fake_create_checkout_session(settings, *, org, plan, success_url, cancel_url):
            captured["org_id"] = org.id
            captured["plan"] = plan
            captured["success_url"] = success_url
            captured["cancel_url"] = cancel_url
            return "https://checkout.stripe.com/pay/cs_test_abc"

        monkeypatch.setattr(console, "create_checkout_session", fake_create_checkout_session)
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert response.headers["location"] == "https://checkout.stripe.com/pay/cs_test_abc"
        assert captured["plan"] == "team"
        assert captured["org_id"] == org_id
        assert captured["success_url"].endswith("?upgraded=1")

    async def test_an_already_subscribed_org_cannot_mint_a_second_checkout_session(
        self, session_factory, org_and_key, monkeypatch
    ):
        """The severe case billing.py's own module docstring warns about:
        Checkout always mints a NEW subscription, so an org that already
        has one reaching this route anyway (a stale page, a browser
        back-button resubmit, a direct POST -- the Overview page hiding
        the button is a UI nicety, not enforcement) must not be allowed
        to create a second one on the same Stripe customer, which is
        silent double billing."""
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.stripe_customer_id = "cus_existing"
            org.stripe_subscription_id = "sub_existing"
            org.plan = "team"

        async def must_not_be_called(*a, **kw):
            raise AssertionError("checkout must not run for an already-subscribed org")

        monkeypatch.setattr(console, "create_checkout_session", must_not_be_called)
        stripe = StripeSettings(
            secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t", price_scale="price_s"
        )
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "scale"})
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH
        async with session_scope(session_factory) as session:
            unchanged = await session.get(Organization, org_id)
            assert unchanged.stripe_subscription_id == "sub_existing"
            assert unchanged.plan == "team"

    async def test_an_unpriced_plan_is_refused(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        # no scale price -- webhook_secret set so this exercises the
        # per-plan price check specifically, not the blanket configured gate.
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "scale"})
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_an_unrecognized_plan_is_refused(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "definitely-not-a-plan"}
            )
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_a_stripe_failure_redirects_home_with_an_error_flag_not_a_500(
        self, session_factory, org_and_key, monkeypatch
    ):
        _org_id, raw_key = org_and_key

        async def failing(*a, **kw):
            raise RuntimeError("stripe unreachable")

        monkeypatch.setattr(console, "create_checkout_session", failing)
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert "billing_error=1" in response.headers["location"]

    async def test_portal_is_absent_without_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(f"{console.CONSOLE_PATH}/billing/portal")
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_portal_redirects_home_with_no_subscription(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        stripe = StripeSettings(secret_key="sk_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/portal")
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_portal_redirects_to_stripe_for_a_subscribed_org(
        self, session_factory, org_and_key, monkeypatch
    ):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.stripe_customer_id = "cus_existing"
            org.plan = "team"
            org.stripe_subscription_id = "sub_existing"

        async def fake_portal(settings, *, customer_id, return_url):
            assert customer_id == "cus_existing"
            return "https://billing.stripe.com/session/bps_test_1"

        monkeypatch.setattr(console, "create_billing_portal_session", fake_portal)
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/portal")
        assert response.status_code == 303
        assert response.headers["location"] == "https://billing.stripe.com/session/bps_test_1"

    async def test_portal_refuses_to_run_with_no_webhook_secret_configured(
        self, session_factory, org_and_key, monkeypatch
    ):
        """The same gap as billing_checkout, on the other route: every
        change a customer makes in the Portal (cancel, switch plan) reaches
        this Hub only through /billing/webhook. Without a webhook_secret
        that route is unregistered, so an org that reached the Portal
        anyway could cancel and keep its paid entitlement forever --
        silently stale state, not a loud failure."""
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.stripe_customer_id = "cus_existing"
            org.plan = "team"
            org.stripe_subscription_id = "sub_existing"

        async def must_not_be_called(*a, **kw):
            raise AssertionError("portal must not run without a working webhook")

        monkeypatch.setattr(console, "create_billing_portal_session", must_not_be_called)
        stripe = StripeSettings(secret_key="sk_test", price_team="price_t")  # no webhook_secret
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/billing/portal")
        assert response.status_code == 303
        assert response.headers["location"] == console.CONSOLE_PATH

    async def test_the_overview_page_shows_upgrade_buttons_when_stripe_is_configured(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        stripe = StripeSettings(
            secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t", price_scale="price_s"
        )
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(console.CONSOLE_PATH)
        assert "Upgrade to Team" in response.text
        assert "Upgrade to Scale" in response.text

    async def test_the_overview_page_shows_no_billing_block_when_stripe_is_not_configured(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(console.CONSOLE_PATH)
        assert "Upgrade to" not in response.text
        assert "Manage billing" not in response.text

    async def test_the_overview_page_offers_manage_billing_for_a_subscribed_org(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.stripe_customer_id = "cus_1"
            org.stripe_subscription_id = "sub_1"
            org.plan = "team"
        stripe = StripeSettings(secret_key="sk_test", webhook_secret="whsec_test", price_team="price_t")
        async with _client(_app(session_factory=session_factory, stripe=stripe)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(console.CONSOLE_PATH)
        assert "Manage billing" in response.text
        assert "Upgrade to Team" not in response.text


# --- Users & API Keys: the one deliberate exception to read-only -----------
#
# These pages are gated on `admin` scope, checked fresh on every request
# (not baked into the session cookie), plus an explicit org-ownership check
# on every id-addressed mutation -- auth.revoke_api_key/rotate_api_key take
# only a bare id and trust a cross-tenant operator caller to have already
# scoped it, which a customer's own browser session has not.


class TestAdminScopeGatesMutation:
    async def test_a_read_only_key_can_view_the_users_page(
        self, session_factory, org_and_readonly_key
    ):
        _org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/users")
        assert response.status_code == 200
        assert "admin-scoped" in response.text

    async def test_a_read_only_key_cannot_create_a_user(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/users/create",
                data={"email": "nope@example.com", "role": rbac.ROLE_VIEWER},
            )
        async with session_scope(session_factory) as session:
            count = await session.scalar(
                select(User).where(User.org_id == org_id).limit(1)
            )
        assert count is None

    async def test_a_read_only_key_cannot_issue_a_key(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read", "write"], "expires_days": "30"},
            )
        assert "shown once" not in response.text
        async with session_scope(session_factory) as session:
            keys = (
                await session.execute(select(ApiKey).where(ApiKey.org_id == org_id))
            ).scalars().all()
        # Only the readonly key this fixture itself issued -- nothing new.
        assert len(keys) == 1

    async def test_a_read_only_key_cannot_disable_a_user(
        self, session_factory, org_and_key
    ):
        org_id, _full_raw_key = org_and_key
        async with session_scope(session_factory) as session:
            user = User(org_id=org_id, email="a@example.com", role=rbac.ROLE_VIEWER)
            session.add(user)
            await session.flush()
            user_id = user.id
            readonly_issued = await auth.issue_api_key(session, org_id, scopes=["read"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, readonly_issued.raw_key)
            await client.post(f"{console.CONSOLE_PATH}/users/{user_id}/disable")
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
        assert row.disabled_at is None

    async def test_scope_narrowing_takes_effect_without_a_new_sign_in(
        self, session_factory, org_and_key
    ):
        """The console re-fetches the key's scopes fresh on every request
        (like the existing revocation-liveness check) rather than trusting
        what was true at sign-in time -- an admin-scoped session narrowed
        to read-only mid-session must lose console-mutation access on its
        very next request."""
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            still_admin = await client.get(f"{console.CONSOLE_PATH}/users")
            assert "Create a user" in still_admin.text
            async with session_scope(session_factory) as session:
                await session.execute(
                    sa_update(ApiKey).where(ApiKey.org_id == org_id).values(scopes=["read"])
                )
            narrowed = await client.get(f"{console.CONSOLE_PATH}/users")
            assert "Create a user" not in narrowed.text
            assert "admin-scoped" in narrowed.text


class TestUserManagement:
    async def test_creating_a_user_through_the_console(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/users/create",
                data={"email": "ana@example.com", "role": rbac.ROLE_ANALYST},
                follow_redirects=True,
            )
        assert "ana@example.com" in response.text
        async with session_scope(session_factory) as session:
            user = (
                await session.execute(select(User).where(User.org_id == org_id))
            ).scalar_one()
        assert user.email == "ana@example.com"
        assert user.role == rbac.ROLE_ANALYST
        # Audited with the ACTUAL console credential, not a borrowed
        # operator-cli label -- see manage.create_user's threaded actor.
        assert user.created_by.startswith("api-key:")

    async def test_setting_a_users_role(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            user = User(org_id=org_id, email="b@example.com", role=rbac.ROLE_VIEWER)
            session.add(user)
            await session.flush()
            user_id = user.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/users/{user_id}/role",
                data={"role": rbac.ROLE_CURATOR},
            )
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
        assert row.role == rbac.ROLE_CURATOR

    async def test_disabling_and_enabling_a_user(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            user = User(org_id=org_id, email="c@example.com", role=rbac.ROLE_VIEWER)
            session.add(user)
            await session.flush()
            user_id = user.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/users/{user_id}/disable")
            async with session_scope(session_factory) as session:
                row = await session.get(User, user_id)
            assert row.disabled_at is not None
            await client.post(f"{console.CONSOLE_PATH}/users/{user_id}/enable")
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
        assert row.disabled_at is None

    async def test_cannot_set_the_role_of_another_orgs_user(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            user = User(org_id=other_org_id, email="d@example.com", role=rbac.ROLE_VIEWER)
            session.add(user)
            await session.flush()
            user_id = user.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/users/{user_id}/role",
                data={"role": rbac.ROLE_OWNER},
            )
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
        assert row.role == rbac.ROLE_VIEWER  # unchanged

    async def test_cannot_disable_another_orgs_user(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            user = User(org_id=other_org_id, email="e@example.com", role=rbac.ROLE_VIEWER)
            session.add(user)
            await session.flush()
            user_id = user.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/users/{user_id}/disable")
        async with session_scope(session_factory) as session:
            row = await session.get(User, user_id)
        assert row.disabled_at is None


class TestApiKeyManagement:
    async def test_issuing_a_key_shows_the_raw_key_once(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read", "write"], "expires_days": "30"},
            )
        assert "shown once" in response.text
        assert "ct_" in response.text

    async def test_an_issued_keys_scopes_persist(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
            )
        async with session_scope(session_factory) as session:
            issued = (
                await session.execute(
                    select(ApiKey).where(ApiKey.org_id == org_id).order_by(ApiKey.created_at.desc())
                )
            ).scalars().first()
        assert issued.scopes == ["read"]

    async def test_issuing_with_no_scopes_checked_issues_nothing(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/keys/issue", data={})
        async with session_scope(session_factory) as session:
            keys = (
                await session.execute(select(ApiKey).where(ApiKey.org_id == org_id))
            ).scalars().all()
        assert len(keys) == 1  # only the fixture's own signing-in key

    async def test_revoking_a_key(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org_id, scopes=["read"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/keys/{issued.key_id}/revoke")
        async with session_scope(session_factory) as session:
            row = await session.get(ApiKey, issued.key_id)
        assert row.revoked_at is not None

    async def test_rotating_a_key_shows_the_new_raw_key_once(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, org_id, scopes=["read"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/keys/{issued.key_id}/rotate")
        assert "shown once" in response.text
        async with session_scope(session_factory) as session:
            old_row = await session.get(ApiKey, issued.key_id)
        assert old_row.revoked_at is not None

    async def test_cannot_revoke_another_orgs_key(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, other_org_id, scopes=["read"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/keys/{issued.key_id}/revoke")
        async with session_scope(session_factory) as session:
            row = await session.get(ApiKey, issued.key_id)
        assert row.revoked_at is None

    async def test_cannot_rotate_another_orgs_key(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            issued = await auth.issue_api_key(session, other_org_id, scopes=["read"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/keys/{issued.key_id}/rotate")
        assert "shown once" not in response.text
        async with session_scope(session_factory) as session:
            row = await session.get(ApiKey, issued.key_id)
        assert row.revoked_at is None


class TestUsersAndKeysAreEscaped:
    async def test_a_users_display_name_cannot_inject_script(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        payload = '<script>alert("xss")</script>'
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/users/create",
                data={"email": "x@example.com", "role": rbac.ROLE_VIEWER, "display_name": payload},
            )
            response = await client.get(f"{console.CONSOLE_PATH}/users")
        assert "<script>alert" not in response.text


class TestAlertRuleManagement:
    async def test_creating_a_rule_through_the_console(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/alerts/create",
                data={
                    "metric": alerts_module.METRIC_QUARANTINE_RATE,
                    "comparator": alerts_module.COMPARATOR_GT,
                    "threshold": "0.2",
                    "cooldown_minutes": "30",
                },
                follow_redirects=True,
            )
        assert alerts_module.METRIC_QUARANTINE_RATE in response.text
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, org_id)
        assert len(rules) == 1
        assert rules[0].threshold == 0.2
        assert rules[0].cooldown_minutes == 30
        # Audited with the actual console credential, not operator-cli.
        assert rules[0].created_by.startswith("api-key:")

    async def test_an_unknown_metric_is_refused_with_an_inline_error(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/alerts/create",
                data={
                    "metric": "not_a_real_metric", "comparator": alerts_module.COMPARATOR_GT,
                    "threshold": "0.2",
                },
            )
        assert "unknown metric" in response.text
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, org_id)
        assert rules == []

    async def test_deleting_a_rule(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            rule = await alerts_module.create_rule(
                session, org_id, alerts_module.METRIC_QUARANTINE_RATE,
                alerts_module.COMPARATOR_GT, 0.5,
            )
            rule_id = rule.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/alerts/{rule_id}/delete")
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, org_id)
        assert rules == []

    async def test_a_read_only_key_cannot_create_a_rule(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/alerts/create",
                data={
                    "metric": alerts_module.METRIC_QUARANTINE_RATE,
                    "comparator": alerts_module.COMPARATOR_GT, "threshold": "0.2",
                },
            )
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, org_id)
        assert rules == []

    async def test_cannot_delete_another_orgs_rule(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            rule = await alerts_module.create_rule(
                session, other_org_id, alerts_module.METRIC_QUARANTINE_RATE,
                alerts_module.COMPARATOR_GT, 0.5,
            )
            rule_id = rule.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/alerts/{rule_id}/delete")
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, other_org_id)
        assert len(rules) == 1


class TestUsageReportFromTheConsole:
    async def test_generating_a_report_shows_the_summary(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/alerts/generate-report")
        assert response.status_code == 200
        assert "Report queued" in response.text
        assert "report.generated" in response.text

    async def test_generating_a_report_queues_a_webhook_delivery(
        self, session_factory, org_and_key
    ):
        from hub import events as events_module
        from hub.models import WebhookDelivery

        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            await events_module.add_endpoint(
                session, org_id, "https://example.invalid/hooks/commontrace",
                signing_key="test-signing-key",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/alerts/generate-report")
        async with session_scope(session_factory) as session:
            deliveries = (
                await session.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.org_id == org_id,
                        WebhookDelivery.event_type == "report.generated",
                    )
                )
            ).scalars().all()
        assert len(deliveries) == 1

    async def test_a_read_only_key_cannot_generate_a_report(
        self, session_factory, org_and_readonly_key
    ):
        _org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/alerts/generate-report")
        assert "Report queued" not in response.text

    async def test_generating_a_report_is_not_reachable_by_get(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/alerts/generate-report")
        assert response.status_code == 405
