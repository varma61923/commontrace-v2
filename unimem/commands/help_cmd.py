"""Grouped command discovery without importing every command implementation."""
from __future__ import annotations

import argparse
import json
import re

_STOP = frozenset({"how", "do", "i", "to", "the", "for", "a", "an", "and", "can", "me", "what", "with",
                   "my", "is", "command", "commands", "use", "please"})


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("help", help="Find commands by name, category or workflow.")
    parser.add_argument("topic", nargs="*", help="Words such as temporal, ingestion, review or a command name.")
    parser.add_argument("--group", help="Filter to a command category.")
    parser.add_argument("--json", action="store_true", help="Machine-readable command metadata and shortcuts.")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from unimem import cli
    from unimem.ui_commands import catalog

    words = [word for word in re.findall(r"\w+", " ".join(args.topic).casefold()) if word not in _STOP]
    rows = catalog()
    matches = []
    for row in rows:
        shortcuts = [name for name, target in cli.ALIASES.items() if target[0] == row["name"]]
        haystack = " ".join([row["name"], row["group"], row["description"], *row["example"], *shortcuts]).casefold()
        if (not args.group or row["group"].lower() == args.group.lower()) and all(word in haystack for word in words):
            matches.append({**row, "shortcuts": shortcuts})
    if args.json:
        print(json.dumps({"commands": matches, "aliases": {k: list(v) for k, v in cli.ALIASES.items()}}, indent=2))
    elif matches:
        groups: dict[str, list[dict]] = {}
        for row in matches:
            groups.setdefault(row["group"], []).append(row)
        for group, entries in groups.items():
            print(group + ":")
            for row in entries:
                aliases = " (" + ", ".join(row["shortcuts"]) + ")" if row["shortcuts"] else ""
                print(f"  {row['name']}{aliases}: {row['description']}")
        print("Use unimem COMMAND --help for arguments and subcommands.")
    else:
        print("No command matches this topic. Run unimem help to browse all categories.")
    return 0 if matches else 1
