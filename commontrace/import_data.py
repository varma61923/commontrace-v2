"""Bulk-import existing trace-like records (a support/CRM/observability
export) into memory/traces/, so a fleet can start from its historical traces
without replacing any existing infrastructure (see README, "Deploying to
Production"). This is deliberately a generic, format-level
importer (JSONL or CSV, with configurable field-name mapping) rather than a
set of vendor-specific connectors (Zendesk, Salesforce, Datadog, ...): this
codebase has no way to test against a real vendor API, and a set of
unverified vendor integrations would be a much larger, riskier claim than
"you can get your existing export into the protocol's Trace shape."

Row -> Trace field mapping is configurable (--title-field etc., see
commontrace/commands/import_cmd.py) because a real export's column/key
names are whatever the source system happens to call them -- there is no
universal standard to assume.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

# Must stay in sync with the `outcome` properties in trace.schema.json. A
# field missing here is not a validation error -- it is silently dropped from
# the imported trace, which is worse: `baseline` was absent, so every
# historical baseline row imported as an ACTIVE trace and `bench --pilot`
# compared the intervention against a control set that no longer existed.
_OUTCOME_BOOL_FIELDS = (
    "resolved", "escalated", "repeated_error", "frustration_signal", "baseline",
)
_OUTCOME_INT_FIELDS = ("tokens_used", "llm_calls")


@dataclass
class FieldMapping:
    title: str = "title"
    context: str = "context"
    solution: str = "solution"
    tags: str = "tags"
    id: str = "id"


@dataclass
class ImportedRow:
    line_no: int
    title: str
    context_text: str
    solution_text: str
    tags: list[str]
    source_id: str = ""
    outcome: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkippedRow:
    line_no: int
    reason: str


def _parse_tags(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    # CSV rows and some JSON exports carry tags as a delimited string.
    text = str(raw)
    for delim in (";", "|", ","):
        if delim in text:
            return [t.strip() for t in text.split(delim) if t.strip()]
    return [text.strip()] if text.strip() else []


def _parse_bool(raw: Any) -> bool | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return None


def _extract_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Outcome fields can arrive two shapes: flat (a CSV row, or a JSONL
    row a spreadsheet tool wrote with `resolved`/`baseline`/etc. as
    top-level columns) or nested under an `outcome` key -- which is what
    this product's OWN exports look like, since trace.schema.json declares
    `outcome` as a nested object (see templates.trace_frontmatter). Only
    checking top-level keys silently dropped every outcome field when
    re-importing our own JSONL output: `baseline`, `resolved`, and the rest
    all disappeared with no error, so a re-imported baseline trace looked
    like an ordinary ACTIVE trace with no outcome recorded at all. A
    top-level field wins over a nested one of the same name if a row
    somehow has both."""
    nested = row.get("outcome")
    nested = nested if isinstance(nested, dict) else {}

    outcome: dict[str, Any] = {}
    for field_name in _OUTCOME_BOOL_FIELDS:
        source = row if field_name in row else nested
        if field_name in source:
            parsed = _parse_bool(source[field_name])
            if parsed is not None:
                outcome[field_name] = parsed
    for field_name in _OUTCOME_INT_FIELDS:
        source = row if field_name in row else nested
        if field_name in source and str(source[field_name]).strip() != "":
            try:
                outcome[field_name] = int(source[field_name])
            except (ValueError, TypeError):
                pass
    return outcome


# The protocol's own field names (protocol/schemas/trace.schema.json). An export
# produced by this product -- `sync --pull`, a Hub `search_traces` dump -- uses
# these, so importing our own output must not require --context-field flags to
# rename a field into the very name we emitted it under.
_PROTOCOL_ALIASES = {"context": "context_text", "solution": "solution_text"}


def _pick(row: dict[str, Any], name: str, alias: str | None = None) -> str:
    value = str(row.get(name, "") or "").strip()
    if not value and alias:
        value = str(row.get(alias, "") or "").strip()
    return value


def _row_to_trace(line_no: int, row: dict[str, Any], mapping: FieldMapping) -> ImportedRow | SkippedRow:
    title = _pick(row, mapping.title)
    context_text = _pick(row, mapping.context, _PROTOCOL_ALIASES.get(mapping.context))
    solution_text = _pick(row, mapping.solution, _PROTOCOL_ALIASES.get(mapping.solution))

    missing = [
        name
        for name, value in (
            (mapping.title, title),
            (f"{mapping.context}/{_PROTOCOL_ALIASES.get(mapping.context, mapping.context)}", context_text),
            (f"{mapping.solution}/{_PROTOCOL_ALIASES.get(mapping.solution, mapping.solution)}", solution_text),
        )
        if not value
    ]
    if missing:
        return SkippedRow(line_no=line_no, reason=f"missing/empty required field(s): {', '.join(missing)}")

    return ImportedRow(
        line_no=line_no,
        title=title,
        context_text=context_text,
        solution_text=solution_text,
        tags=_parse_tags(row.get(mapping.tags)),
        source_id=str(row.get(mapping.id, "") or ""),
        outcome=_extract_outcome(row),
    )


def iter_jsonl(lines: Iterator[str], mapping: FieldMapping) -> Iterator[ImportedRow | SkippedRow]:
    """Row by row, never materializing the whole file.

    `parse_jsonl` below builds on this but collects everything into two
    lists, which is convenient for a handful of rows and is exactly the
    shape that reads a multi-gigabyte historical export entirely into
    memory before writing a single trace file -- a container with a memory
    limit gets OOM-killed before `import_cmd.py` reports anything. Use this
    directly (as `import_cmd.py` does) when the input might be large.
    """
    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            yield SkippedRow(line_no=i, reason=f"invalid JSON: {exc}")
            continue
        if not isinstance(row, dict):
            yield SkippedRow(line_no=i, reason=f"expected a JSON object, got {type(row).__name__}")
            continue
        yield _row_to_trace(i, row, mapping)


def iter_csv(fh, mapping: FieldMapping) -> Iterator[ImportedRow | SkippedRow]:
    """Row by row -- see iter_jsonl. `csv.DictReader` itself already reads
    incrementally; this just avoids collecting its output into a list."""
    reader = csv.DictReader(fh)
    for i, row in enumerate(reader, start=2):  # header is line 1
        yield _row_to_trace(i, dict(row), mapping)


def parse_jsonl(lines: Iterator[str], mapping: FieldMapping) -> tuple[list[ImportedRow], list[SkippedRow]]:
    """List-collecting convenience wrapper over iter_jsonl, kept for small
    inputs and for callers (including this module's own tests) that want
    the whole result at once. `import_cmd.py` uses iter_jsonl directly."""
    imported: list[ImportedRow] = []
    skipped: list[SkippedRow] = []
    for result in iter_jsonl(lines, mapping):
        (imported if isinstance(result, ImportedRow) else skipped).append(result)
    return imported, skipped


def parse_csv(fh, mapping: FieldMapping) -> tuple[list[ImportedRow], list[SkippedRow]]:
    """List-collecting convenience wrapper over iter_csv -- see parse_jsonl."""
    imported: list[ImportedRow] = []
    skipped: list[SkippedRow] = []
    for result in iter_csv(fh, mapping):
        (imported if isinstance(result, ImportedRow) else skipped).append(result)
    return imported, skipped
