"""`commontrace fact`: manage atomic facts lifecycle."""
from __future__ import annotations

import argparse
import sys

from commontrace import hierarchical, paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "fact",
        help="Manage atomic facts lifecycle (ADD, UPDATE, SUPERSEDE, DELETE, search).",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_list = sub.add_parser("list", help="List facts with optional filters.")
    p_list.add_argument("--status", default="active", choices=("active", "superseded", "deleted", "all"))
    p_list.add_argument("--category", default="", choices=("", *hierarchical.CATEGORIES))
    p_list.add_argument("--scope", default="", help="Filter by routing scope.")
    p_list.add_argument("--as-of", default="", help="Point-in-time temporal evaluation date.")
    p_list.add_argument(
        "--include-forgotten", action="store_true",
        help="Include forgotten facts (hidden by default); shown with a [forgotten] marker.",
    )
    p_list.add_argument(
        "--show-expired", action="store_true",
        help="Include TTL-expired facts (hidden by default).",
    )
    p_list.add_argument("--dest", default=None)
    p_list.set_defaults(func=run_list)

    p_add = sub.add_parser("add", help="Add or reinforce an atomic fact.")
    p_add.add_argument("statement", help="The atomic declarative statement.")
    p_add.add_argument("--category", default=hierarchical.DEFAULT_CATEGORY, choices=hierarchical.CATEGORIES)
    p_add.add_argument("--scope", action="append", default=[], help="Routing scope (repeatable).")
    p_add.add_argument("--confidence", type=float, default=0.8, help="Confidence score 0.0 to 1.0.")
    p_add.add_argument("--valid-from", default=None, help="Start date (YYYY-MM-DD or ISO 8601).")
    p_add.add_argument("--valid-until", default=None, help="End date (YYYY-MM-DD or ISO 8601).")
    p_add.add_argument("--expires-at", default=None, help="TTL expiry instant (YYYY-MM-DD or ISO 8601).")
    p_add.add_argument("--source-trace", default="", help="Trace ID where this was observed.")
    p_add.add_argument("--dest", default=None)
    p_add.set_defaults(func=run_add)

    p_srch = sub.add_parser("search", help="Search active facts by relevance and confidence.")
    p_srch.add_argument("query", help="Search query string.")
    p_srch.add_argument("--scope", default="", help="Filter by scope.")
    p_srch.add_argument("--category", default="", choices=("", *hierarchical.CATEGORIES))
    p_srch.add_argument("--limit", type=int, default=10)
    p_srch.add_argument("--as-of", default="", help="Point-in-time date.")
    p_srch.add_argument(
        "--show-expired", action="store_true",
        help="Include TTL-expired facts (hidden by default).",
    )
    p_srch.add_argument("--dest", default=None)
    p_srch.set_defaults(func=run_search)

    p_sup = sub.add_parser("supersede", help="Supersede an existing fact with a new one.")
    p_sup.add_argument("old_id", help="ID of the outdated fact.")
    p_sup.add_argument("new_statement", help="New replacement fact statement or fact ID.")
    p_sup.add_argument("--dest", default=None)
    p_sup.set_defaults(func=run_supersede)

    p_res = sub.add_parser(
        "resolve", help="Resolve a contradiction: invalidate an older fact in favor of newer evidence.")
    p_res.add_argument("old_id", help="ID of the contradicted (older) fact.")
    p_res.add_argument("new_statement", help="Newer replacement fact statement or fact ID.")
    p_res.add_argument("--dest", default=None)
    p_res.set_defaults(func=run_resolve)

    p_del = sub.add_parser("delete", help="Soft-delete a fact.")
    p_del.add_argument("fact_id", help="ID of the fact to delete.")
    p_del.add_argument("--dest", default=None)
    p_del.set_defaults(func=run_delete)

    p_forget = sub.add_parser(
        "forget",
        help="Hide a fact from default listings (reversible, git-audited).",
    )
    p_forget.add_argument("fact_id", help="ID of the fact to forget.")
    p_forget.add_argument(
        "--undo", action="store_true",
        help="Restore a forgotten fact to default listings.",
    )
    p_forget.add_argument("--dest", default=None)
    p_forget.set_defaults(func=run_forget)


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    status_filter = "" if args.status == "all" else args.status
    facts = hierarchical.list_facts(
        root=root,
        status=status_filter,
        scope=args.scope,
        category=args.category,
        as_of=args.as_of or None,
        include_forgotten=bool(getattr(args, "include_forgotten", False)),
        show_expired=bool(getattr(args, "show_expired", False)),
    )
    if not facts:
        print("No matching facts found.")
        return 0

    print(f"{'ID':<18} {'CAT':<14} {'CONF':<6} {'SCOPES':<16} {'STATEMENT'}")
    print("-" * 80)
    for f in facts:
        scopes_str = ",".join(f.scopes) if f.scopes else "(global)"
        stmt = f.statement if len(f.statement) <= 45 else f.statement[:42] + "..."
        if f.forgotten:
            stmt += " [forgotten]"
        print(f"{f.id:<18} {f.category:<14} {f.confidence:<6.2f} {scopes_str:<16} {stmt}")
    return 0


def run_add(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        fact, action = hierarchical.add_fact(
            root=root,
            statement=args.statement,
            category=args.category,
            scopes=args.scope,
            valid_from=args.valid_from,
            valid_until=args.valid_until,
            expires_at=args.expires_at,
            confidence=args.confidence,
            source_trace_id=args.source_trace,
        )
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if action == "NOOP":
        print(f"Reinforced existing fact '{fact.id}' (confirmations: {fact.confirmations}, conf: {fact.confidence}).")
    else:
        print(f"Added fact '{fact.id}' (category: {fact.category}, conf: {fact.confidence}).")
    return 0


def run_search(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    scored = hierarchical.search_facts(
        root=root,
        query=args.query,
        scope=args.scope,
        category=args.category,
        as_of=args.as_of or None,
        limit=args.limit,
        show_expired=bool(getattr(args, "show_expired", False)),
    )
    if not scored:
        print(f"No facts found matching '{args.query}'.")
        return 0

    print(f"{'SCORE':<8} {'CONF':<6} {'ID':<18} {'STATEMENT'}")
    print("-" * 75)
    for fact, score in scored:
        print(f"{score:<8.3f} {fact.confidence:<6.2f} {fact.id:<18} {fact.statement}")
    return 0


def run_supersede(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        old, new = hierarchical.supersede_fact(
            root=root,
            old_fact_id=args.old_id,
            new_fact_id_or_statement=args.new_statement,
        )
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Superseded fact '{old.id}' with '{new.id}'.")
    return 0


def run_resolve(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        old, new = hierarchical.resolve_contradiction(
            root=root,
            old_fact_id=args.old_id,
            new_fact_id_or_statement=args.new_statement,
        )
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"Resolved contradiction: invalidated '{old.id}' in favor of '{new.id}'.")
    return 0


def run_delete(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    ok = hierarchical.delete_fact(root, args.fact_id)
    if not ok:
        print(f"Fact '{args.fact_id}' not found.", file=sys.stderr)
        return 1
    print(f"Deleted fact '{args.fact_id}'.")
    return 0


def run_forget(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        fact = hierarchical.forget_fact(
            root, args.fact_id, undo=bool(getattr(args, "undo", False)),
        )
    except KeyError:
        print(f"Fact '{args.fact_id}' not found.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if fact.forgotten:
        print(f"Forgot fact '{fact.id}' (hidden from default listings).")
    else:
        print(f"Restored fact '{fact.id}' to default listings.")
    return 0
