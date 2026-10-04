"""SELECT-only validation and guarded execution for LLM-generated SQL queries.

Adapted from Cognee Text-to-SQL architecture:
- Rejects non-SELECT statements and dangerous mutating SQL keywords.
- Strips markdown fences, block comments, and trailing semicolons.
- Automatically clamps or appends a LIMIT clause to prevent denial-of-service/OOM.
- Provides read-only query execution with row caps and statement timeouts.
"""
from __future__ import annotations

import re
import sqlite3
import time
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


def ensure_limit(sql: str, max_rows: int = 100) -> str:
    """Cap the result set by clamping an existing LIMIT or appending one."""
    match = _LIMIT_RE.search(sql)
    if match is None:
        return f"{sql} LIMIT {int(max_rows)}"

    def _clamp(limit_match: re.Match) -> str:
        value = int(limit_match.group(1))
        return f"LIMIT {min(value, int(max_rows))}"

    return _LIMIT_RE.sub(_clamp, sql)


def execute_guarded_sql(
    db_path: str,
    sql: str,
    *,
    max_rows: int = 100,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Validate, cap, and safely execute a read-only query against a SQLite database."""
    cleaned = validate_select(sql)
    guarded_sql = ensure_limit(cleaned, max_rows=max_rows)

    start = time.monotonic()
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(guarded_sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = [dict(row) for row in cursor.fetchmany(max_rows)]
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "execution_time_ms": elapsed_ms,
            "sql": guarded_sql,
        }
    finally:
        conn.close()
