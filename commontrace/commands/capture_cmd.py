from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import sys
import uuid

from commontrace import frontmatter, paths, templates, trace_io, validate


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
    """Filesystem-safe fragment of a trace id, for the filename only.

    A uuid4's first 8 chars are already safe; an operator-supplied
    --occasion-id may contain slashes, spaces or anything else a ticket
    system emits, and that must not escape the traces directory or produce
    an unopenable name. The id INSIDE the file is untouched -- only this
    fragment is sanitized, so the experiment join still sees the real id.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", trace_id).strip("-.")
    return (safe[:16] or "trace")


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "trace"


def _find_trace_by_occasion(tdir: str, occasion_id: str) -> str | None:
    """Locate an existing trace file whose `id` is `occasion_id`, regardless
    of its current filename. A filename is `<date>_<title-slug>_<id-suffix>.md`
    -- both the date and the slug can differ between two captures under the
    SAME occasion id (a later day, a refined title), so matching on the
    computed path alone misses a file that is very much already there."""
    for path in sorted(glob.glob(os.path.join(tdir, "*.md"))):
        try:
            fm, _ = frontmatter.read(path)
        except Exception:  # noqa: BLE001 - an unrelated malformed file must not abort the scan
            continue
        if str(fm.get("id", "")) == occasion_id:
            return path
    return None


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    tdir = paths.traces_dir(root)
    os.makedirs(tdir, exist_ok=True)

    # The randomized-holdout experiment (commontrace/experiment.py) joins a
    # logged arm assignment to an outcome on the OCCASION id, and
    # experiment_cmd._outcomes_by_occasion reads that from the trace's `id`.
    # With no way to set it, capture minted a random uuid4, nothing ever
    # joined, and `commontrace experiment` reported every assignment as
    # "no recorded outcome for that occasion yet" -- so the causal
    # measurement this product's strongest claim rests on could not be run
    # end to end at all. trace.schema.json anticipates this: id is any
    # non-empty string, "Locally-only traces MAY use a temporary local id".
    occasion_id = (args.occasion_id or "").strip()
    if args.occasion_id is not None and not occasion_id:
        print("[commontrace] --occasion-id cannot be empty.", file=sys.stderr)
        return 1
    trace_id = occasion_id or str(uuid.uuid4())
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
    out_path = os.path.join(tdir, f"{date}_{slug}_{_id_suffix(trace_id)}.md")

    # One occasion is one task, so re-capturing it updates that task's
    # outcome rather than adding a second. Located by scanning for the
    # occasion id INSIDE existing trace files, not by checking whether
    # `out_path` already exists: `out_path` is derived from today's date
    # and the CURRENT call's title, so a re-capture on a later day, or
    # with a refined title, would compute a different path and never find
    # the original -- silently creating a duplicate trace under the same
    # occasion id instead of updating it, exactly the failure
    # _outcomes_by_occasion's dict-keyed-on-id lookup depends on not
    # happening.
    title, context, solution = args.title, args.context, args.solution
    if occasion_id:
        existing_path = _find_trace_by_occasion(tdir, occasion_id)
        if existing_path is not None:
            out_path = existing_path
            print(f"[commontrace] note: updating the existing trace for occasion "
                  f"{occasion_id!r}.", file=sys.stderr)
            if not args.overwrite:
                # Preserve the historical narrative by default: re-capturing
                # under the same occasion-id exists to attach an outcome once
                # a task concludes (--resolved/--escalated/etc, possibly
                # minutes or hours after the occasion was first logged), not
                # to edit history. --title/--context/--solution are still
                # required on every call, so without this the second call's
                # placeholder or abbreviated text silently replaced the
                # original trace body wholesale -- the only record of what
                # the task actually was.
                existing_instance, _existing_body = trace_io.read(existing_path)
                title = existing_instance.get("title") or title
                context = existing_instance.get("context_text") or context
                solution = existing_instance.get("solution_text") or solution

    outcome = _outcome_from_args(args)
    agent_type = args.agent_type or paths.store_agent_type(root)
    fm = templates.trace_frontmatter(
        trace_id, title, agent_type, tags, args.profile, outcome, agent_id=args.agent_id
    )
    body = templates.trace_body(context, solution)

    # Validate BEFORE writing, so the store is invalid-by-construction
    # impossible rather than invalid-until-someone-audits-it. Without this,
    # `--tokens-used -5` lands on disk, `trace validate` only flags it on a
    # later separate run, and pilot_metrics averages the negative number in
    # the meantime -- a wrong cost figure in a customer-facing report.
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
