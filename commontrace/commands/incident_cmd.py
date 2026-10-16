"""Operator incident evidence workflow."""
from __future__ import annotations

import argparse
import json

from commontrace import incident, paths

REGIMES = {"eu-ai-act": "eu-ai-act", "iso42001": "iso-42001", "nist": "nist-ai-rmf",
           "gdpr22": "decision-rights", "pld": "product-liability"}


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("incident", help="Freeze, inspect and export incident evidence with legal holds.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("freeze", "timeline", "pack", "release"):
        child = sub.add_parser(operation)
        child.add_argument("id")
        child.add_argument("--dest")
        if operation == "freeze":
            child.add_argument("--valid-time", required=True)
        if operation == "release":
            child.add_argument("--reason", required=True)
        if operation == "pack":
            child.add_argument("--regime", choices=REGIMES, default="eu-ai-act")
        child.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.operation == "freeze":
        result = incident.freeze(root, args.id, args.valid_time)
    elif args.operation == "release":
        result = incident.release(root, args.id, args.reason)
    elif args.operation == "pack":
        result = incident.pack(root, args.id, framework=REGIMES[args.regime])
    else:
        result = incident.timeline(root, args.id)
    print(json.dumps(result, indent=2))
    return 0
