"""Guided workflows that execute the existing CLI with argument lists, never a shell."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    from unimem.migration import VENDORS

    parser = subparsers.add_parser("setup", help="Guide runtime setup, document ingestion or competitor import.")
    parser.add_argument("--workflow", choices=("runtime", "ingest", "migrate"))
    parser.add_argument("--dest")
    parser.add_argument("--profile", choices=("local", "team", "server"))
    parser.add_argument("--source", help="A document path or competitor export file.")
    parser.add_argument("--vendor", choices=VENDORS)
    parser.add_argument("--context", action="append", default=[], help="Explicit import context or ingestion scope.")
    parser.add_argument("--space", help="Conversation space for document ingestion.")
    parser.add_argument("--agent", action="append", default=[], help="Runtime agent hooks; repeatable.")
    parser.add_argument("--non-interactive", action="store_true", help="Use explicit options without prompts.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="Execute the validated workflow.")
    action.add_argument("--dry-run", action="store_true", help="Print the workflow without writes.")
    parser.add_argument("--json", action="store_true", help="One JSON plan and execution report.")
    parser.set_defaults(func=run)


def _answer(label: str, default: str = "", *, choices: tuple[str, ...] = ()) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(label + suffix + ": ").strip() or default
    if not value or (choices and value not in choices):
        raise ValueError("choose " + ", ".join(choices) if choices else label + " must not be empty")
    return value


def _plan(args: argparse.Namespace) -> dict:
    from unimem import paths
    from unimem.hook_kit import AGENTS
    from unimem.migration import VENDORS

    interactive = not args.non_interactive
    if interactive and not sys.stdin.isatty():
        raise ValueError("guided setup requires a terminal; use --non-interactive with an explicit --workflow")
    workflow = args.workflow
    if workflow is None:
        if not interactive:
            raise ValueError("non-interactive setup requires --workflow")
        workflow = _answer("Workflow (runtime, ingest, migrate)", "runtime",
                           choices=("runtime", "ingest", "migrate"))
    root = paths.resolve_root(args.dest or (_answer("Store directory", ".") if interactive else None))
    commands: list[list[str]] = []
    next_commands: list[list[str]] = []
    if workflow == "runtime":
        profile = args.profile or (_answer("Profile (local, team, server)", "local",
                                          choices=("local", "team", "server")) if interactive else "local")
        if any(agent not in AGENTS for agent in args.agent):
            raise ValueError("agent hooks must name a supported agent")
        command = ["up", "--setup-only", "--dest", root, "--profile", profile]
        for agent in args.agent:
            command.extend(("--agent", agent))
        commands.append(command)
        next_commands.append(["up", "--dest", root, "--profile", profile])
    else:
        source = args.source or (_answer("Source path") if interactive else "")
        if not source:
            raise ValueError("ingestion and migration require --source")
        source = os.path.abspath(os.path.expanduser(source))
        if not os.path.exists(source):
            raise ValueError("the source path does not exist")
        contexts = args.context
        if not contexts and interactive:
            contexts = [_answer("Memory context (for example, project:payments)")]
        if (not contexts or any(not value.strip() or len(value) > 512
                                or any(ord(c) < 32 or ord(c) == 127 for c in value) for value in contexts)
                or len(contexts) > 32):
            raise ValueError("choose 1-32 explicit nonempty contexts, each at most 512 characters")
        if workflow == "ingest":
            if len(contexts) != 1:
                raise ValueError("document ingestion takes exactly one --context as its routing scope")
            command = ["ingest", source, "--type", "docs", "--scope", contexts[0], "--dest", root, "--json"]
            if args.space:
                command.extend(("--space", args.space))
        else:
            if not os.path.isfile(source):
                raise ValueError("competitor migration requires an export file")
            vendor = args.vendor or (_answer("Vendor (" + ", ".join(VENDORS) + ")", choices=tuple(VENDORS))
                                     if interactive else "")
            if vendor not in VENDORS:
                raise ValueError("competitor migration requires a supported --vendor")
            command = ["migrate", vendor, source, "--dest", root]
            for context in contexts:
                command.extend(("--context", context))
        commands.append(command)
    return {"workflow": workflow, "root": root, "commands": commands, "next_commands": next_commands}


def run(args: argparse.Namespace) -> int:
    from unimem import cli
    from unimem.ui_commands import BoundedCapture

    try:
        plan = _plan(args)
        # Parse every operation before the first write, using its real CLI contract.
        for command in plan["commands"]:
            cli.build_parser(command[0]).parse_args(command)
        apply = args.apply
        if not args.json:
            print("Workflow:")
            for command in plan["commands"]:
                print("  " + shlex.join(["unimem", *command]))
        if not args.non_interactive and not args.dry_run and not apply:
            apply = input("Apply this workflow? [y/N]: ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print("Setup cancelled before execution.", file=sys.stderr)
        return 2
    steps = []
    exit_code = 0
    if apply:
        for command in plan["commands"]:
            output, error = BoundedCapture(), BoundedCapture()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                exit_code = cli.main(command)
            steps.append({"command": command, "exit_code": exit_code,
                          "stdout": output.getvalue(), "stderr": error.getvalue(),
                          "truncated": output.truncated or error.truncated})
            if not args.json:
                print(output.getvalue(), end="")
                print(error.getvalue(), end="", file=sys.stderr)
            if exit_code:
                break
    result = {**plan, "applied": apply, "steps": steps, "exit_code": exit_code}
    if args.json:
        print(json.dumps(result, indent=2))
    elif not apply:
        print("No changes applied. Use --apply to execute this workflow.")
    elif not exit_code:
        for command in plan["next_commands"]:
            print("Next: " + shlex.join(["unimem", *command]))
    return exit_code
