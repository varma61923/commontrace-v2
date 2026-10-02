"""Greenhouse: recruiting events -> signals, for HR and recruiting agents."""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone

from hub.connectors import base

name = "greenhouse"


def validate_config(config: object) -> dict:
    out = base.common_config(config, allowed={"success_stages"})
    stages = out.get("success_stages", [])
    if not isinstance(stages, list) or not all(isinstance(s, str) and s.strip() for s in stages) \
            or len(stages) > 50:
        raise base.ConfigError("success_stages must be a list of up to 50 stage names")
    out["success_stages"] = [s.strip() for s in stages]
    return out


def verify(headers: Mapping[str, str], body: bytes, secret: str, *, now: datetime) -> None:
    supplied = base.header(headers, "signature")
    if not supplied.startswith("sha256 "):
        raise base.SignatureError("missing or malformed Greenhouse Signature")
    expected = "sha256 " + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not base.constant_time_equal(expected, supplied):
        raise base.SignatureError("Greenhouse signature does not match")


def delivery_id(headers: Mapping[str, str], payload: dict) -> str:
    value = base.header(headers, "greenhouse-event-id").strip()
    if value:
        if len(value) > base.MAX_ID_CHARS:
            raise base.SignatureError("Greenhouse-Event-ID is too long", status=400)
        return value
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "body:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def signals(headers: Mapping[str, str], payload: dict, config: dict) -> list[base.Signal]:
    action = payload.get("action")
    if action not in ("hire_candidate", "unhire_candidate", "candidate_stage_change", "reject_candidate"):
        return []
    body = payload.get("payload")
    application = body.get("application") if isinstance(body, dict) else None
    if not isinstance(application, dict):
        raise ValueError("payload.application must be an object")
    occasion = config.get("occasion_prefix", "") + base.clean_id(application.get("id"), "application id")
    event_id = delivery_id(headers, payload)
    now = datetime.now(timezone.utc)

    if action == "hire_candidate":
        return [base.Signal(base.CANDIDATE, occasion, now, event_id, note="hired")]
    if action == "unhire_candidate":
        return [base.Signal(base.REVERSAL, occasion, now, event_id, note="unhired")]
    if action == "reject_candidate":
        return [base.Signal(base.FAILURE, occasion, now, event_id, note="rejected")]
    stage = application.get("current_stage")
    stage_name = str(stage.get("name") or "") if isinstance(stage, dict) else ""
    wanted = {s.lower() for s in config.get("success_stages", [])}
    if stage_name and stage_name.lower() in wanted:
        return [base.Signal(base.SUCCESS, occasion, now, event_id, note=f"reached {stage_name}")]
    return []
