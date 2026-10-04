import sqlite3

import pytest

from commontrace.sql_guard import (
    SqlGuardError,
    ensure_limit,
    execute_guarded_sql,
    strip_sql,
    validate_select,
)


def test_strip_sql():
    raw = "```sql\n-- comment\nSELECT * FROM table;\n```"
    stripped = strip_sql(raw)
    assert stripped == "SELECT * FROM table"


def test_validate_select_valid_queries():
    assert validate_select("SELECT id, name FROM users") == "SELECT id, name FROM users"
    assert validate_select("WITH cte AS (SELECT 1 as x) SELECT * FROM cte").startswith("WITH")
    assert validate_select("select count(*) from logs where status = 'active'")


def test_validate_select_rejects_mutations():
    with pytest.raises(SqlGuardError, match="Only SELECT"):
        validate_select("INSERT INTO users (name) VALUES ('bad')")

    with pytest.raises(SqlGuardError, match="Forbidden keyword 'DROP'"):
        validate_select("SELECT * FROM (DROP TABLE users)")

    with pytest.raises(SqlGuardError, match="Forbidden keyword 'UPDATE'"):
        validate_select("SELECT * FROM users WHERE id IN (UPDATE users SET name='x')")

    with pytest.raises(SqlGuardError, match="Forbidden keyword 'DELETE'"):
        validate_select("SELECT * FROM (DELETE FROM sessions)")

    with pytest.raises(SqlGuardError, match="Only a single SQL statement"):
        validate_select("SELECT 1; SELECT 2")


def test_ensure_limit():
    assert ensure_limit("SELECT * FROM items", max_rows=50) == "SELECT * FROM items LIMIT 50"
    # Clamps excessive limit
    assert ensure_limit("SELECT * FROM items LIMIT 5000", max_rows=100) == "SELECT * FROM items LIMIT 100"
    # Preserves smaller existing limit
    assert ensure_limit("SELECT * FROM items LIMIT 10", max_rows=100) == "SELECT * FROM items LIMIT 10"


def test_ensure_limit_ignores_string_literals():
    assert ensure_limit("SELECT 'limit 999' AS note", max_rows=2) == (
        "SELECT 'limit 999' AS note LIMIT 2"
    )


def test_execute_guarded_sql(tmp_path):
    db_file = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE metrics (id INT, score REAL)")
    conn.execute("INSERT INTO metrics VALUES (1, 0.95), (2, 0.88), (3, 0.99)")
    conn.commit()
    conn.close()

    res = execute_guarded_sql(db_file, "SELECT id, score FROM metrics ORDER BY score DESC", max_rows=2)
    assert res["columns"] == ["id", "score"]
    assert res["row_count"] == 2
    assert res["rows"][0]["id"] == 3
    assert res["rows"][0]["score"] == 0.99
    assert res["execution_time_ms"] >= 0


def test_mcp_sql_guarded_query(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from commontrace import mcp_server

    root_path = tmp_path / "store"
    root_path.mkdir()
    root = str(root_path)
    db_file = str(root_path / "data.db")
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE kv (k TEXT, v TEXT)")
    conn.execute("INSERT INTO kv VALUES ('lang', 'python')")
    conn.commit()
    conn.close()

    server = mcp_server.build_server(root)

    res = asyncio.run(
        server.call_tool(
            "sql_guarded_query",
            {
                "db_path": db_file,
                "sql": "SELECT k, v FROM kv",
            },
        )
    )
    res_text = res.content[0].text if hasattr(res, "content") else str(res)
    assert "columns" in res_text
    assert "python" in res_text

    denied = asyncio.run(server.call_tool(
        "sql_guarded_query", {"db_path": str(tmp_path / "outside.db"), "sql": "SELECT 1"}
    ))
    denied_text = denied.content[0].text if hasattr(denied, "content") else str(denied)
    assert "scope_error" in denied_text

