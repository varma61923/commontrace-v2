from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import sys

from commontrace import frontmatter, paths, templates, validate
from commontrace.commands._format import cell

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("lesson", help="Manage lessons (create, validate, list).")
    sub = p.add_subparsers(dest="lesson_cmd", required=True)

    new = sub.add_parser("new", help="Create a new lesson from the template.")
    new.add_argument("--slug", required=True, help="e.g. lesson_my_rule")
    new.add_argument("--description", required=True)
    new.add_argument(
        "--agent-type", choices=paths.AGENT_TYPES, default=None,
        help="Defaults to the agent_type this store was initialized with.",
    )
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

    ap = sub.add_parser(
        "approve",
        help="Validator step: status review -> active. Refuses a lesson that isn't 'review'.",
    )
    ap.add_argument("slug")
    ap.add_argument("--rationale", default="", help="One sentence recorded in the lesson body.")
    ap.add_argument("--dest", default=None)
    ap.set_defaults(func=run_approve)

    rj = sub.add_parser(
        "reject",
        help="Validator step: status review -> archived. Refuses a lesson that isn't 'review'.",
    )
    rj.add_argument("slug")
    rj.add_argument("--reason", required=True, help="Why this candidate was rejected -- recorded in the lesson body.")
    rj.add_argument("--dest", default=None)
    rj.set_defaults(func=run_reject)


def run_new(args: argparse.Namespace) -> int:
    if not _SLUG_RE.match(args.slug):
        print(
            f"[commontrace] invalid --slug '{args.slug}': only letters, digits, "
            "'_', '-' are allowed (no path separators or '..').",
            file=sys.stderr,
        )
        return 1

    root = paths.resolve_root(args.dest)
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    filename = f"{args.slug}.md" if args.slug.startswith("lesson_") else f"lesson_{args.slug}.md"
    out_path = os.path.join(ldir, filename)
    if os.path.exists(out_path):
        print(f"[commontrace] {out_path} already exists - aborting.", file=sys.stderr)
        return 1

    fm = templates.lesson_frontmatter(
        slug=args.slug,
        description=args.description,
        agent_type=args.agent_type or paths.store_agent_type(root),
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
    if args.path and not os.path.isfile(args.path):
        frontmatter.read(args.path)
    n_checked = 0
    n_failed = 0
    for path in _iter_lesson_paths(root, args.path):
        n_checked += 1
        try:
            fm, _ = frontmatter.read(path)
            errors = validate.validate(fm, schema)
        except (frontmatter.FrontmatterError, OSError, UnicodeDecodeError) as exc:
            errors = [str(exc)]
        if errors:
            n_failed += 1
            print(f"FAIL {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK   {path}")
    print(f"\n[commontrace] {n_checked - n_failed}/{n_checked} lessons valid.")
    return 1 if n_failed else 0


def _resolve_lesson_path(root: str, slug: str) -> str | None:
    if not _SLUG_RE.match(slug):
        return None
    ldir = paths.lessons_dir(root)
    filename = f"{slug}.md" if slug.startswith("lesson_") else f"lesson_{slug}.md"
    path = os.path.join(ldir, filename)
    if os.path.isfile(path):
        return path
    legacy_path = os.path.join(ldir, f"{slug}.md")
    if os.path.isfile(legacy_path):
        return legacy_path
    return None


def _append_body_note(body: str, heading: str, text: str) -> str:
    date = datetime.date.today().isoformat()
    return body.rstrip("\n") + f"\n\n## {heading}\n{date}: {text}\n"


def run_approve(args: argparse.Namespace) -> int:
    """The generic-pipeline Validator step (protocol/PROTOCOL.md §6's
    "Validator" role, e.g. the code-review profile's Lambda): a candidate
    lesson at status=review is only ever activated by an explicit human/
    Validator call to this command, never automatically by whatever
    proposed it (e.g. `commontrace distill`)."""
    root = paths.resolve_root(args.dest)
    path = _resolve_lesson_path(root, args.slug)
    if path is None:
        print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
        return 1

    fm, body = frontmatter.read(path)
    if fm.get("status") != "review":
        print(
            f"[commontrace] {args.slug} has status={fm.get('status')!r}, not 'review' -- "
            "refusing to approve. Only a candidate awaiting review can be approved.",
            file=sys.stderr,
        )
        return 1

    fm["status"] = "active"
    if args.rationale:
        body = _append_body_note(body, "Approved", args.rationale)
    frontmatter.write(path, fm, body)
    print(f"[commontrace] approved {args.slug} (status: review -> active)")
    return 0


def run_reject(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    path = _resolve_lesson_path(root, args.slug)
    if path is None:
        print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
        return 1

    fm, body = frontmatter.read(path)
    if fm.get("status") != "review":
        print(
            f"[commontrace] {args.slug} has status={fm.get('status')!r}, not 'review' -- "
            "refusing to reject. Only a candidate awaiting review can be rejected.",
            file=sys.stderr,
        )
        return 1

    fm["status"] = "archived"
    body = _append_body_note(body, "Rejected", args.reason)
    frontmatter.write(path, fm, body)
    print(f"[commontrace] rejected {args.slug} (status: review -> archived)")
    return 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    for path in _iter_lesson_paths(root, None):
        fm, _ = frontmatter.read(path)
        if args.agent_type and fm.get("agent_type") != args.agent_type:
            continue
        if args.status and fm.get("status") != args.status:
            continue
        # `.get(k, default)` returns the default only when the key is ABSENT.
        # A key present with an empty YAML value parses to None, which has no
        # __format__ for ":8s" -- so a half-finished edit (`status:`) crashed
        # the browsing command with a traceback while `lesson validate`
        # diagnosed the same file cleanly. Coerce before formatting.
        print(
            f"{cell(fm.get('name')):45s} "
            f"[{cell(fm.get('agent_type')):9s}] "
            f"imp={cell(fm.get('importance'))} "
            f"status={cell(fm.get('status')):8s} "
            f"{fm.get('description') or ''}"
        )
    return 0
