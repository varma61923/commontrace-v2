"""`commontrace recall`: one question across lessons, facts, graph and conversations,
packed into one token budget."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "recall",
        help="Recall across every memory channel (lessons, facts, graph, conversations) into one budget.",
    )
    p.add_argument("question")
    p.add_argument("--budget", type=int, default=None, help="Token budget (default: memory/budgets.json or 1500).")
    p.add_argument("--agent", default=None, help="Use this agent's budget and weights from memory/budgets.json.")
    p.add_argument("--channel", action="append", default=[],
                   choices=("lessons", "facts", "graph", "conversations"), help="Only these channels (repeatable).")
    p.add_argument("--as-of", default=None, help="Read every channel as it stood at this moment.")
    p.add_argument("--space", action="append", default=None, help="Conversation spaces (default: all).")
    p.add_argument("--embedder", default="none", help="Conversation embedder: none (lexical), auto, or a tag.")
    p.add_argument("--evidence-budget", type=int, default=0,
                   help="Expand cited fact source quotes within this token allowance (default: disabled).")
    p.add_argument("--scope", default="", help="Route lessons/facts to a scope or public memory.")
    p.add_argument("--weight", action="append", default=[], metavar="CHANNEL=W", help="Channel weight override.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from commontrace import recall

    weights = {}
    for spec in args.weight:
        name, _, value = spec.partition("=")
        try:
            weights[name.strip()] = float(value)
        except ValueError:
            print(f"[commontrace] --weight expects CHANNEL=NUMBER, not {spec!r}", file=sys.stderr)
            return 2
    try:
        result = recall.recall(paths.resolve_root(args.dest), args.question, budget=args.budget, agent=args.agent,
                               channels=tuple(args.channel) or recall.CHANNELS, as_of=args.as_of,
                               weights=weights or None, spaces=args.space, embedder=args.embedder,
                               evidence_budget=args.evidence_budget, scope=args.scope)
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    print(result.context or "(nothing relevant in memory)")
    found = ", ".join(f"{k} {v}" for k, v in result.considered.items())
    print(f"\n[commontrace] {result.tokens}/{result.budget} tokens; considered: {found or 'nothing'}",
          file=sys.stderr)
    for channel, error in result.errors.items():
        print(f"[commontrace] {channel} channel failed: {error}", file=sys.stderr)
    return 0
