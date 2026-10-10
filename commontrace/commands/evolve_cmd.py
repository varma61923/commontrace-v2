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
                 "models", "proposals", "directives", "offline", "recipes", "install", "heartbeat", "distill-session",
                 "skills", "skill-review", "skill-publish", "foresight-review", "hook"):
        q = sub.add_parser(name)
        q.add_argument("--dest", default=None)
        q.add_argument("--context", action="append", default=[], help="Orthogonal scope label; repeatable.")
        if name in ("add", "search", "reflect", "profile", "directive", "question", "foresight", "propose"):
            q.add_argument("text", nargs="?", default="")
        if name == "add":
            q.add_argument("--model", action="store_true",
                           help="One-pass model extraction; default explicit assertion.")
            from commontrace.decay import HALF_LIVES_DAYS

            q.add_argument("--memory-type", default="general", choices=sorted(HALF_LIVES_DAYS))
            q.add_argument("--gliner-model", default=None, help="Optional existing local GLiNER weights directory.")
        if name == "search":
            q.add_argument("--recipe", default="balanced")
            q.add_argument("--retriever", default="hybrid", choices=REGISTRY.names())
            q.add_argument("--center", default="")
            q.add_argument("--reranker", default=None,
                           help="second-stage reranker for the top facts: cross-encoder, bge-reranker-v2-m3, "
                                "mxbai-rerank, cohere[:model], voyage[:model], jina[:model], llm[:model] or none "
                                "(default: memory/budgets.json, else none)")
        if name in ("question", "reflect"):
            q.add_argument("--budget", type=int, default=600)
        if name == "reflect":
            q.add_argument("--exploration-slots", type=int, default=0)
            q.add_argument("--adaptive-budget", action="store_true",
                           help="size the budget by the question's shape and grow it while evidence is thin")
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
        if name in ("install", "heartbeat", "hook"):
            q.add_argument("--agent-id", required=True)
        if name == "hook":
            q.add_argument("hook_operation", choices=("start", "end"))
        if name == "install":
            q.add_argument("--repo", default=None)
            q.add_argument("--commits", type=int, default=100)
            q.add_argument("--rotate", action="store_true", help="Owner-authorized reinstall with a fresh scoped key.")
        if name == "offline":
            q.add_argument("--max-jobs", type=int, default=20)
            q.add_argument("--seconds", type=float, default=30)
        if name == "distill-session":
            q.add_argument("--session-id", required=True)
            q.add_argument("--entries", required=True, help="JSON file of sequenced entries.")
        if name in ("skill-review", "skill-publish", "foresight-review"):
            q.add_argument("--id", required=True)
        if name in ("skill-review", "foresight-review"):
            q.add_argument("--expected-revision", required=True)
            q.add_argument("--actor", required=True)
            if name == "skill-review":
                q.add_argument("--verdict", choices=("HELPS", "HURTS", "NO_MEASURABLE_EFFECT"), required=True)
                q.add_argument("--evidence", required=True, help="JSON paired abstraction-vs-raw experiment evidence.")
            else:
                q.add_argument("--approve", action="store_true")
        if name == "skill-publish":
            q.add_argument("--directory", required=True, help="Explicit target assistant skills directory.")
        q.set_defaults(func=run)


def run(args) -> int:
    root, op = paths.resolve_root(args.dest), args.operation
    context = args.context
    try:
        if op == "hook":
            from commontrace import agent_hooks

            return agent_hooks.main([args.hook_operation, "--agent-id", args.agent_id, "--dest", root])
        if op == "add":
            result = additive_extract.extract(root, args.text, local=not args.model, scopes=context,
                                               memory_type=args.memory_type, entity_model_path=args.gliner_model)
        elif op == "search":
            extra = {"reranker": args.reranker} if args.reranker is not None else {}
            result = REGISTRY.retrieve(args.retriever, root, args.text, recipe=args.recipe,
                                       context=context, center=args.center, **extra)
        elif op == "reflect":
            result = memory_control.reflect(root, args.text, context=context, budget=args.budget,
                                             exploration_slots=args.exploration_slots,
                                             adaptive_budget=args.adaptive_budget)
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
            result = onboarding.install(root, args.agent_id, repo=args.repo, commits=args.commits, rotate=args.rotate)
        elif op == "heartbeat":
            result = agent_registry.heartbeat(root, args.agent_id)
        elif op == "distill-session":
            from commontrace import llm

            with open(args.entries, encoding="utf-8") as fh:
                entries = json.load(fh)
            result = memory_control.distill_session(root, args.session_id, entries, llm.complete)
        elif op in ("skills", "skill-review", "skill-publish"):
            from commontrace import experience_skills

            if op == "skills":
                result = experience_skills.active(root, context=context or None)
            elif op == "skill-review":
                with open(args.evidence, encoding="utf-8") as fh:
                    evidence = json.load(fh)
                result = experience_skills.review(root, args.id, args.expected_revision,
                    actor=args.actor, verdict=args.verdict, evidence=evidence)
            else:
                result = {"path": experience_skills.publish(root, args.id, args.directory, context=context or None)}
        elif op == "foresight-review":
            result = memory_control.review_foresight(root, args.id, args.expected_revision,
                       actor=args.actor, approve=args.approve)
        else:
            raise ValueError("unknown memory evolution command")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, PermissionError) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
