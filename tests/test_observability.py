"""Tests for CommonTrace observability modules."""

from __future__ import annotations

import logging

from commontrace import metrics, observability, tracing


class TestObservability:
    """Tests for observability.py (structured logging)."""

    def test_configure_logging_is_idempotent(self):
        """Calling configure_logging multiple times should not duplicate handlers."""
        observability.configure_logging("INFO")
        first = len(logging.getLogger().handlers)
        observability.configure_logging("INFO")
        assert len(logging.getLogger().handlers) == first == 1

    def test_get_logger_returns_logger(self):
        """get_logger should return a valid logger instance."""
        logger = observability.get_logger("test.module")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test.module"

    def test_trace_context_propagation(self):
        """Trace context should be propagated through context manager."""
        assert observability.get_trace_id() is None
        assert observability.get_operation_id() is None

        with observability.trace_context(trace_id="test-123", operation_id="test-op"):
            assert observability.get_trace_id() == "test-123"
            assert observability.get_operation_id() == "test-op"

        assert observability.get_trace_id() is None
        assert observability.get_operation_id() is None

    def test_redact_secrets_api_keys(self):
        """API keys should be redacted."""
        text = "api_key=sk-abcdefghijklmnop1234567890"
        redacted = observability.redact_secrets(text)
        assert "sk-abc***REDACTED***" in redacted
        assert "1234567890" not in redacted

    def test_redact_secrets_bearer_tokens(self):
        """Bearer tokens should be redacted."""
        text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
        redacted = observability.redact_secrets(text)
        assert "***REDACTED***" in redacted
        assert "123456" not in redacted

    def test_redact_secrets_passwords(self):
        """Passwords should be redacted."""
        text = "password=secret12345"
        redacted = observability.redact_secrets(text)
        assert "***REDACTED***" in redacted
        assert "secret12345" not in redacted

    def test_redact_secrets_empty_text(self):
        """Empty text should be returned as-is."""
        assert observability.redact_secrets("") == ""
        assert observability.redact_secrets(None) is None

    def test_log_operation(self):
        """log_operation should log without errors."""
        observability.log_operation("test_operation", level="INFO", key="value")
        # If we get here without exception, the test passes

    def test_log_error(self):
        """log_error should log errors without exceptions."""
        try:
            raise ValueError("test error")
        except Exception as e:
            observability.log_error("test_operation", e, context="test")


class TestTracing:
    """Tests for tracing.py (OpenTelemetry integration)."""

    def test_setup_tracing_without_otel(self, monkeypatch):
        """setup_tracing should return None when OTEL is not available."""
        # Simulate OTEL not being available
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        result = tracing.setup_tracing()
        assert result is None

    def test_get_tracer_without_otel(self, monkeypatch):
        """get_tracer should return None when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        assert tracing.get_tracer() is None

    def test_start_as_current_span_without_otel(self, monkeypatch):
        """start_as_current_span should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        with tracing.start_as_current_span("test.span"):
            pass  # Should not raise

    def test_add_span_attributes_without_otel(self, monkeypatch):
        """add_span_attributes should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        tracing.add_span_attributes(key="value")  # Should not raise

    def test_set_span_status_without_otel(self, monkeypatch):
        """set_span_status should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        tracing.set_span_status("OK")  # Should not raise

    def test_record_exception_without_otel(self, monkeypatch):
        """record_exception should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        try:
            raise ValueError("test")
        except Exception as e:
            tracing.record_exception(e)  # Should not raise

    def test_trace_memory_operation_without_otel(self, monkeypatch):
        """trace_memory_operation should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        with tracing.trace_memory_operation("store", dataset_id="test"):
            pass  # Should not raise

    def test_trace_query_without_otel(self, monkeypatch):
        """trace_query should be a no-op when OTEL is not available."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", False)
        with tracing.trace_query("semantic", query_text="test"):
            pass  # Should not raise


class TestMetrics:
    """Tests for metrics.py (OpenTelemetry metrics)."""

    def test_setup_metrics_without_otel(self, monkeypatch):
        """setup_metrics should return None when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        result = metrics.setup_metrics()
        assert result is None

    def test_get_meter_without_otel(self, monkeypatch):
        """get_meter should return None when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        assert metrics.get_meter() is None

    def test_record_operation_duration_without_otel(self, monkeypatch):
        """record_operation_duration should be a no-op when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        metrics.record_operation_duration(100.0)  # Should not raise

    def test_increment_items_stored_without_otel(self, monkeypatch):
        """increment_items_stored should be a no-op when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        metrics.increment_items_stored(5)  # Should not raise

    def test_increment_items_retrieved_without_otel(self, monkeypatch):
        """increment_items_retrieved should be a no-op when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        metrics.increment_items_retrieved(3)  # Should not raise

    def test_record_query_results_without_otel(self, monkeypatch):
        """record_query_results should be a no-op when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        metrics.record_query_results(10)  # Should not raise

    def test_increment_operation_errors_without_otel(self, monkeypatch):
        """increment_operation_errors should be a no-op when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)
        metrics.increment_operation_errors()  # Should not raise

    def test_all_metric_helpers_without_otel(self, monkeypatch):
        """All metric helper functions should be no-ops when OTEL is not available."""
        monkeypatch.setattr(metrics, "_OTEL_METRICS_AVAILABLE", False)

        # Test all helper functions
        metrics.increment_bytes_stored(1024)
        metrics.increment_bytes_retrieved(512)
        metrics.increment_vector_searches()
        metrics.increment_graph_edges(5)
        metrics.increment_graph_nodes(3)
        metrics.increment_graph_edges_deleted(2)
        metrics.increment_graph_nodes_deleted(1)
        metrics.increment_items_deleted(1)
        metrics.increment_items_updated(1)
        metrics.increment_cache_hits()
        metrics.increment_cache_misses()

        # If we get here without exceptions, all tests pass


class TestStructuredFormatter:
    """Tests for StructuredFormatter."""

    def test_formatter_adds_context_fields(self):
        """Formatter should add trace_id, operation_id, etc. to log records."""
        formatter = observability.StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )

        with observability.trace_context(trace_id="test-123", operation_id="test-op"):
            formatted = formatter.format(record)
            assert "trace_id=test-123" in formatted
            assert "operation_id=test-op" in formatted

    def test_formatter_redacts_secrets(self):
        """Formatter should redact secrets from formatted messages."""
        formatter = observability.StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="api_key=sk-abcdefghijklmnop1234567890",
            args=(),
            exc_info=None,
        )

        formatted = formatter.format(record)
        assert "***REDACTED***" in formatted
        assert "1234567890" not in formatted


class TestInMemorySpanExporter:
    """Tests for InMemorySpanExporter."""

    def test_export_stores_spans(self, monkeypatch):
        """Exporter should store spans in memory."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", True)

        exporter = tracing.InMemorySpanExporter()

        # Create a mock span
        class MockSpan:
            def __init__(self):
                self.name = "test.span"
                self.context = MockContext()
                self.parent = None
                self.start_time = 0
                self.end_time = 1_000_000  # 1ms in nanoseconds
                self.status = MockStatus()
                self.attributes = {"key": "value"}

        class MockContext:
            def __init__(self):
                self.trace_id = 12345
                self.span_id = 67890

        class MockStatus:
            def __init__(self):
                self.status_code = MockStatusCode()

        class MockStatusCode:
            name = "OK"

        exporter.export([MockSpan()])
        traces = exporter.get_all_traces()
        assert len(traces) == 1
        assert len(traces[list(traces.keys())[0]]) == 1

    def test_export_limits_trace_count(self, monkeypatch):
        """Exporter should limit the number of stored traces."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", True)
        monkeypatch.setattr(tracing, "_MAX_TRACES", 5)

        exporter = tracing.InMemorySpanExporter()

        # Define mock classes outside the loop
        class MockStatusCode:
            name = "OK"

        class MockStatus:
            status_code = MockStatusCode()

        class MockContext:
            def __init__(self, trace_id):
                self.trace_id = trace_id
                self.span_id = 1

        # Create mock spans with different trace IDs
        for i in range(10):
            class MockSpan:
                def __init__(self, trace_id, idx):
                    self.name = f"span.{idx}"
                    self.context = MockContext(trace_id)
                    self.parent = None
                    self.start_time = 0
                    self.end_time = 1_000_000
                    self.status = MockStatus()
                    self.attributes = {}

            exporter.export([MockSpan(i, i)])

        # Should only keep 5 traces
        assert len(exporter.get_all_traces()) == 5

    def test_clear_removes_all_traces(self, monkeypatch):
        """clear should remove all stored traces."""
        monkeypatch.setattr(tracing, "_OTEL_AVAILABLE", True)

        exporter = tracing.InMemorySpanExporter()

        # Define mock classes
        class MockStatusCode:
            name = "OK"

        class MockStatus:
            status_code = MockStatusCode()

        class MockContext:
            trace_id = 12345
            span_id = 67890

        class MockSpan:
            def __init__(self):
                self.name = "test.span"
                self.context = MockContext()
                self.parent = None
                self.start_time = 0
                self.end_time = 1_000_000
                self.status = MockStatus()
                self.attributes = {}

        exporter.export([MockSpan()])
        assert len(exporter.get_all_traces()) == 1

        exporter.clear()
        assert len(exporter.get_all_traces()) == 0
