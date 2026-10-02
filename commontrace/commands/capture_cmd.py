from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import os
import re
import sys
import uuid

from commontrace import frontmatter, memory_guard, paths, templates, trace_io, validate
from commontrace.commands import _validators
from commontrace.frontmatter import locked


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "capture",
        help="Record a Trace (raw experience) into the local store.",
    )
    p.add_argument("--title", required=True, help="Short description of what this trace solves")
    p.add_argument("--context", required=True, help="The problem context")
    p.add_argument("--solution", required=True, help="What worked")
    p.add_argument("--tags", default="", help="Comma-separated tags")
    p.add_argument(
        "--agent-type", default=None,
        help="Defaults to the agent_type this store was initialized with.",
    )
    p.add_argument(
        "--agent-id", default="",
        help="WHICH agent produced this trace, as opposed to --agent-type (what KIND it is). "
             "A fleet of 25 support agents shares one --agent-type, so only this distinguishes "
             "them -- it is what makes 'how many agents does this fleet run' answerable, and "
             "what a Hub plan's agent limit is enforced against. Optional; omitting it "
             "attributes the trace to a single 'unattributed' agent for the org.",
    )
    p.add_argument("--profile", default="", help="Optional profile name (e.g. code-review)")
    p.add_argument("--dest", default=None, help="Store root (default: auto-detect / $COMMONTRACE_ROOT)")
    p.add_argument(
        "--resolved", dest="resolved", action="store_true", default=None,
        help="Underlying task reached a successful conclusion (pilot metric: resolution rate).",
    )
    p.add_argument(
        "--not-resolved", dest="resolved", action="store_false",
        help="Underlying task did NOT reach a successful conclusion.",
    )
    p.add_argument(
        "--escalated", dest="escalated", action="store_true", default=None,
        help="Underlying task required human escalation (pilot metric: escalation rate).",
    )
    p.add_argument(
        "--not-escalated", dest="escalated", action="store_false",
        help="Underlying task did NOT require human escalation.",
    )
    p.add_argument(
        "--repeated-error", dest="repeated_error", action="store_true", default=None,
        help="This trace is a recurrence of a previously captured failure (pilot metric: repeated-error rate).",
    )
    p.add_argument(
        "--not-repeated-error", dest="repeated_error", action="store_false",
        help="This trace is NOT a recurrence of a previously captured failure.",
    )
    p.add_argument(
        "--frustration", dest="frustration", action="store_true", default=None,
        help="Explicit negative signal tied to this trace (pilot metric: frustration rate).",
    )
    p.add_argument(
        "--not-frustration", dest="frustration", action="store_false",
        help="No negative signal tied to this trace.",
    )
    p.add_argument("--tokens-used", type=int, default=None, help="Tokens consumed producing this outcome.")
    p.add_argument("--llm-calls", type=int, default=None, help="LLM calls made producing this outcome.")
    p.add_argument(
        "--baseline", action="store_true", default=False,
        help="Mark this trace as captured during the pre-CommonTrace pilot baseline window.",
    )
    p.add_argument(
        "--occasion-id", default=None,
        help="Use this as the trace id so `commontrace experiment` can join this "
             "outcome to the holdout arm logged by `query --experiment "
             "--occasion-id <same id>`. Without it the randomized-holdout "
             "pipeline cannot attribute outcomes and reports every assignment "
             "as skipped.",
    )
    p.add_argument(
        "--overwrite", action="store_true", default=False,
        help="When --occasion-id matches an existing trace, replace its title/context/"
             "solution with the values given on THIS call. Without this flag, re-capturing "
             "an existing occasion preserves the original narrative and only merges in "
             "outcome fields (--resolved, --escalated, etc.) -- the common case, since "
             "re-capturing under the same occasion-id exists to attach an outcome once a "
             "task concludes, not to edit history.",
    )
    p.set_defaults(func=run)


def _outcome_from_args(args: argparse.Namespace) -> dict:
    outcome = {}
    for field in ("resolved", "escalated", "repeated_error", "frustration", "tokens_used", "llm_calls"):
        key = "frustration_signal" if field == "frustration" else field
        value = getattr(args, field)
        if value is not None:
            outcome[key] = value
    if args.baseline:
        outcome["baseline"] = True
    return outcome


def _id_suffix(trace_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id).strip("-.")
    if not safe:
        return "trace"
    if len(safe) <= 16:
        return safe
    digest = hashlib.blake2s(trace_id.encode("utf-8"), digest_size=3).hexdigest()
    return f"{safe[:9]}-{digest}"


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "trace"


def _free_path(out_path: str, trace_id: str) -> str:
    if not os.path.exists(out_path):
        return out_path
    try:
        fm, _ = frontmatter.read(out_path)
        if str(fm.get("id", "")) == trace_id:
            return out_path
    except Exception:  # noqa: BLE001 - an unreadable file is still occupied
        pass
    stem, ext = os.path.splitext(out_path)
    for n in range(2, 1000):
        candidate = f"{stem}-{n}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return out_path


def _find_trace_by_occasion(tdir: str, occasion_id: str) -> str | None:
    suffix = _id_suffix(occasion_id)
    for path in sorted(glob.glob(os.path.join(tdir, f"*_{suffix}.md"))):
        try:
            fm, _ = frontmatter.read(path)
        except Exception:  # noqa: BLE001
            continue
        if str(fm.get("id", "")) == occasion_id:
            return path

    for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        try:
            fm, _ = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unrelated malformed file must not abort the scan
            continue
        if str(fm.get("id", "")) == occasion_id:
            return path
    return None


def run(args: argparse.Namespace) -> int:
    paths.warn_if_implicit_cwd_store(args.dest)
    if not _validators.check_text_size(
        {"title": args.title, "context": args.context, "solution": args.solution},
        what="trace",
    ):
        return 1
    title, found_title = memory_guard.redact_secrets(args.title)
    context, found_context = memory_guard.redact_secrets(args.context)
    solution, found_solution = memory_guard.redact_secrets(args.solution)
    redacted = found_title + found_context + found_solution
    if redacted:
        print(
            f"[commontrace] redacted {len(redacted)} credential(s) before storing this trace: "
            f"{', '.join(dict.fromkeys(redacted))}.",
            file=sys.stderr,
        )
    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)

    occasion_id = (args.occasion_id or "").strip()
    if args.occasion_id is not None and not occasion_id:
        print("[commontrace] --occasion-id cannot be empty.", file=sys.stderr)
        return 1
    trace_id = occasion_id or str(uuid.uuid4())
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    date = datetime.date.today().isoformat()
    slug = _slugify(title)
    out_path = os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md")

    agent_id = args.agent_id
    outcome = _outcome_from_args(args)
    created_at = None

    lock_target = os.path.join(tdir, f".occasion-{_id_suffix(trace_id)}") if occasion_id else out_path
    with locked(lock_target):
        existing_path = _find_trace_by_occasion(tdir, occasion_id) if occasion_id else None
        if existing_path is None:
            out_path = _free_path(out_path, trace_id)
        if existing_path is not None:
            out_path = existing_path
            print(f"[commontrace] note: updating the existing trace for occasion "
                  f"{occasion_id!r}.", file=sys.stderr)
            if not args.overwrite:
                existing_instance, _existing_body = trace_io.read(existing_path)
                title = existing_instance.get("title") or title
                context = existing_instance.get("context_text") or context
                solution = existing_instance.get("solution_text") or solution
                if not args.tags:
                    tags = existing_instance.get("tags") or tags
                if not agent_id:
                    agent_id = existing_instance.get("agent_id") or agent_id
                outcome = {**(existing_instance.get("outcome") or {}), **outcome}
                created_at = existing_instance.get("created_at") or None

        agent_type = args.agent_type or paths.store_agent_type(root)
        fm = templates.trace_frontmatter(
            trace_id, title, agent_type, tags, args.profile, outcome, agent_id=agent_id
        )
        if created_at:
            fm["created_at"] = created_at
        body = templates.trace_body(context, solution)

        instance = dict(fm)
        instance["context_text"] = context
        instance["solution_text"] = solution
        errors = validate.validate(instance, validate.load_schema("trace.schema.json"))
        if errors:
            print(
                "[commontrace] refusing to write an invalid trace:\n"
                + "\n".join(f"  - {e}" for e in errors),
                file=sys.stderr,
            )
            return 1

        frontmatter.write(out_path, fm, body)

    print(f"[commontrace] captured trace {trace_id} -> {out_path}", file=sys.stderr)
    print(out_path)
    return 0
