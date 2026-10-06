"""`commontrace community`: build and inspect topic communities of lessons+facts."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import communities, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "community",
        help="Topic communities over lessons and facts (label propagation, extractive summaries).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_build = sub.add_parser("build", help="Rebuild memory/communities/ from current lessons and facts.")
    p_build.add_argument("--json", action="store_true")
    p_build.add_argument("--dest", default=None)
    p_build.set_defaults(func=run_build)

    p_list = sub.add_parser("list", help="List the stored communities.")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    p_show = sub.add_parser("show", help="Show one community's members and summary.")
    p_show.add_argument("name", help="Community name (from `community list`).")
    p_show.add_argument("--json", action="store_true")
    p_show.add_argument("--dest", default=None)
    p_show.set_defaults(func=run_show)


def run_build(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        groups = communities.build_communities(root)
    except Exception as exc:  # noqa: BLE001
        print(f"[commontrace] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({name: members for name, members in groups.items()}, indent=2))
        return 0
    n_members = sum(len(members) for members in groups.values())
    print(f"Built {len(groups)} communit{'y' if len(groups) == 1 else 'ies'} "
          f"over {n_members} node(s) -> memory/communities/")
    for name, members in groups.items():
        print(f"  {name}  ({len(members)} member(s))")
    return 0


def run_list(args: argparse.Namespace) -> int:
    rows = communities.list_communities(paths.resolve_root(args.dest))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No communities stored. Run `commontrace community build`.")
        return 0
    print(f"{'NAME':<40} {'SIZE':>4}  TOP TERMS")
    print("-" * 80)
    for row in rows:
        terms = ", ".join(str(t) for t in (row.get("top_terms") or [])[:4])
        print(f"{str(row.get('name', '')):<40} {int(row.get('size') or 0):>4}  {terms}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    community = communities.get_community(paths.resolve_root(args.dest), args.name)
    if community is None:
        print(f"[commontrace] no community named {args.name!r}; try `community list`.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(community, indent=2))
        return 0
    print(f"# {community.get('name', args.name)}  ({int(community.get('size') or 0)} member(s))")
    if community.get("summary"):
        print(str(community["summary"]))
    print()
    for member in community.get("members") or []:
        print(f"  - {member}")
    return 0
