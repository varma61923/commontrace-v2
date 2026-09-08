from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import os
import re
import sys
import uuid

from commontrace import frontmatter, paths, templates, trace_io, validate
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
    """Filesystem-safe fragment of a trace id, for the filename only.

    A uuid4's first 8 chars are already safe; an operator-supplied
    --occasion-id may contain slashes, spaces or anything else a ticket
    system emits, and that must not escape the traces directory or produce
    an unopenable name. The id INSIDE the file is untouched -- only this
    fragment is sanitized, so the experiment join still sees the real id.

    MUST BE INJECTIVE, and a plain prefix is not. This truncated to the first
    16 characters, and the filename is `<date>_<title-slug>_<suffix>.md`, so
    two occasions captured on the same day with the same title whose ids
    differ only after character 16 computed the SAME path -- and the write
    path has no existence check, because `_find_trace_by_occasion` correctly
    declines to match them (it compares the full id inside the file). So the
    second capture silently replaced the first.

    That is not an exotic input. PILOT.md tells operators to use "a ticket
    number, a run id, a job id", and real ones are prefixed:
    JIRA-ROBOTICS-PLATFORM-4711 and ...-4712 share their first 25 characters.
    Reproduced: five captures under such ids left ONE file on disk.

    Hashing the tail keeps the name readable (the prefix still shows which
    occasion it is), keeps it deterministic so the fast-path glob in
    `_find_trace_by_occasion` still finds it, and makes a collision require a
    blake2s collision rather than a shared prefix.
    """
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
    """`out_path`, or a variant of it not already holding a DIFFERENT trace.

    Returns `out_path` unchanged when nothing is there, or when what is there
    is this same trace (the caller's own re-capture, which it handles by
    merging). Only a genuine stranger at that path causes a rename.
    """
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
    """Locate an existing trace file whose `id` is `occasion_id`, regardless
    of its current filename. A filename is `<date>_<title-slug>_<id-suffix>.md`
    -- both the date and the slug can differ between two captures under the
    SAME occasion id (a later day, a refined title), so matching on the
    computed path alone misses a file that is very much already there."""
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
    agent_id = args.agent_id
    outcome = _outcome_from_args(args)
    created_at = None

    # Serializes the whole find-existing -> read -> merge -> write sequence
    # below against any OTHER process capturing under the SAME occasion id
    # at the same time -- e.g. two agents in a fleet both attaching an
    # outcome to the same ticket within moments of each other. Locked on a
    # path derived from the occasion id itself, not `out_path`: the
    # existing-file scan just below can find a DIFFERENT filename than the
    # one already computed (a prior capture on an earlier date, or with a
    # different title slug), so locking only after that scan would leave
    # the scan itself -- and the window between it and the eventual write
    # -- unprotected. Without this, two concurrent re-captures under the
    # same occasion id (one marking --resolved, one marking --escalated)
    # each read the SAME prior outcome dict, merge in their own one field,
    # and whichever write() lands last wins outright: the other's outcome
    # field is silently lost, not merged, with no error and no trace of the
    # loss. A brand-new occasion id has no file to race over yet, but
    # locking here still guarantees that if two processes race a FIRST
    # capture under the same fresh occasion id, the second to acquire the
    # lock re-scans and correctly finds (and merges into) the first one's
    # file instead of unconditionally overwriting it.
    lock_target = os.path.join(tdir, f".occasion-{_id_suffix(trace_id)}") if occasion_id else out_path
    with locked(lock_target):
        existing_path = _find_trace_by_occasion(tdir, occasion_id) if occasion_id else None
        if existing_path is None:
            # Belt and braces. `_id_suffix` is injective now, so this should
            # not fire -- but the failure it guards against destroyed data
            # silently for every occasion id sharing a 16-character prefix,
            # and a check that costs one stat() is cheap next to that. If the
            # computed path is taken by a DIFFERENT trace, step aside rather
            # than overwrite it.
            out_path = _free_path(out_path, trace_id)
        if existing_path is not None:
            out_path = existing_path
            print(f"[commontrace] note: updating the existing trace for occasion "
                  f"{occasion_id!r}.", file=sys.stderr)
            if not args.overwrite:
                # Preserve the historical narrative AND every previously
                # recorded outcome/tag/identity field by default:
                # re-capturing under the same occasion-id exists to attach
                # an outcome once a task concludes (--resolved/--escalated/
                # etc, possibly minutes or hours after the occasion was
                # first logged), not to edit history. --title/--context/
                # --solution are still required on every call, so without
                # this the second call's placeholder or abbreviated text
                # silently replaced the original trace body wholesale.
                # Likewise outcome/tags/agent_id/created_at were being
                # unconditionally overwritten with this call's (often
                # empty/default) values instead of merged -- so re-capturing
                # a trace that already had `--tokens-used 4200 --tags
                # linux,gcc` with only `--resolved` silently erased
                # tokens_used, the tags, and the original creation
                # timestamp. --overwrite exists precisely for the caller
                # who DOES want a clean reset.
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

        # Validate BEFORE writing, so the store is invalid-by-construction
        # impossible rather than invalid-until-someone-audits-it. Without
        # this, `--tokens-used -5` lands on disk, `trace validate` only
        # flags it on a later separate run, and pilot_metrics averages the
        # negative number in the meantime -- a wrong cost figure in a
        # customer-facing report.
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
        # open("w"): a direct write leaves the file readable in a torn state
        # for as long as it takes to flush, so anything scanning
        # memory/traces/ concurrently -- `lesson distill`, `bench`, the
        # attention indexer -- can read a truncated or zero-byte file and
        # fail to parse it. The rename is atomic, so a reader sees either
        # the old file or the whole new one, never a partial one.
        frontmatter.write(out_path, fm, body)

    print(f"[commontrace] captured trace {trace_id} -> {out_path}", file=sys.stderr)
    print(out_path)
    return 0
