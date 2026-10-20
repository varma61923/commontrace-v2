"""`commontrace pack` vertical configuration and offline demo operations."""
from __future__ import annotations

import argparse
import json


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("pack", help="Install a vertical configuration or run its deterministic demo.")
    actions = parser.add_subparsers(dest="pack_action", required=True)
    actions.add_parser("list", help="List the eight shipped vertical packs.").set_defaults(func=run)
    install = actions.add_parser("install", help="Install a pack manifest and documentation.")
    install.add_argument("name")
    install.add_argument("--dest", required=True)
    install.set_defaults(func=run)
    demo = actions.add_parser("demo", help="Run a seeded local synthetic reference demo.")
    demo.add_argument("name")
    demo.add_argument("--seed", type=int, default=20261018)
    demo.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from commontrace import packs

    if args.pack_action == "list":
        result = [item.__dict__ for item in packs.list_packs()]
    elif args.pack_action == "install":
        result = packs.install(args.name, args.dest)
    else:
        result = packs.demo(args.name, seed=args.seed)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
