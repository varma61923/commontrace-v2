"""Intercom: conversation notifications -> signals, for support agents."""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from hub.connectors import base

name = "intercom"

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
