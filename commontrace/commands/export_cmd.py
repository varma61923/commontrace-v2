"""Bulk-export this store's lessons and/or traces to one portable JSONL file
-- the missing counterpart to `commontrace/import_data.py`'s bulk importer.

WHY THIS EXISTS
---------------
`commontrace import` already reads a JSONL/CSV export (generic or from
LangSmith/Langfuse/Braintrust/OTel) into `memory/traces/`, so a fleet can
START from its existing history. There was no way to go the other
direction: get a store's own corpus OUT as one file, for a backup, an
inspection pass in a spreadsheet or a customer's own tooling, or to seed a
second store. `commontrace consolidate`/`reliability`/`taxonomy` all read
the store in place; none of them hand you a portable copy of it.

TRACE ROWS ROUND-TRIP THROUGH THE EXISTING IMPORTER, LESSON ROWS DO NOT
------------------------------------------------------------------------
Exported traces are written in exactly the flat shape
`commontrace/import_data.py`'s GENERIC mapping already expects
(title/context/solution/tags/id, outcome fields inlined) -- so
`commontrace export --kind traces` on one store followed by `commontrace
import` on another is a real, tested round trip, with no new importer
required. Lessons have no such counterpart command today (there is no
bulk lesson importer, and building one -- slug collision handling,
approval-status semantics, schema validation -- is a separate, larger
project than "let an operator get their corpus out"), so a lesson row
carries its frontmatter and body as this store's own interchange shape,
documented as such rather than implied to be import-ready.

Reads only what is on disk locally -- `paths.lessons_dir`/`traces_dir` --
the same boundary `consolidate`/`reliability` already use. It does not
reach into the Hub; `commontrace sync --pull` is the existing command for
moving trace data between a store and the Hub, and this is not a second,
divergent way to do that.
"""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import frontmatter, paths, trace_io
from commontrace.commands._format import read_or_warn
from commontrace.commands._validators import agent_type as _agent_type_arg

KIND_LESSONS = "lessons"
KIND_TRACES = "traces"
KIND_ALL = "all"
KINDS = (KIND_LESSONS, KIND_TRACES, KIND_ALL)


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "export",
        help="Bulk-export this store's lessons and/or traces to one portable JSONL file "
        "-- a backup, a customer-tooling handoff, or a seed for a second store.",
    )
    p.add_argument(
        "--kind", choices=KINDS, default=KIND_ALL,
        help=f"What to export (default: {KIND_ALL}).",
    )
    p.add_argument(
        "--status", default=None,
        help="Only lessons at this status (e.g. active). Default: every status. "
             "Ignored for --kind traces.",
    )
    p.add_argument(
        "--agent-type", type=_agent_type_arg, default=None,
        help="Only records for this fleet. Default: every agent_type.",
    )
    p.add_argument(
        "--out", default=None,
        help="Output file. Default: stdout, so this composes with shell redirection "
             "(`commontrace export > backup.jsonl`).",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _lesson_rows(root: str, status: str | None, agent_type: str | None):
    import glob
    import os

    for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            continue
        fm, body = parsed
        if status is not None and str(fm.get("status") or "") != status:
            continue
        if agent_type is not None and str(fm.get("agent_type") or "") != agent_type:
            continue
        row = {"kind": "lesson"}
        row.update(fm)
        row["body"] = body
        yield row


def _trace_rows(root: str, agent_type: str | None):
    import glob
    import os

    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        parsed = read_or_warn(trace_io.read, path)
        if parsed is None:
            continue
        inst, _body = parsed
        if agent_type is not None and str(inst.get("agent_type") or "") != agent_type:
            continue
        # The exact flat shape import_data.py's GENERIC mapping reads
        # (title/context/solution/tags/id, outcome fields inlined) -- see
        # this module's docstring on why that round trip matters. `kind`
        # is additional and harmless to a re-import: _row_to_trace only
        # ever looks up the field names it was told to map, so an unknown
        # key already on the row is ignored, not rejected.
        outcome = inst.get("outcome") or {}
        row = {
            "kind": "trace",
            "id": inst.get("id", ""),
            "title": inst.get("title", ""),
            "context": inst.get("context_text", ""),
            "solution": inst.get("solution_text", ""),
            "tags": inst.get("tags") or [],
            "agent_type": inst.get("agent_type", ""),
            "agent_id": inst.get("agent_id", ""),
            "profile": inst.get("profile", ""),
            "created_at": inst.get("created_at", ""),
            **{k: v for k, v in outcome.items()},
        }
        if inst.get("extensions"):
            row["extensions"] = inst["extensions"]
        yield row


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    rows = []
    if args.kind in (KIND_LESSONS, KIND_ALL):
        rows.extend(_lesson_rows(root, args.status, args.agent_type))
    if args.kind in (KIND_TRACES, KIND_ALL):
        rows.extend(_trace_rows(root, args.agent_type))

    out = sys.stdout
    opened = None
    if args.out:
        try:
            opened = open(args.out, "w", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        out = opened

    try:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if opened is not None:
            opened.close()

    n_lessons = sum(1 for r in rows if r.get("kind") == "lesson")
    n_traces = sum(1 for r in rows if r.get("kind") == "trace")
    dest_desc = args.out if args.out else "stdout"
    print(
        f"[commontrace] exported {n_lessons} lesson(s), {n_traces} trace(s) to {dest_desc}.",
        file=sys.stderr,
    )
    if n_traces and args.kind != KIND_LESSONS:
        print(
            "  Trace rows are import-ready: `commontrace import <file> --agent-type ...` "
            "reads them back with no field-mapping flags needed.",
            file=sys.stderr,
        )
    return 0
