from __future__ import annotations

import argparse
import sys

from commontrace import kb_packs, paths
from commontrace.commands import _validators


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "kb",
        help="Curated substrate lesson packs (webhooks, migrations, deploys, ...) "
        "installed into this store for review.",
    )
    sub = p.add_subparsers(dest="kb_cmd", required=True)

    ls = sub.add_parser("list", help="List the packs that ship with this install.")
    ls.set_defaults(func=run_list)

    inst = sub.add_parser(
        "install", help="Install a pack's lessons at status=review (never active).",
    )
    inst.add_argument("pack")
    inst.add_argument("--agent-type", type=_validators.agent_type, default=None)
    inst.add_argument("--dest", default=None)
    inst.set_defaults(func=run_install)


def run_list(args: argparse.Namespace) -> int:
    for info in kb_packs.list_packs():
        print(f"  {info.name:<24} {info.count:>3} lessons  v{info.version}  {info.description}")
    return 0


def run_install(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    paths.warn_if_implicit_cwd_store(args.dest)
    try:
        result = kb_packs.install_pack(root, args.pack, agent_type=args.agent_type)
    except kb_packs.UnknownPack as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    print(
        f"[commontrace] {result.pack}@{result.version}: {len(result.written)} lesson(s) "
        f"written at status=review, {len(result.skipped_existing)} already installed (left untouched)."
    )
    if result.written:
        print(
            "  Each needs one judgement the pack cannot make -- where it does NOT apply in\n"
            "  your stack (`do_not_apply_when`, `## Counter-examples`) -- before\n"
            "  `commontrace lesson approve` will activate it:\n"
            "    commontrace lesson list --status review"
        )
    return 0
