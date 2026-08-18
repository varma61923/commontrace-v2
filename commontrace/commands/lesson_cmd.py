from __future__ import annotations

import argparse
import glob
import os
import sys

from commontrace import frontmatter, paths, templates, validate


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("lesson", help="Manage lessons (create, validate, list).")
    sub = p.add_subparsers(dest="lesson_cmd", required=True)

    new = sub.add_parser("new", help="Create a new lesson from the template.")
    new.add_argument("--slug", required=True, help="e.g. lesson_my_rule")
    new.add_argument("--description", required=True)
    new.add_argument("--agent-type", choices=paths.AGENT_TYPES, default="code")
    new.add_argument("--domain", required=True)
    new.add_argument("--tags", default="")
    new.add_argument("--applies-when", default="")
    new.add_argument("--do-not-apply-when", default="")
    new.add_argument("--importance", type=int, default=3)
    new.add_argument("--importance-rationale", default="")
    new.add_argument("--source-traces", default="", help="Comma-separated trace ids/slugs")
    new.add_argument("--dest", default=None)
    new.set_defaults(func=run_new)

    val = sub.add_parser("validate", help="Validate one or all lessons against protocol/schemas/lesson.schema.json.")
    val.add_argument("path", nargs="?", default=None, help="Specific lesson file (default: all lessons in the store)")
    val.add_argument("--dest", default=None)
    val.set_defaults(func=run_validate)

    ls = sub.add_parser("list", help="List lessons in the store.")
    ls.add_argument("--agent-type", default=None)
    ls.add_argument("--status", default=None)
    ls.add_argument("--dest", default=None)
    ls.set_defaults(func=run_list)


def run_new(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    out_path = os.path.join(ldir, f"{args.slug}.md")
    if os.path.exists(out_path):
        print(f"[commontrace] {out_path} already exists - aborting.", file=sys.stderr)
        return 1

    fm = templates.lesson_frontmatter(
        slug=args.slug,
        description=args.description,
        agent_type=args.agent_type,
        domain=args.domain,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        applies_when=args.applies_when,
        do_not_apply_when=args.do_not_apply_when,
        importance=args.importance,
        importance_rationale=args.importance_rationale,
        source_traces=[t.strip() for t in args.source_traces.split(",") if t.strip()],
    )
    frontmatter.write(out_path, fm, templates.lesson_body())
    print(f"[commontrace] created {out_path}")
    return 0


def _iter_lesson_paths(root: str, explicit: str | None):
    if explicit:
        yield explicit
        return
    ldir = paths.lessons_dir(root)
    for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(p) == "lesson_template.md":
            continue
        yield p


def run_validate(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    schema = validate.load_schema("lesson.schema.json")
    n_checked = 0
    n_failed = 0
    for path in _iter_lesson_paths(root, args.path):
        n_checked += 1
        fm, _ = frontmatter.read(path)
        errors = validate.validate(fm, schema)
        if errors:
            n_failed += 1
            print(f"FAIL {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {path}")
    print(f"\n[commontrace] {n_checked - n_failed}/{n_checked} lessons valid.")
    return 1 if n_failed else 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    for path in _iter_lesson_paths(root, None):
        fm, _ = frontmatter.read(path)
        if args.agent_type and fm.get("agent_type") != args.agent_type:
            continue
        if args.status and fm.get("status") != args.status:
            continue
        print(
            f"{fm.get('name', '?'):45s} "
            f"[{fm.get('agent_type', '?'):9s}] "
            f"imp={fm.get('importance', '?')} "
            f"status={fm.get('status', '?'):8s} "
            f"{fm.get('description', '')}"
        )
    return 0
