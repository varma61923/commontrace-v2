"""Export and verify SCITT-style transparency receipts."""
from __future__ import annotations

import argparse
import json

from commontrace import frontier_protocol as fp
from commontrace import paths, transparency_log


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("log", help="Export signed heads, inclusion and consistency proofs.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("head", "prove", "verify", "consistency"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        if name == "prove":
            child.add_argument("--leaf", required=True, type=int)
        if name == "verify":
            child.add_argument("--receipt", required=True)
        if name == "consistency":
            child.add_argument("--from", dest="old_size", required=True, type=int)
            child.add_argument("--to", dest="size", type=int)
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.operation == "head":
        result = transparency_log.head(root)
    elif args.operation == "prove":
        result = transparency_log.prove(root, args.leaf)
    elif args.operation == "consistency":
        result = transparency_log.consistent(root, args.old_size, args.size)
    else:
        with open(args.receipt, "rb") as stream:
            data = stream.read(fp.MAX_BYTES + 1)
        if len(data) > fp.MAX_BYTES:
            raise ValueError("receipt exceeds 1 MiB")
        result = {"valid": transparency_log.verify(root, json.loads(data))}
    print(json.dumps(result, indent=2))
    return 0 if result.get("valid", True) else 1
