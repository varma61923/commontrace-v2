"""Inspect derivations without exposing unauthenticated trust claims."""
from __future__ import annotations

import argparse
import json

from commontrace import lineage, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("lineage", help="Inspect or flag authenticated memory derivations.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("show", "taint", "invalidate"):
        child = sub.add_parser(name)
        child.add_argument("id")
        child.add_argument("--dest")
        if name == "taint":
            child.add_argument("--reason", required=True)
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.operation == "taint":
        result = lineage.taint(root, args.id, args.reason)
    elif args.operation == "invalidate":
        result = lineage.invalidate(root, args.id)
    else:
        result = lineage.explain(root, args.id)
    print(json.dumps(result, indent=2))
    return 0
