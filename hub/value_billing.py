"""Reporting proven value to Stripe as metered usage."""
from __future__ import annotations

import hashlib

from hub import billing, plans


class ValueBillingRefused(Exception):
    """The line was not sent, and why."""


def identifier_for(line: dict, org_id: str) -> str:
    """Stable across retries: the same evidence for the same org is one charge."""
    ev = line.get("evidence") or {}
    seed = "|".join(str(x) for x in (org_id, ev.get("experiment"), ev.get("ledger_root"), ev.get("data_digest")))
    return "ct-value-" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def check(settings: billing.StripeSettings, plan: str, line: dict, *, allow_live: bool = False) -> int:
    """Validate and return the amount in minor units, or raise ValueBillingRefused."""
    if plan not in plans.BILLABLE_PLANS:
        raise ValueBillingRefused(f"plan {plan!r} is not billable: proven value is reported only for "
                                  f"{', '.join(plans.BILLABLE_PLANS)}")
    if line.get("kind") != "value":
        raise ValueBillingRefused("only a value line is reported as usage")
    amount = line.get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        raise ValueBillingRefused("a value line that is not positive reports nothing")
    ev = line.get("evidence") or {}
    if not (ev.get("ledger_root") and ev.get("data_digest") and ev.get("verify")):
        raise ValueBillingRefused("a value line without its evidence (ledger root, digest, how to verify) is not sent")
    if not settings.secret_key:
        raise ValueBillingRefused("no Stripe secret key is configured")
    if settings.secret_key.startswith("sk_live_") and not allow_live:
        raise ValueBillingRefused("a live Stripe key was supplied; pass allow_live to report to the live account")
    return int(round(amount * 100))


async def report_value_line(settings: billing.StripeSettings, *, org_id: str, plan: str, customer_id: str,
                            line: dict, event_name: str, allow_live: bool = False) -> dict:
    """Send one value line as a meter event. Returns Stripe's response."""
    if not customer_id.startswith("cus_"):
        raise ValueBillingRefused("customer_id must be a Stripe customer id (cus_...)")
    if not event_name.strip():
        raise ValueBillingRefused("event_name is the meter's event name and is required")
    minor = check(settings, plan, line, allow_live=allow_live)
    return await billing._post("billing/meter_events", settings.secret_key, {
        "event_name": event_name, "identifier": identifier_for(line, org_id),
        "payload[stripe_customer_id]": customer_id, "payload[value]": str(minor)})
