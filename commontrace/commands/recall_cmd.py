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
    p.add_argument("--fact-scorer", default="overlap-v1", choices=("overlap-v1", "bm25-v1"),
                   help="Fact ranking: compatible overlap or stemmed multilingual BM25.")
    p.add_argument("--weight", action="append", default=[], metavar="CHANNEL=W", help="Channel weight override.")
    p.add_argument("--reranker", default=None,
                   help="Rerank the top fused items: cross-encoder, cross-encoder-fast, bge-reranker-v2-m3, "
                        "mxbai-rerank, cohere[:model], voyage[:model], jina[:model], llm[:model] or none "
                        "(default: memory/budgets.json, else none).")
    p.add_argument("--rerank-depth", type=int, default=None, help="How many fused items the reranker reads (30).")
    p.add_argument("--rerank-blend", type=float, default=None,
                   help="Reranker weight beside the fused order: 0 keeps it, 1 replaces it (0.5).")
    p.add_argument("--adaptive-budget", action="store_true", default=None,
                   help="Size the budget by the question's shape; grow it while evidence is thin.")
    p.add_argument("--max-budget", type=int, default=None, help="With --adaptive-budget: the ceiling (12000).")
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
                               evidence_budget=args.evidence_budget, scope=args.scope,
                               fact_scorer=getattr(args, "fact_scorer", "overlap-v1"),
                               reranker=getattr(args, "reranker", None),
                               rerank_depth=getattr(args, "rerank_depth", None),
                               rerank_blend=getattr(args, "rerank_blend", None),
                               adaptive_budget=getattr(args, "adaptive_budget", None),
                               max_budget=getattr(args, "max_budget", None))
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
    rerank = result.explain.get("rerank")
    if rerank:
        state = f"failed ({rerank['error']}); fused order kept" if rerank.get("error") else \
            f"read {rerank['items']} items in {rerank['latency_ms']} ms, moved {rerank.get('moved', 0)}"
        print(f"[commontrace] reranker {rerank['reranker']} {state}", file=sys.stderr)
    return 0
