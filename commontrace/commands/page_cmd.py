"""`commontrace page`: Manage curated knowledge pages with dry-run diffs."""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import knowledge_pages, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "page",
        help="Manage curated knowledge pages / mental models with dry-run diffs.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="List all curated knowledge pages.")
    p_list.add_argument("--tag", default="", help="Filter by tag.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    # get
    p_get = sub.add_parser("get", help="Get a knowledge page by slug.")
    p_get.add_argument("slug", help="Slug of the page (e.g. architecture/auth).")
    p_get.add_argument("--version", type=int, default=None, help="Specific version to fetch.")
    p_get.add_argument("--json", action="store_true", help="Output JSON.")
    p_get.add_argument("--dest", default=None)
    p_get.set_defaults(func=run_get)

    # update / set
    p_set = sub.add_parser("set", help="Create or update a curated knowledge page.")
    p_set.add_argument("slug", help="Slug of the page.")
    p_set.add_argument("content", help="Markdown content.")
    p_set.add_argument("--title", default="", help="Title of the page.")
    p_set.add_argument("--tags", nargs="*", default=None, help="Tags for this page.")
    p_set.add_argument("--expected-version", type=int, default=None, help="Optimistic concurrency version.")
    p_set.add_argument("--dry-run", action="store_true", help="Preview line-by-line diff with zero disk writes.")
    p_set.add_argument("--comment", default="", help="Commit rationale / comment.")
    p_set.add_argument("--dest", default=None)
    p_set.set_defaults(func=run_set)

    # diff
    p_diff = sub.add_parser("diff", help="Generate dry-run diff against an existing page.")
    p_diff.add_argument("slug", help="Slug of the page.")
    p_diff.add_argument("content", help="Proposed new content.")
    p_diff.add_argument("--dest", default=None)
    p_diff.set_defaults(func=run_diff)

    # history
    p_hist = sub.add_parser("history", help="Show revision history for a page.")
    p_hist.add_argument("slug", nargs="?", default="", help="Optional slug to filter history.")
    p_hist.add_argument("--json", action="store_true", help="Output JSON.")
    p_hist.add_argument("--dest", default=None)
    p_hist.set_defaults(func=run_history)


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    pages = knowledge_pages.list_pages(root, tag=args.tag)
    if args.json:
        print(json.dumps([p.to_dict() for p in pages], indent=2))
        return 0
    if not pages:
        print("[commontrace] No knowledge pages found.")
        return 0
    print(f"[commontrace] {len(pages)} knowledge page(s):")
    for p in pages:
        v_str = f"v{p.version}"
        tags_str = f"[{', '.join(p.tags)}]" if p.tags else ""
        print(f"  {v_str:6s} {p.slug:32s} {p.title:28s} ({p.char_count} chars) {tags_str}")
    return 0


def run_get(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    page = knowledge_pages.get_page(root, args.slug, version=args.version)
    if not page:
        print(f"[commontrace] Knowledge page '{args.slug}' not found.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(page.to_dict(), indent=2))
        return 0
    print(f"# {page.title} ({page.slug} · v{page.version})")
    print(f"Updated: {page.updated_at} by {page.last_actor}")
    if page.tags:
        print(f"Tags: {', '.join(page.tags)}")
    print("-" * 60)
    print(page.content)
    return 0


def run_set(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        res = knowledge_pages.update_page(
            root, args.slug, args.content, title=args.title, tags=args.tags,
            expected_version=args.expected_version, dry_run=args.dry_run,
            actor="cli", comment=args.comment,
        )
        if args.dry_run:
            print(
                f"[commontrace] Dry-run diff for '{args.slug}' "
                f"(v{res['current_version']} -> v{res['proposed_version']}):"
            )

            if res.get("diff"):
                print(res["diff"])
            else:
                print("  (No changes detected)")
        else:
            print(f"[commontrace] Saved page '{args.slug}' (v{res['version']})")
            if res.get("diff"):
                print(res["diff"])
        return 0
    except Exception as exc:
        print(f"[commontrace] Error: {exc}", file=sys.stderr)
        return 1


def run_diff(args: argparse.Namespace) -> int:
    args.dry_run = True
    args.title = ""
    args.tags = None
    args.expected_version = None
    args.comment = ""
    return run_set(args)


def run_history(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    history = knowledge_pages.page_history(root, args.slug)
    if args.json:
        print(json.dumps(history, indent=2))
        return 0
    if not history:
        print(f"[commontrace] No history for '{args.slug}'.")
        return 0
    print(f"[commontrace] {len(history)} revision(s) for '{args.slug}':")
    for h in reversed(history):
        v = h.get("version", "?")
        actor = h.get("actor", "agent")
        ts = h.get("timestamp", "")[:19]
        comment = h.get("comment", "")
        print(f"  v{v:<4} [{ts}] by {actor:<10} - {comment}")
    return 0
