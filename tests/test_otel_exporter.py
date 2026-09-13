"""A live OpenTelemetry SpanExporter that turns completed GenAI spans
into CommonTrace traces as they happen -- audit 6.2's "runtime wrapper"
half.

What these tests defend, in order of how badly getting it wrong would
hurt:

1. **A real span, through a real TracerProvider, produces a real, valid
   trace file** -- not just a unit test of the row-flattening helper in
   isolation.
2. **It reuses the same parsing (`_otel`) and write path `commontrace
   import --source otel` uses**, so the two surfaces cannot silently
   diverge on what counts as a valid GenAI span.
3. **A span with no GenAI content is skipped, not an error** -- an
   uninstrumented span is not a trace, and an exporter that raises would
   take the whole application down for an unrelated telemetry
   side-channel.
4. **The core package works with none of this installed** -- importing
   `commontrace.otel_exporter` itself must never require the SDK; only
   constructing the exporter class does.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip(
    "opentelemetry.sdk", reason="needs the OpenTelemetry SDK: pip install 'commontrace[otel]'"
)

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.trace import Status, StatusCode  # noqa: E402

from commontrace import frontmatter, otel_exporter, paths  # noqa: E402


def _provider_with_exporter(dest, **kwargs):
    exporter = otel_exporter.CommonTraceSpanExporter(dest=str(dest), **kwargs)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def _traces(dest):
    tdir = paths.traces_dir(str(dest))
    if not os.path.isdir(tdir):
        return []
    return [os.path.join(tdir, f) for f in sorted(os.listdir(tdir))]


class TestEndToEndThroughARealTracerProvider:
    def test_a_gen_ai_span_becomes_a_trace_file(self, tmp_path):
        provider = _provider_with_exporter(tmp_path, agent_type="support")
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.prompt", "How do I reset a password?")
            span.set_attribute("gen_ai.completion", "Send a reset link to the account email.")
            span.set_status(Status(StatusCode.OK))
        provider.shutdown()

        written = _traces(tmp_path)
        assert len(written) == 1
        fm, body = frontmatter.read(written[0])
        assert fm["agent_type"] == "support"
        assert "reset a password" in fm["title"].lower() or "reset a password" in body.lower()
        assert "How do I reset a password?" in body
        assert "Send a reset link" in body
        assert fm["outcome"]["resolved"] is True

    def test_an_error_status_is_recorded_as_unresolved(self, tmp_path):
        provider = _provider_with_exporter(tmp_path, agent_type="support")
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.prompt", "p")
            span.set_attribute("gen_ai.completion", "c")
            span.set_status(Status(StatusCode.ERROR, "timed out"))
        provider.shutdown()

        written = _traces(tmp_path)
        fm, body = frontmatter.read(written[0])
        assert fm["outcome"]["resolved"] is False

    def test_a_span_with_no_gen_ai_attributes_is_skipped_not_an_error(self, tmp_path):
        """An uninstrumented span (e.g. an HTTP client span from an
        unrelated library sharing the same TracerProvider) is not a
        trace -- and must not raise into the application."""
        provider = _provider_with_exporter(tmp_path, agent_type="support")
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("GET /health"):
            pass
        provider.shutdown()

        assert _traces(tmp_path) == []

    def test_multiple_spans_produce_multiple_traces(self, tmp_path):
        provider = _provider_with_exporter(tmp_path, agent_type="support")
        tracer = provider.get_tracer("test")
        for i in range(3):
            with tracer.start_as_current_span(f"chat-{i}") as span:
                span.set_attribute("gen_ai.prompt", f"question {i}")
                span.set_attribute("gen_ai.completion", f"answer {i}")
        provider.shutdown()
        assert len(_traces(tmp_path)) == 3

    def test_token_usage_attributes_are_recorded(self, tmp_path):
        provider = _provider_with_exporter(tmp_path, agent_type="support")
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.prompt", "p")
            span.set_attribute("gen_ai.completion", "c")
            span.set_attribute("gen_ai.usage.input_tokens", 100)
            span.set_attribute("gen_ai.usage.output_tokens", 50)
        provider.shutdown()

        fm, _ = frontmatter.read(_traces(tmp_path)[0])
        assert fm["outcome"]["tokens_used"] == 150

    def test_the_profile_is_recorded_when_given(self, tmp_path):
        provider = _provider_with_exporter(tmp_path, agent_type="support", profile="code-review")
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.prompt", "p")
            span.set_attribute("gen_ai.completion", "c")
        provider.shutdown()

        fm, _ = frontmatter.read(_traces(tmp_path)[0])
        assert fm["profile"] == "code-review"


class TestWriteTraceFromRow:
    """The lower-level function, exercised directly against hand-built
    rows -- useful for shapes a real SDK span is unlikely to produce but
    that a receiver should still handle defensively."""

    def test_a_row_with_no_attributes_is_skipped(self, tmp_path):
        result = otel_exporter.write_trace_from_row(
            str(tmp_path), {"name": "span", "attributes": {}, "status": {}},
            agent_type="support",
        )
        assert result is None
        assert _traces(tmp_path) == []

    def test_a_well_formed_row_writes_a_file_that_exists(self, tmp_path):
        result = otel_exporter.write_trace_from_row(
            str(tmp_path),
            {
                "name": "span",
                "attributes": {"gen_ai.prompt": "p", "gen_ai.completion": "c"},
                "status": {},
            },
            agent_type="support",
        )
        assert result is not None
        assert os.path.isfile(result)


class TestSpanToRow:
    def test_flattens_attributes_status_and_ids(self, tmp_path):
        provider = TracerProvider()
        tracer = provider.get_tracer("test")
        captured = {}

        class _Capture:
            def export(self, spans):
                from opentelemetry.sdk.trace.export import SpanExportResult

                captured["span"] = spans[0]
                return SpanExportResult.SUCCESS

            def shutdown(self):
                pass

            def force_flush(self, timeout_millis=30000):
                return True

        provider.add_span_processor(SimpleSpanProcessor(_Capture()))
        with tracer.start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.prompt", "p")
            span.set_status(Status(StatusCode.OK))
        provider.shutdown()

        row = otel_exporter._span_to_row(captured["span"])
        assert row["name"] == "chat"
        assert row["attributes"]["gen_ai.prompt"] == "p"
        assert row["status"]["code"] == "OK"
        assert len(row["spanId"]) == 16
        assert len(row["traceId"]) == 32


def test_importing_the_module_never_requires_the_sdk():
    """Only constructing CommonTraceSpanExporter needs opentelemetry --
    this file already imported the module above, at collection time,
    with the SDK installed; this test instead proves the class itself
    fails informatively when it is NOT, by simulating the absence."""
    import sys

    real_modules = {
        name: mod for name, mod in sys.modules.items()
        if name == "opentelemetry" or name.startswith("opentelemetry.")
    }
    for name in real_modules:
        del sys.modules[name]
    sys.modules["opentelemetry"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(ImportError, match=r"commontrace\[otel\]"):
            otel_exporter.CommonTraceSpanExporter(agent_type="support")
    finally:
        del sys.modules["opentelemetry"]
        sys.modules.update(real_modules)
