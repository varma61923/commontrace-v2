from __future__ import annotations

import ast
import pathlib

CRUD = pathlib.Path(__file__).resolve().parents[1] / "crud.py"


EXPECTED_TRACE_WRITES: dict[str, dict[str, str]] = {
    "search_traces": {"retrievals": "OWNER-PRIVATE"},
    "get_trace": {"retrievals": "OWNER-PRIVATE"},
    "vote_trace": {"trust": "ESTABLISHED", "commons_votes": "ESTABLISHED"},
    "commons_overlap": {"commons_hits": "ESTABLISHED"},
}


def _roots_at_update_trace(node: ast.Call) -> bool:
    cur: ast.AST = node
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        cur = cur.func.value
    return (
        isinstance(cur, ast.Call)
        and isinstance(cur.func, ast.Name)
        and cur.func.id == "update"
        and len(cur.args) == 1
        and isinstance(cur.args[0], ast.Name)
        and cur.args[0].id == "Trace"
    )


def _actual_trace_writes() -> dict[str, set[str]]:
    tree = ast.parse(CRUD.read_text())
    found: dict[str, set[str]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "values"
                and _roots_at_update_trace(node)
            ):
                cols = {kw.arg for kw in node.keywords if kw.arg}
                found.setdefault(fn.name, set()).update(cols)
    return found


class TestEveryTraceWriteIsAccountedFor:
    def test_the_scanner_finds_something(self):
        assert _actual_trace_writes(), (
            "the AST scan found no update(Trace) statements at all; the "
            "write pattern in hub/crud.py changed and this guard is now blind"
        )

    def test_no_undeclared_write_to_a_trace_column(self):
        actual = _actual_trace_writes()
        expected = {fn: set(cols) for fn, cols in EXPECTED_TRACE_WRITES.items()}
        assert actual == expected, (
            "hub/crud.py writes Trace columns this file does not account for.\n"
            f"  found:    { {k: sorted(v) for k, v in sorted(actual.items())} }\n"
            f"  declared: { {k: sorted(v) for k, v in sorted(expected.items())} }\n"
            "If the new write is owner-private (scoped Trace.org_id == org_id) "
            "say so. If it is a number other orgs can see or act on, it needs "
            "hub/commons.py:org_is_established, like trust and commons_hits -- "
            "see the product strategy for what it cost to find that out the other way."
        )

    def test_both_shared_counters_are_marked_established(self):
        for fn, col in (("vote_trace", "trust"), ("commons_overlap", "commons_hits")):
            assert EXPECTED_TRACE_WRITES[fn][col] == "ESTABLISHED"

    def test_the_gate_is_still_called_where_it_is_claimed(self):
        tree = ast.parse(CRUD.read_text())
        for fn_name, expected_call in (
            ("vote_trace", "vote_counts_toward_standing"),
            ("commons_overlap", "hit_counts_toward_quality_signal"),
        ):
            fn = next(
                n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == fn_name
            )
            names = {
                n.func.attr
                for n in ast.walk(fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            assert expected_call in names, (
                f"{fn_name} writes a shared counter but no longer calls "
                f"{expected_call}"
            )
