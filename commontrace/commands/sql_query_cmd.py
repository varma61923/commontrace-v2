"""CLI subcommand for guarded read-only Text-to-SQL execution."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import sql_guard


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "sql_query",
        help="Safely execute guarded read-only SELECT queries on SQLite databases.",
        description="Run guarded SQL queries with forbidden keyword validation and row limits.",
    )
    parser.add_argument("db_path", help="Path to SQLite database file.")
    parser.add_argument("sql", help="SQL SELECT query to execute.")
    parser.add_argument("--max-rows", type=int, default=100, help="Max rows to return.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Query timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Output results as formatted JSON.")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        result = sql_guard.execute_guarded_sql(
            args.db_path,
            args.sql,
            max_rows=args.max_rows,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"Executed: {result['sql']} ({result['row_count']} rows in {result['execution_time_ms']}ms)")
    if result["columns"]:
        print(" | ".join(result["columns"]))
        print("-" * 40)
        for row in result["rows"]:
            print(" | ".join(str(row.get(col, "")) for col in result["columns"]))
    return 0
