"""Govern every adjacent compression level with a registered randomized comparison."""
from __future__ import annotations

import json
import sys

from commontrace import compression, paths


def add_parser(subparsers):
    parser = subparsers.add_parser("compression", help="Trace→episode→observation→lesson→skill→directive ladder.")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("propose", "register", "trial", "outcome", "evaluate", "review", "active", "export-training"):
        child = sub.add_parser(name)
        child.add_argument("--dest")
        if name not in ("propose", "active", "export-training"):
            child.add_argument("id")
        if name == "propose":
            child.add_argument("text")
            child.add_argument("--level", choices=compression.LEVELS[1:], required=True)
            child.add_argument("--source", action="append", required=True)
            child.add_argument("--parent", default="")
            child.add_argument("--actor", required=True)
            child.add_argument("--applies-when", required=True)
            child.add_argument("--do-not-apply-when", required=True)
            child.add_argument("--deny-tool", action="append", default=[])
        if name == "register":
            child.add_argument("--trials", type=int, default=300)
        if name == "outcome":
            child.add_argument("value", type=float)
        if name == "review":
            child.add_argument("--experiment", required=True)
            child.add_argument("--actor", required=True)
            child.add_argument("--expected-revision", required=True)
        child.set_defaults(func=run)


def run(args):
    root, op = paths.resolve_root(args.dest), args.operation
    try:
        if op == "propose":
            result = compression.propose(root, args.text, level=args.level, sources=args.source,
                parent=args.parent, actor=args.actor, applies_when=args.applies_when,
                do_not_apply_when=args.do_not_apply_when, deny_tools=args.deny_tool)
        elif op == "register":
            result = compression.register(root, args.id, trials=args.trials)
        elif op == "trial":
            result = compression.next_trial(root, args.id)
        elif op == "outcome":
            result = compression.outcome(root, args.id, args.value)
        elif op == "evaluate":
            result = compression.evaluate(root, args.id)
        elif op == "review":
            result = compression.review(root, args.id, args.experiment, actor=args.actor,
                                        expected_revision=args.expected_revision)
        elif op == "active":
            result = compression.active(root)
        else:
            result = compression.export_training(root)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
