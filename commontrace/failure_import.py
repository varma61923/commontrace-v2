"""Read a fleet's recurring failures out of data it already has.

WHY THIS EXISTS -- it is the difference between a testable thesis and an
untestable one.

The CommonTrace Knowledge Base rests on one empirical claim: that a
meaningful fraction of what a fleet keeps hitting is *substrate* failure,
the kind already documented in the Knowledge Base. That claim is testable
in an afternoon -- sign a fleet's recurring failures, ask the Knowledge
Base, read the number.

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

# The stdlib default (131072 bytes = 128 KiB) is a defense against a
# pathological file, not a limit any real export is expected to respect --
# a real incident postmortem or a large embedded stack trace routinely
# exceeds it in one CSV field. Past that default, the C-level csv module
# raises `_csv.Error: field larger than field limit`, which read_failures'
# own except tuple below did not catch (csv.Error is not a ValueError
# subclass), so a large-but-legitimate export crashed with a raw traceback
# instead of failing cleanly or, better, just parsing. Raised to 10 MiB per
# field: large enough for any legitimate stack trace/export, small enough
# that a single malicious field cannot exhaust RAM (sys.maxsize allowed a
# single field to grow until the process died before MAX_FAILURES applied).
try:
    csv.field_size_limit(10 * 1024 * 1024)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

# Refuse absurdly large imports before reading them fully into memory:
# read_failures materializes the whole file (fh.read + list(DictReader)),
# so an unbounded read on an untrusted export is a local DoS. 50 MiB is
# far above any plausible failure export (500 failures cap below).
MAX_IMPORT_BYTES = 50 * 1024 * 1024

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


def _has_explicit_extension(path: str) -> bool:
    """Whether `path`'s extension alone determines the format (see _sniff).
    An extension is a deliberate declaration by whoever named the file; a
    leading '[' or '{' is a guess about content that could just as easily be
    a bracket-prefixed log line. read_failures only falls back from a failed
    json/jsonl parse to the lines parser when the format was guessed, never
    when it was declared."""
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


_VALID_FORMATS = ("jsonl", "json", "csv", "lines")


def read_failures(path: str, fmt_override: str | None = None) -> tuple[list[dict], dict]:
    """Parse `path` into failure records, plus a report of what happened.

    Returns (failures, stats) where each failure is
    {"label", "text", "tags"} and stats carries `format`, `rows`,
    `deduplicated` and `truncated` so the caller can tell the user what was
    actually measured. Silence about a 400-row collapse or a 500-row cap
    would make the coverage number describe something other than the file
    the user handed over.

    `fmt_override`, when given, skips sniffing entirely and forces one of
    _VALID_FORMATS -- for a file `_sniff` guesses wrong on (e.g. an
    extension-less export), or to get the real parse error instead of the
    silent lines-parser fallback below.
    """
    if fmt_override is not None and fmt_override not in _VALID_FORMATS:
        raise FailureImportError(
            f"unknown format {fmt_override!r}; expected one of {'/'.join(_VALID_FORMATS)}"
        )
    try:
        # utf-8-sig, not plain utf-8: a BOM-prefixed export (the common case
        # for a CSV/JSON file saved from Excel or a Windows tool) otherwise
        # leaves a literal U+FEFF at the start of `raw`, which lands inside
        # the first JSON key or the first CSV header cell -- either an
        # immediate JSONDecodeError on line 1, or a header column that never
        # matches the name callers expect. utf-8-sig strips the BOM when
        # present and is identical to plain utf-8 when it is not, so this is
        # strictly more permissive with no behavior change for BOM-less files.
        #
        # Size-gated before read: this materializes the whole file, so check
        # st_size first to refuse absurd inputs without allocating them.
        try:
            if os.path.getsize(path) > MAX_IMPORT_BYTES:
                raise FailureImportError(
                    f"{path} is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MiB; "
                    "split the export or pass a smaller file")
        except OSError:
            pass
        # Read as bytes, not text: the fallback cap below must count actual
        # bytes even when the getsize() pre-check above silently no-ops (a
        # transient OSError). Reading in text mode made `len(raw)` count
        # decoded characters, which undercounts by up to 4x on multi-byte
        # UTF-8 content and let that much more than MAX_IMPORT_BYTES of real
        # data through.
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
        # A bracket-prefixed log line ("[2026-08-23 12:00:00] ERROR: ...")
        # sniffs as JSON on its leading '[' and then fails to parse as JSON
        # at all -- content `_sniff` guessed wrong about, not a file that is
        # actually malformed JSON. Falling back to the lines parser (one
        # failure per line, which is exactly what this file already is)
        # turns that into a usable result instead of a hard refusal.
        #
        # Only when the format was a content GUESS: an extension of .json/
        # .jsonl/.csv/.tsv (_has_explicit_extension) is a deliberate
        # declaration by whoever named the file, and an explicit
        # fmt_override is a deliberate assertion by the caller -- in both
        # cases the real parse error is more useful than a silent
        # reinterpretation of a file that says what it is.
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
