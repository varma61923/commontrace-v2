"""Intercom: conversation notifications -> signals, for support agents.

Built from Intercom's published webhook documentation (webhook models):
  * signature: `X-Hub-Signature` is `sha1=` followed by the hex HMAC-SHA1 of the
    JSON request body, keyed by the app's `client_secret`. SHA-1 is Intercom's
    documented scheme and is used here only as the vendor's MAC, compared in
    constant time.
  * notification envelope: `type` ("notification_event"), `id` ("notif_..."),
    `topic`, `created_at` (unix seconds), `delivery_attempts`, `data.item`. The
    `id` is the replay key. Intercom retries and throttles for up to two hours,
    so a legitimate notification can be old: freshness is bounded by FRESHNESS
    on the signed `created_at`, not by the five minutes a timestamped signature
    allows, and the id ledger does the rest.
  * topics (the documented list): conversation.admin.closed,
    conversation.admin.opened, conversation.user.replied, ... For a conversation
    `data.item.id` is the conversation id and `state` is open, closed or snoozed.

What it emits, for the occasion `<occasion_prefix><conversation id>`:
  conversation.admin.closed     CANDIDATE   (matures after window_days)
  conversation.admin.opened     REVERSAL    (reopened by a teammate)
  conversation.user.replied     REVERSAL    (the customer wrote back; ignored when
                                              nothing is pending, which is every
                                              reply before the close)
Everything else, including ping, is acknowledged and ignored. CSAT is not read:
the documented topic list has no rating topic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from hub.connectors import base

name = "intercom"

#: Intercom may deliver a notification up to two hours late (429 throttling); a
#: little slack beyond that, and a short allowance for clock skew the other way.
FRESHNESS_SECONDS = 3 * 3600
SKEW_SECONDS = 300


def validate_config(config: object) -> dict:
    return base.common_config(config, allowed=set())


def verify(headers: Mapping[str, str], body: bytes, secret: str, *, now: datetime) -> None:
    supplied = base.header(headers, "x-hub-signature")
    if not supplied.startswith("sha1="):
        raise base.SignatureError("missing or malformed X-Hub-Signature")
    expected = "sha1=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()  # noqa: S324
    if not base.constant_time_equal(expected, supplied):
        raise base.SignatureError("Intercom signature does not match")
    # The body is signed, so its `created_at` is Intercom's. A body that is not
    # JSON is the route's 400 to give; it is not this check's.
    try:
        created = json.loads(body).get("created_at")
    except (ValueError, UnicodeDecodeError, AttributeError):
        return
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        age = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).timestamp() - created
        if age > FRESHNESS_SECONDS or age < -SKEW_SECONDS:
            raise base.SignatureError("Intercom notification outside the freshness window")


def delivery_id(headers: Mapping[str, str], payload: dict) -> str:
    try:
        return base.clean_id(payload.get("id"), "notification id")
    except ValueError as exc:
        raise base.SignatureError(str(exc), status=400) from None


def signals(headers: Mapping[str, str], payload: dict, config: dict) -> list[base.Signal]:
    topic = payload.get("topic")
    if topic not in ("conversation.admin.closed", "conversation.admin.opened", "conversation.user.replied"):
        return []
    data = payload.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise ValueError("data.item must be an object")
    occasion = config.get("occasion_prefix", "") + base.clean_id(item.get("id"), "conversation id")
    event_id = delivery_id(headers, payload)
    stamp = payload.get("created_at")
    at = datetime.fromtimestamp(stamp, timezone.utc) if isinstance(stamp, (int, float)) \
        and not isinstance(stamp, bool) else datetime.now(timezone.utc)
    if topic == "conversation.admin.closed":
        return [base.Signal(base.CANDIDATE, occasion, at, event_id, note="closed")]
    return [base.Signal(base.REVERSAL, occasion, at, event_id, note=topic.rsplit(".", 1)[-1])]
