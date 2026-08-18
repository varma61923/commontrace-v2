from __future__ import annotations

import argparse
import sys

from commontrace import paths
from commontrace.commands._shellout import has_attention_deps, run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("index", help="Rebuild the semantic attention index over active lessons.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if not has_attention_deps():
        print(
            "[commontrace] Building the attention index requires the optional attention "
            "extra (numpy + sentence-transformers): `pip install commontrace[attention]`.",
            file=sys.stderr,
        )
        return 1
    extra = ["--force"] if args.force else []
    return run_script(
        root,
        "memory/attention/build_index.py",
        extra,
        "The attention index requires the reference scripts from the commontrace-v2 "
        "repo checkout (memory/attention/) plus `pip install commontrace[attention]`.",
    )
