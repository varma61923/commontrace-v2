"""Read a fleet's recurring failures out of data it already has.

WHY THIS EXISTS -- it is the difference between a testable thesis and an
untestable one.

The cross-org commons rests on one empirical claim: that a meaningful
fraction of what a fleet keeps hitting is *substrate* failure, the kind
another org has already solved. That claim is testable in an afternoon --
sign a fleet's recurring failures, ask the commons, read the number.

Except it wasn't, because the only way to produce those signatures was
`memory/traces/*.md` with `outcome.repeated_error: true`, which exists only
after weeks of running CommonTrace. So evaluating whether CommonTrace is
worth adopting required first adopting CommonTrace. That ordering kills the
evaluation before it starts, and it means the one question the company
needs answered can only be answered by people who already bought the
premise.

A prospect has their failures today: an incident export, a postmortem
index, a list of alert titles, a spreadsheet column. This module turns any
of those into the same signatures, computed the same way, so the question
can be asked on day zero with data that already exists.

WHAT LEAVES THE MACHINE: nothing, at this layer. This module produces
MinHash signatures locally and returns them; the caller decides whether to
send them. Failure text is not transmitted and cannot be reconstructed from
a signature -- see commontrace/overlap.py for the precise, non-marketing
version of that claim, including what it does NOT guarantee.
"""
from __future__ import annotations

import csv
import io
import json
import os

# Fields we will accept for the two things we need. Real exports name these
# differently and asking a prospect to rename columns before they can get a
# number is exactly the friction this module exists to remove.
_TITLE_KEYS = ("title", "label", "name", "summary", "subject", "issue", "alert", "error")
_TEXT_KEYS = ("text", "description", "context", "context_text", "body", "detail", "details",
              "message", "notes")
_TAG_KEYS = ("tags", "labels", "components", "service", "team")

# A prospect's export is full of exact repeats -- the same alert 400 times
# is what "recurring" looks like in the raw data. Left in, one noisy alert
# would dominate the coverage fraction and the number would describe that
# alert rather than the fleet. Collapsed, and the count is reported so
# nobody thinks rows went missing silently.
MAX_FAILURES = 500


class FailureImportError(ValueError):
    """The file could not be read as a set of failures. The message says
    what was expected, because 'invalid input' sends someone to Slack."""


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _first(record: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        for actual in record:
            # csv.DictReader's default restkey is None: a ragged row with
            # MORE columns than the header stashes the overflow as a list
            # under record[None], so `record` can hold a non-string key.
            # actual.lower() on that None crashed every row after the first
            # ragged one -- one malformed line took down the whole import.
            if not isinstance(actual, str):
                continue
            if actual.lower().strip() == k and record[actual] not in (None, ""):
                return str(record[actual])
    return ""


def _tags_of(record: dict) -> list[str]:
    raw = None
    for k in _TAG_KEYS:
        for actual in record:
            # Same ragged-CSV guard as _first(): record[None] from
            # csv.DictReader's overflow column would otherwise crash here too.
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
    # "a,b,c" and "a b c" are both common in exports.
    parts = [p.strip() for p in str(raw).replace(";", ",").split(",")]
    return [p for p in parts if p][:20]


def _from_record(record: dict) -> tuple[str, str, list[str]] | None:
    title = _first(record, _TITLE_KEYS)
    text = _first(record, _TEXT_KEYS)
    if not title and not text:
        return None
    # A row with only a description is still usable -- the title is only a
    # human-facing label in the report.
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
    # Both a bare list and the common {"issues": [...]} / {"results": [...]}
    # envelope, because export tools disagree and neither shape is wrong.
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
    """One failure per line -- the format someone produces in ten seconds by
    pasting a column out of a spreadsheet or piping `grep` output."""
    out = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*# \t")
        if not line or line.startswith("//"):
            continue
        out.append((line[:120], line, []))
    return out


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
        # A file of many objects one-per-line is JSONL even though its first
        # character says JSON; try the stricter shape first and fall through.
        first = stripped.splitlines()[0].strip()
        if first.startswith("{") and first.endswith("}") and "\n{" in stripped:
            return "jsonl"
        return "json"
    head = raw.splitlines()[:5]
    if head and all(("," in line or "\t" in line) for line in head[:2]):
        return "csv"
    return "lines"


def read_failures(path: str) -> tuple[list[dict], dict]:
    """Parse `path` into failure records, plus a report of what happened.

    Returns (failures, stats) where each failure is
    {"label", "text", "tags"} and stats carries `format`, `rows`,
    `deduplicated` and `truncated` so the caller can tell the user what was
    actually measured. Silence about a 400-row collapse or a 500-row cap
    would make the coverage number describe something other than the file
    the user handed over.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as exc:
        raise FailureImportError(f"cannot read {path}: {exc}") from exc

    if not raw.strip():
        raise FailureImportError(f"{path} is empty")

    fmt = _sniff(path, raw)
    parsers = {"jsonl": _parse_jsonl, "json": _parse_json, "csv": _parse_csv, "lines": _parse_lines}
    try:
        parsed = parsers[fmt](raw)
    except FailureImportError:
        raise
    except ValueError as exc:
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
