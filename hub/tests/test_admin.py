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


def _app(token: str = "s3cret", session_factory=_explode, config: HubConfig | None = None) -> Starlette:
    app = Starlette()
    admin.add_admin_routes(
        app, session_factory, admin_token=token,
        rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
        config=config,
    )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestConsoleIsAbsentUnlessConfigured:
    async def test_no_routes_are_registered_without_a_token(self):
        app = Starlette()
        async with _client(app) as c:
            for path in ("/admin", "/admin/kb", "/admin/org/whatever"):
                assert (await c.get(path)).status_code == 404, path

    async def test_registering_with_an_empty_token_is_refused(self):
        with pytest.raises(ValueError, match="non-empty admin token"):
            admin.add_admin_routes(Starlette(), _explode, admin_token="")

    async def test_the_config_default_leaves_it_off(self, monkeypatch):
        monkeypatch.delenv("HUB_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("HUB_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        assert HubConfig.from_env().admin_token == ""


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
        assert admin._authorized(_FakeRequest(_basic("anyone-at-all", "s3cret")), "s3cret")

    async def test_an_unauthenticated_request_never_touches_the_database(self):
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


class TestEscaping:
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

        assert len(overview["orgs"]) == 1
        assert overview["truncated"] is True
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
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin/org/not-a-uuid", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "No such organization" in r.text

    async def test_pages_are_not_cached(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert "no-store" in r.headers["cache-control"].split(", ")
        assert "script-src" in r.headers["content-security-policy"]

    async def test_the_console_states_which_actions_it_will_and_will_not_take(
        self, session_factory
    ):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert "reversibility" in r.text
        assert "retyping the exact id/name being destroyed" in r.text

    async def test_the_overview_page_offers_org_creation_and_key_generation(
        self, session_factory
    ):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        lowered = r.text.lower()
        assert 'action="/admin/create-org"' in lowered
        assert 'action="/admin/generate-encryption-key"' in lowered
        for forbidden in ("fetch(", "xmlhttprequest"):
            assert forbidden not in lowered, f"{forbidden} on the overview page"

    async def test_the_org_page_offers_its_full_reversible_and_confirmed_action_set(
        self, session_factory
    ):
        from hub import retention as retention_module
        from hub.db import session_scope
        from hub.models import User

        org_id, _ = await self._seed(session_factory)
        async with session_scope(session_factory) as session:
            await retention_module.place_hold(
                session, org_id, reason="litigation", placed_by="test-setup")
            await retention_module.set_policy(session, org_id, "trace", 90)
            session.add(User(
                org_id=org_id, email="linked@example.com", role="viewer",
                issuer="https://idp.example", external_subject="sub-1",
            ))
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        lowered = r.text.lower()
        for allowed_action in (
            "quarantine/release", "legal-hold/place", "legal-hold/release",
            "retention/set", "retention/clear", "set-plan",
            "users/create", "users/set-role", "users/disable", "users/unlink-sso",
            "tag-subjects", "amend-trace", "keys/issue", "keys/rotate", "keys/revoke",
            "purge-trace", "purge", "purge-subject-traces",
        ):
            assert f'action="/admin/org/{org_id}/{allowed_action}"'.lower() in lowered
        for confirm_field in ("confirm_trace_id", "confirm_name", "confirm_subject_id"):
            assert f'name="{confirm_field}"' in lowered
        for forbidden in ("fetch(", "xmlhttprequest"):
            assert forbidden not in lowered, f"{forbidden} reachable from the org page"

    async def test_no_destructive_action_is_reachable_from_any_page(self, session_factory):
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


def _kb_app(session_factory, *, operator_org_id: str = "", token: str = "s3cret") -> Starlette:
    app = Starlette()
    admin.add_admin_routes(
        app, session_factory, admin_token=token,
        rate_limiter=RateLimiter(per_minute=10_000, burst=10_000),
        operator_org_id=operator_org_id,
    )
    return app


class TestKnowledgeBaseIsTheOnlyExchange:
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
        async with _client(_kb_app(session_factory)) as c:
            r = await c.get("/admin/kb", headers=_basic("op", "s3cret"))
        assert r.status_code == 200
        assert "Orgs never exchange anything with each other" in r.text
        assert "operator" in r.text.lower()

    async def test_a_pending_proposal_is_shown_with_who_proposed_it(self, session_factory):
        sub_id, submitter_id, _ = await self._submit(session_factory)
        async with _client(_kb_app(session_factory)) as c:
            r = await c.get("/admin/kb", headers=_basic("op", "s3cret"))
        assert "Stripe webhooks need idempotency keys" in r.text
        assert submitter_id in r.text
        assert sub_id in r.text

    async def test_accepting_publishes_under_the_operator_org_never_the_submitter(
        self, session_factory
    ):
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
        sub_id, _, _ = await self._submit(session_factory)
        async with _client(_kb_app(session_factory, operator_org_id="")) as c:
            r = await c.post("/admin/kb/review", headers=_basic("op", "s3cret"), data={
                "submission_id": sub_id, "decision": "approve",
                "csrf": admin._csrf_token("s3cret", "approve", sub_id),
            })
        assert r.status_code == 409
        assert "HUB_OPERATOR_ORG_ID" in r.text

    async def test_declining_publishes_nothing_and_awards_nothing(self, session_factory):
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
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            shown = await c.get(r.headers["location"], headers=_basic("op", "s3cret"))
            assert '<div class="flash">Withdrawn.' in shown.text
            forged = await c.get("/admin/kb", params={"done": "Call +1-555-0100 now", "sig": "0" * 32},
                                 headers=_basic("op", "s3cret"))
            assert "555-0100" not in forged.text
            unsigned = await c.get("/admin/kb", params={"done": "Withdrawn."}, headers=_basic("op", "s3cret"))
            assert '<div class="flash">' not in unsigned.text

    async def test_every_console_decision_is_audited_as_such(self, session_factory):
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


class TestUserAndSubjectRightsManagement:
    async def _seed_with_user(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization, Trace, User

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            trace = Trace(
                org_id=org.id, title="A trace", context_text="c", solution_text="s",
                tags=[], agent_type="support", agent_id="w1", subject_ids=["cust-42"],
            )
            user = User(org_id=org.id, email="person@example.com", role="viewer")
            session.add_all([trace, user])
            await session.flush()
            return org.id, trace.id, user.id

    async def test_creating_a_user(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import User

        org_id, _trace_id, _user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/create", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "email": "new@example.com", "role": "analyst",
                    "csrf": admin._csrf_token("s3cret", "create_user", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            created = (
                await session.execute(select(User).where(User.email == "new@example.com"))
            ).scalars().first()
        assert created is not None
        assert created.role == "analyst"
        assert created.created_by == admin._ADMIN_ACTOR

    async def test_an_unknown_role_is_refused(self, session_factory):
        org_id, _trace_id, _user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/create", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "email": "x@example.com", "role": "not-a-role",
                    "csrf": admin._csrf_token("s3cret", "create_user", org_id),
                },
            )
        assert r.status_code == 303

    async def test_setting_a_users_role(self, session_factory):
        from hub.db import session_scope
        from hub.models import User

        org_id, _trace_id, user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/set-role", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id, "role": "curator",
                    "csrf": admin._csrf_token("s3cret", "set_user_role", user_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
        assert user.role == "curator"

    async def test_disabling_and_enabling_a_user_round_trips(self, session_factory):
        from hub.db import session_scope
        from hub.models import User

        org_id, _trace_id, user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/disable", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id,
                    "csrf": admin._csrf_token("s3cret", "disable_user", user_id),
                },
            )
            assert r.status_code == 303
            async with session_scope(session_factory) as session:
                user = await session.get(User, user_id)
            assert user.disabled_at is not None

            r = await c.post(
                f"/admin/org/{org_id}/users/enable", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id,
                    "csrf": admin._csrf_token("s3cret", "enable_user", user_id),
                },
            )
            assert r.status_code == 303
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
        assert user.disabled_at is None

    async def test_linking_and_unlinking_sso_round_trips(self, session_factory):
        from hub.db import session_scope
        from hub.models import User

        org_id, _trace_id, user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/link-sso", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id, "issuer": "https://idp.example", "external_subject": "sub-1",
                    "csrf": admin._csrf_token("s3cret", "link_sso", user_id),
                },
            )
            assert r.status_code == 303
            async with session_scope(session_factory) as session:
                user = await session.get(User, user_id)
            assert user.issuer == "https://idp.example"
            assert user.external_subject == "sub-1"

            r = await c.post(
                f"/admin/org/{org_id}/users/unlink-sso", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id,
                    "csrf": admin._csrf_token("s3cret", "unlink_sso", user_id),
                },
            )
            assert r.status_code == 303
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
        assert user.external_subject == ""

    async def test_linking_a_duplicate_identity_is_refused(self, session_factory):
        from hub.db import session_scope
        from hub.models import User

        org_id, _trace_id, user_id = await self._seed_with_user(session_factory)
        async with session_scope(session_factory) as session:
            other = User(
                org_id=org_id, email="other@example.com", role="viewer",
                issuer="https://idp.example", external_subject="taken",
            )
            session.add(other)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/users/link-sso", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id, "issuer": "https://idp.example", "external_subject": "taken",
                    "csrf": admin._csrf_token("s3cret", "link_sso", user_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            user = await session.get(User, user_id)
        assert user.external_subject == ""

    async def test_finding_traces_by_subject(self, session_factory):
        org_id, trace_id, _user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(
                f"/admin/org/{org_id}", headers=_basic("op", "s3cret"),
                params={"subject_id": "cust-42"},
            )
        assert r.status_code == 200
        assert trace_id in r.text

    async def test_tagging_a_traces_subjects(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id, _user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/tag-subjects", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": trace_id, "subject_ids": "cust-99, cust-100",
                    "csrf": admin._csrf_token("s3cret", "tag_trace_subjects", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, trace_id)
        assert sorted(trace.subject_ids) == ["cust-100", "cust-99"]

    async def test_user_mutations_are_audited_as_operator_console(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        org_id, _trace_id, user_id = await self._seed_with_user(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                f"/admin/org/{org_id}/users/disable", headers=_basic("op", "s3cret"),
                data={
                    "user_id": user_id,
                    "csrf": admin._csrf_token("s3cret", "disable_user", user_id),
                },
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "disable_user")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR


class TestKeyIssuanceFromTheConsole:
    async def _seed(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            return org.id

    async def test_issuing_a_key_shows_it_once(self, session_factory):
        org_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/keys/issue", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "scopes": ["read", "write"], "expires_days": "90",
                    "csrf": admin._csrf_token("s3cret", "issue_key", org_id),
                },
            )
        assert r.status_code == 200
        assert "shown once" in r.text
        assert "ct_" in r.text

    async def test_issuing_with_no_scopes_checked_issues_nothing(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import ApiKey

        org_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                f"/admin/org/{org_id}/keys/issue", headers=_basic("op", "s3cret"),
                data={"org_id": org_id, "csrf": admin._csrf_token("s3cret", "issue_key", org_id)},
            )
        async with session_scope(session_factory) as session:
            keys = (
                await session.execute(select(ApiKey).where(ApiKey.org_id == org_id))
            ).scalars().all()
        assert keys == []

    async def test_rotating_a_key_shows_the_new_one_once(self, session_factory):
        from hub import auth as auth_module
        from hub.db import session_scope

        org_id = await self._seed(session_factory)
        async with session_scope(session_factory) as session:
            issued = await auth_module.issue_api_key(session, org_id)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/keys/rotate", headers=_basic("op", "s3cret"),
                data={
                    "key_id": issued.key_id,
                    "csrf": admin._csrf_token("s3cret", "rotate_key", issued.key_id),
                },
            )
        assert r.status_code == 200
        assert "shown once" in r.text
        async with session_scope(session_factory) as session:
            from hub.models import ApiKey

            old_row = await session.get(ApiKey, issued.key_id)
        assert old_row.revoked_at is not None

    async def test_revoking_a_key(self, session_factory):
        from hub import auth as auth_module
        from hub.db import session_scope
        from hub.models import ApiKey

        org_id = await self._seed(session_factory)
        async with session_scope(session_factory) as session:
            issued = await auth_module.issue_api_key(session, org_id)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/keys/revoke", headers=_basic("op", "s3cret"),
                data={
                    "key_id": issued.key_id,
                    "csrf": admin._csrf_token("s3cret", "revoke_key", issued.key_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(ApiKey, issued.key_id)
        assert row.revoked_at is not None

    async def test_issued_and_rotated_keys_are_audited_as_operator_console(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        org_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                f"/admin/org/{org_id}/keys/issue", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "scopes": ["read"],
                    "csrf": admin._csrf_token("s3cret", "issue_key", org_id),
                },
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "issue_key")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR


class TestAmendingATraceFromTheConsole:
    async def _seed(self, session_factory) -> tuple[str, str]:
        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            trace = Trace(
                org_id=org.id, title="Original title", context_text="original context",
                solution_text="original solution", tags=["a"], agent_type="support",
            )
            session.add(trace)
            await session.flush()
            return org.id, trace.id

    async def test_amending_a_trace_creates_a_new_superseding_trace(self, session_factory, config):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry, Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory, config=config)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/amend-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": trace_id, "title": "Corrected title",
                    "csrf": admin._csrf_token("s3cret", "amend_trace", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            original = await session.get(Trace, trace_id)
            amended = (
                await session.execute(
                    select(Trace).where(Trace.supersedes_trace_id == trace_id)
                )
            ).scalars().one()
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "amend_trace")
                )
            ).scalars().first()
        assert original.superseded_at is not None
        assert original.superseded_by_trace_id == amended.id
        assert amended.title == "Corrected title"
        assert amended.context_text == "original context"
        assert amended.solution_text == "original solution"
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR

    async def test_amending_an_unknown_trace_id_is_a_no_op(self, session_factory, config):
        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory, config=config)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/amend-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": "00000000-0000-0000-0000-000000000000",
                    "title": "x", "csrf": admin._csrf_token("s3cret", "amend_trace", org_id),
                },
            )
        assert r.status_code == 303
        assert "No+such+trace" in r.headers["location"] or "No%20such%20trace" in r.headers["location"]

    async def test_a_fat_fingered_non_uuid_trace_id_is_a_clean_no_op_not_a_500(self, session_factory, config):
        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory, config=config)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/amend-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": "not-a-real-id", "title": "x",
                    "csrf": admin._csrf_token("s3cret", "amend_trace", org_id),
                },
            )
        assert r.status_code == 303
        assert "No+such+trace" in r.headers["location"] or "No%20such%20trace" in r.headers["location"]

    async def test_wrong_csrf_token_is_refused(self, session_factory):
        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/amend-trace", headers=_basic("op", "s3cret"),
                data={"org_id": org_id, "trace_id": trace_id, "title": "x", "csrf": "wrong"},
            )
        assert r.status_code == 403


class TestGeneratingAnEncryptionKey:
    async def test_generating_a_key(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/generate-encryption-key", headers=_basic("op", "s3cret"),
                data={"target": "new", "csrf": admin._csrf_token("s3cret", "generate_encryption_key", "new")},
            )
        assert r.status_code == 200
        assert "shown once" in r.text
        assert "HUB_ENCRYPTION_KEY" in r.text

    async def test_wrong_csrf_token_is_refused(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/generate-encryption-key", headers=_basic("op", "s3cret"),
                data={"target": "new", "csrf": "wrong"},
            )
        assert r.status_code == 403


class TestTheDangerZoneRequiresRetypedConfirmation:
    async def _seed(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme Corp", plan="team")
            session.add(org)
            await session.flush()
            trace = Trace(
                org_id=org.id, title="A trace", context_text="c", solution_text="s",
                tags=[], agent_type="support", agent_id="w1", subject_ids=["cust-1"],
            )
            session.add(trace)
            await session.flush()
            return org.id, trace.id

    async def test_purging_a_trace_with_the_correct_confirmation(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": trace_id, "confirm_trace_id": trace_id,
                    "csrf": admin._csrf_token("s3cret", "purge_trace", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is None

    async def test_purging_a_trace_with_a_mismatched_confirmation_is_refused(
        self, session_factory
    ):
        from urllib.parse import unquote

        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": trace_id, "confirm_trace_id": "not-the-id",
                    "csrf": admin._csrf_token("s3cret", "purge_trace", org_id),
                },
            )
        assert r.status_code == 303
        assert "did not match" in unquote(r.headers["location"])
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is not None

    async def test_purging_an_org_with_the_correct_confirmation(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "confirm_name": "Acme Corp",
                    "csrf": admin._csrf_token("s3cret", "purge_org", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(Organization, org_id)
        assert row is None

    async def test_purging_an_org_with_a_mismatched_confirmation_is_refused(
        self, session_factory
    ):
        from urllib.parse import unquote

        from hub.db import session_scope
        from hub.models import Organization

        org_id, _trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "confirm_name": "wrong name",
                    "csrf": admin._csrf_token("s3cret", "purge_org", org_id),
                },
            )
        assert r.status_code == 303
        assert "did not match" in unquote(r.headers["location"])
        async with session_scope(session_factory) as session:
            row = await session.get(Organization, org_id)
        assert row is not None

    async def test_purging_subject_traces_with_the_correct_confirmation(self, session_factory):
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge-subject-traces", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "subject_id": "cust-1", "confirm_subject_id": "cust-1",
                    "csrf": admin._csrf_token("s3cret", "purge_subject_traces", org_id),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is None

    async def test_purging_subject_traces_with_a_mismatched_confirmation_is_refused(
        self, session_factory
    ):
        from urllib.parse import unquote

        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/purge-subject-traces", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "subject_id": "cust-1", "confirm_subject_id": "cust-2",
                    "csrf": admin._csrf_token("s3cret", "purge_subject_traces", org_id),
                },
            )
        assert r.status_code == 303
        assert "did not match" in unquote(r.headers["location"])
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is not None

    async def test_purges_are_audited_as_operator_console(self, session_factory):
        from sqlalchemy import select

        from hub.db import session_scope
        from hub.models import AuditLogEntry

        org_id, trace_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            await c.post(
                f"/admin/org/{org_id}/purge-trace", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "trace_id": trace_id, "confirm_trace_id": trace_id,
                    "csrf": admin._csrf_token("s3cret", "purge_trace", org_id),
                },
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.action == "purge_trace")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor == admin._ADMIN_ACTOR


class TestRetentionApplyFromTheConsole:
    async def _seed_with_doomed_trace(self, session_factory):
        import datetime

        from hub.db import session_scope
        from hub.models import Organization, Trace

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
            trace = Trace(
                org_id=org.id, title="Stale", context_text="c", solution_text="s",
                tags=[], agent_type="support", agent_id="w1", created_at=old,
            )
            session.add(trace)
            await session.flush()
            return org.id, trace.id

    async def test_previewing_then_applying_deletes_the_doomed_rows(self, session_factory):
        from hub import retention as retention_module
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed_with_doomed_trace(session_factory)
        async with session_scope(session_factory) as session:
            await retention_module.set_policy(session, org_id, "trace", 30)
            plan = await retention_module.plan(session, org_id)
        assert plan.n_doomed >= 1
        digest = plan.digest
        async with _client(_app(session_factory=session_factory)) as c:
            preview = await c.get(
                f"/admin/org/{org_id}", headers=_basic("op", "s3cret"),
                params={"preview_retention": "1"},
            )
            assert digest in preview.text
            r = await c.post(
                f"/admin/org/{org_id}/retention/apply", headers=_basic("op", "s3cret"),
                data={
                    "digest": digest, "confirm_digest": digest,
                    "csrf": admin._csrf_token("s3cret", "retention_apply", digest),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is None

    async def test_a_mismatched_confirmation_is_refused(self, session_factory):
        from urllib.parse import unquote

        from hub import retention as retention_module
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed_with_doomed_trace(session_factory)
        async with session_scope(session_factory) as session:
            await retention_module.set_policy(session, org_id, "trace", 30)
            plan = await retention_module.plan(session, org_id)
        digest = plan.digest
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/retention/apply", headers=_basic("op", "s3cret"),
                data={
                    "digest": digest, "confirm_digest": "not-the-digest",
                    "csrf": admin._csrf_token("s3cret", "retention_apply", digest),
                },
            )
        assert r.status_code == 303
        assert "did not match" in unquote(r.headers["location"])
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is not None

    async def test_a_stale_plan_is_refused(self, session_factory):
        from hub import retention as retention_module
        from hub.db import session_scope
        from hub.models import Trace

        org_id, trace_id = await self._seed_with_doomed_trace(session_factory)
        async with session_scope(session_factory) as session:
            await retention_module.set_policy(session, org_id, "trace", 30)
            plan = await retention_module.plan(session, org_id)
        digest = plan.digest
        async with session_scope(session_factory) as session:
            import datetime

            old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
            session.add(Trace(
                org_id=org_id, title="Another stale one", context_text="c", solution_text="s",
                tags=[], agent_type="support", agent_id="w1", created_at=old,
            ))
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/retention/apply", headers=_basic("op", "s3cret"),
                data={
                    "digest": digest, "confirm_digest": digest,
                    "csrf": admin._csrf_token("s3cret", "retention_apply", digest),
                },
            )
        assert r.status_code == 303
        async with session_scope(session_factory) as session:
            row = await session.get(Trace, trace_id)
        assert row is not None


class TestAutoRefresh:
    async def _seed(self, session_factory):
        from hub.db import session_scope
        from hub.models import Organization

        async with session_scope(session_factory) as session:
            org = Organization(name="Acme", plan="team")
            session.add(org)
            await session.flush()
            return org.id

    async def test_the_overview_page_auto_refreshes(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get("/admin", headers=_basic("op", "s3cret"))
        assert "<script>" in r.text
        assert "location.reload" in r.text

    async def test_the_org_page_auto_refreshes(self, session_factory):
        org_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.get(f"/admin/org/{org_id}", headers=_basic("op", "s3cret"))
        assert "location.reload" in r.text

    async def test_the_kb_page_auto_refreshes(self, session_factory):
        async with _client(_kb_app(session_factory, operator_org_id="x")) as c:
            r = await c.get("/admin/kb", headers=_basic("op", "s3cret"))
        assert "location.reload" in r.text

    async def test_a_freshly_issued_key_disables_auto_refresh(self, session_factory):
        org_id = await self._seed(session_factory)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/keys/issue", headers=_basic("op", "s3cret"),
                data={
                    "org_id": org_id, "scopes": ["read"],
                    "csrf": admin._csrf_token("s3cret", "issue_key", org_id),
                },
            )
        assert "shown once" in r.text
        assert "location.reload" not in r.text

    async def test_a_freshly_rotated_key_disables_auto_refresh(self, session_factory):
        from hub import auth as auth_module
        from hub.db import session_scope

        org_id = await self._seed(session_factory)
        async with session_scope(session_factory) as session:
            issued = await auth_module.issue_api_key(session, org_id)
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                f"/admin/org/{org_id}/keys/rotate", headers=_basic("op", "s3cret"),
                data={
                    "key_id": issued.key_id,
                    "csrf": admin._csrf_token("s3cret", "rotate_key", issued.key_id),
                },
            )
        assert "shown once" in r.text
        assert "location.reload" not in r.text

    async def test_a_freshly_generated_encryption_key_disables_auto_refresh(
        self, session_factory
    ):
        async with _client(_app(session_factory=session_factory)) as c:
            r = await c.post(
                "/admin/generate-encryption-key", headers=_basic("op", "s3cret"),
                data={
                    "target": "new",
                    "csrf": admin._csrf_token("s3cret", "generate_encryption_key", "new"),
                },
            )
        assert "shown once" in r.text
        assert "location.reload" not in r.text
