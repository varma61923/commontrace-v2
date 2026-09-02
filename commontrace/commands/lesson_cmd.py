from __future__ import annotations

import argparse
import datetime
import glob
import os
import sys

from commontrace import frontmatter, lesson_io, paths, templates, validate
from commontrace.commands._format import cell, read_or_warn


def _actor() -> str:
    """Who made this change, for the revision journal.

    Best-effort and clearly labelled as such. The point is not authentication
    -- a local store has no identity to authenticate against -- it is that a
    later reader can tell a person's edit apart from `distill`'s or an
    agent's when asking what changed the instruction the fleet follows.
    """
    import getpass

    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no passwd entry in a container
        return "cli"


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
    ap.add_argument(
        "--force",
        action="store_true",
        help="Approve even if the lesson still contains unedited 'TODO:' scaffolding. "
             "Refused by default -- an active lesson is injected into agents verbatim.",
    )
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

    hist = sub.add_parser(
        "history",
        help="What this lesson has said over time, and who changed it.",
        description=(
            "A lesson's content is the treatment in any experiment measuring it, so "
            "an effect size is about a specific revision, not about a slug. This is "
            "how you recover which -- and it is what makes `commontrace experiment`'s "
            "'edited mid-run' finding actionable rather than merely alarming."
        ),
    )
    hist.add_argument("slug", help="Lesson slug (e.g. lesson_retry_backoff)")
    hist.add_argument("--dest", default=None)
    hist.set_defaults(func=run_history)


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
        # Scaffolded at `review`, not `active`.
        #
        # `lesson new` writes a body that is entirely template text
        # ("## Rule\n[1 actionable sentence]"). Creating that at status
        # `active` made it live the instant it was scaffolded: retrievable by
        # `commontrace query`, counted as coverage by `taxonomy`/`pilot`, and
        # published to the whole fleet by `sync --push` -- all before a single
        # word of it had been written.
        #
        # It also bypassed the one control the protocol defines for exactly
        # this: run_approve's own docstring says a lesson "is only ever
        # activated by an explicit human/Validator call to this command,
        # never automatically by whatever proposed it". `commontrace distill`
        # already honours that by writing candidates at `review`; this path
        # was the inconsistent one.
        status="review",
    )
    lesson_io.write_lesson(out_path, fm, templates.lesson_body(), root=root,
                           actor=_actor(), reason="scaffolded by `lesson new`")
    print(f"[commontrace] created {out_path}")
    print(
        f"  Written at status=review. Fill in the Rule/Why/How-to-apply sections, then:\n"
        f"    commontrace lesson approve {args.slug}"
    )
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
            fm, body = frontmatter.read(path)
            errors = validate.validate(fm, schema)
            # Schema-valid is not the same as fit to inject. An ACTIVE lesson
            # is retrieved and fed to agents verbatim, counted as coverage by
            # `taxonomy`/`pilot`, and pushed to the Hub by `sync` -- so one
            # still full of "TODO:" scaffolding is a defect this command
            # exists to catch, and it used to report it as "valid".
            #
            # Scoped to active lessons on purpose: a `status: review`
            # candidate is SUPPOSED to carry placeholders (that is what
            # `commontrace distill` writes and what a human is being asked to
            # fill in), so flagging those would make the check noise.
            if fm.get("status") == "active":
                unfilled = templates.unfilled_placeholders(fm, body)
                if unfilled:
                    errors = list(errors) + [
                        f"active lesson still contains unedited scaffolding in "
                        f"{', '.join(unfilled)} -- it would be injected into agents as-is"
                    ]
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


# Re-exported from commontrace.lesson_io, which owns the one definition now
# that the MCP server and the holdout logger resolve slugs too.
_resolve_lesson_path = lesson_io.lesson_path
_SLUG_RE = lesson_io.SLUG_RE


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

    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        if fm.get("status") != "review":
            print(
                f"[commontrace] {args.slug} has status={fm.get('status')!r}, not 'review' -- "
                "refusing to approve. Only a candidate awaiting review can be approved.",
                file=sys.stderr,
            )
            return 1

        unfilled = templates.unfilled_placeholders(fm, body)
        if unfilled and not args.force:
            print(
                f"[commontrace] refusing to approve {args.slug}: it still contains "
                f"unedited scaffolding in {', '.join(unfilled)}.\n"
                "  Approving activates a lesson for retrieval and injection -- an\n"
                "  agent injects whatever it is given, so a lesson whose rule is\n"
                "  still 'TODO: ...' teaches the fleet nothing and displaces a real\n"
                "  one. It would also be counted as coverage by `commontrace\n"
                "  taxonomy`/`pilot` and pushed to the Hub by `commontrace sync`.\n"
                f"  Edit {path} first, or pass --force if this really is the\n"
                "  intended content.",
                file=sys.stderr,
            )
            return 1

        fm["status"] = "active"
        if args.rationale:
            body = _append_body_note(body, "Approved", args.rationale)
        lesson_io.write_lesson(path, fm, body, root=root, actor=_actor(),
                               reason=args.rationale or "approved")
    if unfilled:
        print(
            f"[commontrace] warning: approved {args.slug} with --force while "
            f"{', '.join(unfilled)} still contain unedited scaffolding.",
            file=sys.stderr,
        )
    print(f"[commontrace] approved {args.slug} (status: review -> active)")
    return 0


def run_reject(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    path = _resolve_lesson_path(root, args.slug)
    if path is None:
        print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
        return 1

    with frontmatter.locked(path):
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
        lesson_io.write_lesson(path, fm, body, root=root, actor=_actor(),
                               reason=args.reason)
    print(f"[commontrace] rejected {args.slug} (status: review -> archived)")
    return 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    for path in _iter_lesson_paths(root, None):
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, _ = result
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


def run_history(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    records = lesson_io.history(root, args.slug)
    path = lesson_io.lesson_path(root, args.slug)
    now = lesson_io.current_revision(path) if path else None

    if not records:
        # Distinguished carefully. "No lesson" and "a lesson with no recorded
        # history" are different facts, and the second is the ordinary state
        # of every lesson written before the journal existed -- reporting it
        # as the first would send someone looking for a missing file.
        if path is None:
            print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
            return 1
        print(f"[commontrace] {args.slug} is at revision {now}, with no recorded history.")
        print("  Changes are journaled from the first write through `lesson new`, "
              "`lesson approve`, `distill` or the MCP tools.")
        return 0

    print(f"# {args.slug}")
    print()
    print(f"Currently at **{now}**, {len(records)} recorded change(s).")
    print()
    for record in records:
        arrow = f"{record.get('from') or '(new)'} -> {record.get('to')}"
        print(f"- `{arrow}`  {record.get('at', '')}")
        print(f"    by {record.get('actor', 'unknown')}"
              + (f", status {record['status']}" if record.get("status") else ""))
        if record.get("reason"):
            print(f"    {record['reason']}")
    print()
    print("_An effect size from `commontrace experiment` is about the revision that was "
          "on disk while the assignments were made, not about the slug. A lesson edited "
          "during a run makes the two arms measure different treatments, which the "
          "validity section of that report calls out._")
    return 0
