"""Bulk-import existing trace-like records (a support/CRM/observability
export) into memory/traces/, per the pilot deck's "What we connect to" /
"Start from your historical traces" pitch and README's "Low-risk setup: no
infrastructure replacement." This is deliberately a generic, format-level
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

_OUTCOME_BOOL_FIELDS = ("resolved", "escalated", "repeated_error", "frustration_signal")
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
    outcome: dict[str, Any] = {}
    for field_name in _OUTCOME_BOOL_FIELDS:
        if field_name in row:
            parsed = _parse_bool(row[field_name])
            if parsed is not None:
                outcome[field_name] = parsed
    for field_name in _OUTCOME_INT_FIELDS:
        if field_name in row and str(row[field_name]).strip() != "":
            try:
                outcome[field_name] = int(row[field_name])
            except (ValueError, TypeError):
                pass
    return outcome


def _row_to_trace(line_no: int, row: dict[str, Any], mapping: FieldMapping) -> ImportedRow | SkippedRow:
    title = str(row.get(mapping.title, "") or "").strip()
    context_text = str(row.get(mapping.context, "") or "").strip()
    solution_text = str(row.get(mapping.solution, "") or "").strip()

    missing = [
        name
        for name, value in (("title", title), ("context", context_text), ("solution", solution_text))
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


def parse_jsonl(lines: Iterator[str], mapping: FieldMapping) -> tuple[list[ImportedRow], list[SkippedRow]]:
    imported: list[ImportedRow] = []
    skipped: list[SkippedRow] = []
    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            skipped.append(SkippedRow(line_no=i, reason=f"invalid JSON: {exc}"))
            continue
        if not isinstance(row, dict):
            skipped.append(SkippedRow(line_no=i, reason=f"expected a JSON object, got {type(row).__name__}"))
            continue
        result = _row_to_trace(i, row, mapping)
        (imported if isinstance(result, ImportedRow) else skipped).append(result)
    return imported, skipped


def parse_csv(fh, mapping: FieldMapping) -> tuple[list[ImportedRow], list[SkippedRow]]:
    imported: list[ImportedRow] = []
    skipped: list[SkippedRow] = []
    reader = csv.DictReader(fh)
    for i, row in enumerate(reader, start=2):  # header is line 1
        result = _row_to_trace(i, dict(row), mapping)
        (imported if isinstance(result, ImportedRow) else skipped).append(result)
    return imported, skipped
