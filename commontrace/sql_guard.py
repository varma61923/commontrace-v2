"""SELECT-only validation and guarded execution for LLM-generated SQL queries.

Adapted from Cognee Text-to-SQL architecture:
- Rejects non-SELECT statements and dangerous mutating SQL keywords.
- Strips markdown fences, block comments, and trailing semicolons.
- Automatically clamps or appends a LIMIT clause to prevent denial-of-service/OOM.
- Provides read-only query execution with row caps and statement timeouts.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "attach",
    "detach",
    "vacuum",
    "pragma",
    "call",
    "merge",
    "execute",
    "exec",
    "replace",
    "set",
    "reindex",
)

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)


class SqlGuardError(ValueError):
    """Raised when generated SQL violates read-only safety constraints."""


def strip_sql(sql: str | None) -> str:
    """Remove markdown fences, comments, and outer whitespace/semicolons."""
    if sql is None:
        raise SqlGuardError("No SQL was generated.")
    cleaned = _FENCE_RE.sub("", sql.strip()).strip()
    cleaned = _BLOCK_COMMENT_RE.sub(" ", cleaned)
    cleaned = _LINE_COMMENT_RE.sub(" ", cleaned)
    cleaned = cleaned.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def validate_select(sql: str) -> str:
    """Validate that sql is a single read-only SELECT statement and return cleaned SQL.

    Raises SqlGuardError with feedback suitable for retry if validation fails.
    """
    cleaned = strip_sql(sql)
    if not cleaned:
        raise SqlGuardError("The generated SQL is empty.")

    scannable = _STRING_LITERAL_RE.sub("''", cleaned)

    if ";" in scannable:
        raise SqlGuardError("Only a single SQL statement is allowed (no ';' separators).")

    first_token_match = re.match(r"\s*(\w+)", scannable)
    first_token = first_token_match.group(1).lower() if first_token_match else ""
    if first_token not in ("select", "with"):
        raise SqlGuardError(
            f"Only SELECT statements are allowed; got statement starting with '{first_token.upper() or '?'}'."
        )

    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", scannable, re.IGNORECASE):
            raise SqlGuardError(
                f"Forbidden keyword '{keyword.upper()}' found — only read-only SELECT statements are allowed."
            )

    return cleaned


def _mask_string_literals(sql: str) -> str:
    """Blank quoted literals while preserving offsets for safe clause rewriting."""
    chars = list(sql)
    quote = None
    i = 0
    while i < len(chars):
        char = chars[i]
        if quote is None and char in ("'", '"'):
            quote = char
            chars[i] = " "
        elif quote is not None:
            chars[i] = " "
            if char == quote:
                if i + 1 < len(chars) and chars[i + 1] == quote:
                    chars[i + 1] = " "
                    i += 1
                else:
                    quote = None
        i += 1
    return "".join(chars)


def ensure_limit(sql: str, max_rows: int = 100) -> str:
    """Cap the result set by clamping an existing LIMIT or appending one."""
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise SqlGuardError("max_rows must be a positive integer")
    masked = _mask_string_literals(sql)
    match = _LIMIT_RE.search(masked)
    if match is None:
        return f"{sql} LIMIT {max_rows}"
    value = int(match.group(1))
    replacement = f"LIMIT {min(value, max_rows)}"
    return sql[:match.start()] + replacement + sql[match.end():]


def execute_guarded_sql(
    db_path: str,
    sql: str,
    *,
    max_rows: int = 100,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Validate, cap, and safely execute a read-only query against SQLite.

    SQLite's connection ``timeout`` only covers lock acquisition; it does not
    stop an expensive SELECT. A progress handler supplies the missing execution
    deadline and turns a runaway generated query into a controlled error.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise SqlGuardError("timeout_seconds must be a finite positive number")
    cleaned = validate_select(sql)
    guarded_sql = ensure_limit(cleaned, max_rows=max_rows)

    start = time.monotonic()
    deadline = start + timeout_seconds
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    conn.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(guarded_sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = [dict(row) for row in cursor.fetchmany(max_rows)]
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise SqlGuardError(f"query exceeded timeout of {timeout_seconds:g}s") from exc
            raise
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "execution_time_ms": elapsed_ms,
            "sql": guarded_sql,
        }
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
