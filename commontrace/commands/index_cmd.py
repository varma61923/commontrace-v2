from __future__ import annotations

import argparse

from commontrace import paths
from commontrace.commands._shellout import run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("index", help="Rebuild the semantic attention index over active lessons.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    extra = ["--force"] if args.force else []
    return run_script(
        root,
        "memory/attention/build_index.py",
        extra,
        "The attention index requires the reference scripts from the commontrace-v2 "
        "repo checkout (memory/attention/) plus `pip install commontrace[attention]`.",
    )
