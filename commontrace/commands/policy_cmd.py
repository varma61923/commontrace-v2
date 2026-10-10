"""Evaluate supported policies with logged joint propensities; fail closed."""
from __future__ import annotations

import json
import sys

from commontrace import paths, policy


def add_parser(subparsers):
    parser = subparsers.add_parser("policy", help="Held-out conditional policy evaluation and release gate.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("evaluate", "train", "install", "register"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        child.add_argument("--minimum-samples", type=int, default=30)
        if name == "register":
            child.add_argument("--evaluation-samples", type=int, default=300)
        if name != "train":
            child.add_argument("candidate", help="JSON delivery policy, unchanged retrieval eligibility/baseline.")
        child.set_defaults(func=run)


def run(args):
    root = paths.resolve_root(args.dest)
    try:
        if args.operation == "train":
            result = policy.train(root, minimum_cases=args.minimum_samples)
        else:
            with open(args.candidate, encoding="utf-8") as fh:
                candidate = json.load(fh)
            operation = {"install": policy.install, "evaluate": policy.evaluate,
                         "register": policy.preregister}[args.operation]
            if args.operation == "register":
                result = operation(root, candidate, evaluation_samples=args.evaluation_samples)
            else:
                result = operation(root, candidate, minimum_samples=args.minimum_samples)
        print(json.dumps(result, indent=2))
        return 0 if args.operation in ("train", "register") or result["safe_to_release"] else 1
    except (ValueError, OSError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
