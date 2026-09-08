"""Tests for hub/billing.py -- self-serve Stripe upgrades and the webhook
that keeps Organization.plan in sync with what was actually paid for.

Four layers, tested separately:

1. Webhook signature verification -- pure, no DB, no network. The one
   thing standing between "/billing/webhook" and an attacker who can POST
   an arbitrary plan upgrade for any org_id.
2. StripeSettings' price<->plan mapping -- pure, no DB.
3. apply_webhook_event -- needs a real Organization row, but no network:
   this is the actual state-mutation logic, tested against Postgres so a
   wrong query (e.g. matching on the wrong column) shows up here rather
   than only in production.
4. create_checkout_session / create_billing_portal_session -- outbound
   Stripe API calls, monkeypatched at billing._post exactly the way this
   codebase injects session_factory everywhere else, so these tests never
   touch the real network and never need a Stripe account.
5. The webhook ROUTE, end-to-end through a real Starlette app + database,
   proving the signature check actually gates the handler and a valid
   delivery actually lands in the database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
import pytest_asyncio
from starlette.applications import Starlette

from hub import billing
from hub.billing import StripeSettings
from hub.db import session_scope
from hub.models import Organization

pytestmark = pytest.mark.asyncio

WEBHOOK_SECRET = "whsec_test_secret"


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    signed_payload = f"{ts}.".encode("ascii") + payload
    sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# --- Webhook signature verification -----------------------------------------


class TestWebhookSignatureVerification:
    async def test_a_validly_signed_payload_verifies(self):
        payload = b'{"type": "checkout.session.completed"}'
        header = _sign(payload)
        assert billing.verify_webhook_signature(payload, header, WEBHOOK_SECRET) is True

    async def test_the_wrong_secret_is_rejected(self):
        payload = b'{"type": "checkout.session.completed"}'
        header = _sign(payload, secret="whsec_a_different_secret")
        assert billing.verify_webhook_signature(payload, header, WEBHOOK_SECRET) is False

    async def test_a_tampered_payload_is_rejected(self):
        payload = b'{"type": "checkout.session.completed"}'
        header = _sign(payload)
        tampered = b'{"type": "customer.subscription.deleted"}'
        assert billing.verify_webhook_signature(tampered, header, WEBHOOK_SECRET) is False

    async def test_no_signature_header_is_rejected(self):
        assert billing.verify_webhook_signature(b"{}", "", WEBHOOK_SECRET) is False

    @pytest.mark.parametrize("header", [
        "not-a-signature-header",
        "t=12345",             # no v1
        "v1=deadbeef",          # no t
        "t=not-a-number,v1=deadbeef",
    ])
    async def test_a_malformed_header_is_rejected(self, header):
        assert billing.verify_webhook_signature(b"{}", header, WEBHOOK_SECRET) is False

    async def test_a_timestamp_outside_tolerance_is_rejected(self):
        """Without this bound, a captured, validly-signed payload could be
        replayed against this endpoint indefinitely -- the signature alone
        never expires."""
        payload = b"{}"
        old_header = _sign(payload, ts=int(time.time()) - 10_000)
        assert billing.verify_webhook_signature(payload, old_header, WEBHOOK_SECRET) is False

    async def test_a_timestamp_just_inside_tolerance_is_accepted(self):
        payload = b"{}"
        header = _sign(payload, ts=int(time.time()) - 100)
        assert billing.verify_webhook_signature(payload, header, WEBHOOK_SECRET, tolerance=300) is True

    async def test_a_rotating_secret_is_matched_by_either_v1_value(self):
        """Stripe sends multiple v1 values while a webhook signing secret is
        being rotated -- this Hub must accept a payload signed by either the
        old or the new one, not only the first in the header."""
        payload = b"{}"
        ts = int(time.time())
        old_sig = _sign(payload, secret="whsec_old", ts=ts).split("v1=")[1]
        new_sig = _sign(payload, secret=WEBHOOK_SECRET, ts=ts).split("v1=")[1]
        header = f"t={ts},v1={old_sig},v1={new_sig}"
        assert billing.verify_webhook_signature(payload, header, WEBHOOK_SECRET) is True

    async def test_no_configured_secret_always_rejects(self):
        payload = b"{}"
        header = _sign(payload, secret="")
        assert billing.verify_webhook_signature(payload, header, "") is False


# --- StripeSettings price/plan mapping --------------------------------------


class TestStripeSettings:
    async def test_checkout_configured_needs_a_secret_key_and_at_least_one_price(self):
        assert StripeSettings().checkout_configured is False
        assert StripeSettings(secret_key="sk_test", webhook_secret="whsec").checkout_configured is False
        assert StripeSettings(
            secret_key="sk_test", webhook_secret="whsec", price_team="price_team"
        ).checkout_configured is True
        assert StripeSettings(price_team="price_team").checkout_configured is False

    async def test_checkout_configured_also_needs_a_webhook_secret(self):
        """The severe case this guards: secret_key + a price with NO
        webhook_secret would let Checkout collect a real payment while
        add_billing_webhook_route (gated on webhook_secret, hub/server.py)
        stays unregistered -- so Organization.plan never learns the
        payment happened. Charged and never upgraded is worse than the
        button not existing at all, so checkout_configured must require
        the whole round trip, not just the sellable half."""
        assert StripeSettings(secret_key="sk_test", price_team="price_team").checkout_configured is False
        assert StripeSettings(
            secret_key="sk_test", webhook_secret="whsec", price_team="price_team"
        ).checkout_configured is True

    async def test_price_for_plan_resolves_billable_plans_only(self):
        settings = StripeSettings(secret_key="sk", price_team="price_t", price_scale="price_s")
        assert settings.price_for_plan("team") == "price_t"
        assert settings.price_for_plan("scale") == "price_s"
        assert settings.price_for_plan("free") is None
        assert settings.price_for_plan("nonsense") is None

    async def test_plan_for_price_is_the_inverse_mapping(self):
        settings = StripeSettings(secret_key="sk", price_team="price_t", price_scale="price_s")
        assert settings.plan_for_price("price_t") == "team"
        assert settings.plan_for_price("price_s") == "scale"
        assert settings.plan_for_price("price_unknown") is None

    async def test_an_unset_price_never_matches_an_empty_string(self):
        """Two plans sharing an unset (empty-string) price must not alias to
        each other -- otherwise plan_for_price('') would resolve to
        whichever plan happens to come first in the mapping."""
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        assert settings.plan_for_price("") is None


# --- apply_webhook_event: the actual state mutation, against real Postgres --


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="Northwind")
        session.add(o)
        await session.flush()
        return o.id


def _event(event_type: str, obj: dict) -> dict:
    return {"type": event_type, "data": {"object": obj}}


class TestCheckoutSessionCompleted:
    async def test_it_sets_plan_customer_and_subscription(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        event = _event("checkout.session.completed", {
            "client_reference_id": org, "customer": "cus_123", "subscription": "sub_123",
            "metadata": {"org_id": org, "plan": "team"},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "plan -> team" in outcome
        async with session_scope(session_factory) as session:
            fresh = await session.get(Organization, org)
            assert fresh.plan == "team"
            assert fresh.stripe_customer_id == "cus_123"
            assert fresh.stripe_subscription_id == "sub_123"

    async def test_metadata_org_id_is_a_fallback_for_client_reference_id(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_scale="price_s")
        event = _event("checkout.session.completed", {
            "customer": "cus_456", "subscription": "sub_456",
            "metadata": {"org_id": org, "plan": "scale"},
        })
        async with session_scope(session_factory) as session:
            await billing.apply_webhook_event(session, settings, event)
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "scale"

    async def test_an_unknown_org_id_is_a_no_op(self, session_factory):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        fake_org_id = "00000000-0000-0000-0000-000000000000"
        event = _event("checkout.session.completed", {
            "client_reference_id": fake_org_id, "customer": "cus_1",
            "metadata": {"org_id": fake_org_id, "plan": "team"},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "no such org" in outcome

    async def test_a_malformed_org_id_never_crashes_the_webhook(self, session_factory):
        """Stripe's own event never carries anything but what this Hub put
        into metadata at Checkout Session creation, but the webhook must
        not trust that -- a public, network-facing handler that raises on
        unexpected input is a reliability bug independent of trust."""
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        event = _event("checkout.session.completed", {
            "client_reference_id": "not-a-uuid-at-all", "customer": "cus_1",
            "metadata": {"org_id": "not-a-uuid-at-all", "plan": "team"},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "ignored" in outcome

    async def test_a_non_billable_plan_in_metadata_is_ignored(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        event = _event("checkout.session.completed", {
            "client_reference_id": org, "customer": "cus_1",
            "metadata": {"org_id": org, "plan": "operator"},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "ignored" in outcome
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "free"

    async def test_a_missing_customer_id_is_ignored(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        event = _event("checkout.session.completed", {
            "client_reference_id": org, "metadata": {"org_id": org, "plan": "team"},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "ignored" in outcome
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "free"


class TestSubscriptionLifecycle:
    async def test_deletion_reverts_the_org_to_the_free_plan(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "team"
            o.stripe_customer_id = "cus_1"
            o.stripe_subscription_id = "sub_1"

        event = _event("customer.subscription.deleted", {"customer": "cus_1", "id": "sub_1"})
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "plan -> free" in outcome
        async with session_scope(session_factory) as session:
            fresh = await session.get(Organization, org)
            assert fresh.plan == "free"
            assert fresh.stripe_subscription_id is None
            # The customer id is NOT cleared -- a canceled subscriber who
            # resubscribes later should reuse the same Stripe Customer
            # rather than fragmenting their billing history.
            assert fresh.stripe_customer_id == "cus_1"

    async def test_an_active_update_resolves_the_plan_from_the_price_id(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t", price_scale="price_s")
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "team"
            o.stripe_customer_id = "cus_2"
            o.stripe_subscription_id = "sub_2"

        event = _event("customer.subscription.updated", {
            "customer": "cus_2", "id": "sub_2", "status": "active",
            "items": {"data": [{"price": {"id": "price_s"}}]},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "plan -> scale" in outcome
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "scale"

    async def test_a_canceled_status_update_reverts_to_free(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "team"
            o.stripe_customer_id = "cus_3"
            o.stripe_subscription_id = "sub_3"

        event = _event("customer.subscription.updated", {
            "customer": "cus_3", "status": "canceled",
            "items": {"data": [{"price": {"id": "price_t"}}]},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "plan -> free" in outcome

    async def test_an_unresolvable_price_leaves_the_plan_untouched(self, session_factory, org):
        """A price id this deployment has no mapping for (a plan sold
        outside this Hub's own price ids, or a dashboard-side price change
        mid-migration) must not silently downgrade or upgrade anyone."""
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.plan = "team"
            o.stripe_customer_id = "cus_4"
            o.stripe_subscription_id = "sub_4"

        event = _event("customer.subscription.updated", {
            "customer": "cus_4", "status": "active",
            "items": {"data": [{"price": {"id": "price_unmapped"}}]},
        })
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "ignored" in outcome
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "team"

    async def test_an_event_for_an_unknown_customer_is_a_no_op(self, session_factory):
        settings = StripeSettings(secret_key="sk", price_team="price_t")
        event = _event("customer.subscription.deleted", {"customer": "cus_never_seen"})
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "no org for customer" in outcome

    async def test_an_unhandled_event_type_never_raises(self, session_factory):
        settings = StripeSettings(secret_key="sk")
        event = _event("payment_intent.succeeded", {"id": "pi_1"})
        async with session_scope(session_factory) as session:
            outcome = await billing.apply_webhook_event(session, settings, event)
        assert "ignored" in outcome


# --- Outbound Stripe API calls, monkeypatched at billing._post -------------


class TestCreateCheckoutSession:
    async def test_it_posts_the_expected_checkout_shape(self, monkeypatch):
        captured = {}

        async def fake_post(path, secret_key, data):
            captured["path"] = path
            captured["secret_key"] = secret_key
            captured["data"] = data
            return {"url": "https://checkout.stripe.com/pay/cs_test_123"}

        monkeypatch.setattr(billing, "_post", fake_post)
        settings = StripeSettings(secret_key="sk_test", price_team="price_t")
        org = Organization(id="org-1", name="Northwind")
        url = await billing.create_checkout_session(
            settings, org=org, plan="team",
            success_url="https://example.test/app?upgraded=1", cancel_url="https://example.test/app",
        )
        assert url == "https://checkout.stripe.com/pay/cs_test_123"
        assert captured["path"] == "checkout/sessions"
        assert captured["secret_key"] == "sk_test"
        data = captured["data"]
        assert data["mode"] == "subscription"
        assert data["client_reference_id"] == "org-1"
        assert data["line_items[0][price]"] == "price_t"
        assert data["metadata[org_id]"] == "org-1"
        assert data["metadata[plan]"] == "team"
        assert "customer" not in data

    async def test_an_existing_customer_id_is_reused_not_recreated(self, monkeypatch):
        captured = {}

        async def fake_post(path, secret_key, data):
            captured.update(data)
            return {"url": "https://checkout.stripe.com/pay/cs_test_456"}

        monkeypatch.setattr(billing, "_post", fake_post)
        settings = StripeSettings(secret_key="sk_test", price_team="price_t")
        org = Organization(id="org-2", name="Acme", stripe_customer_id="cus_existing")
        await billing.create_checkout_session(
            settings, org=org, plan="team", success_url="https://x/success", cancel_url="https://x/cancel",
        )
        assert captured["customer"] == "cus_existing"

    async def test_an_unpriced_plan_raises_before_any_network_call(self, monkeypatch):
        async def fake_post(*a, **kw):
            raise AssertionError("must not call Stripe for an unpriced plan")

        monkeypatch.setattr(billing, "_post", fake_post)
        settings = StripeSettings(secret_key="sk_test")  # no prices configured
        org = Organization(id="org-3", name="Acme")
        with pytest.raises(ValueError, match="team"):
            await billing.create_checkout_session(
                settings, org=org, plan="team", success_url="https://x", cancel_url="https://x",
            )


class TestCreateBillingPortalSession:
    async def test_it_posts_the_customer_and_return_url(self, monkeypatch):
        captured = {}

        async def fake_post(path, secret_key, data):
            captured["path"] = path
            captured["data"] = data
            return {"url": "https://billing.stripe.com/session/bps_test_1"}

        monkeypatch.setattr(billing, "_post", fake_post)
        settings = StripeSettings(secret_key="sk_test")
        url = await billing.create_billing_portal_session(
            settings, customer_id="cus_1", return_url="https://example.test/app",
        )
        assert url == "https://billing.stripe.com/session/bps_test_1"
        assert captured["path"] == "billing_portal/sessions"
        assert captured["data"] == {"customer": "cus_1", "return_url": "https://example.test/app"}


class TestPostRaisesOnAnErrorResponse:
    async def test_a_4xx_response_becomes_a_stripe_error(self, monkeypatch):
        class _FakeResponse:
            status_code = 402
            text = "card_declined"

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, *a, **kw):
                return _FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())
        with pytest.raises(billing.StripeError, match="402"):
            await billing._post("checkout/sessions", "sk_test", {})


# --- The webhook route, end-to-end ------------------------------------------


def _app(stripe: StripeSettings, session_factory) -> Starlette:
    app = Starlette()
    billing.add_billing_webhook_route(app, session_factory, stripe=stripe)
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestWebhookRoute:
    async def test_a_validly_signed_delivery_updates_the_org(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", webhook_secret=WEBHOOK_SECRET, price_team="price_t")
        body = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "client_reference_id": org, "customer": "cus_e2e", "subscription": "sub_e2e",
                "metadata": {"org_id": org, "plan": "team"},
            }},
        }).encode()
        async with _client(_app(settings, session_factory)) as client:
            response = await client.post(
                "/billing/webhook", content=body,
                headers={"stripe-signature": _sign(body, secret=WEBHOOK_SECRET)},
            )
        assert response.status_code == 200
        async with session_scope(session_factory) as session:
            fresh = await session.get(Organization, org)
            assert fresh.plan == "team"
            assert fresh.stripe_customer_id == "cus_e2e"

    async def test_an_invalid_signature_is_refused_and_changes_nothing(self, session_factory, org):
        settings = StripeSettings(secret_key="sk", webhook_secret=WEBHOOK_SECRET, price_team="price_t")
        body = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {
                "client_reference_id": org, "customer": "cus_forged",
                "metadata": {"org_id": org, "plan": "team"},
            }},
        }).encode()
        async with _client(_app(settings, session_factory)) as client:
            response = await client.post(
                "/billing/webhook", content=body,
                headers={"stripe-signature": "t=1,v1=0000000000000000000000000000000000000000000000000000000000000000"},
            )
        assert response.status_code == 400
        async with session_scope(session_factory) as session:
            assert (await session.get(Organization, org)).plan == "free"

    async def test_a_missing_signature_header_is_refused(self, session_factory):
        settings = StripeSettings(secret_key="sk", webhook_secret=WEBHOOK_SECRET)
        async with _client(_app(settings, session_factory)) as client:
            response = await client.post("/billing/webhook", content=b"{}")
        assert response.status_code == 400

    async def test_a_body_that_is_not_json_is_refused_after_a_valid_signature(self, session_factory):
        settings = StripeSettings(secret_key="sk", webhook_secret=WEBHOOK_SECRET)
        body = b"not json at all"
        async with _client(_app(settings, session_factory)) as client:
            response = await client.post(
                "/billing/webhook", content=body,
                headers={"stripe-signature": _sign(body, secret=WEBHOOK_SECRET)},
            )
        assert response.status_code == 400
