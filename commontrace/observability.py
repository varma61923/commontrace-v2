"""CommonTrace Observability Module.

Provides structured logging with context propagation and secret redaction.
Designed for minimal overhead (<5%) while enabling 10x faster debugging.

This module uses stdlib logging for simplicity and compatibility, with
optional OpenTelemetry integration available in tracing.py and metrics.py.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_operation_id: ContextVar[str | None] = ContextVar("operation_id", default=None)
_dataset_id: ContextVar[str | None] = ContextVar("dataset_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)


def get_trace_id() -> str | None:
    """Get the current trace ID from context, falling back to active OpenTelemetry span."""
    tid = _trace_id.get()
    if tid:
        return tid
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            return format(span.get_span_context().trace_id, "032x")
    except (ImportError, AttributeError):
        pass
    return None


def set_trace_id(trace_id: str) -> Any:
    """Set the trace ID in context. Returns a token for reset."""
    return _trace_id.set(trace_id)


def get_operation_id() -> str | None:
    """Get the current operation ID from context."""
    return _operation_id.get()


def set_operation_id(operation_id: str) -> Any:
    """Set the operation ID in context. Returns a token for reset."""
    return _operation_id.set(operation_id)


def get_dataset_id() -> str | None:
    """Get the current dataset ID from context."""
    return _dataset_id.get()


def set_dataset_id(dataset_id: str) -> Any:
    """Set the dataset ID in context. Returns a token for reset."""
    return _dataset_id.set(dataset_id)


def get_session_id() -> str | None:
    """Get the current session ID from context."""
    return _session_id.get()


def set_session_id(session_id: str) -> Any:
    """Set the session ID in context. Returns a token for reset."""
    return _session_id.set(session_id)


@contextmanager
def trace_context(trace_id: str | None = None, operation_id: str | None = None,
                  dataset_id: str | None = None, session_id: str | None = None):
    """Context manager for setting trace context.

    Usage:
        with trace_context(trace_id="abc-123", operation_id="ingest"):
            logger.info("Processing data")
    """
    tokens = []
    vars = []
    if trace_id:
        tokens.append(set_trace_id(trace_id))
        vars.append(_trace_id)
    if operation_id:
        tokens.append(set_operation_id(operation_id))
        vars.append(_operation_id)
    if dataset_id:
        tokens.append(set_dataset_id(dataset_id))
        vars.append(_dataset_id)
    if session_id:
        tokens.append(set_session_id(session_id))
        vars.append(_session_id)

    try:
        yield
    finally:
        for var, token in zip(vars, tokens):
            var.reset(token)


# ---------------------------------------------------------------------------
# Secret redaction patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    # OpenAI API keys
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),
    # Generic API key patterns
    re.compile(r"(api[_-]?key\s*[=:]\s*)['\"]?[A-Za-z0-9\-_]{16,}['\"]?", re.IGNORECASE),
    # Bearer tokens
    re.compile(r"(bearer\s+)[A-Za-z0-9\-_\.]{20,}", re.IGNORECASE),
    # Passwords
    re.compile(r"(password\s*[=:]\s*)['\"]?[^\s'\"]{8,}['\"]?", re.IGNORECASE),
    # JWT tokens (simplified pattern)
    re.compile(r"(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,})"),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{16})"),
    # GitHub tokens
    re.compile(r"(ghp_[A-Za-z0-9]{36})|(gho_[A-Za-z0-9]{36})|(ghu_[A-Za-z0-9]{36})"),
]


def redact_secrets(text: str) -> str:
    """Redact common API key and secret patterns from text.

    Args:
        text: Input string that may contain secrets

    Returns:
        Text with secrets replaced by [REDACTED] markers
    """
    if not text:
        return text

    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: m.group(0)[:6] + "***REDACTED***", result)
    return result


# ---------------------------------------------------------------------------
# Structured logging formatter
# ---------------------------------------------------------------------------

class StructuredFormatter(logging.Formatter):
    """Custom formatter that adds context fields and redacts secrets.

    Output format: [level] timestamp logger_name trace_id=... operation_id=... message
    """

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        if fmt is None:
            fmt = (
                "[%(levelname)s] %(asctime)s %(name)s trace_id=%(trace_id)s "
                "operation_id=%(operation_id)s dataset_id=%(dataset_id)s "
                "session_id=%(session_id)s %(message)s"
            )
        if datefmt is None:
            datefmt = "%Y-%m-%d %H:%M:%S"
        super().__init__(fmt=fmt, datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        # Add context fields to record without clobbering existing values
        record.trace_id = getattr(record, "trace_id", None) or get_trace_id() or ""
        record.operation_id = getattr(record, "operation_id", None) or get_operation_id() or ""
        record.dataset_id = getattr(record, "dataset_id", None) or get_dataset_id() or ""
        record.session_id = getattr(record, "session_id", None) or get_session_id() or ""

        # Format the message
        formatted = super().format(record)

        # Redact secrets from the formatted message
        return redact_secrets(formatted)


# ---------------------------------------------------------------------------
# Logger configuration
# ---------------------------------------------------------------------------

_configured: bool = False
_config_lock = threading.Lock()


def configure_logging(level: str | int = logging.INFO,
                      handler: logging.Handler | None = None) -> None:
    """Configure structured logging for CommonTrace.

    This function is idempotent - calling it multiple times will not
    duplicate handlers.

    Args:
        level: Log level (string or int). Default: INFO
        handler: Optional custom handler. If None, uses StreamHandler to stdout.
    """
    global _configured

    with _config_lock:
        if _configured:
            return

        # Convert string level to int if needed
        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)

        # Create or use provided handler
        if handler is None:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(StructuredFormatter())

        # Configure root logger
        root_logger = logging.getLogger()
        root_logger.setLevel(level)

        # Remove any existing handlers to avoid duplicates
        root_logger.handlers.clear()

        # Add our handler
        root_logger.addHandler(handler)

        # Set level for commontrace package
        logging.getLogger("commontrace").setLevel(level)

        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Get a logger with the given name.

    Ensures logging is configured before returning the logger.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    if not _configured:
        configure_logging()
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def log_operation(operation: str, level: str = "INFO", **kwargs) -> None:
    """Log an operation with context.

    Args:
        operation: Description of the operation
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        **kwargs: Additional context fields
    """
    logger = get_logger("commontrace.operations")
    log_func = getattr(logger, level.lower(), logger.info)

    context = {"operation": operation}
    context.update(kwargs)

    log_func("%s", operation, extra=context)


def log_error(operation: str, error: Exception, **kwargs) -> None:
    """Log an error with context.

    Args:
        operation: Description of the operation that failed
        error: The exception that occurred
        **kwargs: Additional context fields
    """
    logger = get_logger("commontrace.errors")
    context = {
        "operation": operation,
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    context.update(kwargs)
    logger.error("%s failed: %s", operation, error, extra=context, exc_info=True)

    try:
        from commontrace import metrics, tracing
        tracing.record_exception(error)
        metrics.increment_operation_errors(attributes={"operation": operation, "error.type": type(error).__name__})
    except Exception:
        pass
