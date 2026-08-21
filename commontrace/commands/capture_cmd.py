from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import uuid

from commontrace import frontmatter, paths, templates, validate


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
        "--agent-type", choices=paths.AGENT_TYPES, default=None,
        help="Defaults to the agent_type this store was initialized with.",
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


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "trace"


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)

    trace_id = str(uuid.uuid4())
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    date = datetime.date.today().isoformat()
    slug = _slugify(args.title)
    # The trace id is in the filename unconditionally, not only as a
    # collision fallback. `if os.path.exists(): pick another name` is
    # check-then-act: two agents capturing a same-titled trace in the same
    # second both see False and both write `<date>_<slug>.md`, so one
    # silently overwrites the other -- and concurrent capture is the normal
    # case for a fleet, not an edge case. Including the id makes the name
    # unique by construction, with no window to lose.
    out_path = os.path.join(tdir, f"{date}_{slug}_{trace_id[:8]}.md")

    outcome = _outcome_from_args(args)
    agent_type = args.agent_type or paths.store_agent_type(root)
    fm = templates.trace_frontmatter(trace_id, args.title, agent_type, tags, args.profile, outcome)
    body = templates.trace_body(args.context, args.solution)

    # Validate BEFORE writing, so the store is invalid-by-construction
    # impossible rather than invalid-until-someone-audits-it. Without this,
    # `--tokens-used -5` lands on disk, `trace validate` only flags it on a
    # later separate run, and pilot_metrics averages the negative number in
    # the meantime -- a wrong cost figure in a customer-facing report.
    instance = dict(fm)
    instance["context_text"] = args.context
    instance["solution_text"] = args.solution
    errors = validate.validate(instance, validate.load_schema("trace.schema.json"))
    if errors:
        print(
            "[commontrace] refusing to write an invalid trace:\n"
            + "\n".join(f"  - {e}" for e in errors),
            file=sys.stderr,
        )
        return 1

    # frontmatter.write (NamedTemporaryFile + os.replace), not a raw
    # open("w"): a direct write leaves the file readable in a torn state for
    # as long as it takes to flush, so anything scanning memory/traces/
    # concurrently -- `lesson distill`, `bench`, the attention indexer --
    # can read a truncated or zero-byte file and fail to parse it. The
    # rename is atomic, so a reader sees either the old file or the whole
    # new one, never a partial one.
    frontmatter.write(out_path, fm, body)

    print(f"[commontrace] captured trace {trace_id} -> {out_path}", file=sys.stderr)
    print(out_path)
    return 0
