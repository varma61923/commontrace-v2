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
MAX_SQL_BYTES = 64 * 1024
MAX_VALUE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10_000


class SqlGuardError(ValueError):
    """Raised when generated SQL violates read-only safety constraints."""


def strip_sql(sql: str | None) -> str:
    """Remove markdown fences, comments, and outer whitespace/semicolons."""
    if sql is None:
        raise SqlGuardError("No SQL was generated.")
    if not isinstance(sql, str) or len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise SqlGuardError(f"SQL must be text of at most {MAX_SQL_BYTES} bytes")
    cleaned = _FENCE_RE.sub("", sql.strip()).strip()
    cleaned, _masked = _scan_sql(cleaned)
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

    _cleaned, scannable = _scan_sql(cleaned)

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


def _scan_sql(sql: str) -> tuple[str, str]:
    """Strip comments only outside quotes and mask literals/identifiers.

    SQLite accepts single/double quotes, bracket identifiers and backticks.
    Preserve offsets and quoted comment markers instead of rewriting their data.
    """
    chars = list(sql)
    masked = list(sql)
    i = 0
    while i < len(chars):
        start = i
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = len(sql) if end < 0 else end
            chars[start:i] = " " * (i - start)
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise SqlGuardError("Unterminated SQL comment")
            i = end + 2
            chars[start:i] = " " * (i - start)
        elif sql[i] in ("'", '"', "`", "["):
            quote = "]" if sql[i] == "[" else sql[i]
            i += 1
            while i < len(sql):
                if sql[i] == quote:
                    i += 1
                    if quote != "]" and i < len(sql) and sql[i] == quote:
                        i += 1
                        continue
                    break
                i += 1
            else:
                raise SqlGuardError("Unterminated SQL quote")
        else:
            i += 1
            continue
        masked[start:i] = " " * (i - start)
    return "".join(chars), "".join(masked)


def _mask_string_literals(sql: str) -> str:
    return _scan_sql(sql)[1]


def ensure_limit(sql: str, max_rows: int = 100) -> str:
    """Cap the result set by clamping an existing LIMIT or appending one."""
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_ROWS:
        raise SqlGuardError(f"max_rows must be a positive integer at most {MAX_ROWS}")
    masked = _mask_string_literals(sql)
    # Only the outer LIMIT bounds the result. Nested limits belong to their
    # subquery and must retain their semantics.
    depth = 0
    limit_start = None
    for match in re.finditer(r"[()]|\blimit\b", masked, re.IGNORECASE):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            limit_start = match.start()
    if limit_start is None:
        return f"{sql} LIMIT {max_rows}"
    literal = re.fullmatch(r"limit\s+(\d+)(\s+offset\s+\d+)?\s*", masked[limit_start:], re.IGNORECASE)
    if literal is not None:
        value = int(literal.group(1))
        begin = limit_start + literal.start(1)
        end = limit_start + literal.end(1)
        return sql[:begin] + str(min(value, max_rows)) + sql[end:]
    # Expressions, negative limits and SQLite's LIMIT offset,count syntax
    # cannot be clamped by replacing the first integer.
    # This is a validated generated SELECT, not interpolated application data;
    # execution is also checked by SQLite's read-only authorizer below.
    return f"SELECT * FROM ({sql}) AS guarded_query LIMIT {max_rows}"  # nosec B608


def _read_only_authorizer(action: int, arg1: str | None, arg2: str | None,
                          database: str | None, source: str | None) -> int:
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_DENY if (arg1 or "").lower().startswith("pragma_") else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or arg1 or "").lower() in {"load_extension", "readfile", "writefile", "eval"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


_LEGACY_SAFE_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "total", "length", "typeof",
    "abs", "round", "coalesce", "ifnull", "nullif", "instr", "unicode",
})


def _legacy_authorizer(action: int, arg1: str | None, arg2: str | None,
                       database: str | None, source: str | None) -> int:
    # Python 3.10 exposes no sqlite3 setlimit. Keep normal analytical SELECTs,
    # but reject allocation functions/aggregates instead of relying on a VM
    # timeout to interrupt a single randomblob/group_concat/printf invocation.
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or arg1 or "").lower() not in _LEGACY_SAFE_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return _read_only_authorizer(action, arg1, arg2, database, source)


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
        conn.execute("PRAGMA query_only=ON")
        if hasattr(conn, "setlimit"):
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 128)
            conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        conn.set_authorizer(_read_only_authorizer if hasattr(conn, "setlimit") else _legacy_authorizer)
        cursor = conn.cursor()
        try:
            cursor.execute(guarded_sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = []
            result_bytes = 0
            for _ in range(max_rows):
                row = cursor.fetchone()
                if row is None:
                    break
                # Include conservative container/value overhead, so many tiny
                # scalar rows cannot inflate a small cell-byte budget into
                # hundreds of MiB of retained Python dictionaries.
                result_bytes += 64 + sum(64 + (len(value.encode("utf-8")) if isinstance(value, str)
                                              else len(value) if isinstance(value, bytes) else 8) for value in row)
                if result_bytes > MAX_RESULT_BYTES:
                    raise SqlGuardError(f"query result exceeded {MAX_RESULT_BYTES}-byte limit")
                rows.append(dict(row))
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise SqlGuardError(f"query exceeded timeout of {timeout_seconds:g}s") from exc
            if any(marker in str(exc).lower() for marker in ("authorized", "prohibited", "too big")):
                raise SqlGuardError(f"query violated read-only/resource limits: {exc}") from exc
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
