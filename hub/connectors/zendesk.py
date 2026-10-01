"""Zendesk: ticket events -> signals.

Built from Zendesk's published documentation:
  * signature: developer.zendesk.com/documentation/webhooks/verifying/ --
    `X-Zendesk-Webhook-Signature` is base64(HMAC-SHA256(secret, TIMESTAMP + BODY)),
    TIMESTAMP being `X-Zendesk-Webhook-Signature-Timestamp` (ISO-8601 UTC,
    e.g. 2021-03-25T05:09:27Z). The secret is used as the literal string, not
    base64-decoded.
  * payloads: the event-subscribed ticket events
    (api-reference/webhooks/event-types/ticket-events/): top-level `id` (event
    uuid), `type`, `time`, `detail.id` (ticket id) and, for status changes,
    `event.current` / `event.previous`; for CSAT, `event.satisfaction_score.score`.
    Statuses are upper case (NEW, OPEN, PENDING, HOLD, SOLVED, CLOSED).

What it emits, for the occasion `<occasion_prefix><ticket id>`:
  ticket.status_changed to SOLVED/CLOSED          CANDIDATE   (matures after window_days)
  ticket.status_changed from SOLVED/CLOSED back   REVERSAL    (the ticket was reopened)
  ticket.csat_received with a BAD score           FAILURE
Everything else is acknowledged and ignored. A GOOD score is not a success on
its own: success is staying solved. Human takeover is not exposed by these
events and is not inferred.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timezone

from hub.connectors import base

name = "zendesk"

_SOLVED = {"SOLVED", "CLOSED"}
_STATUS = "zen:event-type:ticket.status_changed"
_CSAT = "zen:event-type:ticket.csat_received"


def validate_config(config: object) -> dict:
    return base.common_config(config, allowed=set())


def verify(headers: Mapping[str, str], body: bytes, secret: str, *, now: datetime) -> None:
    signature = base.header(headers, "x-zendesk-webhook-signature")
    stamp = base.header(headers, "x-zendesk-webhook-signature-timestamp")
    if not signature or not stamp:
        raise base.SignatureError("missing Zendesk signature headers")
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), stamp.encode("utf-8") + body, hashlib.sha256).digest()
    ).decode("ascii")
    if not base.constant_time_equal(expected, signature):
        raise base.SignatureError("Zendesk signature does not match")
    # Checked after the signature, so the timestamp being read is the signed one.
    try:
        signed_at = base.parse_timestamp(stamp)
    except ValueError:
        raise base.SignatureError("unreadable Zendesk signature timestamp") from None
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if abs((current - signed_at).total_seconds()) > base.TOLERANCE_SECONDS:
        raise base.SignatureError("Zendesk signature timestamp outside the replay window")


def delivery_id(headers: Mapping[str, str], payload: dict) -> str:
    """The event's own uuid, from the (already authenticated) payload."""
    try:
        return base.clean_id(payload.get("id"), "event id")
    except ValueError as exc:
        raise base.SignatureError(str(exc), status=400) from None


def signals(headers: Mapping[str, str], payload: dict, config: dict) -> list[base.Signal]:
    kind = payload.get("type")
    if kind not in (_STATUS, _CSAT):
        return []
    detail, event = payload.get("detail"), payload.get("event")
    if not isinstance(detail, dict) or not isinstance(event, dict):
        raise ValueError("detail and event must be objects")
    prefix = config.get("occasion_prefix", "")
    occasion = prefix + base.clean_id(detail.get("id"), "detail.id")
    event_id = base.clean_id(payload.get("id"), "event id")
    at = base.parse_timestamp(str(payload.get("time") or "")) if payload.get("time") else \
        datetime.now(timezone.utc)

    if kind == _STATUS:
        current = str(event.get("current") or "").upper()
        previous = str(event.get("previous") or "").upper()
        if current in _SOLVED and previous not in _SOLVED:
            return [base.Signal(base.CANDIDATE, occasion, at, event_id, note=f"{previous or '?'} -> {current}")]
        if previous in _SOLVED and current and current not in _SOLVED:
            return [base.Signal(base.REVERSAL, occasion, at, event_id, note=f"reopened ({previous} -> {current})")]
        return []

    rating = event.get("satisfaction_score")
    score = str(rating.get("score") if isinstance(rating, dict) else "").upper()
    if score == "BAD":
        return [base.Signal(base.FAILURE, occasion, at, event_id, note="CSAT BAD")]
    return []
