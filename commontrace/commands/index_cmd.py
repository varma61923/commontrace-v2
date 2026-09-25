from __future__ import annotations

import argparse
import sys

from commontrace import paths
from commontrace.commands._shellout import has_attention_deps, run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("index", help="Rebuild the semantic attention index over active lessons.")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--model", default=None,
        help="Embedding model to build with (one of the trusted models build_index.py "
             "lists). Default: the one the existing index was built with, else the "
             "default model. A different model is a different semantic ranking, which "
             "a store under experiment records as a new treatment.",
    )
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
    model = getattr(args, "model", None)
    if model:
        extra += ["--model", model]
    else:
        from commontrace.commands.query_cmd import _fallback_model_args

        extra += _fallback_model_args(root)
    return run_script(
        root,
        "memory/attention/build_index.py",
        extra,
        "The reference attention scripts ship inside the package, so this means a "
        "damaged install -- try `pip install --force-reinstall commontrace`.",
    )
