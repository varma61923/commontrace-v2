from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys

from commontrace import (
    approval,
    evidence_io,
    frontmatter,
    holdout_io,
    lesson_cache,
    lesson_io,
    memory_guard,
    paths,
    redundancy,
    reliability,
    templates,
    trace_io,
    validate,
)
from commontrace.commands import _llm_draft, _validators
from commontrace.commands._format import cell, read_or_warn


def _actor() -> str:
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
        "--agent-type", type=_validators.agent_type, default=None,
        help="Any lowercase slug (the taxonomy is open -- e.g. code, hr, robotics, "
             "legal). Defaults to the agent_type this store was initialized with.",
    )
    new.add_argument("--domain", required=True)
    new.add_argument("--tags", default="")
    new.add_argument("--applies-when", default="")
    new.add_argument("--do-not-apply-when", default="")
    new.add_argument("--importance", type=int, default=3)
    new.add_argument("--importance-rationale", default="")
    new.add_argument("--source-traces", default="", help="Comma-separated trace ids/slugs")
    new.add_argument(
        "--scopes", default="",
        help="Comma-separated project/team scopes. Unscoped lessons remain global.",
    )
    new.add_argument("--valid-from", default="", help="First valid instant (YYYY-MM-DD or ISO 8601).")
    new.add_argument("--valid-until", default="", help="Exclusive expiry instant (YYYY-MM-DD or ISO 8601).")
    new.add_argument("--dest", default=None)
    new.set_defaults(func=run_new)

    val = sub.add_parser("validate", help="Validate one or all lessons against protocol/schemas/lesson.schema.json.")
    val.add_argument("path", nargs="?", default=None, help="Specific lesson file (default: all lessons in the store)")
    val.add_argument("--dest", default=None)
    val.set_defaults(func=run_validate)

    ls = sub.add_parser("list", help="List lessons in the store.")
    ls.add_argument("--agent-type", default=None)
    ls.add_argument("--status", default=None)
    ls.add_argument("--scope", default="", help="Include global lessons and lessons in this scope.")
    ls.add_argument(
        "--json", action="store_true",
        help="Print one JSON array of lessons (name, agent_type, importance, status, "
             "description, domain, scopes, validity, path) for scripts.",
    )
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
        help="Approve even if the lesson still contains unedited 'TODO:' scaffolding, "
             "or a high-confidence secret/prompt-injection pattern the content-safety scan "
             "flagged. Refused by default -- an active lesson is injected into agents verbatim.",
    )
    ap.add_argument("--dest", default=None)
    ap.set_defaults(func=run_approve)

    withdraw = sub.add_parser("revoke", help="Durably revoke a lesson approval and archive it.")
    withdraw.add_argument("slug")
    withdraw.add_argument("--reason", default="approval revoked")
    withdraw.add_argument("--dest", default=None)
    withdraw.set_defaults(func=run_revoke)

    rj = sub.add_parser(
        "reject",
        help="Validator step: status review -> archived. Refuses a lesson that isn't 'review'.",
    )
    rj.add_argument("slug")
    rj.add_argument("--reason", required=True, help="Why this candidate was rejected -- recorded in the lesson body.")
    rj.add_argument("--dest", default=None)
    rj.set_defaults(func=run_reject)

    sr = sub.add_parser(
        "suggest-revision",
        help="Draft a tightened activation condition for a MISCALIBRATED lesson, "
             "from its own retrieval evidence. Writes a new review-status lesson; "
             "changes nothing about the original until you approve the draft.",
    )
    sr.add_argument("slug")
    sr.add_argument(
        "--draft", action="store_true",
        help="Ask a configured LLM (COMMONTRACE_LLM_API_KEY) to tighten "
             "applies_when/do_not_apply_when from the same evidence, instead of a "
             "'TODO: tighten' placeholder. Falls back to the placeholder, with a "
             "stated reason, if no provider is configured or it refuses.",
    )
    sr.add_argument("--dest", default=None)
    sr.set_defaults(func=run_suggest_revision)

    srw = sub.add_parser(
        "suggest-rewrite",
        help="Draft a full rewrite for a HARMFUL lesson (its rule, not just when it "
             "fires, may be wrong). Requires an LLM -- there is no honest heuristic "
             "placeholder for 'what should this rule actually say'. Writes a new "
             "review-status lesson; changes nothing about the original.",
    )
    srw.add_argument("slug")
    srw.add_argument("--dest", default=None)
    srw.set_defaults(func=run_suggest_rewrite)

    aa = sub.add_parser(
        "auto-approve",
        help="Activate LLM-drafted review lessons that pass every approval gate. Requires "
             "`auto_approve_drafts: true` in memory/approval-policy.yaml and a started holdout.",
    )
    aa.add_argument("--dest", default=None)
    aa.set_defaults(func=run_auto_approve)

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
    hist.add_argument(
        "--as-of", default=None, metavar="DATE",
        help="Print what this lesson actually SAID at this point in time (YYYY-MM-DD "
             "or full ISO 8601), reconstructed from the revision journal, instead of "
             "the list of changes. What environments.py calls 'which release is "
             "current' upgraded to 'what did this environment actually serve on any "
             "past date' -- see commontrace/lesson_io.py's content_as_of.",
    )
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

    valid_from_arg = getattr(args, "valid_from", "")
    valid_until_arg = getattr(args, "valid_until", "")
    try:
        valid_from = lesson_cache.parse_moment(valid_from_arg) if valid_from_arg else None
        valid_until = lesson_cache.parse_moment(valid_until_arg) if valid_until_arg else None
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if valid_from is not None and valid_until is not None and valid_from >= valid_until:
        print("[commontrace] --valid-until must be later than --valid-from", file=sys.stderr)
        return 1

    root = paths.resolve_root(args.dest)
    ldir = paths.lessons_dir(root)
    paths.warn_if_implicit_cwd_store(args.dest)
    if not _validators.check_text_size(
        {"description": args.description, "applies_when": args.applies_when,
         "do_not_apply_when": args.do_not_apply_when,
         "importance_rationale": args.importance_rationale},
        what="lesson",
    ):
        return 1
    os.makedirs(ldir, exist_ok=True)
    filename = f"{args.slug}.md" if args.slug.startswith("lesson_") else f"lesson_{args.slug}.md"
    out_path = os.path.join(ldir, filename)
    with frontmatter.locked(out_path):
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
            status="review",
            scopes=[t.strip() for t in getattr(args, "scopes", "").split(",") if t.strip()],
            valid_from=valid_from_arg,
            valid_until=valid_until_arg,
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
        safe_path = paths.enforce_boundary(root, explicit)
        yield safe_path
        return
    ldir = paths.lessons_dir(root)
    for p in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(p) == "lesson_template.md":
            continue
        yield p


def _active_lesson_texts(root: str, *, exclude: str = "", scope: str = "") -> list[tuple[str, str]]:
    out = []
    for path in _iter_lesson_paths(root, None):
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            continue
        fm, body = parsed
        if str(fm.get("status", "")) != "active":
            continue
        raw_scopes = fm.get("scopes")
        scopes = (
            {str(item).strip() for item in raw_scopes}
            if isinstance(raw_scopes, (list, tuple, set))
            else {str(raw_scopes).strip()} if raw_scopes else set()
        )
        if scope and scopes and scope not in scopes:
            continue
        slug = str(fm.get("name", ""))
        if not slug or slug == exclude:
            continue
        out.append((slug, redundancy.comparable_text(fm, body)))
    return out


def run_validate(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    schema = validate.load_schema("lesson.schema.json")
    if args.path:
        safe_path = paths.enforce_boundary(root, args.path)
        if not os.path.isfile(safe_path):
            frontmatter.read(safe_path)
    n_checked = 0
    n_failed = 0
    for path in _iter_lesson_paths(root, args.path):
        n_checked += 1
        try:
            fm, body = frontmatter.read(path)
            errors = validate.validate(fm, schema)
            try:
                valid_from = lesson_cache.parse_moment(fm["valid_from"]) if fm.get("valid_from") else None
                valid_until = lesson_cache.parse_moment(fm["valid_until"]) if fm.get("valid_until") else None
            except ValueError as exc:
                errors = list(errors) + [str(exc)]
                valid_from = valid_until = None
            if valid_from is not None and valid_until is not None and valid_from >= valid_until:
                errors = list(errors) + ["valid_until must be later than valid_from"]
            if fm.get("expires") not in (None, ""):
                try:
                    frontmatter.validate_expires(fm["expires"])
                except frontmatter.FrontmatterError as exc:
                    errors = list(errors) + [str(exc)]
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


_resolve_lesson_path = lesson_io.lesson_path
_SLUG_RE = lesson_io.SLUG_RE


def _append_body_note(body: str, heading: str, text: str) -> str:
    date = datetime.date.today().isoformat()
    return body.rstrip("\n") + f"\n\n## {heading}\n{date}: {text}\n"


def _guard_fields(fm: dict, body: str) -> dict:
    return {
        "description": fm.get("description", ""),
        "applies_when": fm.get("applies_when", ""),
        "do_not_apply_when": fm.get("do_not_apply_when", ""),
        "importance_rationale": fm.get("importance_rationale", ""),
        "domain": fm.get("domain", ""),
        "body": body,
    }


def run_approve(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    approver = getattr(args, "approver", None) or _actor()
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

        try:
            policy = approval.load_policy(root)
            approval.check(
                policy, slug=args.slug, approver=approver,
                authors=approval.authors_of(root, args.slug),
            )
        except (approval.ApprovalDenied, approval.PolicyError) as exc:
            print(f"[commontrace] refusing to approve {args.slug}: {exc}", file=sys.stderr)
            return 1

        guard = memory_guard.scan_fields(_guard_fields(fm, body))
        if guard.should_block and not args.force:
            print(
                f"[commontrace] refusing to approve {args.slug}: the content-safety scan "
                f"flagged this lesson -- {guard.summary()}.\n"
                "  Approving activates a lesson for retrieval and injection -- an agent\n"
                "  injects whatever it is given, so a credential or a prompt-injection\n"
                "  payload here would be replayed into every later decision this lesson\n"
                "  matches. If this is a false positive (e.g. a lesson that legitimately\n"
                f"  documents an example credential pattern), review {path} and pass\n"
                "  --force if this really is the intended content.",
                file=sys.stderr,
            )
            for f in guard.blocking_findings:
                print(f"    - [{f.category}] {f.label} in {f.field}: {f.excerpt!r}", file=sys.stderr)
            return 1

        duplicate = redundancy.closest(
            redundancy.comparable_text(fm, body),
            _active_lesson_texts(root, exclude=args.slug, scope=getattr(args, "scope", "")),
            threshold=redundancy.DEFAULT_THRESHOLD,
        )
        if duplicate is not None and not args.force:
            print(
                f"[commontrace] refusing to approve {args.slug}: it restates the active "
                f"lesson {duplicate.a!r} (similarity {duplicate.similarity:.2f}).\n"
                "  Two lessons saying the same thing compete for the same retrieval slot\n"
                "  forever, and neither wins reliably. Review the other one:\n"
                f"    commontrace lesson history {duplicate.a}\n"
                "  If this is genuinely a different rule, pass --force.",
                file=sys.stderr,
            )
            return 1

        fm["status"] = "active"
        fm.update(getattr(args, "extra_frontmatter", None) or {})
        if args.rationale:
            body = _append_body_note(body, "Approved", args.rationale)
        from commontrace import lesson_admission

        try:
            fm[lesson_admission.RECEIPT_FIELD] = lesson_admission.issue(root, path, fm, body, actor=approver)
        except (lesson_admission.AdmissionError, OSError) as exc:
            print(f"[commontrace] approval receipt could not be recorded: {type(exc).__name__}.", file=sys.stderr)
            return 1
        lesson_io.write_lesson(path, fm, body, root=root, actor=approver,
                               reason=args.rationale or "approved")
    if unfilled:
        print(
            f"[commontrace] warning: approved {args.slug} with --force while "
            f"{', '.join(unfilled)} still contain unedited scaffolding.",
            file=sys.stderr,
        )
    if guard.should_block:
        print(
            f"[commontrace] warning: approved {args.slug} with --force while the "
            f"content-safety scan still flagged it -- {guard.summary()}.",
            file=sys.stderr,
        )
    if duplicate is not None:
        print(
            f"[commontrace] warning: approved {args.slug} with --force while it still "
            f"restates the active lesson {duplicate.a!r} (similarity {duplicate.similarity:.2f}).",
            file=sys.stderr,
        )
    print(f"[commontrace] approved {args.slug} (status: review -> active)")
    print("  `commontrace release cut` records the active set as a rollback point.")
    return 0


def run_revoke(args: argparse.Namespace) -> int:
    from commontrace import lesson_admission

    root = paths.resolve_root(args.dest)
    path = lesson_io.lesson_path(root, args.slug)
    if path is None:
        print("[commontrace] no lesson found to revoke.", file=sys.stderr)
        return 1
    with frontmatter.locked(path):
        fm, body = frontmatter.read(path)
        try:
            lesson_admission.revoke(root, path, actor=_actor())
        except (lesson_admission.AdmissionError, OSError) as exc:
            print(f"[commontrace] approval revocation could not be recorded: {type(exc).__name__}.", file=sys.stderr)
            return 1
        fm["status"] = "archived"
        lesson_io.write_lesson(path, fm, body, root=root, actor=_actor(), reason=args.reason)
    print(f"[commontrace] revoked approval for {args.slug}")
    return 0


def run_auto_approve(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    try:
        policy = approval.load_policy(root)
    except approval.PolicyError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1
    if not policy.auto_approve_drafts:
        print(
            "[commontrace] auto-approval is off. Set `auto_approve_drafts: true` in "
            f"{approval.policy_path(root)} to allow it.",
            file=sys.stderr,
        )
        return 1
    config = holdout_io.load_config(root)
    if not config.started_at or config.rate < approval.AUTO_APPROVE_MIN_HOLDOUT:
        print(
            "[commontrace] refusing: auto-approval needs a started holdout of at least "
            f"{approval.AUTO_APPROVE_MIN_HOLDOUT:.0%}, so every lesson it activates is measured.\n"
            "  Start one: commontrace experiment --configure --rate 0.1",
            file=sys.stderr,
        )
        return 1

    approved: list[str] = []
    refused: list[str] = []
    for path in _iter_lesson_paths(root, None):
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            continue
        fm, _body = parsed
        if fm.get("status") != "review" or "llm_draft" not in fm:
            continue
        slug = str(fm.get("name") or "")
        rc = run_approve(argparse.Namespace(
            slug=slug, force=False, dest=args.dest,
            rationale=f"auto-approved with a {config.rate:.0%} holdout running",
            approver=approval.AUTO_APPROVER, extra_frontmatter={"auto_approved": True},
        ))
        (approved if rc == 0 else refused).append(slug)
    print(
        f"[commontrace] auto-approve: {len(approved)} activated, "
        f"{len(refused)} refused by the approval gates."
    )
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


def _occasion_labels(root: str, wanted: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not wanted:
        return out
    for path in sorted(glob.glob(os.path.join(paths.episodes_dir(root), "*.md"))):
        if os.path.basename(path).startswith("_") or "template" in os.path.basename(path):
            continue
        parsed = read_or_warn(frontmatter.read, path)
        if parsed is None:
            continue
        fm, _body = parsed
        name = str(fm.get("name", os.path.basename(path)))
        if name in wanted:
            label = str(fm.get("task_invocation") or "").strip()
            if label:
                out[name] = label
    remaining = wanted - set(out)
    if remaining:
        for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
            if os.path.basename(path) == "README.md":
                continue
            parsed = read_or_warn(trace_io.read, path)
            if parsed is None:
                continue
            inst, _body = parsed
            tid = str(inst.get("id", ""))
            if tid in remaining:
                label = str(inst.get("title") or "").strip()
                if label:
                    out[tid] = label
    return out


EVIDENCE_PER_SECTION = 40


def _listed_ids(hit_occasions: list, miss_occasions: list) -> set[str]:
    return {ev.occasion_id for ev in hit_occasions[-EVIDENCE_PER_SECTION:] + miss_occasions[-EVIDENCE_PER_SECTION:]}


def _evidence_sections(hit_occasions: list, miss_occasions: list, labels: dict) -> list[str]:
    def _section(heading: str, occasions: list) -> list[str]:
        lines = [f"### {heading}"]
        if not occasions:
            lines.append("(none)")
        if len(occasions) > EVIDENCE_PER_SECTION:
            lines.append(f"({len(occasions)} occasions; the {EVIDENCE_PER_SECTION} most recent are listed. "
                         "`commontrace reliability` has the rest.)")
            occasions = occasions[-EVIDENCE_PER_SECTION:]
        for ev in occasions:
            label = labels.get(ev.occasion_id)
            lines.append(f"- `{ev.occasion_id}`" + (f": {label}" if label else ""))
        return lines
    return [*_section("Fired and helped", hit_occasions), "", *_section("Fired but did not help", miss_occasions)]


def run_suggest_revision(args: argparse.Namespace) -> int:
    """Draft a tightened activation condition for a MISCALIBRATED lesson."""
    root = paths.resolve_root(args.dest)
    path = _resolve_lesson_path(root, args.slug)
    if path is None:
        print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
        return 1
    parsed = read_or_warn(frontmatter.read, path)
    if parsed is None:
        return 1
    fm, body = parsed
    slug = str(fm.get("name", "")) or lesson_io.canonical_slug(args.slug)

    evidence = evidence_io.load_evidence(root)
    scores = {s.slug: s for s in reliability.score_lessons(evidence)}
    verdict_row = scores.get(slug)
    if verdict_row is None or verdict_row.verdict != reliability.VERDICT_MISCALIBRATED:
        current = verdict_row.verdict if verdict_row else "no evidence yet"
        print(
            f"[commontrace] '{slug}' is not MISCALIBRATED (currently: {current}) -- "
            "refusing to draft a revision.",
            file=sys.stderr,
        )
        if verdict_row is not None and verdict_row.verdict == reliability.VERDICT_HARMFUL:
            print(
                "  A HARMFUL verdict means the rule itself may be wrong, not just its\n"
                "  activation condition -- tightening WHEN it fires would not fix that.\n"
                "  Consider `commontrace lesson reject` or rewriting it by hand.",
                file=sys.stderr,
            )
        else:
            print("  Run `commontrace reliability` for the current verdict.", file=sys.stderr)
        return 1

    hit_occasions = [ev for ev in evidence if slug in ev.retrieved and slug in ev.hit]
    miss_occasions = [ev for ev in evidence if slug in ev.retrieved and slug not in ev.hit]
    labels = _occasion_labels(
        root, _listed_ids(hit_occasions, miss_occasions),
    )

    evidence_lines = [
        f"Reliability verdict at the time this draft was written: MISCALIBRATED "
        f"-- {verdict_row.rationale}",
        "",
        *_evidence_sections(hit_occasions, miss_occasions, labels),
    ]

    llm_draft = None
    if args.draft:
        llm_draft = _llm_draft.try_draft(
            instruction=(
                "This lesson fires more broadly than it should (MISCALIBRATED): it "
                "helps on some occasions and not on others below. Tighten "
                "applies_when/do_not_apply_when so it fires only where it actually "
                "helps. Do not change the rule itself -- echo the current rule text "
                "back unchanged in your 'rule' field."
            ),
            slug=slug, current_rule_text=body, applies_when=str(fm.get("applies_when", "")),
            do_not_apply_when=str(fm.get("do_not_apply_when", "")), evidence_lines=evidence_lines,
            allowed_evidence_ids=_listed_ids(hit_occasions, miss_occasions),
        )

    draft_slug = f"{lesson_io.canonical_slug(slug)}-revision"
    out_path = os.path.join(paths.lessons_dir(root), f"lesson_{draft_slug}.md")

    with frontmatter.locked(out_path):
        if os.path.exists(out_path):
            existing = read_or_warn(frontmatter.read, out_path)
            if existing is not None and existing[0].get("status") == "review":
                print(
                    f"[commontrace] a draft already exists for '{slug}' "
                    f"({draft_slug}, status=review) -- approve or reject it "
                    "before drafting another.",
                    file=sys.stderr,
                )
                return 1
        draft_fm = templates.lesson_frontmatter(
            slug=draft_slug,
            description=f"Revision draft: tighten the activation condition for {slug}",
            agent_type=str(fm.get("agent_type") or paths.store_agent_type(root)),
            domain=str(fm.get("domain") or ""),
            tags=list(fm.get("tags") or []),
            applies_when=(
                llm_draft.applies_when if llm_draft else f"TODO: tighten -- was: {fm.get('applies_when', '')}"
            ),
            do_not_apply_when=(
                llm_draft.do_not_apply_when if llm_draft
                else f"TODO: tighten -- was: {fm.get('do_not_apply_when', '')}"
            ),
            importance=int(fm.get("importance") or 3),
            importance_rationale=(
                f"Drafted from {slug}'s own MISCALIBRATED evidence "
                f"({verdict_row.n_hit}/{verdict_row.n_retrieved})."
            ),
            source_traces=[],
            status="review",
            scopes=list(fm.get("scopes") or []),
            valid_from=str(fm.get("valid_from") or ""),
            valid_until=str(fm.get("valid_until") or ""),
        )
        draft_fm["revises"] = slug
        if llm_draft is not None:
            draft_fm["llm_draft"] = dict(
                llm_draft.provenance,
                cited_evidence=llm_draft.evidence,
                unverifiable_evidence=llm_draft.unverifiable_evidence,
            )
        draft_body = body.rstrip("\n") + "\n\n## Evidence for revision\n" + "\n".join(evidence_lines) + "\n"
        lesson_io.write_lesson(
            out_path, draft_fm, draft_body, root=root, actor=_actor(),
            reason=f"suggest-revision draft of {slug}" + (" (LLM-assisted)" if llm_draft else ""),
        )
    print(f"[commontrace] drafted {out_path}" + (" (LLM-assisted)" if llm_draft else ""))
    print(
        f"  Nothing changed for '{slug}' yet -- this is a new, separate draft.\n"
        "  Read the evidence, rewrite applies_when/do_not_apply_when, then:\n"
        f"    commontrace lesson approve {draft_slug}\n"
        f"    commontrace lesson reject {slug} --reason \"superseded by {draft_slug}\""
        "   # once you're satisfied"
    )
    return 0


def run_suggest_rewrite(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    path = _resolve_lesson_path(root, args.slug)
    if path is None:
        print(f"[commontrace] no lesson found for slug '{args.slug}'.", file=sys.stderr)
        return 1
    parsed = read_or_warn(frontmatter.read, path)
    if parsed is None:
        return 1
    fm, body = parsed
    slug = str(fm.get("name", "")) or lesson_io.canonical_slug(args.slug)

    evidence = evidence_io.load_evidence(root)
    scores = {s.slug: s for s in reliability.score_lessons(evidence)}
    verdict_row = scores.get(slug)
    if verdict_row is None or verdict_row.verdict != reliability.VERDICT_HARMFUL:
        current = verdict_row.verdict if verdict_row else "no evidence yet"
        print(
            f"[commontrace] '{slug}' is not HARMFUL (currently: {current}) -- "
            "refusing to draft a rewrite.",
            file=sys.stderr,
        )
        if verdict_row is not None and verdict_row.verdict == reliability.VERDICT_MISCALIBRATED:
            print("  Use `commontrace lesson suggest-revision` instead.", file=sys.stderr)
        else:
            print("  Run `commontrace reliability` for the current verdict.", file=sys.stderr)
        return 1

    hit_occasions = [ev for ev in evidence if slug in ev.retrieved and slug in ev.hit]
    miss_occasions = [ev for ev in evidence if slug in ev.retrieved and slug not in ev.hit]
    labels = _occasion_labels(root, _listed_ids(hit_occasions, miss_occasions))
    evidence_lines = [
        f"Reliability verdict at the time this draft was written: HARMFUL "
        f"-- {verdict_row.rationale}",
        "",
        *_evidence_sections(hit_occasions, miss_occasions, labels),
    ]

    llm_draft = _llm_draft.try_draft(
        instruction=(
            "This lesson is judged HARMFUL: its rule itself may be wrong, not just "
            "over-broad. Propose a corrected rule, and the activation condition "
            "under which the CORRECTED rule should fire."
        ),
        slug=slug, current_rule_text=body, applies_when=str(fm.get("applies_when", "")),
        do_not_apply_when=str(fm.get("do_not_apply_when", "")), evidence_lines=evidence_lines,
        allowed_evidence_ids=_listed_ids(hit_occasions, miss_occasions),
    )

    draft_slug = f"{lesson_io.canonical_slug(slug)}-rewrite"
    out_path = os.path.join(paths.lessons_dir(root), f"lesson_{draft_slug}.md")

    with frontmatter.locked(out_path):
        if os.path.exists(out_path):
            existing = read_or_warn(frontmatter.read, out_path)
            if existing is not None and existing[0].get("status") == "review":
                print(
                    f"[commontrace] a draft already exists for '{slug}' "
                    f"({draft_slug}, status=review) -- approve or reject it "
                    "before drafting another.",
                    file=sys.stderr,
                )
                return 1
        draft_fm = templates.lesson_frontmatter(
            slug=draft_slug,
            description=f"Rewrite draft: {slug} was judged HARMFUL",
            agent_type=str(fm.get("agent_type") or paths.store_agent_type(root)),
            domain=str(fm.get("domain") or ""),
            tags=list(fm.get("tags") or []),
            applies_when=(
                llm_draft.applies_when if llm_draft
                else f"TODO: rewrite -- was: {fm.get('applies_when', '')}"
            ),
            do_not_apply_when=(
                llm_draft.do_not_apply_when if llm_draft
                else f"TODO: rewrite -- was: {fm.get('do_not_apply_when', '')}"
            ),
            importance=int(fm.get("importance") or 3),
            importance_rationale=(
                f"Drafted from {slug}'s own HARMFUL evidence "
                f"({verdict_row.n_hit}/{verdict_row.n_retrieved})."
            ),
            source_traces=[],
            status="review",
            scopes=list(fm.get("scopes") or []),
            valid_from=str(fm.get("valid_from") or ""),
            valid_until=str(fm.get("valid_until") or ""),
        )
        draft_fm["revises"] = slug
        if llm_draft is not None:
            draft_fm["llm_draft"] = dict(
                llm_draft.provenance,
                cited_evidence=llm_draft.evidence,
                unverifiable_evidence=llm_draft.unverifiable_evidence,
            )
        rule_text = llm_draft.rule if llm_draft else (
            f"TODO: rewrite this rule -- the original was judged HARMFUL: {verdict_row.rationale}"
        )
        draft_body = (
            f"## Rule\n{rule_text}\n\n"
            "## Why\n"
            f"The original lesson ({slug}) was judged HARMFUL. See the evidence below.\n\n"
            "## How to apply\n"
            + (llm_draft.applies_when if llm_draft else "TODO: when to invoke it, how to use it concretely.")
            + "\n\n## Counter-examples\n"
            + (llm_draft.do_not_apply_when if llm_draft else "TODO: cases where the rule does NOT apply.")
            + "\n\n## Evidence for rewrite\n" + "\n".join(evidence_lines) + "\n"
        )
        lesson_io.write_lesson(
            out_path, draft_fm, draft_body, root=root, actor=_actor(),
            reason=f"suggest-rewrite draft of {slug}" + (" (LLM-assisted)" if llm_draft else ""),
        )
    if llm_draft is None:
        print(
            f"[commontrace] drafted {out_path} with TODO placeholders -- no LLM draft "
            "was available, so the rule itself still needs to be written by hand.",
            file=sys.stderr,
        )
    else:
        print(f"[commontrace] drafted {out_path} (LLM-assisted)")
    print(
        f"  Nothing changed for '{slug}' yet -- this is a new, separate draft.\n"
        "  Read the evidence, review/rewrite the rule and activation condition, then:\n"
        f"    commontrace lesson approve {draft_slug}\n"
        f"    commontrace lesson reject {slug} --reason \"superseded by {draft_slug}\""
        "   # once you're satisfied"
    )
    return 0


def run_list(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    as_json = getattr(args, "json", False)
    rows = []
    for path in _iter_lesson_paths(root, None):
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, _ = result
        if args.agent_type and fm.get("agent_type") != args.agent_type:
            continue
        if args.status and fm.get("status") != args.status:
            continue
        scope = getattr(args, "scope", "").strip()
        lesson_scopes = {str(item).strip() for item in fm.get("scopes") or [] if str(item).strip()}
        if scope and lesson_scopes and scope not in lesson_scopes:
            continue
        if as_json:
            rows.append({
                "name": fm.get("name"), "agent_type": fm.get("agent_type"),
                "importance": fm.get("importance"), "status": fm.get("status"),
                "description": fm.get("description") or "", "domain": fm.get("domain"),
                "scopes": fm.get("scopes") or [],
                "valid_from": fm.get("valid_from") or "",
                "valid_until": fm.get("valid_until") or "",
                "path": path,
            })
            continue
        print(
            f"{cell(fm.get('name')):45s} "
            f"[{cell(fm.get('agent_type')):9s}] "
            f"imp={cell(fm.get('importance'))} "
            f"status={cell(fm.get('status')):8s} "
            f"{fm.get('description') or ''}"
        )
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, default=str))
    return 0


def run_history(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.as_of:
        try:
            fm, body = lesson_io.content_as_of(root, args.slug, args.as_of)
        except lesson_io.ContentAsOfError as exc:
            print(f"[commontrace] {exc}", file=sys.stderr)
            return 1
        print(f"# {args.slug} as of {args.as_of}")
        print()
        print(f"applies_when: {fm.get('applies_when', '')}")
        print(f"do_not_apply_when: {fm.get('do_not_apply_when', '')}")
        print(f"status: {fm.get('status', '')}")
        print()
        print(body)
        return 0

    records = lesson_io.history(root, args.slug)
    path = lesson_io.lesson_path(root, args.slug)
    now = lesson_io.current_revision(path) if path else None

    if not records:
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
