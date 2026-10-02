from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import update as sa_update
from starlette.applications import Starlette

from hub import alerts as alerts_module
from hub import auth, console, crud, rbac
from hub import commons as commons_module
from hub import events as events_module
from hub.billing import StripeSettings
from hub.db import session_scope
from hub.models import ApiKey, Organization, Trace, User, Vote, WebhookEndpoint


async def _fake_public_resolve(hostname: str) -> list:
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]


@pytest.fixture(autouse=True)
def _skip_real_dns_for_webhook_hosts(monkeypatch):
    monkeypatch.setattr(events_module, "_default_resolve", _fake_public_resolve)

pytestmark = pytest.mark.asyncio

SECRET = "console-signing-secret"


def _explode(*a, **kw):
    raise AssertionError("no database access should happen for this request")


def _app(
    secret: str = SECRET, session_factory=_explode, stripe: StripeSettings | None = None,
    signing_key: str = "", config=None,
) -> Starlette:
    app = Starlette()
    if secret:
        console.add_console_routes(
            app, session_factory, console_secret=secret, stripe=stripe, signing_key=signing_key,
            config=config,
        )
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


class TestAbsentUnlessConfigured:
    async def test_no_routes_without_a_secret(self):
        async with _client(_app(secret="")) as client:
            for path in ("", "/signin", "/proof", "/memory", "/kb", "/users", "/keys", "/alerts"):
                response = await client.get(f"{console.CONSOLE_PATH}{path}")
                assert response.status_code == 404, path

    async def test_the_secret_is_not_the_operator_token(self):
        from hub.config import HubConfig

        config = HubConfig(database_url="postgresql+asyncpg://x/y")
        assert config.console_secret == ""
        assert config.admin_token == ""


class TestTheSessionCannotBeForged:
    async def test_a_valid_session_round_trips(self):
        token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        claims = console.read_session(SECRET, token)
        assert claims and claims["org"] == "org-1" and claims["key"] == "ct_live_ab"

    async def test_a_different_secret_is_rejected(self):
        token = console.issue_session("attacker-guess", "org-1", "ct_live_ab")
        assert console.read_session(SECRET, token) is None

    @pytest.mark.parametrize("mangle", [
        lambda t: t[:-4] + "AAAA",
        lambda t: t.split(".")[0],
        lambda t: "." + t.split(".")[1],
        lambda t: t.replace(".", "", 1),
        lambda t: "",
        lambda t: "not-a-token",
    ])
    async def test_a_mangled_token_is_rejected(self, mangle):
        token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        assert console.read_session(SECRET, mangle(token)) is None

    async def test_the_org_id_cannot_be_edited_by_its_holder(self):
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
        raw_key = "ct_live_SUPERSECRETVALUE1234567890"
        token = console.issue_session(SECRET, "org-1", raw_key[:10])
        assert "SUPERSECRETVALUE" not in token
        import base64
        body = token.split(".")[0]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        assert "SUPERSECRETVALUE" not in decoded


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
        assert "HttpOnly" in header
        assert "samesite=strict" in header.lower()
        assert "Path=/app" in header

    async def test_the_cookie_is_secure_behind_a_declared_proxy(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        app = Starlette()
        console.add_console_routes(app, session_factory, console_secret=SECRET, trusted_proxy_hops=1)
        async with _client(app) as client:
            response = await _signed_in(client, raw_key)
        assert "Secure" in response.headers["set-cookie"]
        async with _client(_app(session_factory=session_factory)) as client:
            response = await _signed_in(client, raw_key)
        assert "Secure" not in response.headers["set-cookie"]

    async def test_a_sibling_subdomain_cannot_post_as_the_signed_in_user(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            forged = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
                headers={"Sec-Fetch-Site": "same-site"},
            )
            assert forged.status_code == 403
            old_browser = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
                headers={"Origin": "https://evil.example"},
            )
            assert old_browser.status_code == 403
            async with session_scope(session_factory) as session:
                keys = (await session.execute(select(ApiKey).where(ApiKey.org_id == org_id))).scalars().all()
            assert len(keys) == 1
            own = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
                headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://test"},
            )
            assert "shown once" in own.text

    async def test_sign_out_clears_the_session(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/signout")
            response = await client.get(console.CONSOLE_PATH)
            assert response.status_code == 303

    async def test_a_get_does_not_sign_you_out(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            page = await client.get(f"{console.CONSOLE_PATH}/signout")
            assert page.status_code == 200 and "Sign out?" in page.text
            assert (await client.get(console.CONSOLE_PATH)).status_code == 200
            overview = (await client.get(console.CONSOLE_PATH)).text
            assert f'<form method="post" action="{console.CONSOLE_PATH}/signout">' in overview
            forged = await client.post(f"{console.CONSOLE_PATH}/signout", headers={"Sec-Fetch-Site": "cross-site"})
            assert forged.status_code == 403
            assert (await client.get(console.CONSOLE_PATH)).status_code == 200

    async def test_sign_in_attempts_from_one_ipv6_slash_64_share_a_limit(self, session_factory):
        app = Starlette()
        console.add_console_routes(app, session_factory, console_secret=SECRET, trusted_proxy_hops=1)
        async with _client(app) as client:
            texts = [
                (await client.post(
                    f"{console.CONSOLE_PATH}/signin", data={"api_key": "ct_live_nope"},
                    headers={"X-Forwarded-For": f"2001:db8:1:2::{i:x}"},
                )).text
                for i in range(1, 21)
            ]
        assert any("Too many attempts" in t for t in texts)

    async def test_sign_in_is_rate_limited(self, session_factory):
        app = Starlette()
        console.add_console_routes(app, session_factory, console_secret=SECRET)
        async with _client(app) as client:
            texts = [(await _signed_in(client, "ct_live_nope")).text for _ in range(20)]
        assert any("Too many attempts" in t for t in texts)


class TestFaviconIsServedInline:
    async def test_the_signed_in_page_declares_an_inline_icon(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(console.CONSOLE_PATH)
        assert 'rel="icon"' in response.text
        assert "data:image/svg+xml" in response.text

    async def test_the_signin_page_declares_one_too(self):
        async with _client(_app()) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/signin")
        assert 'rel="icon"' in response.text

    async def test_the_shared_proof_page_declares_one_too(self):
        body = console._shared_page("<p>irrelevant</p>", expires_at=9999999999)
        assert 'rel="icon"' in body.body.decode()


class TestRevocationEndsTheSession:
    async def test_revoking_the_key_locks_the_browser_out(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            assert (await client.get(console.CONSOLE_PATH)).status_code == 200

            await _revoke_keys(session_factory, org_id)

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


class TestItChangesNothing:
    async def test_no_route_accepts_a_state_changing_method(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            for path in ("", "/proof", "/memory", "/kb"):
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    response = await client.request(
                        method, f"{console.CONSOLE_PATH}{path}")
                    assert response.status_code == 405, (method, path)


def _every_visible_input_has_a_label(html: str) -> bool:
    import re
    label_targets = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', html))
    labelledby_targets = set(re.findall(r'\baria-labelledby="([^"]+)"', html))
    named = label_targets | labelledby_targets
    for tag in re.findall(r'<(?:input|select)\b[^>]*>', html):
        if re.search(r'\btype="(hidden|submit|button|checkbox)"', tag):
            continue
        match = re.search(r'\bid="([^"]+)"', tag)
        if match is None or match.group(1) not in named:
            return False
    return True


class TestAccessibleFormControls:
    async def test_the_signin_page_labels_its_input(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/signin")
        assert _every_visible_input_has_a_label(response.text)

    async def test_the_memory_search_box_is_labeled(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/memory")
        assert _every_visible_input_has_a_label(response.text)

    async def test_the_users_page_forms_are_labeled(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            session.add(User(org_id=org_id, email="labeled@example.com", role=rbac.ROLE_VIEWER))
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/users")
        assert _every_visible_input_has_a_label(response.text)

    async def test_every_scope_checkbox_has_its_own_label(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/keys")
        assert response.text.count('<label><input type="checkbox" name="scopes"') >= 1

    async def test_the_keys_page_forms_are_labeled(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/keys")
        assert _every_visible_input_has_a_label(response.text)

    async def test_the_alerts_page_forms_are_labeled(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/alerts")
        assert _every_visible_input_has_a_label(response.text)

    async def test_the_webhooks_page_forms_are_labeled(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/webhooks")
        assert _every_visible_input_has_a_label(response.text)

    async def test_the_proof_pages_experiment_form_is_labeled(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/proof")
        assert _every_visible_input_has_a_label(response.text)


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
        async with session_scope(session_factory) as session:
            row = await session.get(KnowledgeBaseSubmission, submission["id"])
            assert row.rejection_reason == "this is your own business logic, not substrate knowledge"

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            text = (await client.get(f"{console.CONSOLE_PATH}/kb")).text
        assert "this is your own business logic, not substrate knowledge" in text


class TestTheProofPageLeadsWithValidity:
    async def test_a_compromised_verdict_appears_above_the_effects(self):
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
        assert "n=100" in html and "n=120" in html

    async def test_a_non_admin_viewer_sees_no_experiment_controls(self):
        html = console._render_proof(
            {"headline": "", "metrics": []}, {"experiment_running": False}, is_admin=False,
        )
        assert "Start experiment" not in html
        assert "Stop experiment" not in html

    async def test_an_admin_viewer_sees_a_start_form_when_nothing_is_running(self):
        html = console._render_proof(
            {"headline": "", "metrics": []}, {"experiment_running": False}, is_admin=True,
        )
        assert "Start experiment" in html
        assert "Stop experiment" not in html

    async def test_an_admin_viewer_sees_a_stop_button_when_running(self):
        html = console._render_proof(
            {"headline": "", "metrics": []},
            {"experiment_running": True, "n_observations": 0, "n_occasions": 0,
             "integrity": {"verdict": "SOUND", "effects_readable": True, "findings": [],
                           "projections": [], "n_assignments": 0, "n_resolved": 0},
             "effects": []},
            is_admin=True,
        )
        assert "Stop experiment" in html
        assert "Start experiment" not in html

    async def test_an_experiment_error_is_shown_inline(self):
        html = console._render_proof(
            {"headline": "", "metrics": []}, {"experiment_running": False}, is_admin=True,
            experiment_error="Holdout rate must be a number.",
        )
        assert "Holdout rate must be a number." in html

    async def test_the_shared_view_never_shows_experiment_controls(self):
        html = console._render_proof({"headline": "", "metrics": []}, {"experiment_running": False})
        assert "Start experiment" not in html


class TestTheOverviewIsHonestAboutMissingData:
    async def test_no_searches_yet_is_not_rendered_as_a_zero_miss_rate(self):
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


class TestShareTokensAreDistinctFromSessions:
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
        session_token = console.issue_session(SECRET, "org-1", "ct_live_ab")
        assert console.read_share_token(SECRET, session_token) is None

    async def test_a_share_token_is_not_a_valid_session(self):
        share_token = console.issue_share_token(SECRET, "org-1")
        assert console.read_session(SECRET, share_token) is None


class TestProofSharing:
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

    async def test_the_knowledge_base_page_says_only_what_the_console_wrote(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            spoofed = await client.get(f"{console.CONSOLE_PATH}/kb", params={"done": "Call +1-555-0100"})
            real = await client.get(f"{console.CONSOLE_PATH}/kb", params={"done": "voted"})
        assert "555-0100" not in spoofed.text
        assert "your vote was recorded" in real.text

    async def test_a_link_in_the_url_is_never_presented_as_the_one_generated(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(
                f"{console.CONSOLE_PATH}/proof", params={"share_url": "https://evil.example/x"})
        assert response.status_code == 200
        assert "evil.example" not in response.text
        assert "Shareable link generated." not in response.text

    async def test_a_signed_in_org_can_mint_and_then_view_its_own_link(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            proof_response = await client.post(f"{console.CONSOLE_PATH}/proof/share")
            assert proof_response.status_code == 200
            assert "Shareable link generated." in proof_response.text
            assert "location" not in proof_response.headers

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
        token = console.issue_share_token(SECRET, "org-1")
        forged = token[:-4] + "AAAA"
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/proof/shared/{forged}")
        assert response.status_code == 404

    async def test_a_full_session_token_is_refused_at_the_shared_route(
        self, session_factory, org_and_key
    ):
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
        org_id, _raw_key = org_and_key
        share_token = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            client.cookies.set(console.SESSION_COOKIE, share_token)
            response = await client.get(console.CONSOLE_PATH)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_shared_view_is_rate_limited_per_org(self, session_factory, org_and_key):
        org_id, _raw_key = org_and_key
        token = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            statuses = [
                (await client.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")).status_code
                for _ in range(85)
            ]
        assert 429 in statuses

    async def test_revoking_ends_every_link_minted_before(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        before = console.issue_share_token(SECRET, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            assert (await client.get(f"{console.CONSOLE_PATH}/proof/shared/{before}")).status_code == 200
            await _signed_in(client, raw_key)
            revoked = await client.post(f"{console.CONSOLE_PATH}/proof/share/revoke")
            assert revoked.status_code == 303
            assert revoked.headers["location"].endswith("/proof?done=revoked")
            page = await client.get(revoked.headers["location"])
            assert "has been revoked" in page.text
            assert (await client.get(f"{console.CONSOLE_PATH}/proof/shared/{before}")).status_code == 404

            minted = await client.post(f"{console.CONSOLE_PATH}/proof/share")
        import re
        token = re.search(r'value="([^"]+)"', minted.text).group(1).rsplit("/", 1)[-1]
        async with _client(_app(session_factory=session_factory)) as anon:
            assert (await anon.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")).status_code == 200

    async def test_revocation_is_audited(self, session_factory, org_and_key):
        from sqlalchemy import select

        from hub.models import AuditLogEntry

        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/proof/share/revoke")
        async with session_factory() as session:
            actions = (await session.execute(
                select(AuditLogEntry.action).where(AuditLogEntry.org_id == org_id)
            )).scalars().all()
        assert "revoke_share_links" in actions

    async def test_revoking_without_a_session_redirects_to_signin(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(f"{console.CONSOLE_PATH}/proof/share/revoke")
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_a_link_for_an_org_that_no_longer_exists_is_a_404(self, session_factory):
        import uuid as _uuid

        token = console.issue_share_token(SECRET, str(_uuid.uuid4()))
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/proof/shared/{token}")
        assert response.status_code == 404

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


class TestBillingCheckoutAndPortal:
    async def test_checkout_is_absent_without_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(f"{console.CONSOLE_PATH}/billing/checkout", data={"plan": "team"})
        assert response.status_code == 303
        assert response.headers["location"].endswith("/signin")

    async def test_checkout_refuses_to_run_with_no_webhook_secret_configured(
        self, session_factory, org_and_key, monkeypatch
    ):
        _org_id, raw_key = org_and_key

        async def must_not_be_called(*a, **kw):
            raise AssertionError("checkout must not run without a working webhook")

        monkeypatch.setattr(console, "create_checkout_session", must_not_be_called)
        stripe = StripeSettings(secret_key="sk_test", price_team="price_t")
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
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.stripe_customer_id = "cus_existing"
            org.plan = "team"
            org.stripe_subscription_id = "sub_existing"

        async def must_not_be_called(*a, **kw):
            raise AssertionError("portal must not run without a working webhook")

        monkeypatch.setattr(console, "create_billing_portal_session", must_not_be_called)
        stripe = StripeSettings(secret_key="sk_test", price_team="price_t")
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
        assert row.role == rbac.ROLE_VIEWER

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
        assert len(keys) == 1

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

    async def test_a_non_numeric_threshold_is_refused_with_an_inline_error(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/alerts/create",
                data={
                    "metric": alerts_module.METRIC_QUARANTINE_RATE,
                    "comparator": alerts_module.COMPARATOR_GT,
                    "threshold": "not-a-number",
                },
            )
        assert "Threshold must be a number" in response.text
        async with session_scope(session_factory) as session:
            rules = await alerts_module.list_rules(session, org_id)
        assert rules == []

    async def test_threshold_submitted_as_a_file_part_is_refused_not_a_500(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/alerts/create",
                data={
                    "metric": alerts_module.METRIC_QUARANTINE_RATE,
                    "comparator": alerts_module.COMPARATOR_GT,
                },
                files={"threshold": ("threshold.txt", b"0.2", "text/plain")},
            )
        assert response.status_code == 200
        assert "Threshold must be a number" in response.text
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


class TestWebhookManagement:
    async def test_adding_an_endpoint_shows_the_secret_once(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        app = _app(session_factory=session_factory, signing_key="test-signing-key")
        async with _client(app) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/webhooks/create",
                data={"url": "https://example.invalid/hooks/commontrace"},
            )
        assert "shown once" in response.text
        assert "example.invalid" in response.text

    async def test_adding_an_endpoint_with_no_signing_key_configured_shows_the_error(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/webhooks/create",
                data={"url": "https://example.invalid/hooks/commontrace"},
            )
        assert response.status_code == 200
        assert "no HUB_LEDGER_SIGNING_KEY" in response.text

    async def test_an_added_endpoints_subscriptions_persist(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/webhooks/create",
                data={
                    "url": "https://example.invalid/hooks/commontrace",
                    "events": ["trace.quarantined", "experiment.verdict"],
                },
            )
        async with session_scope(session_factory) as session:
            endpoints = await events_module.endpoints_for(session, org_id)
        assert len(endpoints) == 1
        assert sorted(endpoints[0].events) == ["experiment.verdict", "trace.quarantined"]

    async def test_a_non_https_url_is_refused_with_an_inline_error(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/webhooks/create",
                data={"url": "http://example.invalid/hooks"},
            )
        assert "must be https" in response.text

    async def test_a_read_only_key_cannot_add_an_endpoint(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/webhooks/create",
                data={"url": "https://example.invalid/hooks"},
            )
        async with session_scope(session_factory) as session:
            endpoints = await events_module.endpoints_for(session, org_id)
        assert endpoints == []

    async def test_rotating_an_endpoints_secret_shows_it_once(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            endpoint, _secret = await events_module.add_endpoint(
                session, org_id, "https://example.invalid/hooks",
                signing_key="test-signing-key",
            )
            endpoint_id, original_version = endpoint.id, endpoint.key_version
        app = _app(session_factory=session_factory, signing_key="test-signing-key")
        async with _client(app) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/webhooks/{endpoint_id}/rotate")
        assert "shown once" in response.text
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint_id)
        assert row.key_version == original_version + 1

    async def test_cannot_rotate_another_orgs_endpoint(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            endpoint, _secret = await events_module.add_endpoint(
                session, other_org_id, "https://example.invalid/hooks",
                signing_key="test-signing-key",
            )
            endpoint_id, original_version = endpoint.id, endpoint.key_version
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/webhooks/{endpoint_id}/rotate")
        assert "shown once" not in response.text
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint_id)
        assert row.key_version == original_version

    async def test_disabling_an_endpoint(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            endpoint, _secret = await events_module.add_endpoint(
                session, org_id, "https://example.invalid/hooks",
                signing_key="test-signing-key",
            )
            endpoint_id = endpoint.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/webhooks/{endpoint_id}/disable")
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint_id)
        assert row.enabled is False

    async def test_cannot_disable_another_orgs_endpoint(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            endpoint, _secret = await events_module.add_endpoint(
                session, other_org_id, "https://example.invalid/hooks",
                signing_key="test-signing-key",
            )
            endpoint_id = endpoint.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/webhooks/{endpoint_id}/disable")
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint_id)
        assert row.enabled is True

    async def test_the_page_shows_only_this_orgs_endpoints(
        self, session_factory, org_and_key, other_org_and_key
    ):
        org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            await events_module.add_endpoint(
                session, org_id, "https://mine.invalid/hooks", signing_key="test-signing-key",
            )
            await events_module.add_endpoint(
                session, other_org_id, "https://theirs.invalid/hooks",
                signing_key="test-signing-key",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/webhooks")
        assert "mine.invalid" in response.text
        assert "theirs.invalid" not in response.text


class TestAuditLogPage:
    async def test_shows_entries_for_this_org(self, session_factory, org_and_key):
        from hub import audit as audit_module

        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            await audit_module.record(
                session, actor="api-key:ct_test", action="issue_key",
                org_id=org_id, target_type="api_key", target_id="abc123",
                summary="prefix=ct_test scopes=read",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert "issue_key" in response.text
        assert "abc123" in response.text

    async def test_does_not_show_another_orgs_entries(
        self, session_factory, org_and_key, other_org_and_key
    ):
        from hub import audit as audit_module

        _org_id, raw_key = org_and_key
        other_org_id, _other_raw_key = other_org_and_key
        async with session_scope(session_factory) as session:
            await audit_module.record(
                session, actor="api-key:ct_other", action="revoke_key",
                org_id=other_org_id, target_type="api_key", target_id="xyz789",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert "xyz789" not in response.text

    async def test_visible_to_a_read_only_key(self, session_factory, org_and_readonly_key):
        from hub import audit as audit_module

        org_id, raw_key = org_and_readonly_key
        async with session_scope(session_factory) as session:
            await audit_module.record(
                session, actor="api-key:ct_ro", action="issue_key",
                org_id=org_id, target_type="api_key", target_id="ro123",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert response.status_code == 200
        assert "ro123" in response.text

    async def test_console_actions_are_themselves_audited(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
            )
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert "issue_key" in response.text

    async def test_pagination_links_appear_past_a_page(self, session_factory, org_and_key):
        from hub import audit as audit_module

        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            for i in range(55):
                await audit_module.record(
                    session, actor="api-key:ct_test", action="issue_key",
                    org_id=org_id, target_type="api_key", target_id=f"k{i}",
                )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert "Older" in response.text


class TestExperimentControlFromTheConsole:
    async def test_starting_an_experiment_sets_the_holdout_rate(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "0.25", "outcome": "resolved"},
                follow_redirects=True,
            )
        assert response.status_code == 200
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.holdout_rate == 0.25
        assert org.holdout_salt

    async def test_starting_an_experiment_is_audited_with_the_real_actor(
        self, session_factory, org_and_key
    ):
        from hub.models import AuditLogEntry

        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "0.25", "outcome": "resolved"},
            )
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(
                        AuditLogEntry.org_id == org_id,
                        AuditLogEntry.action == "start_experiment",
                    )
                )
            ).scalars().first()
        assert entry is not None
        assert entry.actor.startswith("api-key:")
        assert entry.actor != "operator-cli"

    async def test_an_invalid_rate_is_refused_with_an_inline_error(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "1.5", "outcome": "resolved"},
            )
        assert "strictly between 0 and 1" in response.text
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.holdout_rate == 0.0

    async def test_a_non_numeric_rate_is_refused_with_an_inline_error(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "not-a-number", "outcome": "resolved"},
            )
        assert "must be a number" in response.text

    async def test_a_read_only_key_cannot_start_an_experiment(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "0.25", "outcome": "resolved"},
            )
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.holdout_rate == 0.0

    async def test_stopping_an_experiment_clears_the_holdout_rate(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/start",
                data={"rate": "0.25", "outcome": "resolved"},
            )
            response = await client.post(
                f"{console.CONSOLE_PATH}/proof/experiment/stop", follow_redirects=True,
            )
        assert response.status_code == 200
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.holdout_rate == 0.0

    async def test_stopping_when_nothing_is_running_shows_an_error(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(f"{console.CONSOLE_PATH}/proof/experiment/stop")
        assert "No experiment is running" in response.text

    async def test_a_read_only_key_cannot_stop_an_experiment(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.holdout_rate = 0.3
            org.holdout_salt = "existing-salt"
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(f"{console.CONSOLE_PATH}/proof/experiment/stop")
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.holdout_rate == 0.3

    async def test_starting_is_not_reachable_by_get(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/proof/experiment/start")
        assert response.status_code == 405


class TestAssignmentsCsvExport:
    async def test_downloading_the_csv(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.holdout_rate = 0.3
            org.holdout_salt = "csv-test-salt"
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/proof/assignments.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]

    async def test_requires_sign_in(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(
                f"{console.CONSOLE_PATH}/proof/assignments.csv", follow_redirects=False,
            )
        assert response.status_code in (302, 303)


class TestAutoRefresh:
    async def test_the_overview_page_auto_refreshes(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(console.CONSOLE_PATH)
        assert "location.reload" in response.text

    async def test_the_audit_log_page_auto_refreshes(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/audit")
        assert "location.reload" in response.text

    async def test_the_keys_page_never_auto_refreshes(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            plain = await client.get(f"{console.CONSOLE_PATH}/keys")
            issued = await client.post(
                f"{console.CONSOLE_PATH}/keys/issue",
                data={"scopes": ["read"], "expires_days": "30"},
            )
        assert "location.reload" not in plain.text
        assert "shown once" in issued.text
        assert "location.reload" not in issued.text


class TestKnowledgeBaseBrowse:
    async def _seed_entry(self, session_factory, org_id, title="Pool exhausted",
                          tags=None, *, trust=1.0, votes=0, hits=0):
        from hub import commons
        tags = tags if tags is not None else ["postgres"]
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=org_id, title=title,
                context_text="requests queued behind a saturated pool",
                solution_text="raise pool_size and set a command timeout",
                tags=tags, agent_type="code",
                shared_with_commons=True, shared_at=datetime.now(timezone.utc),
                shared_rationale="test fixture: operator-curated",
                commons_signature=commons.signature_for(title, "ctx", tags),
                commons_source="seed", trust=trust, commons_votes=votes,
                commons_hits=hits,
            )
            session.add(trace)
            await session.flush()
            return trace.id

    async def test_the_catalogue_lists_curated_entries(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        await self._seed_entry(session_factory, org_id, title="Deadlock on upsert")
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert response.status_code == 200
        assert "Browse the open repository" in response.text
        assert "Deadlock on upsert" in response.text

    async def test_it_never_shows_another_orgs_private_trace(
        self, session_factory, org_and_key, other_org_and_key
    ):
        org_id, raw_key = org_and_key
        other_id, _other_key = other_org_and_key
        async with session_scope(session_factory) as session:
            session.add(Trace(
                org_id=other_id, title="Acme private incident",
                context_text="internal", solution_text="internal",
                tags=["postgres"], agent_type="code",
            ))
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "Acme private incident" not in response.text

    async def test_an_entrys_standing_is_shown_to_the_reader(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        await self._seed_entry(session_factory, org_id, trust=1.0, votes=5)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "established" in response.text

    async def test_a_disputed_entry_is_labelled_as_such(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        await self._seed_entry(session_factory, org_id, trust=0.1, votes=9)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "disputed" in response.text

    async def test_it_filters_by_tag(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        await self._seed_entry(session_factory, org_id, title="pg thing", tags=["postgres"])
        await self._seed_entry(session_factory, org_id, title="redis thing", tags=["redis"])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb?tag=redis")
        assert "redis thing" in response.text
        assert "pg thing" not in response.text

    async def test_a_hostile_entry_title_renders_inert(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        await self._seed_entry(
            session_factory, org_id, title="<script>alert('xss')</script>")
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "<script>alert('xss')</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    async def test_it_requires_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.get(f"{console.CONSOLE_PATH}/kb", follow_redirects=False)
        assert response.status_code == 303


class TestKnowledgeBaseSubmitFromTheConsole:
    async def test_an_admin_key_can_propose_an_entry(self, session_factory, org_and_key, config):
        from hub.models import KnowledgeBaseSubmission
        org_id, raw_key = org_and_key
        app = _app(session_factory=session_factory, config=config)
        async with _client(app) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/submit",
                data={
                    "title": "Retry storms after a failover",
                    "context_text": "every client retried at once and re-saturated the primary",
                    "solution_text": "added jittered exponential backoff",
                    "tags": "postgres, failover",
                },
                follow_redirects=False,
            )
        assert response.status_code == 303
        async with session_scope(session_factory) as session:
            submission = (
                await session.execute(
                    select(KnowledgeBaseSubmission).where(
                        KnowledgeBaseSubmission.org_id == org_id)
                )
            ).scalars().first()
        assert submission is not None
        assert submission.title == "Retry storms after a failover"
        assert submission.status == "pending"

    async def test_a_read_only_key_cannot_propose(
        self, session_factory, org_and_readonly_key, config
    ):
        _org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory, config=config)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/submit",
                data={"title": "t", "context_text": "c", "solution_text": "s"},
            )
        assert "admin-scoped key" in response.text

    async def test_a_missing_field_comes_back_as_an_inline_error(
        self, session_factory, org_and_key, config
    ):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory, config=config)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/submit",
                data={"title": "only a title"},
            )
        assert "are all required" in response.text
        assert "Browse the open repository" in response.text

    async def test_proposing_requires_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/submit",
                data={"title": "t", "context_text": "c", "solution_text": "s"},
                follow_redirects=False,
            )
        assert response.status_code == 303


class TestKnowledgeBaseVotingFromTheConsole:
    async def _seed_entry(self, session_factory, org_id, title="Pool exhausted"):
        from hub import commons
        tags = ["postgres"]
        async with session_scope(session_factory) as session:
            trace = Trace(
                org_id=org_id, title=title, context_text="ctx", solution_text="fix",
                tags=tags, agent_type="code", shared_with_commons=True,
                shared_at=datetime.now(timezone.utc), shared_rationale="seed",
                commons_signature=commons.signature_for(title, "ctx", tags),
                commons_source="seed",
            )
            session.add(trace)
            await session.flush()
            return trace.id

    async def test_an_upvote_is_recorded_and_moves_the_tally(
        self, session_factory, org_and_key, establish_orgs
    ):
        org_id, raw_key = org_and_key
        entry_id = await self._seed_entry(session_factory, org_id)
        await establish_orgs(org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""},
                follow_redirects=False,
            )
        assert response.status_code == 303
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_votes == 1
        assert trace.trust == 1.0

    async def test_a_downvote_carries_its_reason(self, session_factory, org_and_key):
        from hub.models import Vote
        org_id, raw_key = org_and_key
        entry_id = await self._seed_entry(session_factory, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "down",
                      "feedback_tag": "security_concern"},
            )
        async with session_scope(session_factory) as session:
            vote = (
                await session.execute(select(Vote).where(Vote.trace_id == entry_id))
            ).scalars().one()
        assert vote.vote_type == "down"
        assert vote.feedback_tag == "security_concern"

    async def test_voting_again_changes_the_vote_rather_than_adding_one(
        self, session_factory, org_and_key, establish_orgs
    ):
        org_id, raw_key = org_and_key
        entry_id = await self._seed_entry(session_factory, org_id)
        await establish_orgs(org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""})
            await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "down", "feedback_tag": "wrong"})
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_votes == 1
        assert trace.trust == 0.0

    async def test_the_catalogue_shows_this_orgs_own_vote(
        self, session_factory, org_and_key
    ):
        org_id, raw_key = org_and_key
        entry_id = await self._seed_entry(session_factory, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            before = await client.get(f"{console.CONSOLE_PATH}/kb")
            assert 'class="v voted"' not in before.text
            await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""})
            after = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert 'class="v voted"' in after.text

    async def test_a_read_only_key_cannot_vote(self, session_factory, org_and_readonly_key):
        org_id, raw_key = org_and_readonly_key
        entry_id = await self._seed_entry(session_factory, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""})
        assert "Voting needs a key with" in response.text
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_votes == 0

    async def test_a_read_only_key_is_shown_no_vote_buttons(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        await self._seed_entry(session_factory, org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "/kb/vote" not in response.text

    async def test_it_cannot_vote_on_a_trace_outside_the_knowledge_base(
        self, session_factory, org_and_key, other_org_and_key
    ):
        _org_id, raw_key = org_and_key
        other_id, _other_key = other_org_and_key
        async with session_scope(session_factory) as session:
            private = Trace(
                org_id=other_id, title="Acme private", context_text="c",
                solution_text="s", tags=[], agent_type="code",
            )
            session.add(private)
            await session.flush()
            private_id = private.id
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": private_id, "vote": "up", "feedback_tag": ""})
        assert "no longer in the Knowledge Base" in response.text
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, private_id)
        assert trace.commons_votes == 0

    async def test_a_tampered_vote_value_is_refused_cleanly(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        entry_id = await self._seed_entry(session_factory, org_and_key[0])
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "sideways", "feedback_tag": ""})
        assert response.status_code == 200
        assert "vote_type" in response.text

    async def test_voting_requires_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": "x", "vote": "up"},
                follow_redirects=False,
            )
        assert response.status_code == 303


class TestTheDoubleSubmitGuardDoesNotBreakForms:
    async def test_the_guard_never_disables_a_control(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "ctSubmitting" in response.text, "the guard should be present at all"
        assert "disabled=true" not in response.text.replace(" ", "")

    async def test_the_vote_form_keeps_its_action_in_the_button(
        self, session_factory, org_and_key
    ):
        from hub import commons
        org_id, raw_key = org_and_key
        tags = ["postgres"]
        async with session_scope(session_factory) as session:
            session.add(Trace(
                org_id=org_id, title="Pool exhausted", context_text="c",
                solution_text="s", tags=tags, agent_type="code",
                shared_with_commons=True, shared_at=datetime.now(timezone.utc),
                shared_rationale="seed",
                commons_signature=commons.signature_for("Pool exhausted", "c", tags),
                commons_source="seed",
            ))
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert 'name="vote" value="up"' in response.text
        assert 'name="vote" value="down"' in response.text


class TestAutoContributeToggleFromTheConsole:
    async def test_it_is_off_for_a_new_org_and_says_so(self, session_factory, org_and_key):
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "Turn automatic contribution on" in response.text

    async def test_turning_it_on_and_off_round_trips(self, session_factory, org_and_key):
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/kb/auto-contribute", data={"enabled": "1"})
            async with session_scope(session_factory) as session:
                org = await session.get(Organization, org_id)
                assert org.commons_auto_contribute is True

            await client.post(
                f"{console.CONSOLE_PATH}/kb/auto-contribute", data={"enabled": "0"})
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.commons_auto_contribute is False

    async def test_it_is_audited(self, session_factory, org_and_key):
        from hub.models import AuditLogEntry
        org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/kb/auto-contribute", data={"enabled": "1"})
        async with session_scope(session_factory) as session:
            entry = (
                await session.execute(
                    select(AuditLogEntry).where(
                        AuditLogEntry.action == "set_commons_auto_contribute")
                )
            ).scalars().first()
        assert entry is not None
        assert entry.org_id == org_id
        assert "enabled=True" in entry.summary

    async def test_a_read_only_key_cannot_change_it(
        self, session_factory, org_and_readonly_key
    ):
        org_id, raw_key = org_and_readonly_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/auto-contribute", data={"enabled": "1"})
        assert "admin-scoped key" in response.text
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
        assert org.commons_auto_contribute is False

    async def test_changing_it_requires_a_session(self, session_factory):
        async with _client(_app(session_factory=session_factory)) as client:
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/auto-contribute",
                data={"enabled": "1"}, follow_redirects=False,
            )
        assert response.status_code == 303


class TestTheConsoleSaysWhenAVoteDoesNotCountYet:
    async def _operator_entry(self, session_factory, title="Someone else's entry"):
        from hub import commons
        tags = ["substrate"]
        async with session_scope(session_factory) as session:
            operator = Organization(name="operator-org")
            session.add(operator)
            await session.flush()
            trace = Trace(
                org_id=operator.id, title=title, context_text="ctx", solution_text="fix",
                tags=tags, agent_type="code", shared_with_commons=True,
                shared_at=datetime.now(timezone.utc), shared_rationale="seed",
                commons_signature=commons.signature_for(title, "ctx", tags),
                commons_source="seed",
            )
            session.add(trace)
            await session.flush()
            return trace.id

    async def test_a_new_org_is_told_its_vote_is_recorded_but_not_counted(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        entry_id = await self._operator_entry(session_factory)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""},
                follow_redirects=True,
            )
        assert "recorded" in response.text
        assert "does not count toward" in response.text
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_votes == 0

    async def test_an_established_org_gets_the_plain_thanks_and_moves_the_tally(
        self, session_factory, org_and_key, establish_orgs
    ):
        org_id, raw_key = org_and_key
        await establish_orgs(org_id)
        entry_id = await self._operator_entry(session_factory)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "up", "feedback_tag": ""},
                follow_redirects=True,
            )
        assert "does not count toward" not in response.text
        async with session_scope(session_factory) as session:
            trace = await session.get(Trace, entry_id)
        assert trace.commons_votes == 1

    async def test_the_catalogue_states_the_bar_before_anyone_votes(
        self, session_factory, org_and_key
    ):
        _org_id, raw_key = org_and_key
        await self._operator_entry(session_factory)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "recorded but not yet counted" in response.text
        assert str(commons_module.COMMONS_VOTER_MIN_TRACES) in response.text
        assert str(commons_module.COMMONS_VOTER_MIN_AGE_HOURS) in response.text

    async def test_an_established_org_is_not_shown_the_notice(
        self, session_factory, org_and_key, establish_orgs
    ):
        org_id, raw_key = org_and_key
        await establish_orgs(org_id)
        await self._operator_entry(session_factory)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "recorded but not yet counted" not in response.text


class TestTheCatalogueShowsTheGroundsForAVerdict:
    async def _operator_entry(self, session_factory, title="Someone else's entry"):
        from hub import commons
        tags = ["substrate"]
        async with session_scope(session_factory) as session:
            operator = Organization(name="operator-org")
            session.add(operator)
            await session.flush()
            trace = Trace(
                org_id=operator.id, title=title, context_text="ctx", solution_text="fix",
                tags=tags, agent_type="code", shared_with_commons=True,
                shared_at=datetime.now(timezone.utc), shared_rationale="seed",
                commons_signature=commons.signature_for(title, "ctx", tags),
                commons_source="seed",
            )
            session.add(trace)
            await session.flush()
            return trace.id

    async def test_a_security_concern_is_shown_even_on_an_unproven_entry(
        self, session_factory, org_and_key, establish_orgs
    ):
        org_id, raw_key = org_and_key
        entry_id = await self._operator_entry(session_factory)
        await establish_orgs(org_id)
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            await client.post(
                f"{console.CONSOLE_PATH}/kb/vote",
                data={"trace_id": entry_id, "vote": "down",
                      "feedback_tag": "security_concern"})
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "unproven" in response.text
        assert "security concern" in response.text

    async def test_a_corrected_entry_says_so(
        self, session_factory, org_and_key, config
    ):
        from hub.abuse import make_rate_limiter
        _org_id, raw_key = org_and_key
        entry_id = await self._operator_entry(session_factory)
        async with session_scope(session_factory) as session:
            operator_org = (await session.get(Trace, entry_id)).org_id
        async with session_scope(session_factory) as session:
            await crud.amend_trace(
                session, operator_org, entry_id, config, make_rate_limiter(config),
                solution_text="corrected advice", actor="operator-console",
            )
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert "revised" in response.text
        assert "corrected advice" in response.text

    async def test_an_unflagged_entry_shows_no_concern_pills(
        self, session_factory, org_and_key
    ):
        await self._operator_entry(session_factory)
        _org_id, raw_key = org_and_key
        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert 'class="concerns"' not in response.text

    async def test_free_text_feedback_is_never_rendered_to_the_catalogue(
        self, session_factory, org_and_key, establish_orgs
    ):
        secret = "internal-host-db7.corp.example"
        org_id, raw_key = org_and_key
        entry_id = await self._operator_entry(session_factory)
        await establish_orgs(org_id)
        async with session_scope(session_factory) as session:
            await crud.vote_trace(
                session, org_id, entry_id, "down",
                feedback_tag="wrong", feedback_text=secret, actor="mcp",
            )
        async with session_scope(session_factory) as session:
            stored = (
                await session.execute(select(Vote).where(Vote.trace_id == entry_id))
            ).scalars().one()
        assert stored.feedback_text == secret, "premise: the text really is on the vote"

        async with _client(_app(session_factory=session_factory)) as client:
            await _signed_in(client, raw_key)
            response = await client.get(f"{console.CONSOLE_PATH}/kb")
        assert secret not in response.text
        assert "does not work" in response.text
