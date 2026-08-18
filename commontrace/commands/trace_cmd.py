from __future__ import annotations

import argparse
import glob
import os

from commontrace import paths, trace_io, validate


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("trace", help="Inspect Traces captured into the local store.")
    sub = p.add_subparsers(dest="trace_cmd", required=True)

    val = sub.add_parser("validate", help="Validate one or all traces against protocol/schemas/trace.schema.json.")
    val.add_argument("path", nargs="?", default=None)
    val.add_argument("--dest", default=None)
    val.set_defaults(func=run_validate)

    ls = sub.add_parser("list", help="List traces in the store.")
    ls.add_argument("--agent-type", default=None)
    ls.add_argument("--dest", default=None)
    ls.set_defaults(func=run_list)


def _iter_trace_paths(root: str, explicit: str | None):
    if explicit:
        yield explicit
        return
    tdir = paths.traces_dir(root)
    for p in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        if os.path.basename(p) == "README.md":
            continue
        yield p


def run_validate(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    schema = validate.load_schema("trace.schema.json")
    n_checked = 0
    n_failed = 0
    for path in _iter_trace_paths(root, args.path):
        n_checked += 1
        instance, _ = trace_io.read(path)
        errors = validate.validate(instance, schema)
        if errors:
            n_failed += 1
            print(f"FAIL {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {path}")
    print(f"\n[commontrace] {n_checked - n_failed}/{n_checked} traces valid.")
    return 1 if n_failed else 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    for path in _iter_trace_paths(root, None):
        instance, _ = trace_io.read(path)
        if args.agent_type and instance.get("agent_type") != args.agent_type:
            continue
        print(f"{instance.get('id', '?'):36s} [{instance.get('agent_type', '?'):9s}] {instance.get('title', '')}")
    return 0
