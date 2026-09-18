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

    async def test_overview_totals_cover_every_org_not_just_the_rendered_page(
        self, session_factory, monkeypatch
    ):
        """`traces_by_org`/`quarantined_by_org`/`keys_by_org` are already
        unrestricted, fleet-wide GROUP BY queries -- but the top-line
        `total_traces`/`total_quarantined`/`total_keys` tiles used to be
        summed from `rows`, which is truncated to the first `_MAX_ROWS`
        organizations by name. Once a deployment had more orgs than that,
        the three summary tiles silently undercounted, with no truncation
        notice anywhere near them (the one that exists sits below the
        per-org table). Monkeypatching `_MAX_ROWS` down to 1 makes this
        reproducible with two orgs instead of two hundred and one."""
        from hub import auth as hub_auth
        from hub.db import session_scope
        from hub.models import Organization, Trace

        monkeypatch.setattr(admin, "_MAX_ROWS", 1)

        async with session_scope(session_factory) as session:
            org_a = Organization(name="Org A", plan="team")
            org_b = Organization(name="Org B", plan="team")
            session.add_all([org_a, org_b])
            await session.flush()
            for org in (org_a, org_b):
                session.add(Trace(
                    org_id=org.id, title="t", context_text="c", solution_text="s",
                    tags=[], agent_type="support", agent_id="w1",
                    quarantined=True, quarantine_reason="spam",
                ))
                await hub_auth.issue_api_key(session, org.id, expires_days=90)

        async with session_scope(session_factory) as session:
            overview = await admin._overview(session)

        # Only one org's row is actually rendered ...
        assert len(overview["orgs"]) == 1
        assert overview["truncated"] is True
        # ... but both orgs' traces/quarantines/keys must still count.
        assert overview["total_traces"] == 2
        assert overview["total_quarantined"] == 2
        assert overview["total_keys"] == 2

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

    async def test_the_console_states_which_actions_it_will_and_will_not_take(
        self, session_factory
    ):
        """An operator must not be left wondering whether a click here
        changed something -- and the claim has to stay true as the console
        grows. The rule is reversibility, and the page says so."""
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert "reversibility" in r.text
        assert "Creating an organization is additive and reversible" in r.text

    async def test_the_overview_page_offers_only_org_creation(self, session_factory):
        """Creating an organization is additive and reversible (there is
        nothing yet on a brand-new org for a mistaken click to lose), so it
        is the one button on this page. Every other action needs an
        existing organization to act on and lives on that org's own page."""
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        lowered = r.text.lower()
        assert 'action="/admin/create-org"' in lowered
        for forbidden in (
            "/purge", "retention/", "/issue", "generate-encryption-key",
            "/legal-hold", "/quarantine", "/set-plan", "fetch(", "xmlhttprequest",
        ):
            assert forbidden not in lowered, f"{forbidden} on the overview page"

    async def test_the_org_page_offers_only_reversible_actions(self, session_factory):
        """The line is reversibility, not squeamishness. Irreversible and
        credential-bearing actions -- purge-org, issue-key,
        generate-encryption-key, the digest-confirmed retention-apply --
        stay in the CLI, so no form on this page may target them. Legal
        holds, retention policy, and quarantine release ARE reversible and
        are deliberately buttons here."""
        from hub import retention as retention_module
        from hub.db import session_scope

        org_id, _ = await self._seed(session_factory)
        # Placed directly rather than through the console under test, so a
        # release/clear form has an existing row to target -- both only
        # render for a row that exists.
        async with session_scope(session_factory) as session:
            await retention_module.place_hold(
                session, org_id, reason="litigation", placed_by="test-setup")
            await retention_module.set_policy(session, org_id, "trace", 90)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        lowered = r.text.lower()
        for allowed_action in (
            "quarantine/release", "legal-hold/place", "legal-hold/release",
            "retention/set", "retention/clear", "set-plan",
        ):
            assert f'action="/admin/org/{org_id}/{allowed_action}"'.lower() in lowered
        for forbidden in (
            "/purge", "retention/apply", "/issue", "generate-encryption-key",
            "fetch(", "xmlhttprequest",
        ):
            assert forbidden not in lowered, f"{forbidden} reachable from the org page"

    async def test_no_destructive_action_is_reachable_from_any_page(self, session_factory):
        """The irreversible commands must never become a POST target. If one
        of these ever appears in a form action, this test is the thing that
        says so."""
        org_id, _ = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            pages = [
                await c.get("/admin", headers=_basic("op", "s3cret")),
                await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret")),
                await c.get("/admin/kb", headers=_basic("op", "s3cret")),
            ]
        for r in pages:
            for path in ("/admin/purge", "/admin/org/delete", "/admin/key", "/admin/issue"):
                assert f'action="{path}' not in r.text

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


# --- Knowledge Base moderation ----------------------------------------------


def _kb_app(session_factory, *, operator_org_id: str = "", token: str = "s3cret") -> Starlette:
    app = Starlette()
    admin.add_admin_routes(
        app, session_factory, admin_token=token,
        rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
        operator_org_id=operator_org_id,
    )
    return app


class TestKnowledgeBaseIsTheOnlyExchange:
    """Orgs never exchange anything with each other -- a fleet's traces stay
    private to that fleet. The Knowledge Base is the single surface where
    content crosses an org boundary, and it does so by passing through an
    operator: a customer PROPOSES, an operator PUBLISHES, and what gets
    published is owned by the operator's org rather than the submitter's."""

    async def _submit(self, session_factory, *, title="Stripe webhooks need idempotency keys"):
        from hub.db import session_scope
        from hub.models import KnowledgeBaseSubmission, Organization

        async with session_scope(session_factory) as session:
            submitter = Organization(name="Submitting Co", plan="team")
            operator = Organization(name="Operator", plan="operator")
            session.add_all([submitter, operator])
            await session.flush()
            sub = KnowledgeBaseSubmission(
                org_id=submitter.id, title=title,
                context_text="A webhook is delivered more than once.",
                solution_text="Key the handler on the event id.",
                tags=["webhooks"], agent_type="code",
                rationale="Vendor behaviour, not our business logic.",
                status="pending",
            )
            session.add(sub)
            await session.flush()
            return sub.id, submitter.id, operator.id

    async def test_the_page_states_the_boundary(self, session_factory):
        """An operator should be able to see, without opening the source,
        that orgs do not share with each other."""
        async with _client(_kb_app(session_factory)) as c:
            r = await c.get("/admin/kb", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "Orgs never exchange anything with each other" in r.text
        assert "operator" in r.text.lower()

    async def test_a_pending_proposal_is_shown_with_who_proposed_it(self, session_factory):
        """Accepting credits that org's allowance, so the reviewer has to see
        which org it was -- a field the customer-facing projection rightly
        omits."""
        sub_id, submitter_id, _ = await self._submit(session_factory)
        async with _client(_kb_app(session_factory)) as c:
            r = await c.get("/admin/kb", headers=_basic("op", "s3cret"))
        assert "Stripe webhooks need idempotency keys" in r.text
        assert submitter_id in r.text
        assert sub_id in r.text

    async def test_accepting_publishes_under_the_operator_org_never_the_submitter(
        self, session_factory
    ):
        """The ownership rule that makes this not org-to-org sharing."""
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import Trace

        sub_id, submitter_id, operator_id = await self._submit(session_factory)
        app = _kb_app(session_factory, operator_org_id=operator_id)
        async with _client(app) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", sub_id),
            })
        assert r.status_code == 303

        async with session_scope(session_factory) as session:
            published = (await session.execute(
                select(Trace).where(Trace.commons_source == "seed")
            )).scalars().all()
        assert len(published) == 1
        assert published[0].org_id == operator_id
        assert published[0].org_id != submitter_id

    async def test_accepting_fails_closed_without_an_operator_org(self, session_factory):
        """Publishing under the wrong org would put a customer's id on
        Knowledge Base content -- the one mistake this boundary exists to
        prevent, and not one a UI should be able to make."""
        sub_id, _, _ = await self._submit(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="")) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", sub_id),
            })
        assert r.status_code == 409
        assert "HUB_OPERATOR_ORG_ID" in r.text

    async def test_declining_publishes_nothing_and_awards_nothing(self, session_factory):
        """That silence is the adverse-selection defence."""
        from sqlalchemy import func, select

        from hub.db import session_scope
        from hub.models import Organization, Trace

        sub_id, submitter_id, operator_id = await self._submit(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id=operator_id)) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "reject", "reason": "too specific",
                "csrf": admin._csrf_token("s3cret", "reject", sub_id),
            })
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            assert int(await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.commons_source == "seed")
            ) or 0) == 0
            submitter = await session.get(Organization, submitter_id)
            assert submitter.bonus_commons_queries == 0


class TestModerationIsCsrfProtected:
    """Auth here is HTTP Basic, and a browser re-sends those credentials on a
    cross-site form POST -- so a mutating endpoint without a token is
    forgeable by any page the operator visits while authenticated."""

    async def _pending(self, session_factory):
        from hub.db import session_scope
        from hub.models import KnowledgeBaseSubmission, Organization

        async with session_scope(session_factory) as session:
            org = Organization(name="Sub Co", plan="team")
            session.add(org)
            await session.flush()
            sub = KnowledgeBaseSubmission(
                org_id=org.id, title="t", context_text="c", solution_text="s",
                tags=[], agent_type="code", rationale="r", status="pending",
            )
            session.add(sub)
            await session.flush()
            return sub.id

    async def test_a_post_without_a_token_is_refused(self, session_factory):
        sub_id = await self._pending(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"),
                             data={"submission_id": sub_id, "decision": "approve"})
        assert r.status_code == 403

    async def test_a_token_for_one_action_cannot_be_replayed_as_another(self, session_factory):
        """The token binds the action to its target: a 'decline' token must
        not approve anything."""
        sub_id = await self._pending(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "reject", sub_id),
            })
        assert r.status_code == 403

    async def test_a_token_for_one_target_cannot_be_replayed_on_another(self, session_factory):
        sub_id = await self._pending(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", "some-other-id"),
            })
        assert r.status_code == 403

    async def test_a_cross_site_post_is_refused_before_anything_else(self, session_factory):
        sub_id = await self._pending(session_factory)
        headers = {**_basic("op", "s3cret"), "Sec-Fetch-Site": "cross-site"}
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/review", headers=headers, data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", sub_id),
            })
        assert r.status_code == 403

    async def test_an_unauthenticated_post_is_challenged_not_executed(self, session_factory):
        sub_id = await self._pending(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/review", data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", sub_id),
            })
        assert r.status_code == 401

    async def test_the_token_is_unforgeable_without_the_admin_secret(self):
        assert admin._csrf_token("secret-a", "approve", "x") != admin._csrf_token(
            "secret-b", "approve", "x")
        assert not admin._csrf_ok("secret-a", "approve", "x",
                                  admin._csrf_token("secret-b", "approve", "x"))
        assert not admin._csrf_ok("secret-a", "approve", "x", "")


class TestRetractionIsReversible:
    """Retract and restore are the pair that makes moderation safe to do from
    a browser at all: the worst outcome of a wrong click is undoing it."""

    async def _published(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            op = Organization(name="Operator", plan="operator")
            session.add(op)
            await session.flush()
            t = Trace(
                org_id=op.id, title="Published entry", context_text="c", solution_text="s",
                tags=[], agent_type="code", shared_with_commons=True, commons_source="seed",
            )
            session.add(t)
            await session.flush()
            return t.id

    async def test_retract_then_restore_round_trips(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        trace_id = await self._published(session_factory)
        app = _kb_app(session_factory, operator_org_id="x")

        async with _client(app) as c:
            r = await c.post("/admin/kb/retract", headers=_basic("op", "s3cret"), data={
                "trace_id": trace_id, "reason": "superseded",
                "csrf": admin._csrf_token("s3cret", "retract", trace_id),
            })
            assert r.status_code == 303
            async with session_scope(session_factory) as session:
                assert (await session.get(Trace, trace_id)).commons_retracted_at is not None

            r = await c.post("/admin/kb/restore", headers=_basic("op", "s3cret"), data={
                "trace_id": trace_id,
                "csrf": admin._csrf_token("s3cret", "restore", trace_id),
            })
            assert r.status_code == 303
            async with session_scope(session_factory) as session:
                assert (await session.get(Trace, trace_id)).commons_retracted_at is None

    async def test_a_decision_redirects_so_a_reload_cannot_repeat_it(self, session_factory):
        trace_id = await self._published(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.post("/admin/kb/retract", headers=_basic("op", "s3cret"), data={
                "trace_id": trace_id,
                "csrf": admin._csrf_token("s3cret", "retract", trace_id),
            })
        assert r.status_code == 303
        assert r.headers["location"].startswith("/admin/kb?done=")

    async def test_every_console_decision_is_audited_as_such(self, session_factory):
        """"Who published this entry" must be answerable after the fact, and a
        console decision distinguishable from a terminal one."""
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        trace_id = await self._published(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            await c.post("/admin/kb/retract", headers=_basic("op", "s3cret"), data={
                "trace_id": trace_id, "reason": "bad advice",
                "csrf": admin._csrf_token("s3cret", "retract", trace_id),
            })
        async with session_scope(session_factory) as session:
            actors = (await session.execute(select(AuditLogEntry.actor))).scalars().all()
        assert admin._ADMIN_ACTOR in actors


class TestOrgScopedMutationsAreReversible:
    """Legal holds, retention policy, and quarantine release: the org page's
    own set of reversible actions (hub/admin.py's module docstring). Every
    one of these can be undone by a second click, which is what makes it
    safe to expose here at all -- unlike purge-org or retention-apply,
    which stay in the CLI.
    """

    async def _seed(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            trace = Trace(
                org_id=org.id, title="A trace", context_text="c", solution_text="s",
                tags=[], agent_type="support", agent_id="w1",
                quarantined=True, quarantine_reason="looked like spam",
            )
            session.add(trace)
            await session.flush()
            return org.id, trace.id

    async def test_releasing_a_quarantine(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/quarantine/release", headers=_basic("op", "s3cret"),
                data={
                    "trace_id": trace_id,
                    "csrf": admin._csrf_token("s3cret", "release_quarantine", trace_id),
                },
            )
        assert r.status_code == 303
        assert r.headers["location"].startswith(f"/admin/org/{org_id}?done=")
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
        assert trace.quarantined is False
        assert trace.quarantine_reason == ""

    async def test_releasing_a_quarantine_wrong_csrf_token_is_refused(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/quarantine/release", headers=_basic("op", "s3cret"),
                data={"trace_id": trace_id, "csrf": "wrong"},
            )
        assert r.status_code == 403
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
        assert trace.quarantined is True

    async def test_placing_and_releasing_a_legal_hold_round_trips(self, session_factory):
        from hub import retention as retention_module
        from hub.db import session_scope

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/legal-hold/place", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "reason": "litigation hold",
                    "csrf": admin._csrf_token("s3cret", "place_hold", org_id),
                },
            )
            assert r.status_code == 303
            async with session_scope(session_factory) as session:
                holds = await retention_module.active_holds(session, org_id)
            assert len(holds) == 1
            hold_id = holds[0].id

            r = await c.post(
                f"/admin/org/{org_id}/legal-hold/release", headers=_basic("op", "s3cret"),
                data={
                    "hold_id": hold_id,
                    "csrf": admin._csrf_token("s3cret", "release_hold", hold_id),
                },
            )
            assert r.status_code == 303
        async with session_scope(session_factory) as session:
            holds = await retention_module.active_holds(session, org_id)
        assert holds == []

    async def test_setting_and_clearing_a_retention_policy_round_trips(self, session_factory):
        from hub import retention as retention_module

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/retention/set", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "object_type": "trace", "status": "any", "days": "90",
                    "csrf": admin._csrf_token("s3cret", "set_retention", org_id),
                },
            )
            assert r.status_code == 303
            from hub.db import session_scope
            async with session_scope(session_factory) as session:
                policies = await retention_module.policies_for(session, org_id)
            assert len(policies) == 1 and policies[0].max_age_days == 90

            r = await c.post(
                f"/admin/org/{org_id}/retention/clear", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "object_type": "trace", "status": "any",
                    "target": "trace/any",
                    "csrf": admin._csrf_token("s3cret", f"clear_retention:{org_id}", "trace/any"),
                },
            )
            assert r.status_code == 303
        from hub.db import session_scope
        async with session_scope(session_factory) as session:
            policies = await retention_module.policies_for(session, org_id)
        assert policies == []

    async def test_an_unknown_object_type_is_refused_with_a_flash_message(self, session_factory):
        from urllib.parse import unquote

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/retention/set", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "object_type": "not-a-real-kind", "days": "90",
                    "csrf": admin._csrf_token("s3cret", "set_retention", org_id),
                },
            )
        assert r.status_code == 303
        assert "Could not set policy" in unquote(r.headers["location"])

    async def test_org_scoped_mutations_are_audited_as_operator_console(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                f"/admin/org/{org_id}/quarantine/release", headers=_basic("op", "s3cret"),
                data={
                    "trace_id": trace_id,
                    "csrf": admin._csrf_token("s3cret", "release_quarantine", trace_id),
                },
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "release_quarantine")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR

    async def test_wrong_password_is_rejected_on_a_mutating_route(self, session_factory):
        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/quarantine/release", headers=_basic("op", "wrong"),
                data={
                    "trace_id": trace_id,
                    "csrf": admin._csrf_token("s3cret", "release_quarantine", trace_id),
                },
            )
        assert r.status_code == 401

    async def test_changing_a_plan(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/set-plan", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "plan_name": "team",
                    "csrf": admin._csrf_token("s3cret", "set_plan", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.plan == "team"

    async def test_an_unknown_plan_is_refused(self, session_factory):
        from urllib.parse import unquote

        from hub.db import session_scope
        from hub.models import Organization

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/set-plan", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "plan_name": "not-a-real-plan",
                    "csrf": admin._csrf_token("s3cret", "set_plan", org_id),
                },
            )
        assert r.status_code == 303
        assert "Unknown plan" in unquote(r.headers["location"])
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.plan != "not-a-real-plan"


class TestCreateOrgFromTheConsole:
    async def test_creating_an_organization(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import Organization

        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/create-org", headers=_basic("op", "s3cret"),
                data={
                    "name": "Brand New Org", "target": "new",
                    "csrf": admin._csrf_token("s3cret", "create_org", "new"),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            org = (
                await session.execute(select(Organization).where(Organization.name == "Brand New Org"))
            ).scalars().first()
        assert org is not None
        assert r.headers["location"].startswith(f"/admin/org/{org.id}")

    async def test_an_empty_name_is_refused(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import Organization

        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/create-org", headers=_basic("op", "s3cret"),
                data={"name": "", "target": "new", "csrf": admin._csrf_token("s3cret", "create_org", "new")},
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            count = len((await session.execute(select(Organization))).scalars().all())
        assert count == 0

    async def test_creation_is_audited_as_operator_console(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                "/admin/create-org", headers=_basic("op", "s3cret"),
                data={
                    "name": "Audited Org", "target": "new",
                    "csrf": admin._csrf_token("s3cret", "create_org", "new"),
                },
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "create_org")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR

    async def test_wrong_csrf_token_is_refused(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import Organization

        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/create-org", headers=_basic("op", "s3cret"),
                data={"name": "Should Not Exist", "target": "new", "csrf": "wrong"},
            )
        assert r.status_code == 403
        async with session_scope(session_factory) as session:
            count = len((await session.execute(select(Organization))).scalars().all())
        assert count == 0
