"""Inspect derivations without exposing unauthenticated trust claims."""
from __future__ import annotations

import argparse
import json

from unimem import lineage, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("lineage", help="Inspect or flag authenticated memory derivations.")
    sub = parser.add_subparsers(dest="operation", required=True)
    child = sub.add_parser("contagion", help="UniMem visible read/write contagion radius.")
    child.add_argument("id")
    child.add_argument("--dest")
    child.add_argument("--context", action="append", default=[])
    child.add_argument("--max-depth", type=int, default=16)
    child.add_argument("--max-nodes", type=int, default=2000)
    child.add_argument("--freeze", help="UniMem operator incident ID to freeze with this radius.")
    child.set_defaults(func=run)
    for name in ("show", "taint", "invalidate"):
        child = sub.add_parser(name)
        child.add_argument("id")
        child.add_argument("--dest")
        if name == "taint":
            child.add_argument("--reason", required=True)
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.operation == "contagion":
        from unimem import contagion

        result = (contagion.freeze(root, args.id, args.freeze, context=args.context) if args.freeze
                  else contagion.radius(root, args.id, context=args.context,
                                        max_depth=args.max_depth, max_nodes=args.max_nodes))
    elif args.operation == "taint":
        result = lineage.taint(root, args.id, args.reason)
    elif args.operation == "invalidate":
        result = lineage.invalidate(root, args.id)
    else:
        result = lineage.explain(root, args.id)
    print(json.dumps(result, indent=2))
    return 0
