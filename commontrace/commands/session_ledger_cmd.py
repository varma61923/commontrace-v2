"""`commontrace session-ledger`: Track per-session cost and tokens with per-model attribution."""
from __future__ import annotations

import argparse
import json

from commontrace import paths, session_ledger


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "session-ledger",
        help="Track and inspect per-session token and cost ledger with model attribution.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    # show
    p_show = sub.add_parser("show", help="Show token and cost breakdown for a session.")
    p_show.add_argument("session_id", help="Session ID to inspect.")
    p_show.add_argument("--json", action="store_true", help="Output JSON.")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=run_show)

    # summary
    p_sum = sub.add_parser("summary", help="Show global token and cost expenditure.")
    p_sum.add_argument("--since", default="", help="Filter since ISO date.")
    p_sum.add_argument("--until", default="", help="Filter until ISO date.")
    p_sum.add_argument("--json", action="store_true", help="Output JSON.")
    p_sum.add_argument("--dest", default=None)
    p_sum.set_defaults(func=run_summary)

    # record
    p_rec = sub.add_parser("record", help="Record an LLM call into the session ledger.")
    p_rec.add_argument("session_id", help="Session ID.")
    p_rec.add_argument("--model", required=True, help="Model name (e.g. gpt-4o, claude-sonnet-5).")
    p_rec.add_argument("--prompt-tokens", type=int, required=True, help="Prompt tokens.")
    p_rec.add_argument("--completion-tokens", type=int, required=True, help="Completion tokens.")
    p_rec.add_argument("--provider", default="", help="Provider name (e.g. anthropic, openai).")
    p_rec.add_argument("--cost", type=float, default=None, help="Explicit cost in USD.")
    p_rec.add_argument("--occasion", default="", help="Context tag (e.g. recall, answer, extract).")
    p_rec.add_argument("--dest", default=None)
    p_rec.set_defaults(func=run_record)


def run_show(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    summary = session_ledger.session_summary(root, args.session_id)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"[commontrace] Session Ledger: {summary['session_id']}")
    print(f"  Total Calls:      {summary['call_count']}")
    print(f"  Prompt Tokens:    {summary['prompt_tokens']:,}")
    print(f"  Output Tokens:    {summary['completion_tokens']:,}")
    print(f"  Total Tokens:     {summary['total_tokens']:,}")
    print(f"  Estimated Cost:   ${summary['total_cost_usd']:.6f}")
    if summary["by_model"]:
        print("\n  Attribution by Model:")
        for m, stat in summary["by_model"].items():
            print(
                f"    - {m:<18} {stat['calls']} call(s) | {stat['total_tokens']:,} tokens | ${stat['cost_usd']:.6f}"
            )
    if summary["by_occasion"]:
        print("\n  Attribution by Occasion:")
        for occ, stat in summary["by_occasion"].items():
            print(
                f"    - {occ:<18} {stat['calls']} call(s) | {stat['total_tokens']:,} tokens | ${stat['cost_usd']:.6f}"
            )
    return 0


def run_summary(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    summary = session_ledger.overall_ledger_summary(root, since=args.since, until=args.until)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print("[commontrace] Global Session Ledger Summary:")
    print(f"  Sessions:         {summary['session_count']}")
    print(f"  Total Calls:      {summary['call_count']}")
    print(f"  Prompt Tokens:    {summary['prompt_tokens']:,}")
    print(f"  Output Tokens:    {summary['completion_tokens']:,}")
    print(f"  Total Tokens:     {summary['total_tokens']:,}")
    print(f"  Total Spend:      ${summary['total_cost_usd']:.6f}")
    if summary["by_model"]:
        print("\n  Spend by Model:")
        for m, stat in summary["by_model"].items():
            print(
                f"    - {m:<18} {stat['calls']} call(s) | {stat['total_tokens']:,} tokens | ${stat['cost_usd']:.6f}"
            )
    return 0


def run_record(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    entry = session_ledger.record_usage(
        root, args.session_id, args.model, args.prompt_tokens, args.completion_tokens,
        provider=args.provider, cost_usd=args.cost, occasion=args.occasion,
    )
    print(
        f"[commontrace] Recorded {entry.total_tokens:,} tokens "
        f"(${entry.cost_usd:.6f}) for session '{entry.session_id}' [{entry.model}]"
    )
    return 0

