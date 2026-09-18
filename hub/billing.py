"""Self-serve plan upgrades via Stripe, and the webhook that keeps
`Organization.plan` in sync with what was actually paid for.

WHY THIS EXISTS
----------------
hub/plans.py makes the entitlement real ("what a plan actually permits") but
explicitly declines to take payment -- "faking [billing] here would be
theatre... what it does is make the entitlement real, so that when a price
is attached, there is something to attach it to." Until this module, the
only way to move an org onto `team`/`scale` was an operator editing
`Organization.plan` by hand. This is the other half: a customer clicks
"Upgrade", pays Stripe directly, and this Hub's own record of what they are
entitled to updates itself from Stripe's own signed notification of what
happened -- no operator, no support ticket, no manual row edit.

WHAT THIS DOES NOT DO
-----------------------
No `stripe` SDK. httpx is already a Hub dependency (hub/requirements.txt);
Stripe's REST API is a handful of `POST`s with API-key Basic auth and
form-encoded bodies, and webhook verification is one documented HMAC check
-- not enough surface to justify a second pinned dependency, a second
version to track, for one vendor.

No proration or plan-switch logic of our own. Checkout Sessions
(`create_checkout_session`) mint a NEW subscription and are for an org with
none yet; an org that already has one is sent to Stripe's own Billing
Portal (`create_billing_portal_session`) instead, where Stripe -- not this
code -- computes any proration for an upgrade/downgrade/cancellation. This
is a deliberate, narrow surface: get that branch wrong (offering Checkout
to an already-subscribed org) and Stripe creates a SECOND subscription on
the same customer, which is silent double billing, not a cosmetic bug.

No dunning, no tax, no invoicing UI -- hub/plans.py already named these as
belonging to "a billing system that has to handle tax, dunning, refunds and
disputes," and that system is Stripe itself, reachable by every customer
through the Billing Portal this module links to.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from hub import plans
from hub.db import session_scope
from hub.models import Organization, ProcessedWebhookEvent

logger = logging.getLogger("commontrace.hub.billing")

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Stripe's own client libraries default to a 5 minute tolerance on webhook
# timestamps; matched here so a legitimate delivery retry inside Stripe's
# own window is never rejected, while a captured, validly-signed payload
# cannot be replayed indefinitely.
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    """A Stripe API call returned something other than 2xx."""


@dataclass(frozen=True)
class StripeSettings:
    """Everything billing.py needs, gathered in one place so hub/server.py
    builds it once from HubConfig and every route handler below takes this
    instead of six separate strings. Deliberately NOT a HubConfig itself --
    this module has no reason to know about database URLs, rate limits, or
    anything else that dataclass carries, and coupling to it here would
    make billing.py's own tests need a full HubConfig just to construct a
    settings object."""

    secret_key: str = ""
    webhook_secret: str = ""
    price_team: str = ""
    price_scale: str = ""

    @property
    def checkout_configured(self) -> bool:
        """Whether there is enough here to offer a self-serve upgrade at
        all. Requires all three: `secret_key` alone is not enough -- with
        no price configured for either paid plan, "Upgrade" would have
        nothing to sell -- and `webhook_secret` is not optional either,
        despite gating a DIFFERENT route (add_billing_webhook_route,
        hub/server.py). Offering Checkout while the webhook stays
        unregistered is not a smaller version of this feature, it is the
        worst version of it: a customer completes a real Stripe payment,
        and this Hub has no route left to ever learn it happened, so
        Organization.plan never moves off 'free'. Charged and never
        upgraded is a strictly worse outcome than the button not existing,
        so this checks for a working round trip, not just a sellable one.
        """
        return bool(self.secret_key and self.webhook_secret and (self.price_team or self.price_scale))

    def price_for_plan(self, plan: str) -> str | None:
        return {"team": self.price_team, "scale": self.price_scale}.get(plan) or None

    def plan_for_price(self, price_id: str) -> str | None:
        mapping = {price: name for name, price in (("team", self.price_team), ("scale", self.price_scale)) if price}
        return mapping.get(price_id)


async def _post(path: str, secret_key: str, data: dict[str, str]) -> dict:
    """One Stripe REST call. Isolated in its own tiny function (rather than
    inlined at each call site) so tests can monkeypatch exactly this and
    nothing else -- the same dependency-injection seam this codebase uses
    for `session_factory` everywhere else, applied to the one outbound
    network call this module makes."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{STRIPE_API_BASE}/{path}", data=data, auth=(secret_key, ""))
    if response.status_code >= 400:
        raise StripeError(f"Stripe {path} returned {response.status_code}: {response.text[:500]}")
    return response.json()


async def _delete(path: str, secret_key: str) -> dict:
    """DELETE to Stripe's API. Its own tiny function for the same reason
    `_post` is one: an isolated seam tests can monkeypatch without
    touching the real network."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.delete(f"{STRIPE_API_BASE}/{path}", auth=(secret_key, ""))
    if response.status_code >= 400:
        raise StripeError(f"Stripe DELETE {path} returned {response.status_code}: {response.text[:500]}")
    return response.json()


async def cancel_subscription(settings: StripeSettings, *, subscription_id: str) -> None:
    """Cancel a Stripe subscription IMMEDIATELY (not at period end).

    Called before permanently deleting an org that has one
    (hub/crud.py:confirm_org_deletion, hub/manage.py:purge_org) -- an org
    row deleted with its subscription still active keeps charging that
    customer's card on every future billing cycle, with no CommonTrace
    account left to ever notice or reconcile it. Raises StripeError on
    failure; every caller must refuse to proceed with deletion when this
    raises rather than delete the org anyway. An uncancelled subscription
    with the org record still in place can be retried or handled by an
    operator going straight to Stripe; the same subscription with no org
    row left at all is a customer with no path back to their own billing
    relationship.
    """
    await _delete(f"subscriptions/{subscription_id}", settings.secret_key)


async def create_checkout_session(
    settings: StripeSettings, *, org: Organization, plan: str, success_url: str, cancel_url: str,
) -> str:
    """Mint a Stripe-hosted Checkout URL for `org` to subscribe to `plan`.

    Only for an org with no existing subscription -- see this module's own
    docstring on why an already-subscribed org must go through
    `create_billing_portal_session` instead. Reuses `org.stripe_customer_id`
    when one already exists (a prior, abandoned or expired Checkout attempt)
    so a second attempt does not fragment one org's billing history across
    two Stripe Customer objects.
    """
    price = settings.price_for_plan(plan)
    if not price:
        raise ValueError(f"no Stripe price configured for plan {plan!r}")
    data = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Belt and suspenders: client_reference_id is Stripe's own
        # general-purpose correlation field, metadata.org_id is ours. The
        # webhook handler below reads either, so losing one to a future
        # Stripe API change still leaves the other.
        "client_reference_id": org.id,
        "line_items[0][price]": price,
        "line_items[0][quantity]": "1",
        "metadata[org_id]": org.id,
        "metadata[plan]": plan,
    }
    if org.stripe_customer_id:
        data["customer"] = org.stripe_customer_id
    payload = await _post("checkout/sessions", settings.secret_key, data)
    return payload["url"]


async def create_billing_portal_session(settings: StripeSettings, *, customer_id: str, return_url: str) -> str:
    """A Stripe-hosted page where an already-subscribed org manages its own
    payment method, invoices, plan changes and cancellation -- everything
    hub/plans.py named as belonging to a real billing system rather than to
    this codebase."""
    payload = await _post(
        "billing_portal/sessions", settings.secret_key, {"customer": customer_id, "return_url": return_url}
    )
    return payload["url"]


def verify_webhook_signature(
    payload: bytes, sig_header: str, secret: str, *, now: float | None = None,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
) -> bool:
    """Stripe's documented scheme: a `Stripe-Signature` header shaped like
    `t=<unix ts>,v1=<hex hmac>[,v1=<hex hmac>...]` (Stripe sends multiple
    `v1` values while rotating a webhook signing secret). Verifies HMAC-SHA256
    of `f"{t}.{payload}"` keyed by `secret`, constant-time, against every
    `v1` value present, and additionally rejects a timestamp more than
    `tolerance` seconds from `now` -- without that bound, a captured,
    validly-signed request body could be replayed against this endpoint
    indefinitely, since the payload's own signature would still verify.
    """
    if not sig_header or not secret:
        return False
    parts: dict[str, list[str]] = {}
    for item in sig_header.split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    timestamps = parts.get("t")
    signatures = parts.get("v1")
    if not timestamps or not signatures:
        return False
    try:
        timestamp = int(timestamps[0])
    except ValueError:
        return False
    if now is None:
        now = time.time()
    if abs(now - timestamp) > tolerance:
        return False
    signed_payload = f"{timestamp}.".encode("ascii") + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def _looks_like_org_id(value: str | None) -> bool:
    if not value:
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def apply_webhook_event(session: AsyncSession, settings: StripeSettings, event: dict) -> str:
    """Apply one already signature-verified Stripe event, exactly once.

    Checks `processed_webhook_events` for this event's id BEFORE calling
    `_apply_webhook_event_once` below, and records it after -- structural
    idempotency against Stripe's at-least-once delivery, not an accident
    of the handlers underneath happening to be pure overwrites (see
    alembic revision 37d2580be8db's own docstring for why that distinction
    matters). Events with no `id` (malformed, or a test fixture that
    omitted one) are applied but never deduplicated -- there is nothing to
    key a ledger row on.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if event_id:
        already = await session.get(ProcessedWebhookEvent, event_id)
        if already is not None:
            return f"ignored {event_type}: event {event_id} already processed -- {already.outcome}"
    outcome = await _apply_webhook_event_once(session, settings, event)
    if event_id:
        session.add(ProcessedWebhookEvent(id=event_id, event_type=event_type, outcome=outcome[:500]))
    return outcome


async def _apply_webhook_event_once(session: AsyncSession, settings: StripeSettings, event: dict) -> str:
    """The actual per-event-type logic, run at most once per event id by
    `apply_webhook_event` above. Returns a short human-readable
    description of what happened (or why nothing did), for the webhook
    route to log -- never raises on an event type or shape this
    integration does not act on. Stripe sends far more event types than
    this integration cares about, and treating an unhandled one as an
    error would make this endpoint fragile against Stripe adding new
    event types over time, which is exactly the kind of change that
    should be a silent no-op here.
    """
    event_type = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object")) or {}
    if not isinstance(obj, dict):
        return f"ignored {event_type}: malformed event object"

    if event_type == "checkout.session.completed":
        org_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("org_id")
        plan = (obj.get("metadata") or {}).get("plan")
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        if not _looks_like_org_id(org_id):
            return f"ignored {event_type}: missing or malformed org_id"
        if plan not in plans.BILLABLE_PLANS:
            return f"ignored {event_type}: unrecognized plan {plan!r}"
        if not customer_id:
            return f"ignored {event_type}: no customer id on the session"
        org = await session.get(Organization, org_id)
        if org is None:
            return f"ignored {event_type}: no such org {org_id}"
        org.plan = plan
        org.stripe_customer_id = customer_id
        org.stripe_subscription_id = subscription_id
        return f"org {org_id}: plan -> {plan} (checkout.session.completed)"

    if event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        customer_id = obj.get("customer")
        if not customer_id:
            return f"ignored {event_type}: no customer id"
        org = (
            await session.execute(select(Organization).where(Organization.stripe_customer_id == customer_id))
        ).scalar_one_or_none()
        if org is None:
            # Not a bug: this fires for any Stripe customer on the account,
            # not only ones this Hub created a subscription for (an
            # operator poking around in the Stripe dashboard, a test
            # customer). Nothing to reconcile against.
            return f"ignored {event_type}: no org for customer {customer_id}"

        if event_type == "customer.subscription.deleted":
            org.plan = plans.DEFAULT_PLAN
            org.stripe_subscription_id = None
            return f"org {org.id}: plan -> {plans.DEFAULT_PLAN} (subscription deleted)"

        status = str(obj.get("status") or "")
        items = ((obj.get("items") or {}).get("data")) or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        resolved_plan = settings.plan_for_price(price_id) if price_id else None

        if status in ("active", "trialing") and resolved_plan:
            org.plan = resolved_plan
            org.stripe_subscription_id = str(obj.get("id") or org.stripe_subscription_id or "") or None
            return f"org {org.id}: plan -> {resolved_plan} (subscription {status})"
        if status in ("canceled", "unpaid", "incomplete_expired"):
            org.plan = plans.DEFAULT_PLAN
            org.stripe_subscription_id = None
            return f"org {org.id}: plan -> {plans.DEFAULT_PLAN} (subscription {status})"
        return f"ignored {event_type}: status={status!r} price={price_id!r} -- nothing to apply"

    return f"ignored unhandled event type {event_type!r}"


def add_billing_webhook_route(app, session_factory, *, stripe: StripeSettings) -> None:
    """Mount `POST /billing/webhook`. Call only when `stripe.webhook_secret`
    is set -- omitted entirely otherwise, the same absent-unless-configured
    posture `/admin` and `/app` already follow: a deployment that has not
    opted into billing has no webhook route to probe.

    Deliberately outside `hub/console.py`'s `/app` namespace and outside any
    session-cookie auth: Stripe, not a signed-in browser, calls this route,
    and its own trust boundary is the HMAC signature checked first, before
    the body is even parsed as JSON.
    """

    async def webhook(request: Request) -> Response:
        body = await request.body()
        sig_header = request.headers.get("stripe-signature", "")
        if not verify_webhook_signature(body, sig_header, stripe.webhook_secret):
            logger.warning("stripe webhook signature verification failed")
            return PlainTextResponse("invalid signature", status_code=400)
        try:
            event = json.loads(body)
        except ValueError:
            return PlainTextResponse("invalid payload", status_code=400)
        if not isinstance(event, dict):
            return PlainTextResponse("invalid payload", status_code=400)
        async with session_scope(session_factory) as session:
            outcome = await apply_webhook_event(session, stripe, event)
        logger.info("stripe webhook: %s", outcome)
        return PlainTextResponse("ok", status_code=200)

    app.add_route("/billing/webhook", webhook, methods=["POST"])
