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
    p.add_argument(
        "--format", choices=("native", "cogx"), default="native",
        help="native (default): one JSON row per lesson/trace (JSONL). "
             "cogx: one portable cogx/v1 JSON document carrying lessons, "
             "facts, graph nodes/edges, and memory blocks. "
             "--kind traces has no cogx counterpart (use native for traces).",
    )
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
    fmt = getattr(args, "format", "native") or "native"

    if fmt == "cogx":
        return _run_cogx(args, root)

    rows = []
    if args.kind in (KIND_LESSONS, KIND_ALL):
        rows.extend(_lesson_rows(root, args.status, args.agent_type))
    if args.kind in (KIND_TRACES, KIND_ALL):
        rows.extend(_trace_rows(root, args.agent_type))

    out = sys.stdout
    opened = None
    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
            opened = open(safe_out, "w", encoding="utf-8", newline="\n")
        except (OSError, ValueError) as exc:
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


def _run_cogx(args: argparse.Namespace, root: str) -> int:
    from commontrace import interop

    if args.kind == KIND_TRACES:
        print(
            "[commontrace] --kind traces has no cogx counterpart: traces only "
            "travel in the native (JSONL) export. Re-run without --format cogx, "
            "or use --kind lessons|all.",
            file=sys.stderr,
        )
        return 1

    kinds: list[str] | None = None
    if args.kind == KIND_LESSONS:
        kinds = [interop.KIND_LESSON]
    try:
        records = interop.collect_store(
            root, kinds=kinds, status=args.status, agent_type=args.agent_type,
        )
    except ValueError as exc:
        print(f"[commontrace] error: {exc}", file=sys.stderr)
        return 1
    envelope = interop.build_envelope(records)

    out = sys.stdout
    opened = None
    if args.out:
        try:
            safe_out = paths.safe_prepare_output_path(args.out)
            opened = open(safe_out, "w", encoding="utf-8", newline="\n")
        except (OSError, ValueError) as exc:
            print(f"[commontrace] could not write {args.out!r}: {exc}", file=sys.stderr)
            return 1
        out = opened

    try:
        out.write(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    finally:
        if opened is not None:
            opened.close()

    tallied: dict[str, int] = {}
    for rec in records:
        tallied[rec["kind"]] = tallied.get(rec["kind"], 0) + 1
    breakdown = ", ".join(f"{k}: {tallied[k]}" for k in sorted(tallied)) or "no records"
    dest_desc = args.out if args.out else "stdout"
    print(f"[commontrace] exported {len(records)} cogx record(s) ({breakdown}) to {dest_desc}.",
          file=sys.stderr)
    return 0
