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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from hub import plans
from hub.db import session_scope
from hub.models import Organization, ProcessedWebhookEvent

logger = logging.getLogger("commontrace.hub.billing")

STRIPE_API_BASE = "https://api.stripe.com/v1"

WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(Exception):
    """A Stripe API call returned something other than 2xx."""


@dataclass(frozen=True)
class StripeSettings:
    secret_key: str = ""
    webhook_secret: str = ""
    price_team: str = ""
    price_scale: str = ""

    @property
    def checkout_configured(self) -> bool:
        return bool(self.secret_key and self.webhook_secret and (self.price_team or self.price_scale))

    def price_for_plan(self, plan: str) -> str | None:
        return {"team": self.price_team, "scale": self.price_scale}.get(plan) or None

    def plan_for_price(self, price_id: str) -> str | None:
        mapping = {price: name for name, price in (("team", self.price_team), ("scale", self.price_scale)) if price}
        return mapping.get(price_id)


async def _post(path: str, secret_key: str, data: dict[str, str]) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(f"{STRIPE_API_BASE}/{path}", data=data, auth=(secret_key, ""))
    if response.status_code >= 400:
        raise StripeError(f"Stripe {path} returned {response.status_code}: {response.text[:500]}")
    return response.json()


async def _delete(path: str, secret_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.delete(f"{STRIPE_API_BASE}/{path}", auth=(secret_key, ""))
    if response.status_code >= 400:
        raise StripeError(f"Stripe DELETE {path} returned {response.status_code}: {response.text[:500]}")
    return response.json()


async def cancel_subscription(settings: StripeSettings, *, subscription_id: str) -> None:
    """Cancel a Stripe subscription IMMEDIATELY (not at period end)."""
    await _delete(f"subscriptions/{subscription_id}", settings.secret_key)


async def create_checkout_session(
    settings: StripeSettings, *, org: Organization, plan: str, success_url: str, cancel_url: str,
) -> str:
    """Mint a Stripe-hosted Checkout URL for `org` to subscribe to `plan`."""
    price = settings.price_for_plan(plan)
    if not price:
        raise ValueError(f"no Stripe price configured for plan {plan!r}")
    data = {
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
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
    payload = await _post(
        "billing_portal/sessions", settings.secret_key, {"customer": customer_id, "return_url": return_url}
    )
    return payload["url"]


def verify_webhook_signature(
    payload: bytes, sig_header: str, secret: str, *, now: float | None = None,
    tolerance: int = WEBHOOK_TOLERANCE_SECONDS,
) -> bool:
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
    """Apply one already signature-verified Stripe event, exactly once."""
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
        try:
            async with session_scope(session_factory) as session:
                outcome = await apply_webhook_event(session, stripe, event)
        except IntegrityError:
            event_id = str(event.get("id") or "")
            if not event_id:
                raise
            async with session_scope(session_factory) as session:
                recorded = await session.get(ProcessedWebhookEvent, event_id)
            if recorded is None:
                raise
            logger.info("stripe webhook: event %s already processed (concurrent delivery)", event_id)
            return PlainTextResponse("ok", status_code=200)
        logger.info("stripe webhook: %s", outcome)
        return PlainTextResponse("ok", status_code=200)

    app.add_route("/billing/webhook", webhook, methods=["POST"])
