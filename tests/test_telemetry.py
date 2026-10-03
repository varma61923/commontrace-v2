"""Observability: spans feed metrics, context propagates into JSON logs, secrets are
redacted, Prometheus text renders, and the gateway and CLI are instrumented."""
import asyncio
import io
import json
import logging
import subprocess
import sys

import pytest

from commontrace import telemetry

SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture(autouse=True)
def clean():
    telemetry.reset()
    yield
    telemetry.reset()


def _hist(name, **labels):
    for h in telemetry.metrics()["histograms"]:
        if h["name"] == name and all(h["labels"].get(k) == str(v) for k, v in labels.items()):
            return h
    return None


def test_span_records_duration_and_outcome():
    with telemetry.span("unit.ok"):
        pass
    with pytest.raises(ValueError), telemetry.span("unit.fail"):
        raise ValueError("boom")
    assert _hist("commontrace_operation_ms", operation="unit.ok", outcome="ok")["count"] == 1
    assert _hist("commontrace_operation_ms", operation="unit.fail", outcome="error")["count"] == 1


def test_counters_and_prometheus_text():
    telemetry.count("commontrace_things", kind='a"b')
    telemetry.count("commontrace_things", kind='a"b')
    telemetry.observe("commontrace_wait_ms", 3.0)
    text = telemetry.prometheus()
    assert 'commontrace_things_total{kind="a\\"b"} 2' in text
    assert 'commontrace_wait_ms_bucket{le="5"} 1' in text
    assert 'commontrace_wait_ms_bucket{le="+Inf"} 1' in text
    assert "commontrace_wait_ms_count 1" in text


def test_redaction():
    assert SECRET not in telemetry.redact(f"key={SECRET}")
    assert telemetry.redact(5) == 5
    assert len(telemetry.redact("x" * 1000)) < 300


def test_json_logs_carry_context_and_redact():
    stream = io.StringIO()
    telemetry.configure_logging("json", "INFO", stream=stream)
    log = logging.getLogger("commontrace.test")
    with telemetry.bind(request_id="req-1", tool="retrieve"):
        log.info("used %s", SECRET, extra={"count": 3})
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["request_id"] == "req-1" and line["tool"] == "retrieve"
    assert line["count"] == 3 and SECRET not in line["msg"]
    logging.getLogger("commontrace").handlers.clear()
    logging.getLogger("commontrace").propagate = True


def test_bind_nests_and_resets():
    with telemetry.bind(a=1):
        with telemetry.bind(b=2):
            assert telemetry.current() == {"a": 1, "b": 2}
        assert telemetry.current() == {"a": 1}
    assert telemetry.current() == {}


def test_wrap_tool_keeps_signature_and_counts():
    import inspect

    async def retrieve(task: str, top_k: int = 5) -> dict:
        return {"task": task, "request": telemetry.current()["request_id"]}

    wrapped = telemetry.wrap_tool(retrieve)
    assert list(inspect.signature(wrapped).parameters) == ["task", "top_k"]
    assert inspect.iscoroutinefunction(wrapped)
    out = asyncio.run(wrapped("x"))
    assert out["task"] == "x" and out["request"]
    assert any(c["name"] == "commontrace_tool_calls" and c["labels"]["tool"] == "retrieve"
               for c in telemetry.metrics()["counters"])


def test_otel_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COMMONTRACE_OTEL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert telemetry.otel_enabled() is False
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    assert telemetry.otel_enabled() is True
    monkeypatch.setenv("COMMONTRACE_OTEL", "0")
    assert telemetry.otel_enabled() is False


def test_gateway_request_id_and_metrics(tmp_path):
    from commontrace.gateway import Gateway

    (tmp_path / "memory" / "lessons").mkdir(parents=True)
    gw = Gateway(str(tmp_path), token="t" * 32)
    resp = gw.handle("GET", "/v1/health", {"X-Request-Id": "abc-123"})
    assert resp.headers["X-Request-Id"] == "abc-123"
    assert len(gw.handle("GET", "/v1/health", {"X-Request-Id": "bad id!"}).headers["X-Request-Id"]) == 16
    assert gw.handle("GET", "/v1/metrics", {}).status == 401
    auth = {"Authorization": "Bearer " + "t" * 32}
    text = gw.handle("GET", "/v1/metrics", auth).body.decode()
    assert 'commontrace_gateway_requests_total{method="GET",path="/v1/health",status="200"} 2' in text
    data = json.loads(gw.handle("GET", "/v1/metrics?format=json", auth).body)
    assert data["histograms"]


def test_cli_emits_json_logs_when_configured(tmp_path):
    env = {"COMMONTRACE_LOG_FORMAT": "json", "COMMONTRACE_LOG_LEVEL": "DEBUG", "PATH": "/usr/bin:/bin"}
    import os

    env = {**os.environ, **env}
    out = subprocess.run([sys.executable, "-m", "commontrace.cli", "graph", "extract", "Redis TimeoutError",  # nosec
                          "--spacy", "off", "--dest", str(tmp_path)], capture_output=True, text=True, env=env,
                         check=False)
    assert out.returncode == 0
    spans = [json.loads(line) for line in out.stderr.splitlines() if line.startswith("{")]
    assert any(s.get("span") == "cli.graph.extract" and s.get("command") == "graph" for s in spans)
