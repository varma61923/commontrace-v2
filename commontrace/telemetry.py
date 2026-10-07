"""Observability: spans, structured logs and metrics, with nothing to install.

- `span(name, **attrs)` times an operation. With OpenTelemetry installed and enabled
  (`COMMONTRACE_OTEL=1`, or an `OTEL_EXPORTER_OTLP_ENDPOINT`), it is a real OTel span
  exported over OTLP; either way its duration and outcome land in the metrics below.
- `bind(**fields)` attaches context (request id, tool, space, ...) that every log line
  and span inside it carries, across threads started with `contextvars.copy_context`.
- `configure_logging()` makes `commontrace.*` loggers write one JSON object per line
  (`COMMONTRACE_LOG_FORMAT=json`) with the bound context, and redacts anything that
  looks like a credential before it is written.
- `metrics()` is a snapshot of counters and latency histograms; `prometheus()`
  renders it in the Prometheus text format (the gateway serves it at /metrics).

Attribute values are redacted and truncated before export; spans never carry message
bodies, only sizes and counts."""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import math
import os
import threading
import time
import uuid
from bisect import bisect_left
from collections.abc import Callable, Iterator
from typing import Any

BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_MAX_ATTR = 256
_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("commontrace_telemetry", default={})  # noqa: B039
_LOCK = threading.Lock()
_COUNTERS: dict[tuple[str, tuple], float] = {}
_HISTOGRAMS: dict[tuple[str, tuple], list] = {}
_TRACER: Any = None
_TRACER_STATE = {"tried": False, "error": ""}


# --- redaction -------------------------------------------------------------------

def redact(value: Any) -> Any:
    """A value safe to export: credentials replaced, long strings cut."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = str(value)
    if len(text) < 12:
        return text  # too short for any credential pattern; skip loading the scanner
    from commontrace import memory_guard

    text, _found = memory_guard.redact_secrets(text)
    return text if len(text) <= _MAX_ATTR else text[:_MAX_ATTR] + "..."


def _safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Bound structured exports and scrub credentials in keys and nested values.

    Logging must also succeed for cycles, arbitrary application objects and
    huge containers. Limits apply before serialization; object representations
    are scanned and credential-named fields redact even unrecognized values.
    """
    from commontrace import memory_guard

    remaining = 2048
    ancestors: set[int] = set()

    def bound(value: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            return "[TRUNCATED log data]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, (dict, list, tuple)):
            if id(value) in ancestors:
                return "[TRUNCATED cyclic log data]"
            ancestors.add(id(value))
            try:
                if isinstance(value, dict):
                    result: dict[str, Any] = {}
                    for index, (key, nested) in enumerate(value.items()):
                        if index >= 64 or remaining < 1:
                            break
                        safe_key = str(redact(key))
                        if safe_key in result:
                            raise ValueError("duplicate sanitized log keys")
                        result[safe_key] = bound(nested, depth + 1)
                    return result
                return [bound(nested, depth + 1) for nested in value[:64]]
            finally:
                ancestors.remove(id(value))
        try:
            return redact(value)
        except Exception:
            return "[REDACTED unrenderable log data]"

    try:
        bounded = bound(fields, 0)
        safe, _ = memory_guard.sanitize_metadata(bounded, pii=memory_guard.privacy_redaction_enabled())
        return dict(safe)
    except Exception:
        return {"error": "[REDACTED invalid structured log data]"}


# --- context ---------------------------------------------------------------------

def current() -> dict:
    return dict(_CONTEXT.get())


@contextlib.contextmanager
def bind(**fields: Any) -> Iterator[dict]:
    """Add fields to the context for the duration of the block."""
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield merged
    finally:
        _CONTEXT.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def spawn_background(
    target: Callable,
    *args: Any,
    name: str = "commontrace-worker",
    daemon: bool = True,
    **kwargs: Any,
) -> threading.Thread:
    """Spawn a background thread with the current telemetry/tracing context propagated."""
    ctx = contextvars.copy_context()
    thread = threading.Thread(
        target=ctx.run,
        args=(target, *args),
        kwargs=kwargs,
        name=name,
        daemon=daemon,
    )
    thread.start()
    return thread


# --- tracing ---------------------------------------------------------------------

def otel_enabled() -> bool:
    flag = os.environ.get("COMMONTRACE_OTEL", "").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    return flag in ("1", "true", "on", "yes") or bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def _tracer():
    global _TRACER
    if _TRACER_STATE["tried"]:
        return _TRACER
    with _LOCK:
        if _TRACER_STATE["tried"]:
            return _TRACER
        _TRACER_STATE["tried"] = True
        if not otel_enabled():
            return None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = trace.get_tracer_provider()
            if not isinstance(provider, TracerProvider):
                service = os.environ.get("OTEL_SERVICE_NAME", "commontrace")
                provider = TracerProvider(resource=Resource.create({"service.name": service}))
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
                except ImportError:
                    _TRACER_STATE["error"] = "no OTLP exporter installed; spans are created but not exported"
                trace.set_tracer_provider(provider)
            _TRACER = trace.get_tracer("commontrace")
        except ImportError:
            _TRACER_STATE["error"] = "opentelemetry-sdk is not installed"
            _TRACER = None
        return _TRACER


class Span:
    """What a block can add to its span while it runs."""

    def __init__(self, name: str, otel_span=None):
        self.name = name
        self.attributes: dict[str, Any] = {}
        self._otel = otel_span

    def set(self, **attrs: Any) -> None:
        for key, value in _safe_fields(attrs).items():
            self.attributes[key] = redact(value)
            if self._otel is not None:
                with contextlib.suppress(Exception):
                    self._otel.set_attribute(f"commontrace.{key}", self.attributes[key])


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Span]:
    """Time a block as `name`; record it as an OTel span when tracing is on."""
    tracer = _tracer()
    started = time.perf_counter()
    outcome = "ok"
    cm = tracer.start_as_current_span(name) if tracer is not None else contextlib.nullcontext()
    with cm as otel_span:
        handle = Span(name, otel_span)
        handle.set(**{**{k: v for k, v in current().items() if k != "request_id"}, **attrs})
        if otel_span is not None and current().get("request_id"):
            otel_span.set_attribute("commontrace.request_id", current()["request_id"])
        try:
            yield handle
        except BaseException as exc:
            outcome = "error"
            handle.set(error=type(exc).__name__)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            observe("commontrace_operation_ms", elapsed_ms, operation=name, outcome=outcome)
            if _LOG.isEnabledFor(logging.DEBUG):
                _LOG.debug("span", extra={"span": name, "ms": round(elapsed_ms, 2), "outcome": outcome,
                                          "attrs": handle.attributes})


def traced(name: str | None = None):
    """Decorator form of `span`."""
    def wrap(func):
        label = name or f"{func.__module__.rsplit('.', 1)[-1]}.{func.__name__}"

        def inner(*args, **kwargs):
            with span(label):
                return func(*args, **kwargs)

        inner.__name__ = func.__name__
        inner.__doc__ = func.__doc__
        inner.__wrapped__ = func
        return inner
    return wrap


def wrap_tool(func, prefix: str = "mcp"):
    """Trace an (async or sync) tool function, keeping its signature for schema
    generation; each call gets its own request id."""
    import functools
    import inspect

    label = f"{prefix}.{func.__name__}"
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def run_async(*args, **kwargs):
            with bind(request_id=new_request_id(), tool=func.__name__), span(label):
                count("commontrace_tool_calls", tool=func.__name__, surface=prefix)
                return await func(*args, **kwargs)
        return run_async

    @functools.wraps(func)
    def run(*args, **kwargs):
        with bind(request_id=new_request_id(), tool=func.__name__), span(label):
            count("commontrace_tool_calls", tool=func.__name__, surface=prefix)
            return func(*args, **kwargs)
    return run


# --- metrics ---------------------------------------------------------------------

def _labels(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)[:64]) for k, v in _safe_fields(labels).items()))


def count(name: str, value: float = 1.0, **labels: Any) -> None:
    key = (name, _labels(labels))
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0.0) + value


def observe(name: str, value: float, **labels: Any) -> None:
    key = (name, _labels(labels))
    with _LOCK:
        hist = _HISTOGRAMS.get(key)
        if hist is None:
            hist = _HISTOGRAMS[key] = [[0] * (len(BUCKETS_MS) + 1), 0.0, 0]
        hist[0][bisect_left(BUCKETS_MS, value)] += 1
        hist[1] += value
        hist[2] += 1


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _HISTOGRAMS.clear()


def _quantile(buckets: list[int], total: int, q: float) -> float:
    target, seen = q * total, 0
    for i, n in enumerate(buckets):
        seen += n
        if seen >= target and n:
            return float(BUCKETS_MS[i]) if i < len(BUCKETS_MS) else float("inf")
    return 0.0


def metrics() -> dict:
    """{"counters": [...], "histograms": [...]} with p50/p95 upper bounds."""
    with _LOCK:
        counters = [{"name": n, "labels": dict(lbl), "value": v} for (n, lbl), v in sorted(_COUNTERS.items())]
        hists = [{"name": n, "labels": dict(lbl), "count": h[2], "sum_ms": round(h[1], 3),
                  "p50_ms": _quantile(h[0], h[2], 0.5), "p95_ms": _quantile(h[0], h[2], 0.95)}
                 for (n, lbl), h in sorted(_HISTOGRAMS.items())]
    return {"counters": counters, "histograms": hists}


def _prom_labels(labels: tuple, extra: tuple = ()) -> str:
    pairs = [*labels, *extra]
    if not pairs:
        return ""
    esc = (lambda v: v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))  # noqa: E731
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in pairs) + "}"


def prometheus() -> str:
    lines: list[str] = []
    with _LOCK:
        names = sorted({n for n, _ in _COUNTERS})
        for name in names:
            lines.append(f"# TYPE {name}_total counter")
            for (n, lbl), v in sorted(_COUNTERS.items()):
                if n == name:
                    lines.append(f"{name}_total{_prom_labels(lbl)} {v:g}")
        for name in sorted({n for n, _ in _HISTOGRAMS}):
            lines.append(f"# TYPE {name} histogram")
            for (n, lbl), (buckets, total, count_) in sorted(_HISTOGRAMS.items()):
                if n != name:
                    continue
                running = 0
                for bound, hits in zip((*BUCKETS_MS, "+Inf"), buckets):
                    running += hits
                    lines.append(f"{name}_bucket{_prom_labels(lbl, (('le', str(bound)),))} {running}")
                lines.append(f"{name}_sum{_prom_labels(lbl)} {total:.3f}")
                lines.append(f"{name}_count{_prom_labels(lbl)} {count_}")
    return "\n".join(lines) + "\n"


# --- structured logging -------------------------------------------------------------

_LOG = logging.getLogger("commontrace.telemetry")
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, bound context, extras;
    every string redacted."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(), "logger": record.name, "msg": redact(record.getMessage()),
        }
        entry.update({k: redact(v) for k, v in current().items()})
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value if isinstance(value, (dict, list)) else redact(value)
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info).splitlines()[-1])
        return json.dumps(_safe_fields(entry), allow_nan=False)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from commontrace import memory_guard

        text = super().format(record)
        return memory_guard.redact_secrets(text)[0]


_CONFIGURED = {"done": False}


def configure_logging(fmt: str | None = None, level: str | None = None, stream=None) -> None:
    """Configure the `commontrace` logger once (env: COMMONTRACE_LOG_FORMAT=json|text,
    COMMONTRACE_LOG_LEVEL). Without either variable nothing changes."""
    fmt = (fmt or os.environ.get("COMMONTRACE_LOG_FORMAT", "")).strip().lower()
    level = (level or os.environ.get("COMMONTRACE_LOG_LEVEL", "")).strip().upper()
    if not fmt and not level:
        return
    logger = logging.getLogger("commontrace")
    for handler in list(logger.handlers):
        if getattr(handler, "_commontrace", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler._commontrace = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if fmt == "json" else
                         _RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level, logging.INFO) if level else logging.INFO)
    logger.propagate = False
    _CONFIGURED["done"] = True


def status() -> dict:
    """For `commontrace doctor`."""
    _tracer()
    try:
        import opentelemetry  # noqa: F401

        otel_installed = True
    except ImportError:
        otel_installed = False
    return {"otel_installed": otel_installed, "otel_enabled": otel_enabled(),
            "tracing": _TRACER is not None, "note": _TRACER_STATE["error"],
            "log_format": os.environ.get("COMMONTRACE_LOG_FORMAT", "") or "default",
            "endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")}
