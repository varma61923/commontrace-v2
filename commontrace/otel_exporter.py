"""A live OpenTelemetry `SpanExporter` that turns completed GenAI-
semantic-convention spans into CommonTrace traces as they happen --
audit 6.2's "runtime wrapper" half.

"CommonTrace consumes OTel, it does not emit it" was true before this
module: the only path in was an offline `commontrace import --source
otel <file>`. This is not auto-instrumentation (it adds no
instrumentation to anything; your application, or an existing vendor
SDK, must already be producing spans) and not an OTel Collector
component -- it is the runtime wrapper you attach to a `TracerProvider`
you already have, so a span becomes a trace the moment it completes
instead of via an export-then-import round trip.

REUSES THE SAME PARSING AND WRITE PATH `commontrace import` USES
-------------------------------------------------------------------
Every span is converted through `commontrace/adapters.py`'s existing
`_otel`-shaped normalization (`import_data._row_to_trace`, the exact
function `commontrace import --source otel` calls per row) and written
through the identical schema-validated, atomically-written path -- not
a second, parallel writer with its own idea of what a valid trace is.
A span with no recognizable GenAI content (no prompt/completion
attributes) is silently skipped, the same way `commontrace import`
skips a row missing a required field: an uninstrumented span is not an
error, it simply is not a trace.

OPTIONAL, LAZILY IMPORTED
-------------------------
`commontrace[otel]` is the only thing that needs `opentelemetry-sdk` --
importing THIS module costs nothing extra; only constructing
`CommonTraceSpanExporter` imports the SDK's types, matching
`commontrace/hub_client.py`'s own "the core CLI install stays
PyYAML-only" discipline for its own optional dependency.
"""

from __future__ import annotations

import datetime
import os
import uuid
from typing import TYPE_CHECKING, Any

from commontrace import frontmatter, import_data, paths, templates, validate
from commontrace.commands.capture_cmd import _id_suffix
from commontrace.commands.import_cmd import _slugify

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExportResult

_SOURCE = "otel"


def _span_to_row(span: "ReadableSpan") -> dict[str, Any]:
    """A live `ReadableSpan`, flattened to the same {name, attributes,
    status} shape `commontrace/adapters.py`'s file-based OTel parsing
    already reads. `attributes` here is a plain dict (the SDK's native
    in-memory shape); `adapters.otel_attributes` already handles that
    shape as well as the OTLP-JSON {key, value} list a wire export uses,
    so no second attribute-flattening path is needed for either.
    """
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
    """Normalize one already-flattened span row and write it as a trace
    through the SAME schema-validated, atomically-written path
    `commontrace import --source otel` uses.

    Returns the path written, or None if the row has no recognizable
    GenAI content, or produces a schema-invalid trace -- both are a
    skip, never an exception: an exporter that raises into the
    application it is attached to would take that application down for
    a telemetry side-channel unrelated to its actual work.
    """
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
    # Unconditionally id-suffixed, same reasoning as capture_cmd.py/
    # import_cmd.py: a live exporter is exactly the concurrent-write case
    # those modules' own comments describe -- many spans can complete and
    # export in the same second.
    out_path = os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md")
    body = templates.trace_body(result.context_text, result.solution_text)
    # Atomic (NamedTemporaryFile + os.replace) via frontmatter.write, same
    # as every other writer in this package.
    frontmatter.write(out_path, fm, body)
    return out_path


class CommonTraceSpanExporter:
    """An `opentelemetry.sdk.trace.export.SpanExporter`. Attach it to your
    own `TracerProvider`:

        provider.add_span_processor(BatchSpanProcessor(CommonTraceSpanExporter(
            agent_type="support",
        )))

    and a completed span carrying GenAI semantic-convention attributes
    becomes a CommonTrace trace the moment it exports -- no file, no
    separate `commontrace import` step.
    """

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

    def export(self, spans) -> "SpanExportResult":
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            for span in spans:
                write_trace_from_row(
                    self._root, _span_to_row(span),
                    agent_type=self._agent_type, profile=self._profile,
                )
        except Exception:  # noqa: BLE001 - never raise into the app this is attached to
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
