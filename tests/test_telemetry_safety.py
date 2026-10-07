"""Nested application data cannot leak credentials through logs or metrics."""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from commontrace import telemetry


def formatted(extra: dict[str, Any]) -> str:
    record = logging.LogRecord("commontrace.boundary", logging.INFO, "", 0, "event", (), None)
    record.__dict__.update(extra)
    return telemetry.JsonFormatter().format(record)


def test_nested_extras_and_named_short_credentials_are_redacted() -> None:
    wire = formatted({"payload": {"password": "tiny", "authorization": "Bearer arbitrary123",
                                  "items": [{"api_key": "not-a-recognized-provider-key"},
                                            "https://user:secret@service.example/path"]}})
    parsed = json.loads(wire)
    assert parsed["payload"]["password"].startswith("[REDACTED")
    assert "tiny" not in wire and "arbitrary123" not in wire
    assert "not-a-recognized-provider-key" not in wire and "user:secret" not in wire


def test_structured_log_data_is_bounded_cycle_safe_and_json_finite() -> None:
    cycle: dict[str, Any] = {"count": 7}
    cycle["self"] = cycle
    wire = formatted({"payload": cycle, "large": ["x" * 1000] * 10000, "cost": float("inf")})
    parsed = json.loads(wire)
    assert parsed["payload"]["count"] == 7
    assert "cyclic" in parsed["payload"]["self"]
    assert len(parsed["large"]) <= 64 and len(wire) < 25000
    assert parsed["cost"] == "inf"


def test_application_object_representation_is_scanned() -> None:
    class ApplicationValue:
        def __str__(self) -> str:
            return "Bearer sensitive-application-value"

    wire = formatted({"details": ApplicationValue()})
    assert "sensitive-application-value" not in wire


def test_pii_policy_applies_to_structured_log_leaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMMONTRACE_REDACT_PII", "1")
    wire = formatted({"payload": {"address": "agent@example.com", "source": "192.0.2.4"}})
    assert "agent@example.com" not in wire and "192.0.2.4" not in wire


def test_span_attributes_and_metric_labels_use_named_credential_policy() -> None:
    span = telemetry.Span("safe")
    span.set(api_key="tiny", count=3)
    assert span.attributes["api_key"].startswith("[REDACTED")
    assert span.attributes["count"] == 3
    telemetry.reset()
    try:
        telemetry.count("test_safe_metric", password="tiny")
        assert "tiny" not in telemetry.prometheus()
    finally:
        telemetry.reset()
