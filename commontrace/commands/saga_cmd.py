"""`commontrace saga`: Manage ordered incident/migration narrative sagas."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import paths, sagas


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "saga",
        help="Manage incident and migration narratives with watermarked running briefs.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="List all sagas.")
    p_list.add_argument("--status", default="", help="Filter by status (active/resolved/archived).")
    p_list.add_argument("--tag", default="", help="Filter by tag.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    # get
    p_get = sub.add_parser("get", help="Get a saga and its chronological timeline.")
    p_get.add_argument("id", help="Saga ID.")
    p_get.add_argument("--json", action="store_true", help="Output JSON.")
    p_get.add_argument("--dest", default=None)
    p_get.set_defaults(func=run_get)

    # create
    p_create = sub.add_parser("create", help="Create a new saga narrative.")
    p_create.add_argument("id", help="Saga ID.")
    p_create.add_argument("title", help="Saga title.")
    p_create.add_argument("--tags", nargs="*", default=[], help="Tags for this saga.")
    p_create.add_argument("--brief", default="", help="Initial running brief.")
    p_create.add_argument("--status", default="active", help="Initial status (default: active).")
    p_create.add_argument("--dest", default=None)
    p_create.set_defaults(func=run_create)

    # append
    p_app = sub.add_parser("append", help="Append an event to a saga.")
    p_app.add_argument("id", help="Saga ID.")
    p_app.add_argument("title", help="Event headline/title.")
    p_app.add_argument("--desc", default="", help="Detailed description.")
    p_app.add_argument("--actor", default="cli", help="Actor responsible.")
    p_app.add_argument("--brief", default="", help="Updated running brief.")
    p_app.add_argument("--watermark", default="", help="Watermark timestamp for brief.")
    p_app.add_argument("--dest", default=None)
    p_app.set_defaults(func=run_append)

    # brief
    p_brief = sub.add_parser("brief", help="Update the watermarked running brief.")
    p_brief.add_argument("id", help="Saga ID.")
    p_brief.add_argument("brief", help="New running brief text.")
    p_brief.add_argument("--watermark", required=True, help="Watermark timestamp.")
    p_brief.add_argument("--dest", default=None)
    p_brief.set_defaults(func=run_brief)


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    items = sagas.list_sagas(root, status=args.status, tag=args.tag)
    if args.json:
        print(json.dumps([s.to_dict() for s in items], indent=2))
        return 0
    if not items:
        print("[commontrace] No sagas found.")
        return 0
    print(f"[commontrace] {len(items)} saga(s):")
    for s in items:
        status_tag = f"[{s.status.upper()}]"
        print(f"  {status_tag:12s} {s.id:24s} {s.title} ({len(s.events)} events)")
        if s.watermarked_running_brief:
            preview = s.watermarked_running_brief.replace("\n", " ")[:80]
            print(f"               Brief: {preview}...")
    return 0


def run_get(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    saga = sagas.get_saga(root, args.id)
    if not saga:
        print(f"[commontrace] Saga '{args.id}' not found.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(saga.to_dict(), indent=2))
        return 0
    print(f"[commontrace] Saga: {saga.title} ({saga.id}) [{saga.status}]")
    if saga.tags:
        print(f"  Tags: {', '.join(saga.tags)}")
    if saga.watermarked_running_brief:
        print(f"\n  Running Brief (watermark: {saga.watermark}):\n  {saga.watermarked_running_brief}\n")
    print(f"  Events ({len(saga.events)}):")
    for e in saga.events:
        print(f"    [{e.timestamp[:19]}] {e.title} (by {e.actor})")
        if e.description:
            print(f"      {e.description}")
    return 0


def run_create(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        saga = sagas.create_saga(
            root, args.id, args.title, tags=args.tags, brief=args.brief, status=args.status,
        )
        print(f"[commontrace] Created saga '{saga.id}': {saga.title}")
        return 0
    except Exception as exc:
        print(f"[commontrace] Error: {exc}", file=sys.stderr)
        return 1


def run_append(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        saga = sagas.append_saga_event(
            root, args.id, args.title, description=args.desc, actor=args.actor,
            new_brief=args.brief if args.brief else None,
            watermark=args.watermark if args.watermark else None,
        )
        print(f"[commontrace] Appended event to '{saga.id}' (now {len(saga.events)} events)")
        return 0
    except Exception as exc:
        print(f"[commontrace] Error: {exc}", file=sys.stderr)
        return 1


def run_brief(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        saga = sagas.update_running_brief(root, args.id, args.brief, args.watermark)
        print(f"[commontrace] Updated brief for '{saga.id}' (watermark: {saga.watermark})")
        return 0
    except Exception as exc:
        print(f"[commontrace] Error: {exc}", file=sys.stderr)
        return 1
