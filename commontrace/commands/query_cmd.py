from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from commontrace import experiment, frontmatter, paths, retrieval
from commontrace.commands._format import read_or_warn
from commontrace.commands._shellout import has_attention_deps, run_script


def _positive_int(raw: str) -> int:
    """argparse type= for --top-k: a Python slice silently accepts a
    negative count (`order[:-1]` is "all but the last", not an error), so
    `--top-k -1` returned nearly the ENTIRE index instead of failing --
    the opposite of what a caller asking for "a small number of results"
    intended. Rejected here, at parse time, rather than clamped silently:
    a negative top-k is a caller bug worth surfacing, not a value with a
    sensible default to fall back to."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--top-k must be >= 1, got {value}")
    return value


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "query",
        help="Retrieve top-K relevant lessons for a task (semantic pre-filter, "
        "or a lexical fallback with only the core install).",
    )
    p.add_argument("task", help="Incoming task / query string")
    p.add_argument("--top-k", type=_positive_int, default=10)
    p.add_argument(
        "--lexical", action="store_true",
        help="Force the pure-Python lexical fallback even if the attention extra is installed.",
    )
    p.add_argument("--agent-type", default=None)
    p.add_argument(
        "--experiment", action="store_true",
        help="Randomized holdout mode: deliberately withhold a fraction of otherwise-"
        "matching lessons and log the assignment, so `commontrace experiment` can later "
        "measure whether injection actually CAUSES better outcomes. Without this, every "
        "lesson-value number is correlational and confounded by which situations trigger "
        "each lesson.",
    )
    p.add_argument(
        "--occasion-id", default=None,
        help="Identifier for this decision, used to join the holdout assignment to its "
        "outcome later. Must match the episode `name` or trace `id` you record afterwards.",
    )
    p.add_argument("--holdout-rate", type=float, default=experiment.DEFAULT_HOLDOUT_RATE)
    p.add_argument("--experiment-salt", default="default")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _iter_active_lessons(root: str, agent_type: str | None) -> list[tuple[str, dict]]:
    ldir = paths.lessons_dir(root)
    out = []
    for path in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, _ = result
        if fm.get("status") != "active":
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        out.append((path, fm))
    return out


def _apply_holdout(args: argparse.Namespace, root: str, slugs: list[str]) -> set[str]:
    """Decide which of the matching lessons to withhold, and log it.

    The log records *eligibility*: every lesson here matched the task, and
    was then either injected or deliberately withheld. That distinction is
    what makes the later comparison causal rather than confounded, so it is
    written at decision time and never reconstructed.
    """
    withheld = {
        slug for slug in slugs
        if experiment.is_held_out(slug, args.occasion_id, args.holdout_rate, args.experiment_salt)
    }

    from commontrace.commands.experiment_cmd import holdout_log_path

    path = holdout_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Locked, and flushed inside the lock. O_APPEND makes a single write()
    # atomic, but Python buffers: a fleet whose agents retrieve concurrently
    # writes more than one buffer's worth, and a flush boundary can land
    # mid-line. The corrupted line is then dropped when the log is read --
    # and a DROPPED OBSERVATION IS NOT NEUTRAL. It removes one arm's data
    # point from a randomized comparison, which biases the causal number
    # this whole experiment exists to produce. Cheap to prevent, expensive
    # and near-impossible to detect after the fact.
    with frontmatter.locked(path):
        with open(path, "a", encoding="utf-8") as fh:
            for slug in slugs:
                fh.write(json.dumps({
                    "occasion_id": args.occasion_id,
                    "lesson": slug,
                    "injected": slug not in withheld,
                    "rate": args.holdout_rate,
                    "salt": args.experiment_salt,
                }) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return withheld


def _slug_of_semantic_line(line: str) -> str | None:
    """The slug from one `memory/attention/query.py` result line, or None.

    That script emits `<slug> | cosine=<x> | importance=<n>` for hits and
    `#`-prefixed header/warning lines for everything else.
    """
    if line.startswith("#") or "|" not in line:
        return None
    slug = line.split("|", 1)[0].strip()
    return slug or None


def _slugs_from_semantic_output(stdout: str) -> list[str]:
    seen: list[str] = []
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        # A slug can legitimately appear twice (top-k hit plus importance-floor
        # override); the holdout must treat it as one eligible lesson.
        if slug is not None and slug not in seen:
            seen.append(slug)
    return seen


def _run_lexical(args: argparse.Namespace, root: str) -> int:
    lessons = _iter_active_lessons(root, args.agent_type)
    ranked = retrieval.rank_lessons(args.task, lessons, top_k=args.top_k)
    if not ranked:
        print("[commontrace] no lexical matches. Try `commontrace lesson list` for a full view.")
        return 0

    withheld: set[str] = set()
    if args.experiment:
        if not args.occasion_id:
            print(
                "[commontrace] --experiment requires --occasion-id: without it the holdout "
                "assignment cannot be joined to an outcome, so nothing could be measured.",
                file=sys.stderr,
            )
            return 1
        withheld = _apply_holdout(args, root, [r.slug for r in ranked])

    for r in ranked:
        if r.slug in withheld:
            # Printed rather than hidden so a human driving this can see the
            # experiment is running. An automated retriever should skip these.
            print(f"{r.slug:45s} [WITHHELD - holdout]")
            continue
        print(f"{r.slug:45s} score={r.score:5.1f}  {r.description}")
        print(f"  matched: {', '.join(r.matched_terms)}  ({r.path})")

    if args.experiment:
        print(
            f"\n[commontrace] experiment: {len(ranked) - len(withheld)} injected, "
            f"{len(withheld)} withheld at {args.holdout_rate:.0%} for occasion "
            f"{args.occasion_id!r}. Record the outcome under that id, then run "
            "`commontrace experiment`."
        )
    return 0


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    if args.lexical or not has_attention_deps():
        if not args.lexical:
            print(
                "[commontrace] Semantic retrieval requires the optional attention extra "
                "(numpy + sentence-transformers): `pip install commontrace[attention]`. "
                "Falling back to lexical (word-overlap) retrieval.",
                file=sys.stderr,
            )
        return _run_lexical(args, root)

    if args.agent_type:
        # The semantic script has no agent_type filter. Saying so beats
        # silently returning unfiltered results that look filtered.
        print(
            "[commontrace] --agent-type is not supported by the semantic retriever and "
            "was NOT applied. Use --lexical to filter by agent type.",
            file=sys.stderr,
        )

    missing_hint = (
        "Retrieval requires the reference attention scripts from the commontrace-v2 "
        "repo checkout (memory/attention/) plus `pip install commontrace[attention]`. "
        "Falling back: `commontrace query --lexical`, or `commontrace lesson list` for a full view."
    )
    script_args = [args.task, "--top-k", str(args.top_k)]
    script_path = os.path.join("memory", "attention", "query.py")

    if not args.experiment:
        return run_script(root, script_path, script_args, missing_hint)

    if not args.occasion_id:
        print(
            "[commontrace] --experiment requires --occasion-id: without it the holdout "
            "assignment cannot be joined to an outcome, so nothing could be measured.",
            file=sys.stderr,
        )
        return 1

    # The holdout has to apply on BOTH retrieval paths. Previously only the
    # lexical branch honoured it, so a fleet with the attention extra
    # installed -- the recommended production setup -- ran `--experiment` and
    # silently measured nothing: no arms were ever logged, and `commontrace
    # experiment` reported "no holdout assignments recorded yet" forever.
    #
    # Rather than duplicate the assignment logic into the reference script,
    # capture what it ranked and apply the same _apply_holdout the lexical
    # path uses. Ranking stays in one place; arm assignment stays in one place.
    rc, stdout = run_script(root, script_path, script_args, missing_hint, capture=True)
    if rc != 0:
        sys.stdout.write(stdout)
        return rc

    slugs = _slugs_from_semantic_output(stdout)
    if not slugs:
        sys.stdout.write(stdout)
        print(
            "[commontrace] --experiment: the semantic retriever returned no lessons, "
            "so no holdout arms were recorded for this occasion.",
            file=sys.stderr,
        )
        return 0

    withheld = _apply_holdout(args, root, slugs)
    for line in stdout.splitlines():
        slug = _slug_of_semantic_line(line)
        if slug is not None and slug in withheld:
            print(f"{slug} | [WITHHELD - holdout]")
        else:
            print(line)
    print(
        f"\n[commontrace] experiment: {len(slugs) - len(withheld)} injected, "
        f"{len(withheld)} withheld at {args.holdout_rate:.0%} for occasion "
        f"{args.occasion_id!r}. Record the outcome under that id, then run "
        "`commontrace experiment`."
    )
    return 0
