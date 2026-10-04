"""`commontrace ontology`: inspect or install the store's knowledge-graph ontology."""
from __future__ import annotations

import argparse
import os
import sys

from commontrace import ontology, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ontology",
        help="Show the active ontology or install the starter one.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_show = sub.add_parser("show", help="Print the active ontology source and summary.")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=run_show)

    p_set = sub.add_parser(
        "set-starter", help="Write the starter ontology.yaml (refused if one exists).")
    p_set.add_argument("--dest", default=None)
    p_set.add_argument("--force", action="store_true", help="Overwrite an existing ontology file.")
    p_set.set_defaults(func=run_set_starter)


def run_show(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    path = ontology.ontology_path(root)
    onto = ontology.load(root)
    print(f"source: {path or '(built-in defaults)'}")
    print(f"entity types: {len(onto.entity_types)}  relations: {len(onto.relations)}  "
          f"strict: {onto.strict}")
    return 0


def run_set_starter(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    existing = ontology.ontology_path(root)
    if existing is not None and not args.force:
        print(f"[commontrace] ontology already set at {existing} (use --force to overwrite)",
              file=sys.stderr)
        return 1
    out = os.path.join(paths.memory_dir(root), "ontology.yaml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(ontology.TEMPLATE)
    print(f"Wrote starter ontology to {out}.")
    return 0
