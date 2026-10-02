"""CommonTrace OpenTelemetry Tracing Module.

Provides OpenTelemetry integration with semantic conventions (memory-semconv v0.1.0).
Designed for minimal overhead with optional dependency on opentelemetry packages.

If OpenTelemetry is not installed, all tracing functions become no-ops.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Optional

# Try to import OpenTelemetry - make it optional
try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
        SpanExporter,
        SpanExportResult,
    )
    from opentelemetry.trace import StatusCode
    from opentelemetry.trace.status import Status

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False
    # Create dummy types for type checking when OTEL is not available
    trace = None  # type: ignore
    TracerProvider = object  # type: ignore
    SpanExporter = object  # type: ignore

    from enum import Enum

    class SpanExportResult(Enum):  # type: ignore
        SUCCESS = 0
        FAILURE = 1

# ---------------------------------------------------------------------------
# Semantic attribute constants (memory-semconv v0.1.0)
# ---------------------------------------------------------------------------

# Memory operation attributes
MEMORY_OPERATION_NAME = "memory.operation.name"
MEMORY_OPERATION_TYPE = "memory.operation.type"  # store, retrieve, delete, update
MEMORY_OPERATION_STATUS = "memory.operation.status"

# Dataset/session attributes
MEMORY_DATASET_ID = "memory.dataset.id"
MEMORY_DATASET_NAME = "memory.dataset.name"
MEMORY_SESSION_ID = "memory.session.id"

# Data attributes
MEMORY_DATA_ID = "memory.data.id"
MEMORY_DATA_TYPE = "memory.data.type"
MEMORY_DATA_SIZE_BYTES = "memory.data.size_bytes"

# Query attributes
MEMORY_QUERY_TYPE = "memory.query.type"  # semantic, vector, hybrid, graph
MEMORY_QUERY_TEXT = "memory.query.text"
MEMORY_QUERY_RESULT_COUNT = "memory.query.result_count"

# Graph attributes
MEMORY_GRAPH_NODE_COUNT = "memory.graph.node_count"
MEMORY_GRAPH_EDGE_COUNT = "memory.graph.edge_count"

# Error attributes
ERROR_TYPE = "error.type"
ERROR_MESSAGE = "error.message"

# CommonTrace-specific attributes
COMMONTRACE_COMPONENT = "commontrace.component"
COMMONTRACE_VERSION = "commontrace.version"

# ---------------------------------------------------------------------------
# In-memory span exporter for testing/debugging
# ---------------------------------------------------------------------------

_MAX_TRACES = 50


class InMemorySpanExporter:
    """Simple in-memory span exporter for testing and debugging.

    Stores the last N traces in memory for inspection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._traces: dict[str, list[dict]] = {}
        self._trace_order: list[str] = []
        self._stopped: bool = False

    def export(self, spans: list) -> SpanExportResult:
        """Export spans to in-memory storage."""
        if not _OTEL_AVAILABLE:
            return SpanExportResult.SUCCESS

        with self._lock:
            if self._stopped:
                return SpanExportResult.FAILURE

            for span in spans:
                trace_id = format(span.context.trace_id, "032x")
                span_dict = {
                    "name": span.name,
                    "trace_id": trace_id,
                    "span_id": format(span.context.span_id, "016x"),
                    "parent_span_id": (
                        format(span.parent.span_id, "016x") if span.parent else None
                    ),
                    "start_time_ns": span.start_time,
                    "end_time_ns": span.end_time,
                    "duration_ms": (
                        (span.end_time - span.start_time) / 1_000_000
                        if span.end_time and span.start_time
                        else 0.0
                    ),
                    "status": span.status.status_code.name if span.status else "UNSET",
                    "attributes": dict(span.attributes) if span.attributes else {},
                }

                if trace_id not in self._traces:
                    self._traces[trace_id] = []
                    self._trace_order.append(trace_id)

                self._traces[trace_id].append(span_dict)

                # Evict oldest traces if over limit
                while len(self._trace_order) > _MAX_TRACES:
                    oldest = self._trace_order.pop(0)
                    self._traces.pop(oldest, None)

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Shutdown the exporter and reject future exports."""
        with self._lock:
            self._stopped = True
            self._traces.clear()
            self._trace_order.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush - checks stopped state."""
        with self._lock:
            return not self._stopped

    def get_last_trace(self) -> list[dict] | None:
        """Get the last completed trace."""
        with self._lock:
            if not self._trace_order:
                return None
            last_id = self._trace_order[-1]
            return list(self._traces[last_id])

    def get_all_traces(self) -> dict[str, list[dict]]:
        """Get all stored traces."""
        with self._lock:
            return {tid: list(spans) for tid, spans in self._traces.items()}

    def clear(self) -> None:
        """Clear all stored traces."""
        with self._lock:
            self._traces.clear()
            self._trace_order.clear()


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_tracer: Optional[trace.Tracer] = None
_provider: Optional[TracerProvider] = None
_exporter: Optional[InMemorySpanExporter] = None
_configured: bool = False
_config_lock = threading.Lock()


def setup_tracing(service_name: str = "commontrace",
                  service_version: str = "2.0.0",
                  console_output: bool = False,
                  otlp_endpoint: str | None = None) -> Optional[trace.Tracer]:
    """Set up OpenTelemetry tracing for CommonTrace.

    This function is idempotent - calling it multiple times will not
    reconfigure tracing.

    Args:
        service_name: Service name for OpenTelemetry resource
        service_version: Service version
        console_output: If True, export spans to console for debugging
        otlp_endpoint: Optional OTLP endpoint for remote export

    Returns:
        The configured tracer, or None if OpenTelemetry is not available
    """
    global _tracer, _provider, _exporter, _configured

    if not _OTEL_AVAILABLE:
        return None

    with _config_lock:
        if _configured:
            return _tracer

        # Create resource
        deployment_env = (
            os.getenv("DEPLOYMENT_ENVIRONMENT")
            or os.getenv("ENVIRONMENT")
            or "production"
        )
        resource = Resource.create({
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": deployment_env,
        })

        # Create provider
        _provider = TracerProvider(resource=resource)

        # Add in-memory exporter for debugging
        _exporter = InMemorySpanExporter()
        _provider.add_span_processor(SimpleSpanProcessor(_exporter))

        # Add console exporter if requested
        if console_output:
            console_exporter = ConsoleSpanExporter()
            _provider.add_span_processor(SimpleSpanProcessor(console_exporter))

        # Add OTLP exporter if endpoint provided
        if otlp_endpoint:
            _try_add_otlp_exporter(_provider, otlp_endpoint)

        # Register as global tracer provider
        trace.set_tracer_provider(_provider)

        # Get tracer
        _tracer = _provider.get_tracer("commontrace", service_version)
        _configured = True

        return _tracer


def _try_add_otlp_exporter(provider: TracerProvider, endpoint: str) -> None:
    """Try to add OTLP exporter; silently fails if package not installed."""
    import logging

    logger = logging.getLogger("commontrace.tracing")

    try:
        # Try HTTP exporter first (more compatible)
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPHttpSpanExporter,
        )
        exporter = OTLPHttpSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("OTel: OTLP HTTP trace exporter registered → %s", endpoint)
    except ImportError:
        try:
            # Fall back to gRPC
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("OTel: OTLP gRPC trace exporter registered → %s", endpoint)
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but OTLP exporter packages "
                "are not installed. Install with: pip install opentelemetry-exporter-otlp"
            )


def get_tracer() -> Optional[trace.Tracer]:
    """Get the configured tracer, or None if not configured."""
    return _tracer


def get_exporter() -> Optional[InMemorySpanExporter]:
    """Get the in-memory span exporter for testing."""
    return _exporter


def shutdown_tracing() -> None:
    """Shutdown tracing and clear global state."""
    global _tracer, _provider, _exporter, _configured

    with _config_lock:
        if _provider:
            _provider.force_flush()
            _provider.shutdown()
        _tracer = None
        _provider = None
        _exporter = None
        _configured = False


# ---------------------------------------------------------------------------
# Tracing context managers and helpers
# ---------------------------------------------------------------------------

_current_span: ContextVar[Any] = ContextVar("current_span", default=None)


class _FallbackSpan:
    """Lightweight span representation when OpenTelemetry is unavailable."""

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.name = name
        self.attributes = dict(attributes or {})
        self.status = "UNSET"
        self.status_description = ""
        self.exceptions: list[Exception] = []

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self.attributes.update(attributes)

    def set_status(self, status: Any, description: str = "") -> None:
        self.status = str(status)
        self.status_description = description

    def record_exception(self, exception: Exception) -> None:
        self.exceptions.append(exception)
        self.status = "ERROR"
        self.status_description = str(exception)


@contextmanager
def start_as_current_span(name: str, attributes: dict | None = None):
    """Context manager for creating a span as the current span.

    Args:
        name: Span name
        attributes: Optional span attributes

    Example:
        with start_as_current_span("memory.store", attributes={"operation": "ingest"}):
            # do work
    """
    attrs = dict(attributes or {})
    if not _OTEL_AVAILABLE or _tracer is None:
        fallback_span = _FallbackSpan(name, attrs)
        token = _current_span.set(fallback_span)
        try:
            yield fallback_span
        finally:
            _current_span.reset(token)
        return

    with _tracer.start_as_current_span(name, attributes=attrs) as span:
        token = _current_span.set(span)
        try:
            yield span
        finally:
            _current_span.reset(token)


def get_current_span() -> Any:
    """Retrieve the current active span (OTel or fallback)."""
    if _OTEL_AVAILABLE:
        otel_span = trace.get_current_span()
        if otel_span and otel_span.get_span_context().is_valid:
            return otel_span
    return _current_span.get()


def add_span_attributes(**attributes) -> None:
    """Add attributes to the current span.

    Args:
        **attributes: Key-value pairs to add as span attributes
    """
    span = get_current_span()
    if span is not None:
        span.set_attributes(attributes)


def set_span_status(status: str, description: str = "") -> None:
    """Set the status of the current span.

    Args:
        status: Status string ("OK", "ERROR", "UNSET")
        description: Optional description
    """
    span = get_current_span()
    if span is not None:
        if _OTEL_AVAILABLE and hasattr(span, "set_status") and not isinstance(span, _FallbackSpan):
            status_code = getattr(StatusCode, status.upper(), StatusCode.UNSET)
            span.set_status(Status(status_code, description))
        elif hasattr(span, "set_status"):
            span.set_status(status, description)


def record_exception(exception: Exception) -> None:
    """Record an exception on the current span.

    Args:
        exception: The exception to record
    """
    span = get_current_span()
    if span is not None:
        span.record_exception(exception)
        if _OTEL_AVAILABLE and not isinstance(span, _FallbackSpan):
            span.set_status(Status(StatusCode.ERROR, str(exception)))


# ---------------------------------------------------------------------------
# Convenience functions for common operations
# ---------------------------------------------------------------------------

@contextmanager
def trace_memory_operation(operation_type: str, **attributes):
    """Trace a memory operation with standard attributes.

    Args:
        operation_type: Type of operation (store, retrieve, delete, update)
        **attributes: Additional attributes

    Example:
        with trace_memory_operation("store", dataset_id="abc", data_size=1024):
            # perform store operation
    """
    attrs = {MEMORY_OPERATION_TYPE: operation_type}
    attrs.update(attributes)
    with start_as_current_span(f"memory.{operation_type}", attributes=attrs):
        yield


@contextmanager
def trace_query(query_type: str, query_text: str = "", **attributes):
    """Trace a memory query operation.

    Args:
        query_type: Type of query (semantic, vector, hybrid, graph)
        query_text: The query text
        **attributes: Additional attributes

    Example:
        with trace_query("semantic", query_text="find similar documents"):
            # perform query
    """
    attrs = {
        MEMORY_QUERY_TYPE: query_type,
        MEMORY_QUERY_TEXT: query_text,
    }
    attrs.update(attributes)
    with start_as_current_span(f"memory.query.{query_type}", attributes=attrs):
        yield
