"""Read a fleet's recurring failures out of data it already has."""
from __future__ import annotations

import csv
import io
import json
import os

try:
    csv.field_size_limit(10 * 1024 * 1024)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

MAX_IMPORT_BYTES = 50 * 1024 * 1024

_TITLE_KEYS = ("title", "label", "name", "summary", "subject", "issue", "alert", "error")
_TEXT_KEYS = ("text", "description", "context", "context_text", "body", "detail", "details",
              "message", "notes")
_TAG_KEYS = ("tags", "labels", "components", "service", "team")

MAX_FAILURES = 500


class FailureImportError(ValueError):
    ...


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _first(record: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        for actual in record:
            if not isinstance(actual, str):
                continue
            if actual.lower().strip() == k and record[actual] not in (None, ""):
                return str(record[actual])
    return ""


def _tags_of(record: dict) -> list[str]:
    raw = None
    for k in _TAG_KEYS:
        for actual in record:
            if not isinstance(actual, str):
                continue
            if actual.lower().strip() == k and record[actual]:
                raw = record[actual]
                break
        if raw is not None:
            break
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()][:20]
    parts = [p.strip() for p in str(raw).replace(";", ",").split(",")]
    return [p for p in parts if p][:20]


def _from_record(record: dict) -> tuple[str, str, list[str]] | None:
    title = _first(record, _TITLE_KEYS)
    text = _first(record, _TEXT_KEYS)
    if not title and not text:
        return None
    return (title or text[:120], text, _tags_of(record))


def _parse_jsonl(raw: str) -> list[tuple[str, str, list[str]]]:
    out = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise FailureImportError(
                f"line {i} is not valid JSON. For JSONL, every line must be one "
                f"object, e.g. {{\"title\": \"...\", \"text\": \"...\"}} ({exc})"
            ) from exc
        if not isinstance(record, dict):
            raise FailureImportError(f"line {i} is not a JSON object")
        parsed = _from_record(record)
        if parsed:
            out.append(parsed)
    return out


def _parse_json(raw: str) -> list[tuple[str, str, list[str]]]:
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("issues", "results", "items", "incidents", "data", "failures"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise FailureImportError(
            "expected a JSON array of objects, or an object with an array under "
            "'issues'/'results'/'items'/'incidents'/'data'/'failures'"
        )
    out = []
    for record in data:
        if isinstance(record, dict):
            parsed = _from_record(record)
            if parsed:
                out.append(parsed)
        elif isinstance(record, str) and record.strip():
            out.append((record.strip()[:120], record.strip(), []))
    return out


def _parse_csv(raw: str) -> list[tuple[str, str, list[str]]]:
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
    if not rows:
        raise FailureImportError("no data rows found (a header row is required)")
    if not any(_from_record(r) for r in rows):
        header = ", ".join(str(h) for h in (rows[0].keys() if rows else []))
        raise FailureImportError(
            "no usable column found. Expected one of "
            f"{'/'.join(_TITLE_KEYS[:4])} or {'/'.join(_TEXT_KEYS[:4])}; got: {header}"
        )
    return [p for p in (_from_record(r) for r in rows) if p]


def _parse_lines(raw: str) -> list[tuple[str, str, list[str]]]:
    out = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*# \t")
        if not line or line.startswith("//"):
            continue
        out.append((line[:120], line, []))
    return out


def _has_explicit_extension(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".jsonl", ".ndjson", ".json", ".csv", ".tsv")


def _sniff(path: str, raw: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext == ".json":
        return "json"
    if ext in (".csv", ".tsv"):
        return "csv"

    stripped = raw.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        first = stripped.splitlines()[0].strip()
        if first.startswith("{") and first.endswith("}") and "\n{" in stripped:
            return "jsonl"
        return "json"
    head = raw.splitlines()[:5]
    if head and all(("," in line or "\t" in line) for line in head[:2]):
        return "csv"
    return "lines"


_VALID_FORMATS = ("jsonl", "json", "csv", "lines")


def read_failures(path: str, fmt_override: str | None = None) -> tuple[list[dict], dict]:
    """Parse `path` into failure records, plus a report of what happened."""
    if fmt_override is not None and fmt_override not in _VALID_FORMATS:
        raise FailureImportError(
            f"unknown format {fmt_override!r}; expected one of {'/'.join(_VALID_FORMATS)}"
        )
    try:
        try:
            if os.path.getsize(path) > MAX_IMPORT_BYTES:
                raise FailureImportError(
                    f"{path} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB; "
                    "split the export or pass a smaller file")
        except OSError:
            pass
        with open(path, "rb") as fh:
            raw_bytes = fh.read(MAX_IMPORT_BYTES + 1)
        if len(raw_bytes) > MAX_IMPORT_BYTES:
            raise FailureImportError(
                f"{path} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB; "
                "split the export or pass a smaller file")
        raw = raw_bytes.decode("utf-8-sig", errors="replace")
    except OSError as exc:
        raise FailureImportError(f"cannot read {path}: {exc}") from exc

    if not raw.strip():
        raise FailureImportError(f"{path} is empty")

    fmt = fmt_override or _sniff(path, raw)
    allow_lines_fallback = fmt_override is None and not _has_explicit_extension(path)
    parsers = {"jsonl": _parse_jsonl, "json": _parse_json, "csv": _parse_csv, "lines": _parse_lines}
    try:
        parsed = parsers[fmt](raw)
    except (FailureImportError, ValueError, csv.Error) as exc:
        if allow_lines_fallback and fmt in ("json", "jsonl"):
            fallback = _parse_lines(raw)
            if fallback:
                fmt = "lines"
                parsed = fallback
            elif isinstance(exc, FailureImportError):
                raise
            else:
                raise FailureImportError(f"could not read {path} as json/jsonl: {exc}") from exc
        elif isinstance(exc, FailureImportError):
            raise
        else:
            raise FailureImportError(f"could not read {path} as {fmt}: {exc}") from exc

    if not parsed:
        raise FailureImportError(
            f"{path} parsed as {fmt} but contained no usable failures. Each entry "
            "needs at least a title or a description."
        )

    rows = len(parsed)
    seen: set[str] = set()
    failures = []
    for title, text, tags in parsed:
        key = _norm(f"{title} {text}")
        if key in seen:
            continue
        seen.add(key)
        failures.append({"label": title.strip()[:120], "text": text.strip(), "tags": tags})

    truncated = max(0, len(failures) - MAX_FAILURES)
    stats = {
        "format": fmt,
        "rows": rows,
        "deduplicated": rows - len(failures),
        "unique": len(failures),
        "truncated": truncated,
    }
    return failures[:MAX_FAILURES], stats
