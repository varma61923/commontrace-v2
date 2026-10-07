from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from commontrace import adapters

try:
    csv.field_size_limit(10 * 1024 * 1024)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

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
    source: str = adapters.GENERIC


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


_PROTOCOL_ALIASES = {"context": "context_text", "solution": "solution_text"}


def _pick(row: dict[str, Any], name: str, alias: str | None = None) -> str:
    value = str(row.get(name, "") or "").strip()
    if not value and alias:
        value = str(row.get(alias, "") or "").strip()
    return value


def _row_to_trace(line_no: int, row: dict[str, Any], mapping: FieldMapping) -> ImportedRow | SkippedRow:
    if mapping.source != adapters.GENERIC:
        row = adapters.normalize(row, mapping.source)
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

    from commontrace.memory_guard import privacy_redaction_enabled, redact_secrets, sanitize_metadata

    title, _ = redact_secrets(title)
    context_text, _ = redact_secrets(context_text)
    solution_text, _ = redact_secrets(solution_text)
    if privacy_redaction_enabled():
        title, context_text, solution_text = sanitize_metadata([title, context_text, solution_text], pii=True)[0]
    return ImportedRow(
        line_no=line_no,
        title=title,
        context_text=context_text,
        solution_text=solution_text,
        tags=sanitize_metadata(_parse_tags(row.get(mapping.tags)), pii=privacy_redaction_enabled())[0],
        source_id=redact_secrets(str(row.get(mapping.id, "") or ""))[0],
        outcome=_extract_outcome(row),
    )


def iter_jsonl(lines: Iterator[str], mapping: FieldMapping) -> Iterator[ImportedRow | SkippedRow]:
    """Row by row, never materializing the whole file."""
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
    reader = csv.DictReader(fh)
    for i, row in enumerate(reader, start=2):
        yield _row_to_trace(i, dict(row), mapping)


def parse_jsonl(lines: Iterator[str], mapping: FieldMapping) -> tuple[list[ImportedRow], list[SkippedRow]]:
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
