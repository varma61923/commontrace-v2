"""CLI subcommand for managing and replaying procedural memories."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import procedural


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "procedural",
        help="Record, list, and replay structured agent procedural workflows.",
        description="Manage procedural memory trajectories with token-budgeted replay.",
    )
    sub = parser.add_subparsers(dest="procedural_op", required=True)

    # list
    p_list = sub.add_parser("list", help="List procedural memories in store.")
    p_list.add_argument("--root", default=".", help="Store root directory.")
    p_list.add_argument("--agent-id", default=None, help="Filter by agent ID.")

    # replay
    p_replay = sub.add_parser("replay", help="Replay procedural memory into structured prompt context.")
    p_replay.add_argument("memory_id", help="Procedural memory ID.")
    p_replay.add_argument("--root", default=".", help="Store root directory.")
    p_replay.add_argument("--budget", type=int, default=1500, help="Token budget constraint.")

    # create
    p_create = sub.add_parser("create", help="Create procedural memory from JSON input.")
    p_create.add_argument("--root", default=".", help="Store root directory.")
    p_create.add_argument("--objective", required=True, help="Task objective.")
    p_create.add_argument("--status", default="in_progress", help="Progress status.")
    p_create.add_argument("--agent-id", default="", help="Agent ID.")
    p_create.add_argument("--steps-json", help="JSON string or file containing array of steps.")

    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    op = args.procedural_op
    root = args.root

    if op == "list":
        items = procedural.list_procedural_memories(root, agent_id=args.agent_id)
        if not items:
            print("No procedural memories recorded.")
            return 0
        for item in items:
            print(
                f"[{item['id']}] {item['task_objective']} - {item['progress_status']} "
                f"({item['steps_count']} steps, ~{item['token_estimate']} tok)"
            )
        return 0

    if op == "replay":
        mem = procedural.load_procedural_memory(root, args.memory_id)
        if mem is None:
            print(f"Error: Procedural memory '{args.memory_id}' not found.", file=sys.stderr)
            return 1
        rendered = procedural.format_procedural_memory(mem, token_budget=args.budget)
        print(rendered)
        return 0

    if op == "create":
        steps_raw = []
        if args.steps_json:
            try:
                steps_raw = json.loads(args.steps_json)
            except Exception:
                with open(args.steps_json, encoding="utf-8") as f:
                    steps_raw = json.load(f)
        import uuid
        mem = procedural.ProceduralMemory(
            id=uuid.uuid4().hex[:16],
            task_objective=args.objective,
            progress_status=args.status,
            steps=[procedural.ProceduralStep.from_dict(s) for s in steps_raw],
            agent_id=args.agent_id,
        )
        saved_path = procedural.save_procedural_memory(root, mem)
        print(f"Created procedural memory {mem.id} at {saved_path}")
        return 0

    return 0
