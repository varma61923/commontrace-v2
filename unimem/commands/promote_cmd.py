"""Preregister, evaluate and certify a memory policy change."""
from __future__ import annotations

import argparse
import json

from unimem import frontier_protocol as fp
from unimem import paths, promotion


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("promote", help="Preregister and certify a supported memory-policy upgrade.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("register", "evaluate", "certify", "rollback"):
        child = sub.add_parser(operation)
        child.add_argument("input", help="Bounded JSON request containing the plan, logged rows, or certificate.")
        child.add_argument("--dest")
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    with open(args.input, "rb") as stream:
        raw = stream.read(fp.MAX_BYTES + 1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("promotion request exceeds 1 MiB")
    body = json.loads(raw)
    root = paths.resolve_root(args.dest)
    if args.operation == "evaluate":
        result = promotion.evaluate(**body)
    elif args.operation == "register":
        result = promotion.register(root, **body)
    elif args.operation == "certify":
        result = promotion.certify(root, **body)
    else:
        result = promotion.rollback(root, **body)
    print(json.dumps(result, indent=2))
    return 1 if args.operation == "evaluate" and not result["promote"] else 0
