"""Telling a customer's own systems what happened here, without telling them
anything that was in a trace.

WHY THIS EXISTS
---------------
Everything this Hub knows was readable only by polling it. A fleet that
wanted to open a ticket when a memory was quarantined, or gate a deploy on
an experiment reaching a verdict, had to run a cron job against
`hub/manage.py` and diff the output against last time. That is the shape
that makes a product a silo: the interesting moments are known here and
nowhere else, at the moment they happen and never again.

So: a small, versioned set of events, queued durably and delivered to an
HTTP endpoint the org configures, signed so the receiver can tell a real one
from anything else.

WHAT AN EVENT MAY CARRY, AND WHY IT IS A WHITELIST
--------------------------------------------------
A webhook is EGRESS to a third party. This product's entire trust story is
that a fleet's experience stays on the fleet's own infrastructure, so an
event bus that shipped `context_text` to whatever URL was last configured
would quietly undo it -- and would do so in the one place nobody looks,
since a webhook is set up once and then forgotten.

Every event type therefore declares its exact fields, and `emit` REFUSES a
payload with any other key. Not a denylist of dangerous names -- those fail
the moment someone adds a field nobody thought to ban -- and not a
convention, because a convention is what an urgent Friday patch is exempt
from. Events carry ids, counts, verdicts and timestamps. To find out what a
trace SAYS, a receiver has to come back and ask, authenticated, over the
tenant-scoped API.

THE SIGNING SECRET IS NOT STORED
--------------------------------
Unlike an API key, which the Hub only ever VERIFIES (and so can keep as an
argon2 hash), a webhook secret has to be used to compute an HMAC on every
delivery -- it must be recoverable, which normally means a secret sitting in
a column waiting for a database dump to find it.

It is instead DERIVED, per endpoint, from the deployment's own signing key
(`HUB_LEDGER_SIGNING_KEY`) plus the endpoint id and its key version. So the
database holds a version integer and nothing else; an attacker with a full
dump of `webhook_endpoints` learns which URLs an org uses and gains no
ability to forge a single event. Rotation bumps the version, which changes
the derived secret without touching a stored value.

DELIVERY IS AT-LEAST-ONCE, AND SAYS SO
--------------------------------------
Deliveries are queued rows, retried with backoff, and given up on loudly
rather than silently. A receiver must therefore be idempotent on `event_id`
-- which is why every payload carries one, and why the docs say this rather
than implying exactly-once and being wrong at the worst moment.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
from dataclasses import dataclass, field

from sqlalchemy import select

from hub.models import WebhookDelivery, WebhookEndpoint

_SECRET_DOMAIN = "commontrace-webhook-secret-v1"
_SIGNATURE_VERSION = "v1"

#: How far a delivery's timestamp may be from the receiver's clock before it
#: should be rejected as a replay. Published here because the receiver is
#: the one enforcing it and needs a number to enforce.
REPLAY_TOLERANCE_SECONDS = 300

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

#: After this many attempts a delivery is given up on and marked failed.
#: Bounded rather than infinite: an endpoint that has been wrong for a week
#: is a configuration problem, and a queue that retries it forever hides
#: that behind a number nobody reads.
MAX_ATTEMPTS = 8

#: Backoff in seconds per attempt, then the last value repeats.
_BACKOFF = (30, 60, 300, 900, 3600, 7200, 21600)


class EventError(Exception):
    """An event could not be emitted as described."""


@dataclass(frozen=True)
class EventType:
    name: str
    describe: str
    #: The ONLY keys a payload of this type may carry. See the module
    #: docstring: this is a whitelist because a denylist fails the moment
    #: somebody adds a field nobody thought to ban.
    fields: tuple[str, ...] = field(default_factory=tuple)


EVENT_TYPES: dict[str, EventType] = {
    e.name: e for e in (
        EventType(
            "trace.created", "A new trace was contributed",
            ("trace_id", "agent_type", "agent_id", "n_tags"),
        ),
        EventType(
            "trace.quarantined",
            "A trace was held pending review (hub/abuse.py)",
            ("trace_id", "reason"),
        ),
        EventType(
            "trace.deleted", "A trace was deleted",
            ("trace_id",),
        ),
        EventType(
            "experiment.started",
            "A randomized holdout began withholding memory",
            ("rate", "salt", "outcome"),
        ),
        EventType(
            "experiment.stopped", "A randomized holdout stopped",
            ("salt",),
        ),
        EventType(
            "experiment.verdict",
            "The holdout reached a verdict for one memory -- the event a "
            "deploy gate actually wants",
            ("trace_id", "verdict", "effect", "p_value", "n_treated",
             "n_control", "underpowered"),
        ),
        EventType(
            "retention.purged", "A retention plan was applied",
            ("plan", "n_deleted", "n_held"),
        ),
        EventType(
            "legal_hold.placed", "Data was frozen against retention",
            ("hold_id", "object_type", "target_id"),
        ),
        EventType(
            "legal_hold.released", "A freeze was lifted",
            ("hold_id",),
        ),
        EventType(
            "usage.limit_reached",
            "An org hit a plan entitlement (hub/plans.py)",
            ("metric", "limit", "used"),
        ),
        EventType(
            "alert.triggered",
            "An alert rule's threshold was crossed (hub/alerts.py)",
            ("rule_id", "metric", "comparator", "threshold", "value"),
        ),
        EventType(
            "report.generated",
            "A periodic usage summary was generated (hub/alerts.py)",
            ("period", "plan", "traces_total", "commons_queries_used",
             "commons_queries_allowance"),
        ),
    )
}

EVENT_NAMES = tuple(EVENT_TYPES)

#: Values an event field may hold. Deliberately scalars only: a nested
#: object is where free text gets in without anyone deciding to put it
#: there.
_ALLOWED_VALUE_TYPES = (str, int, float, bool, type(None))

#: An id or a verdict is short. A field long enough to be prose is prose.
_MAX_FIELD_CHARS = 200


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def check_payload(event_type: str, payload: dict) -> dict:
    """Validate one payload against its type's declared fields.

    Raises rather than dropping the offending key: an event silently
    stripped of the field a receiver was built around fails at the
    receiver, hours later, as a mystery.
    """
    try:
        spec = EVENT_TYPES[event_type]
    except KeyError:
        raise EventError(
            f"unknown event type {event_type!r}; known types: "
            + ", ".join(EVENT_NAMES)
        ) from None

    unknown = sorted(set(payload) - set(spec.fields))
    if unknown:
        raise EventError(
            f"{event_type} may not carry {', '.join(unknown)}. Its fields are "
            f"{', '.join(spec.fields)}. Events carry ids and counts, never "
            "trace content -- a receiver that needs the text asks for it over "
            "the tenant-scoped API."
        )
    for key, value in payload.items():
        if not isinstance(value, _ALLOWED_VALUE_TYPES):
            raise EventError(
                f"{event_type}.{key} must be a string, number, boolean or "
                f"null, not {type(value).__name__}: a nested object is where "
                "free text gets in without anyone deciding to put it there"
            )
        if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
            raise EventError(
                f"{event_type}.{key} is {len(value)} characters; the limit is "
                f"{_MAX_FIELD_CHARS}. A field long enough to be prose is prose, "
                "and prose does not leave this deployment in an event."
            )
    return dict(payload)


# --- endpoints and secrets ---------------------------------------------------

def derive_secret(signing_key: str, endpoint_id: str, key_version: int) -> str:
    """The endpoint's signing secret, recomputed rather than stored.

    See the module docstring: an API key is only ever verified and so can
    live as a hash, but a webhook secret must be USED on every delivery.
    Deriving it means a full dump of `webhook_endpoints` yields no ability
    to forge an event.
    """
    if not signing_key:
        raise EventError(
            "this deployment has no HUB_LEDGER_SIGNING_KEY, so webhook "
            "signatures cannot be derived. Set one before configuring an "
            "endpoint: an unsigned webhook is indistinguishable from anything "
            "else that can reach the customer's URL."
        )
    return hmac.new(
        signing_key.encode("utf-8"),
        f"{_SECRET_DOMAIN}{endpoint_id}{key_version}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def signature_header(secret: str, timestamp: int, body: str) -> str:
    """The `X-CommonTrace-Signature` value for one delivery.

    `t=<unix>,v1=<hex>`, with the timestamp INSIDE the signed material.
    Signing the body alone would let anyone who captured one delivery
    replay it forever, and the timestamp is only protection if altering it
    breaks the signature.
    """
    signed = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},{_SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str, header: str, body: str, *,
    now: int | None = None, tolerance: int = REPLAY_TOLERANCE_SECONDS,
) -> bool:
    """Whether a delivery is genuine and recent.

    Lives here, in the product, rather than only in the docs: a receiver
    implementing this from prose gets the timestamp-in-the-signed-material
    detail wrong roughly half the time, and this function is what the tests
    and the customer-facing example both use.
    """
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    sent = parts.get(_SIGNATURE_VERSION, "")
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    moment = now if now is not None else int(_now().timestamp())
    if abs(moment - timestamp) > tolerance:
        return False
    expected = signature_header(secret, timestamp, body).split(f"{_SIGNATURE_VERSION}=")[1]
    # compare_digest, not ==: a plain comparison leaks the position of the
    # first differing byte through timing, which is enough to forge one.
    return hmac.compare_digest(sent, expected)


async def add_endpoint(
    session, org_id: str, url: str, *, events: list[str] | None = None,
    signing_key: str = "",
) -> tuple[WebhookEndpoint, str]:
    """Register a URL. Returns the endpoint and its secret, shown once."""
    if not url.startswith("https://"):
        raise EventError(
            f"a webhook endpoint must be https, got {url!r}. Events carry no "
            "trace content, but they do carry trace ids and experiment "
            "verdicts, and plaintext delivery puts those on the wire."
        )
    for name in events or []:
        if name not in EVENT_TYPES:
            raise EventError(
                f"unknown event type {name!r}; known types: "
                + ", ".join(EVENT_NAMES)
            )
    endpoint = WebhookEndpoint(
        org_id=org_id, url=url, events=sorted(set(events or EVENT_NAMES)),
    )
    session.add(endpoint)
    await session.flush()
    return endpoint, derive_secret(signing_key, endpoint.id, endpoint.key_version)


async def rotate_secret(session, endpoint_id: str, *, signing_key: str = "") -> str:
    endpoint = await session.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise EventError(f"no webhook endpoint with id {endpoint_id}")
    endpoint.key_version += 1
    await session.flush()
    return derive_secret(signing_key, endpoint.id, endpoint.key_version)


async def endpoints_for(session, org_id: str) -> list[WebhookEndpoint]:
    rows = await session.execute(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.org_id == org_id)
        .order_by(WebhookEndpoint.created_at)
    )
    return list(rows.scalars())


# --- emitting ----------------------------------------------------------------

async def emit(
    session, org_id: str, event_type: str, payload: dict | None = None,
    *, now: datetime.datetime | None = None,
) -> list[WebhookDelivery]:
    """Queue one delivery per subscribed, enabled endpoint.

    Adds to the caller's session without committing, like `audit.record` and
    for the same reason: an event announcing something that then rolled back
    is worse than no event, because the receiver acts on it.

    An org with no endpoints is the overwhelmingly common case and costs one
    indexed query returning nothing -- callers do not have to check first,
    which is what keeps emit calls from being conditionally skipped and
    quietly forgotten.
    """
    body = check_payload(event_type, payload or {})
    rows = await session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.org_id == org_id,
            WebhookEndpoint.enabled.is_(True),
        )
    )
    moment = now or _now()
    queued = []
    for endpoint in rows.scalars():
        if event_type not in (endpoint.events or ()):
            continue
        delivery = WebhookDelivery(
            org_id=org_id, endpoint_id=endpoint.id, event_type=event_type,
            payload=body, created_at=moment, next_attempt_at=moment,
        )
        session.add(delivery)
        queued.append(delivery)
    return queued


def envelope(delivery: WebhookDelivery) -> dict:
    """The JSON body a receiver sees.

    `event_id` is the idempotency key and is stable across retries --
    delivery is at-least-once, and a receiver that treats each POST as a
    new fact will double-count the first time a timeout is followed by a
    successful retry.

    Call this AFTER incrementing `attempts`, which is what `deliver_pending`
    does: `attempt` is meant to number the delivery being made, so a first
    delivery that announced itself as attempt 2 would read to a receiver as
    "you already missed one".
    """
    return {
        "event_id": delivery.id,
        "type": delivery.event_type,
        "org_id": delivery.org_id,
        "created_at": delivery.created_at.isoformat(),
        # `attempts` is incremented BEFORE the send (see deliver_pending), so
        # this is the number of THIS attempt: the first delivery says 1.
        "attempt": delivery.attempts,
        "data": delivery.payload or {},
    }


def _backoff(attempts: int) -> int:
    return _BACKOFF[min(attempts, len(_BACKOFF) - 1)]


@dataclass(frozen=True)
class DeliveryResult:
    attempted: int = 0
    delivered: int = 0
    retrying: int = 0
    gave_up: int = 0


async def deliver_pending(
    session, transport, *, signing_key: str = "",
    now: datetime.datetime | None = None, limit: int = 100,
) -> DeliveryResult:
    """Attempt the deliveries that are due.

    `transport(url, body, headers) -> None` raising on failure. Injected
    rather than imported so this is testable without a network, and so a
    deployment can put its own egress proxy, allowlist or mTLS in front of
    it without this module growing an opinion about any of them.
    """
    moment = now or _now()
    rows = await session.execute(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == STATUS_PENDING,
            WebhookDelivery.next_attempt_at <= moment,
        )
        .order_by(WebhookDelivery.created_at)
        .limit(limit)
    )
    attempted = delivered = retrying = gave_up = 0
    for delivery in rows.scalars():
        endpoint = await session.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None or not endpoint.enabled:
            # The endpoint went away or was disabled after this was queued.
            # Marked failed with a reason rather than retried forever or
            # deleted: "we stopped trying, here is why" is the fact an
            # operator needs.
            delivery.status = STATUS_FAILED
            delivery.last_error = "endpoint removed or disabled before delivery"
            gave_up += 1
            continue

        attempted += 1
        delivery.attempts += 1
        body = json.dumps(envelope(delivery), ensure_ascii=False, sort_keys=True)
        timestamp = int(moment.timestamp())
        headers = {
            "Content-Type": "application/json",
            "X-CommonTrace-Event": delivery.event_type,
            "X-CommonTrace-Event-Id": delivery.id,
            "X-CommonTrace-Signature": signature_header(
                derive_secret(signing_key, endpoint.id, endpoint.key_version),
                timestamp, body,
            ),
        }
        try:
            await transport(endpoint.url, body, headers)
        except Exception as exc:  # noqa: BLE001 - any failure is a retry
            delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if delivery.attempts >= MAX_ATTEMPTS:
                delivery.status = STATUS_FAILED
                gave_up += 1
            else:
                delivery.next_attempt_at = moment + datetime.timedelta(
                    seconds=_backoff(delivery.attempts)
                )
                retrying += 1
            continue
        delivery.status = STATUS_DELIVERED
        delivery.delivered_at = moment
        delivery.last_error = ""
        delivered += 1

    return DeliveryResult(attempted, delivered, retrying, gave_up)


async def pending_count(session, org_id: str | None = None) -> int:
    from sqlalchemy import func

    query = select(func.count()).select_from(WebhookDelivery).where(
        WebhookDelivery.status == STATUS_PENDING
    )
    if org_id:
        query = query.where(WebhookDelivery.org_id == org_id)
    return int(await session.scalar(query) or 0)


async def failed_deliveries(
    session, org_id: str | None = None, limit: int = 50
) -> list[WebhookDelivery]:
    """What never landed. The dead-letter view an operator actually needs:
    a queue that gives up silently is a queue that lies about delivery."""
    query = select(WebhookDelivery).where(WebhookDelivery.status == STATUS_FAILED)
    if org_id:
        query = query.where(WebhookDelivery.org_id == org_id)
    rows = await session.execute(
        query.order_by(WebhookDelivery.created_at.desc()).limit(limit)
    )
    return list(rows.scalars())


def http_transport(timeout: float = 10.0):
    """The default transport: an HTTPS POST that raises on a bad status.

    A separate factory rather than a module-level client so a deployment
    can put its own egress proxy, allowlist or mTLS in front of deliveries
    without this module growing an opinion about any of them -- and so the
    tests never need a network.
    """
    import httpx

    async def send(url: str, body: str, headers: dict) -> None:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, content=body, headers=headers)
            # A 2xx is the only success. A receiver answering 200 to
            # everything is its own problem; a receiver answering 500 is
            # ours to retry, and treating "it returned bytes" as delivery
            # is how a queue comes to report 100% success while the other
            # end has been broken for a week.
            response.raise_for_status()

    return send
