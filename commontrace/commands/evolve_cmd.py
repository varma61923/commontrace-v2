"""Memory evolution commands using the same operations as the local/HTTP SDK."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import additive_extract, agent_registry, memory_control, onboarding, paths
from commontrace.search_recipes import REGISTRY, recipe_manifest


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("evolve", help="Search recipes, standing questions, rules, profiles and SDK onboarding.")
    sub = p.add_subparsers(dest="operation", required=True)
    for name in ("add", "search", "reflect", "profile", "directive", "question", "foresight", "propose",
                 "models", "proposals", "directives", "offline", "recipes", "install", "heartbeat", "distill-session"):
        q = sub.add_parser(name)
        q.add_argument("--dest", default=None)
        q.add_argument("--context", action="append", default=[], help="Orthogonal scope label; repeatable.")
        if name in ("add", "search", "reflect", "profile", "directive", "question", "foresight", "propose"):
            q.add_argument("text", nargs="?", default="")
        if name == "add":
            q.add_argument("--model", action="store_true",
                           help="One-pass model extraction; default explicit assertion.")
            q.add_argument("--memory-type", default="general", choices=("general", "temporary", "environment"))
        if name == "search":
            q.add_argument("--recipe", default="balanced")
            q.add_argument("--retriever", default="hybrid", choices=REGISTRY.names())
            q.add_argument("--center", default="")
        if name in ("question", "reflect"):
            q.add_argument("--budget", type=int, default=600)
        if name == "question":
            q.add_argument("--refresh-seconds", type=int, default=3600)
        if name == "directive":
            q.add_argument("--deny-tool", action="append", default=[])
            q.add_argument("--require-tag", action="append", default=[])
        if name in ("foresight", "propose"):
            q.add_argument("--source", action="append", required=True)
        if name == "foresight":
            q.add_argument("--valid-from", required=True)
            q.add_argument("--expires-at", required=True)
        if name in ("install", "heartbeat"):
            q.add_argument("--agent-id", required=True)
        if name == "install":
            q.add_argument("--repo", default=None)
            q.add_argument("--commits", type=int, default=100)
        if name == "offline":
            q.add_argument("--max-jobs", type=int, default=20)
            q.add_argument("--seconds", type=float, default=30)
        if name == "distill-session":
            q.add_argument("--session-id", required=True)
            q.add_argument("--entries", required=True, help="JSON file of sequenced entries.")
        q.set_defaults(func=run)


def run(args) -> int:
    root, op = paths.resolve_root(args.dest), args.operation
    context = args.context
    try:
        if op == "add":
            result = additive_extract.extract(root, args.text, local=not args.model, scopes=context,
                                               memory_type=args.memory_type)
        elif op == "search":
            result = REGISTRY.retrieve(args.retriever, root, args.text, recipe=args.recipe,
                                       context=context, center=args.center)
        elif op == "reflect":
            result = memory_control.reflect(root, args.text, context=context, budget=args.budget)
        elif op == "profile":
            result = memory_control.profile(root, args.text, context=context)
        elif op == "directive":
            result = memory_control.directive(root, args.text, labels=context,
                                               deny_tools=args.deny_tool, required_tags=args.require_tag)
        elif op == "question":
            result = memory_control.standing_question(root, args.text, context=context,
                           budget=args.budget, refresh_seconds=args.refresh_seconds)
        elif op == "foresight":
            result = memory_control.foresight(root, args.text, context=context, sources=args.source,
                           valid_from=args.valid_from, expires_at=args.expires_at)
        elif op == "propose":
            result = memory_control.proposal(root, args.text, context=context, sources=args.source)
        elif op in ("models", "proposals", "directives"):
            result = memory_control.records(root, {"models": "mental-model", "proposals": "proposal",
                                                  "directives": "directive"}[op], context=context or None)
        elif op == "offline":
            result = memory_control.offline_pass(root, max_jobs=args.max_jobs, seconds=args.seconds)
        elif op == "recipes":
            result = recipe_manifest()
        elif op == "install":
            result = onboarding.install(root, args.agent_id, repo=args.repo, commits=args.commits)
        elif op == "heartbeat":
            result = agent_registry.heartbeat(root, args.agent_id)
        elif op == "distill-session":
            from commontrace import llm

            with open(args.entries, encoding="utf-8") as fh:
                entries = json.load(fh)
            result = memory_control.distill_session(root, args.session_id, entries, llm.complete)
        else:
            raise ValueError("unknown memory evolution command")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
