"""Every write to a Knowledge Base row must declare which gate protects it.

STRATEGY.md §26 fixed a specific bug -- `commons_hits` moved a shared
number without passing the anti-sockpuppet bar that `trust` /
`commons_votes` passed -- and then named its own falsifier: *a third
shared number, added later, that moves without passing
`org_is_established`.* It also said what the real fix would be if that
happened again, rather than another prose section: route every
shared-number write through one place, or at minimum make a new one
impossible to add silently.

This is that, at the cheapest honest level. It does not enforce the gate
-- a test cannot tell a correct authorization check from a plausible one
-- but it removes the failure mode that actually occurred *twice* in this
codebase, both times the same way: a signal was added, the author
reasonably did not think of it as "shared", and nothing anywhere
disagreed. (The other instance was the MCP tool inventories; see
hub/tests/test_commons_toggle.py.)

WHY THIS IS AN AST SCAN AND NOT A HAND-KEPT LIST
------------------------------------------------
A hand-kept list on both sides is what failed before: `hub/smoke.py` and
`install_cmd._HUB_TOOLS` were pinned to EACH OTHER rather than to the
server, so they drifted together and stayed consistent while both were
wrong. So one side here is generated from the code itself -- the actual
`update(Trace).values(...)` statements in hub/crud.py -- and only the
other side is written down. There is nothing for the two to drift into
agreement about.

SCOPE, STATED SO IT IS NOT MISTAKEN FOR MORE
--------------------------------------------
This covers `update(Trace)` statements in hub/crud.py, which is how every
counter in that file is written and deliberately so (atomic in-database
UPDATE, never a read-modify-write through the ORM -- see the comments at
each site). It does NOT see ORM attribute assignment, raw SQL, or writes
from other modules. A determined author can still add an ungated shared
number; what they cannot do is add one along the path all four existing
ones use without this test noticing.
"""
from __future__ import annotations

import ast
import pathlib

CRUD = pathlib.Path(__file__).resolve().parents[1] / "crud.py"


# Every `update(Trace)` write in hub/crud.py, and what keeps it honest.
# Adding a column here is a deliberate act with a reviewer attached; that
# is the entire point of the file.
#
#   OWNER-PRIVATE -- the statement is scoped `Trace.org_id == org_id`, so
#   it can only ever touch the calling org's own row. A KB entry is owned
#   by the operator org, so no customer reaches one this way, and there is
#   no cross-tenant number to protect.
#
#   ESTABLISHED -- the value is visible to, or acts on behalf of, orgs
#   other than the writer, so hub/commons.py:org_is_established gates
#   whether the write counts. The behaviour itself is pinned in
#   hub/tests/test_commons_hit_integrity.py and test_commons.py; this only
#   pins that the write is known about.
EXPECTED_TRACE_WRITES: dict[str, dict[str, str]] = {
    "search_traces": {"retrievals": "OWNER-PRIVATE"},
    "get_trace": {"retrievals": "OWNER-PRIVATE"},
    "vote_trace": {"trust": "ESTABLISHED", "commons_votes": "ESTABLISHED"},
    "commons_overlap": {"commons_hits": "ESTABLISHED"},
}


def _roots_at_update_trace(node: ast.Call) -> bool:
    """Whether a `.values(...)` call sits on a chain starting `update(Trace)`.

    Walks down the attribute chain rather than pattern-matching one shape,
    because `.where(...)` and `.values(...)` are written in either order at
    different sites in this file and both are correct SQLAlchemy.
    """
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
        """A scan that silently matched nothing would make every assertion
        below vacuously true -- which is precisely how a guard rots into
        decoration. Pinned first, before anything is compared."""
        assert _actual_trace_writes(), (
            "the AST scan found no update(Trace) statements at all; the "
            "write pattern in hub/crud.py changed and this guard is now blind"
        )

    def test_no_undeclared_write_to_a_trace_column(self):
        """The falsifier STRATEGY.md §26 named, as a test rather than a
        prediction. A new counter on Trace fails here until someone writes
        down which gate protects it."""
        actual = _actual_trace_writes()
        expected = {fn: set(cols) for fn, cols in EXPECTED_TRACE_WRITES.items()}
        assert actual == expected, (
            "hub/crud.py writes Trace columns this file does not account for.\n"
            f"  found:    { {k: sorted(v) for k, v in sorted(actual.items())} }\n"
            f"  declared: { {k: sorted(v) for k, v in sorted(expected.items())} }\n"
            "If the new write is owner-private (scoped Trace.org_id == org_id) "
            "say so. If it is a number other orgs can see or act on, it needs "
            "hub/commons.py:org_is_established, like trust and commons_hits -- "
            "see STRATEGY.md §26 for what it cost to find that out the other way."
        )

    def test_both_shared_counters_are_marked_established(self):
        """Not redundant with the map above: that one would still pass if
        somebody relabelled a shared counter OWNER-PRIVATE to quiet this
        file. These two are shared by construction -- `commons_visible()`
        puts them in front of every org with Knowledge Base access -- so
        the label is not a judgement call."""
        for fn, col in (("vote_trace", "trust"), ("commons_overlap", "commons_hits")):
            assert EXPECTED_TRACE_WRITES[fn][col] == "ESTABLISHED"

    def test_the_gate_is_still_called_where_it_is_claimed(self):
        """A crude check that earns its place: the map says these two
        writes are gated, and the behavioural tests prove the gate WORKS,
        but both would keep passing if a future edit gated the wrong call
        site. This pins that the predicate is named inside the function
        that claims it."""
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
