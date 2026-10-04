from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import json
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import select

from hub.encryption import NULL_CIPHER, EnvelopeCipher
from hub.models import WebhookDelivery, WebhookEndpoint

_SECRET_DOMAIN = "commontrace-webhook-secret-v1"
_SIGNATURE_VERSION = "v1"

REPLAY_TOLERANCE_SECONDS = 300

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

MAX_ATTEMPTS = 8

_BACKOFF = (30, 60, 300, 900, 3600, 7200, 21600)


class EventError(Exception):
    """An event could not be emitted as described."""


_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 carrier-grade NAT / shared space
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / AWS / GCP / Azure metadata service
)


@dataclass(frozen=True)
class Allowlist:
    """Operator-configured exceptions to the private-range block."""

    hostnames: frozenset[str] = frozenset()
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    def allows_host(self, host: str) -> bool:
        return host.lower() in self.hostnames

    def allows_ip(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in net for net in self.networks)


async def _default_resolve(hostname: str) -> list:
    return await asyncio.to_thread(socket.getaddrinfo, hostname, None)


async def _reject_private_target(
    url: str, *, resolve=None, allowlist: Allowlist | None = None,
) -> list[str]:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise EventError(f"webhook scheme must be http or https, got {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise EventError(f"a webhook endpoint must have a resolvable host, got {url!r}")
    if allowlist and allowlist.allows_host(hostname):
        return [hostname]
    resolve = resolve or _default_resolve
    try:
        addrinfo = await resolve(hostname)
    except socket.gaierror as exc:
        raise EventError(f"could not resolve webhook host {hostname!r}: {exc}") from None
    validated_ips: list[str] = []
    for family, _type, _proto, _canonname, sockaddr in addrinfo:
        raw_ip = sockaddr[0]
        ip = ipaddress.ip_address(raw_ip)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if allowlist and allowlist.allows_ip(ip):
            validated_ips.append(raw_ip)
            continue
        if (
            any(ip in net for net in _EXTRA_BLOCKED_NETWORKS)
            or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or not ip.is_global
        ):
            raise EventError(
                f"webhook host {hostname!r} resolves to {raw_ip}, a private/"
                "internal address. Events are egress to a third party's own "
                "infrastructure, never a way to reach this deployment's own "
                "internal network."
            )
        validated_ips.append(raw_ip)
    return validated_ips


@dataclass(frozen=True)
class EventType:
    name: str
    describe: str
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
        EventType(
            "user.privileged_role_granted",
            "A person now holds Security Admin or Owner",
            ("user_id", "role", "actor"),
        ),
    )
}

EVENT_NAMES = tuple(EVENT_TYPES)

_ALLOWED_VALUE_TYPES = (str, int, float, bool, type(None))

_MAX_FIELD_CHARS = 200


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def check_payload(event_type: str, payload: dict) -> dict:
    """Validate one payload against its type's declared fields."""
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


def derive_secret(signing_key: str, endpoint_id: str, key_version: int) -> str:
    """The endpoint's signing secret, recomputed rather than stored."""
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
    """The `X-CommonTrace-Signature` value for one delivery."""
    signed = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},{_SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str, header: str, body: str, *,
    now: int | None = None, tolerance: int = REPLAY_TOLERANCE_SECONDS,
) -> bool:
    """Whether a delivery is genuine and recent."""
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
    return hmac.compare_digest(sent, expected)


async def add_endpoint(
    session, org_id: str, url: str, *, events: list[str] | None = None,
    signing_key: str = "", cipher: EnvelopeCipher = NULL_CIPHER,
) -> tuple[WebhookEndpoint, str]:
    """Register a URL. Returns the endpoint and its secret, shown once."""
    if not url.startswith("https://"):
        raise EventError(
            f"a webhook endpoint must be https, got {url!r}. Events carry no "
            "trace content, but they do carry trace ids and experiment "
            "verdicts, and plaintext delivery puts those on the wire."
        )
    await _reject_private_target(url)
    for name in events or []:
        if name not in EVENT_TYPES:
            raise EventError(
                f"unknown event type {name!r}; known types: "
                + ", ".join(EVENT_NAMES)
            )
    endpoint = WebhookEndpoint(
        org_id=org_id, url=cipher.encrypt(url), events=sorted(set(events or EVENT_NAMES)),
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


async def emit(
    session, org_id: str, event_type: str, payload: dict | None = None,
    *, now: datetime.datetime | None = None,
) -> list[WebhookDelivery]:
    """Queue one delivery per subscribed, enabled endpoint."""
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
    """The JSON body a receiver sees."""
    return {
        "event_id": delivery.id,
        "type": delivery.event_type,
        "org_id": delivery.org_id,
        "created_at": delivery.created_at.isoformat(),
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
    cipher: EnvelopeCipher = NULL_CIPHER,
) -> DeliveryResult:
    """Attempt the deliveries that are due."""
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
            await transport(cipher.decrypt(endpoint.url), body, headers)
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
    query = select(WebhookDelivery).where(WebhookDelivery.status == STATUS_FAILED)
    if org_id:
        query = query.where(WebhookDelivery.org_id == org_id)
    rows = await session.execute(
        query.order_by(WebhookDelivery.created_at.desc()).limit(limit)
    )
    return list(rows.scalars())


def http_transport(timeout: float = 10.0, allowlist: Allowlist | None = None):
    """The default transport: an HTTPS POST that raises on a bad status.

    Hardened against SSRF (CWE-918):
    - Deny-by-default for private/loopback/link-local/metadata ranges.
    - follow_redirects=False (no redirects followed across egress).
    """
    import httpx

    async def send(url: str, body: str, headers: dict) -> None:
        await _reject_private_target(url, allowlist=allowlist)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(url, content=body, headers=headers)
            response.raise_for_status()

    return send
