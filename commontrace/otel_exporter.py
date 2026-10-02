from __future__ import annotations

import datetime
import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

from commontrace import adapters, frontmatter, holdout_io, import_data, paths, templates, validate
from commontrace.commands.capture_cmd import _id_suffix
from commontrace.commands.import_cmd import _slugify

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult

_SOURCE = "otel"


def _span_to_row(span: "ReadableSpan") -> dict[str, Any]:
    status = span.status
    ctx = span.context
    return {
        "name": span.name,
        "attributes": dict(span.attributes or {}),
        "status": {
            "code": status.status_code.name if status is not None else "UNSET",
            "message": (status.description or "") if status is not None else "",
        },
        "spanId": format(ctx.span_id, "016x") if ctx is not None else "",
        "traceId": format(ctx.trace_id, "032x") if ctx is not None else "",
    }


def write_trace_from_row(
    root: str, row: dict[str, Any], *, agent_type: str, profile: str = "",
) -> str | None:
    result = import_data._row_to_trace(0, dict(row), import_data.FieldMapping(source=_SOURCE))
    if isinstance(result, import_data.SkippedRow):
        return None

    trace_id = str(uuid.uuid4())
    fm = templates.trace_frontmatter(
        trace_id, result.title, agent_type, result.tags, profile, result.outcome or None,
    )
    instance = dict(fm)
    instance["context_text"] = result.context_text
    instance["solution_text"] = result.solution_text
    schema = validate.load_schema("trace.schema.json")
    if validate.validate(instance, schema):
        return None

    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)
    date = datetime.date.today().isoformat()
    slug = _slugify(result.title)
    out_path = os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md")
    body = templates.trace_body(result.context_text, result.solution_text)
    frontmatter.write(out_path, fm, body)
    return out_path


def record_occasion_from_row(root: str, row: dict[str, Any]) -> bool:
    flat = adapters.normalize(dict(row), source=_SOURCE)
    occasion, succeeded = flat.get("occasion_id"), flat.get("occasion_succeeded")
    if not occasion or not isinstance(succeeded, bool):
        return False
    try:
        return holdout_io.record_outcome(root, occasion, succeeded)
    except holdout_io.ConflictingOutcome:
        return False


class CommonTraceSpanExporter:
    """OpenTelemetry span exporter that writes AI traces into CommonTrace store."""

    def __init__(self, *, agent_type: str, dest: str | None = None, profile: str = ""):
        try:
            import opentelemetry.sdk.trace.export  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "CommonTraceSpanExporter needs the OpenTelemetry SDK: "
                "pip install 'commontrace[otel]'"
            ) from exc
        self._root = paths.resolve_root(dest)
        self._agent_type = agent_type
        self._profile = profile
        self._stopped = False
        self._lock = threading.Lock()

    def export(self, spans) -> "SpanExportResult":
        from opentelemetry.sdk.trace.export import SpanExportResult

        with self._lock:
            if self._stopped:
                return SpanExportResult.FAILURE

        try:
            for span in spans:
                row = _span_to_row(span)
                record_occasion_from_row(self._root, row)
                write_trace_from_row(
                    self._root, row, agent_type=self._agent_type, profile=self._profile,
                )
        except Exception:  # noqa: BLE001 - never raise into the app this is attached to
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Shut down the exporter and reject future exports."""
        with self._lock:
            self._stopped = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush pending spans if not stopped."""
        with self._lock:
            return not self._stopped
