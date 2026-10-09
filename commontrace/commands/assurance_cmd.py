"""Operator signing, authenticated provenance, forensics and cost reporting."""
from __future__ import annotations

import importlib
import json
import sys

from commontrace import assurance, memory_authority, paths


def add_parser(subparsers):
    parser = subparsers.add_parser("assurance", help="Sign, audit, replay and measure memory operations.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("signing", "provenance", "forget-certificate", "forensics", "action-vote", "cost"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        if name == "signing":
            child.add_argument("--algorithm", choices=("ed25519", "hmac-sha256"), default="ed25519")
        elif name != "cost":
            child.add_argument("id")
        if name in ("forensics", "action-vote"):
            child.add_argument("--evaluator", required=True, help="Trusted local module:function callback.")
            child.add_argument("--seed", type=int, default=0)
            child.add_argument("--seconds", type=float, default=30)
        if name == "action-vote":
            child.add_argument("--action", required=True, help="JSON action file.")
        child.set_defaults(func=run)


def run(args):
    root = paths.resolve_root(args.dest)
    try:
        if args.operation == "signing":
            result = memory_authority.configure_signing(root, algorithm=args.algorithm)
        elif args.operation == "provenance":
            result = memory_authority.provenance(root, args.id)
        elif args.operation == "forget-certificate":
            result = memory_authority.forgetting_certificate(root, args.id)
        elif args.operation == "cost":
            result = assurance.cost_report(root)
        else:
            module, name = args.evaluator.split(":", 1)
            evaluator = getattr(importlib.import_module(module), name)
            if args.operation == "forensics":
                result = assurance.forensics(root, args.id, evaluator, seed=args.seed, seconds=args.seconds)
            else:
                with open(args.action, encoding="utf-8") as fh:
                    action = json.load(fh)
                result = assurance.action_vote(root, args.id, action, evaluator, seed=args.seed, seconds=args.seconds)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result.get("allowed") is False else 0
    except (ValueError, OSError, PermissionError, ImportError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
