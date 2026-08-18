from __future__ import annotations

import argparse
import os

from commontrace import paths
from commontrace.commands._shellout import run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "query",
        help="Retrieve top-K relevant lessons for a task (semantic attention pre-filter).",
    )
    p.add_argument("task", help="Incoming task / query string")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    return run_script(
        root,
        os.path.join("memory", "attention", "query.py"),
        [args.task, "--top-k", str(args.top_k)],
        "Retrieval requires the reference attention scripts from the commontrace-v2 "
        "repo checkout (memory/attention/) plus `pip install commontrace[attention]`. "
        "Falling back: grep memory/lessons/*.md for now, or run "
        "`commontrace lesson list` for a non-semantic view.",
    )
