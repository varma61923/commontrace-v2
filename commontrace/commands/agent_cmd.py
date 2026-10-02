"""`commontrace agent`: CLI interface for the autonomous agent execution loop.

Provides `commontrace agent run` to execute multi-turn tasks with dynamic
working memory assembly, execution trace logging, and dreaming consolidation.
"""
from __future__ import annotations

import argparse
import json
import sys


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "agent",
        help="Autonomous agent loop: run a multi-turn task with live working memory.",
    )
    sub = p.add_subparsers(dest="agent_cmd")

    run_p = sub.add_parser("run", help="Run an agent task.")
    run_p.add_argument("prompt", nargs="?", default="", help="Task prompt for the agent.")
    run_p.add_argument("--prompt-file", default=None, help="Read prompt from a file.")
    run_p.add_argument("--max-turns", type=int, default=20, help="Max agent turns.")
    run_p.add_argument("--dest", default=None, help="CommonTrace store root.")
    run_p.add_argument("--dream", action="store_true",
                       help="Trigger a dreaming consolidation pass after the run.")
    run_p.add_argument("--agent-type", default="agent", help="Agent type label for the trace.")
    run_p.add_argument("--json", dest="output_json", action="store_true",
                       help="Output result as JSON.")

    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from commontrace import paths
    from commontrace.agent_loop import AgentLoop

    root = args.dest or paths.store_root()

    prompt = args.prompt
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as pf:
                prompt = pf.read().strip()
        except Exception as exc:
            print(f"[commontrace agent] could not read prompt file: {exc}", file=sys.stderr)
            return 1

    if not prompt:
        print("[commontrace agent] error: prompt is required (positional or --prompt-file).",
              file=sys.stderr)
        return 1

    agent_type = args.agent_type
    loop = AgentLoop(root=root, agent_type=agent_type)

    print(
        f"[commontrace agent] run: prompt_len={len(prompt)} max_turns={args.max_turns} "
        f"agent_type={agent_type!r} dest={root!r}",
        file=sys.stderr,
    )

    result = loop.run(
        prompt=prompt,
        max_turns=args.max_turns,
        dream_on_complete=args.dream,
    )

    if args.output_json:
        print(json.dumps({
            "run_id": result.run_id,
            "turns": len(result.turns),
            "success": result.success,
            "blocks_updated": result.blocks_updated,
            "facts_recorded": result.facts_recorded,
            "trace_path": result.trace_path,
            "final_answer": result.final_answer[:500],
        }, indent=2))
    else:
        print(f"[commontrace agent] run complete: {result.run_id[:12]}")
        print(f"  turns:           {len(result.turns)}")
        print(f"  success:         {result.success}")
        print(f"  blocks updated:  {result.blocks_updated}")
        print(f"  facts recorded:  {result.facts_recorded}")
        print(f"  trace:           {result.trace_path}")

    return 0 if result.success else 1
