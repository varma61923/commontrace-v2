"""CLI subcommand for memory defense and sensitive data screening."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import defense


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "defense",
        help="Screen content for secrets, credentials, PII, and injection patterns.",
        description="Inspect and redact sensitive data using 40+ regex patterns and checksums.",
    )
    sub = parser.add_subparsers(dest="defense_op", required=True)

    # screen
    p_screen = sub.add_parser("screen", help="Screen text or file for sensitive patterns.")
    p_screen.add_argument("text", nargs="?", help="Text to screen (or omit to read from stdin).")
    p_screen.add_argument("--file", help="File to read content from.")
    p_screen.add_argument("--action", default="redact", choices=["allow", "redact", "block"], help="Policy action.")
    p_screen.add_argument("--json", action="store_true", help="Output results as JSON.")

    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    op = args.defense_op
    if op == "screen":
        content = ""
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                content = f.read()
        elif args.text:
            content = args.text
        else:
            content = sys.stdin.read()

        policy = defense.DefensePolicy(
            enabled=True,
            rules=(defense.PolicyRule(on="sensitive_data", action=defense.DefenseAction(args.action)),),
        )
        decision = defense.screen_content(content, policy=policy)

        if args.json:
            print(json.dumps({
                "action": decision.action.value,
                "detector": decision.detector,
                "message": decision.message,
                "redacted_content": decision.redacted_content,
                "matched_types": decision.matched_types,
                "hits": decision.hits,
            }, indent=2))
            return 0 if decision.action != defense.DefenseAction.BLOCK else 2

        if decision.matched_types:
            print(f"[{decision.action.value.upper()}] Matches found: {', '.join(decision.matched_types)}")
            for hit in decision.hits:
                print(f"  - {hit['detector']}: {hit['preview']}")
            if decision.action == defense.DefenseAction.REDACT and decision.redacted_content:
                print("\nRedacted Content:\n" + decision.redacted_content)
        else:
            print("[ALLOW] No sensitive patterns detected.")
        return 0 if decision.action != defense.DefenseAction.BLOCK else 2

    return 0
