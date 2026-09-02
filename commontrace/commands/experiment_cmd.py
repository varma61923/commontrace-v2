from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from commontrace import experiment, frontmatter, holdout_io, integrity, paths, trace_io
from commontrace.commands._format import read_or_warn

# Re-exported from commontrace.holdout_io, which owns the one definition
# now that the MCP retriever writes this log too.
holdout_log_path = holdout_io.holdout_log_path




def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "experiment",
        help="Measure whether injecting lessons CAUSES better outcomes, via randomized holdout.",
    )
    p.add_argument("--min-arm", type=int, default=experiment.DEFAULT_MIN_ARM)
    p.add_argument(
        "--detect", type=float, default=experiment.DEFAULT_PRACTICAL_EFFECT,
        help="The smallest effect worth calling a result (default 0.10). A null from a "
             "design that could not have detected this is reported as UNDERPOWERED, not "
             "as 'no measurable effect' -- clearing --min-arm is a floor on running the "
             "test, not evidence the test could see anything.",
    )
    p.add_argument(
        "--configure", action="store_true",
        help="Set this store's holdout rate, so EVERY retriever uses it -- `commontrace "
             "query` and the MCP `retrieve` tool alike. Changing the rate starts a fresh "
             "randomization (see below); it does not reinterpret the assignments already "
             "made.",
    )
    p.add_argument(
        "--rate", type=float, default=None,
        help="With --configure: the fraction of eligible lessons to withhold. 0 stops the "
             "experiment. Use `--plan` first to find out what rate your occasion budget "
             "can actually answer with.",
    )
    p.add_argument(
        "--note", default="",
        help="With --configure: why this experiment was started, recorded alongside it.",
    )
    p.add_argument(
        "--plan", action="store_true",
        help="Design the experiment instead of analysing one: how many occasions and what "
             "holdout rate are needed to detect --detect. Run this BEFORE the pilot.",
    )
    p.add_argument(
        "--occasions", type=int, default=None,
        help="With --plan: how many occasions you expect in the window. Answers 'what "
             "rate do I need', which is the question a pilot actually has.",
    )
    p.add_argument(
        "--baseline", type=float, default=None,
        help="With --plan: the success rate you expect without the memory. Defaults to "
             "this store's own observed rate when there is one, else 0.5 (the most "
             "pessimistic, so a plan built on no data does not understate the sample).",
    )
    p.add_argument(
        "--holdout-rate", type=float, default=experiment.DEFAULT_HOLDOUT_RATE,
        help="With --plan: the rate you intend to run at.",
    )
    p.add_argument("--alpha", type=float, default=0.05, help="False discovery rate.")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any lesson significantly HURTS outcomes.",
    )
    p.add_argument(
        "--salt", default=None,
        help="Analyse a specific randomization instead of the current one. Every run the "
             "store has ever done is in the log; the default is the one configured now, "
             "because pooling two is not a bigger experiment, it is a broken one.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _outcomes_by_occasion(root: str) -> dict[str, bool]:
    """occasion_id -> did the underlying task succeed."""
    out: dict[str, bool] = {}

    for path in sorted(glob.glob(os.path.join(paths.episodes_dir(root), "*.md"))):
        if "template" in os.path.basename(path):
            continue
        result = read_or_warn(frontmatter.read, path)
        if result is None:
            continue
        fm, _ = result
        verdict = str(fm.get("verdict", "")).upper()
        if verdict == "CONFORM":
            out[str(fm.get("name", ""))] = True
        elif verdict in ("ABANDON", "ARBITRATION"):
            out[str(fm.get("name", ""))] = False

    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        result = read_or_warn(trace_io.read, path)
        if result is None:
            continue
        inst, _ = result
        outcome = inst.get("outcome") or {}
        resolved = outcome.get("resolved")
        if outcome.get("repeated_error") is True:
            resolved = False
        if isinstance(resolved, bool):
            out[str(inst.get("id", ""))] = resolved

    return out


def _load(root: str) -> tuple[list[integrity.Assignment], float, int]:
    """Every logged assignment, joined to its outcome. Returns (rows, rate, corrupt).

    `succeeded is None` means no outcome was ever recorded for that occasion.
    Those rows are carried rather than dropped here, which is the difference
    between this and what it replaced: the estimate cannot use them, but the
    validity checks are largely ABOUT them, and a loader that filtered first
    would hand the auditor a record with the evidence already removed
    (commontrace/integrity.py).
    """
    records, corrupt = holdout_io.read_log(root)
    if not records:
        return [], experiment.DEFAULT_HOLDOUT_RATE, corrupt

    outcomes = _outcomes_by_occasion(root)
    rows = [
        integrity.Assignment(
            lesson=rec.lesson,
            occasion_id=rec.occasion_id,
            injected=rec.injected,
            rate=rec.rate,
            salt=rec.salt,
            succeeded=outcomes.get(rec.occasion_id),
            at=rec.at,
            revision=rec.revision,
        )
        for rec in records
    ]
    rate = sum(r.rate for r in rows) / len(rows)
    return rows, rate, corrupt


def _observations(rows: list[integrity.Assignment]) -> list[experiment.HoldoutObservation]:
    """The resolved, de-duplicated subset the estimate is computed on.

    Collapsing retries is `integrity.normalize`'s job, not a second copy of
    it here: the auditor and the estimate must agree on what one assignment
    is, or the attrition rate is reported against a denominator the effect
    size never used.
    """
    unique, _ = integrity.normalize(rows)
    return [
        experiment.HoldoutObservation(
            lesson_slug=r.lesson,
            occasion_id=r.occasion_id,
            injected=r.injected,
            succeeded=bool(r.succeeded),
        )
        for r in unique if r.succeeded is not None
    ]



def _revisions_under_test(rows: list[integrity.Assignment]) -> dict[str, list[str]]:
    """lesson -> the revision(s) its assignments were made against.

    One entry means the effect describes that exact text. More than one means
    the lesson was edited mid-run and the effect describes neither -- which
    `integrity.check_treatment_stability` reports as INVALIDATES.
    """
    # Chronological (the log is append-ordered), so a lesson that moved reads
    # in the direction it actually moved.
    out: dict[str, list[str]] = {}
    for r in rows:
        if r.revision and r.revision not in out.setdefault(r.lesson, []):
            out[r.lesson].append(r.revision)
    return dict(sorted(out.items()))


def _observed_baseline(root: str) -> float | None:
    """This store's own success rate, for planning against reality.

    A plan is only as good as the baseline it assumes, and the rate a fleet
    actually resolves at is sitting in its own traces. Falls back to 0.5 when
    there is nothing to read, which is the most pessimistic assumption
    (variance peaks there) and therefore the one that will not understate the
    sample a real experiment needs.
    """
    outcomes = list(_outcomes_by_occasion(root).values())
    if len(outcomes) < 20:
        return None
    return sum(1 for ok in outcomes if ok) / len(outcomes)


def _run_configure(args: argparse.Namespace, root: str) -> int:
    """Start, change or stop this store's experiment."""
    if args.rate is None:
        current = holdout_io.load_config(root)
        print(_render_config(current, root))
        return 0
    try:
        config = holdout_io.configure(
            root, rate=args.rate, detect=args.detect, note=args.note)
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    if not config.running:
        print("[commontrace] holdout stopped. Retrieval injects every matching lesson "
              "from now on, and nothing further is measured.\n"
              "  Assignments already recorded are untouched; `commontrace experiment` "
              "still reports on them.")
        return 0

    print(f"[commontrace] holdout set to {config.rate:.0%} for this store.")
    print(f"  Salt: {config.salt}")
    print()
    # The consequence people get wrong, stated at the moment they cause it.
    print("  This starts a FRESH randomization. Assignment is a hash of (lesson, "
          "occasion, salt)\n"
          "  compared against the rate, so a new rate re-randomizes every occasion --\n"
          "  the assignments before and after are two different experiments, and pooling\n"
          "  them would let one occasion sit in both arms. Rotating the salt makes that\n"
          "  explicit: `commontrace experiment` reports on this run, and names the\n"
          "  earlier one as a prior experiment rather than mixing them.")
    print()
    print("  Every retriever now uses it -- `commontrace query --experiment` and the MCP\n"
          "  `retrieve` tool alike. That share of your work runs without its memory while\n"
          "  the experiment is live; that is the price of a causal answer.")
    return 0


def _render_config(config: holdout_io.ExperimentConfig, root: str) -> str:
    if not config.running:
        return (
            "[commontrace] no holdout is configured for this store, so nothing here is "
            "causal.\n"
            "  Start one with:  commontrace experiment --configure --rate 0.5\n"
            "  Size it first with:  commontrace experiment --plan --occasions <n>"
        )
    lines = [
        f"[commontrace] holdout running at {config.rate:.0%} (salt {config.salt}).",
        f"  Target effect: {config.detect:.0%}. Started: {config.started_at[:19] or 'unknown'}.",
    ]
    if config.note:
        lines.append(f"  Note: {config.note}")
    rows, _rate, _corrupt = _load(root)
    current = [r for r in rows if r.salt == config.salt]
    lines.append(f"  {len(current)} assignment(s) under this randomization"
                 + (f", {len(rows) - len(current)} from earlier ones."
                    if len(rows) != len(current) else "."))
    return "\n".join(lines)


def _run_plan(args: argparse.Namespace, root: str) -> int:
    observed = _observed_baseline(root)
    baseline = args.baseline if args.baseline is not None else (observed or 0.5)
    if not 0 < baseline < 1 or not 0 < args.detect < 1:
        print("[commontrace] --baseline and --detect must be strictly between 0 and 1.",
              file=sys.stderr)
        return 1
    design = experiment.plan(
        effect=args.detect, baseline=baseline, rate=args.holdout_rate,
        occasions_budget=args.occasions,
    )
    if args.json:
        import dataclasses

        print(json.dumps(dataclasses.asdict(design), indent=2))
        return 0
    print(experiment.render_plan(design))
    if args.baseline is None:
        print()
        print(
            f"_Baseline {baseline:.0%} "
            + (f"from this store's own {len(_outcomes_by_occasion(root))} recorded outcome(s)._"
               if observed is not None else
               "assumed: fewer than 20 recorded outcomes here to read one from. 50% is the "
               "most pessimistic assumption, so this will not understate the sample._")
        )
    # A design that no budget can answer is a planning failure, and a script
    # running this before a pilot should be able to act on it.
    return 1 if design.verdict == "infeasible" else 0


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.configure:
        return _run_configure(args, root)
    if args.plan:
        return _run_plan(args, root)
    all_rows, rate, n_corrupt = _load(root)
    # SCOPED TO ONE RANDOMIZATION, matching what the Hub already does in SQL.
    # Assignment is a hash of (lesson, occasion, salt) against a rate, so
    # changing either re-randomizes every occasion -- and pooling assignments
    # from two of them lets a single occasion sit in opposite arms. That is
    # not a larger sample, it is a comparison of nothing against nothing, and
    # `integrity.check_assignment_drift` exists precisely to catch it.
    #
    # Before this the local report analysed every line in the log, so the
    # first time anyone changed their holdout rate the report became
    # permanently invalid -- and said so, via a finding, which is better than
    # silence but worse than not doing it.
    config = holdout_io.load_config(root)
    wanted_salt = args.salt if args.salt is not None else config.salt
    rows = [r for r in all_rows if r.salt == wanted_salt]
    other = len(all_rows) - len(rows)
    if all_rows and not rows:
        print(
            f"[commontrace] {len(all_rows)} assignment(s) recorded, but none under the "
            f"current randomization (salt {wanted_salt!r}).\n"
            "  They belong to earlier experiments. Analysing them together would pool "
            "two randomizations\n"
            "  into one comparison, so they are not mixed in. To read an earlier run: "
            "`commontrace experiment --salt <salt>`.\n"
            "  Salts present: "
            + ", ".join(sorted({r.salt for r in all_rows if r.salt})),
            file=sys.stderr,
        )
        return 0
    n_lines = len(rows)
    report = integrity.audit(rows, min_arm=args.min_arm)
    obs = _observations(rows)
    n_no_outcome = report.n_assignments - report.n_resolved
    n_duplicate = report.n_duplicates
    if n_corrupt:
        print(
            f"[commontrace] WARNING: {n_corrupt} unparseable line(s) in the holdout log.\n"
            "  Each is a lost observation from one arm, which biases the effect size\n"
            "  rather than just reducing n. Treat the numbers below as unreliable\n"
            "  until this is explained -- writes are locked, so something else\n"
            "  wrote to memory/holdout_log.jsonl.",
            file=sys.stderr,
        )

    if n_lines == 0:
        print(
            "[commontrace] no holdout assignments recorded yet.\n\n"
            "  Every lesson-value number available without this is CORRELATIONAL: a\n"
            "  lesson is retrieved because the situation matched it, so the occasions\n"
            "  where it fired differ systematically from the ones where it didn't.\n"
            "  That bias does not shrink with more data.\n\n"
            "  To start measuring cause instead, retrieve with:\n"
            "    commontrace query \"<task>\" --experiment --occasion-id <id>\n"
            "  then record the outcome for that same <id> as an episode or a trace.",
            file=sys.stderr,
        )
        return 0

    if not obs:
        print(
            f"[commontrace] {n_lines} holdout assignment(s) recorded, but none have a matching\n"
            "  outcome yet, so nothing can be measured.\n"
            "  The `--occasion-id` passed to `query` must match the episode `name` or the\n"
            "  trace `id` that records how the task turned out.",
            file=sys.stderr,
        )
        return 0

    effects = experiment.analyze(
        obs, min_arm=args.min_arm, alpha=args.alpha, detectable=args.detect)
    summary = experiment.ExperimentSummary(
        n_observations=len(obs),
        n_lessons=len({o.lesson_slug for o in obs}),
        holdout_rate=rate,
        effects=effects,
    )

    revisions = _revisions_under_test(rows)

    if args.json:
        import dataclasses

        print(json.dumps({
            **dataclasses.asdict(summary),
            "integrity": dataclasses.asdict(report),
            # Which text each effect is about. An effect attached to a slug
            # alone is attached to a mutable name, and silently stops
            # describing the lesson the moment anyone edits it.
            "revisions_under_test": revisions,
        }, indent=2, default=str))
    else:
        # Validity FIRST, effects second. A report that leads with a
        # significant number and mentions the caveat underneath is exactly how
        # a broken one gets quoted: the headline travels and the caveat does
        # not. tests/test_integrity.py holds a fleet where the lesson does
        # nothing and the estimate reads HURTS at p=0.003 -- if that page opens
        # with "HURTS", someone retires a lesson that was fine.
        print(integrity.render(report))
        if other:
            print()
            print(f"_Scoped to the current randomization (salt `{wanted_salt}`). "
                  f"{other} assignment(s) from earlier experiments are excluded — "
                  "pooling two randomizations is not a bigger sample, it lets one "
                  "occasion sit in both arms. Read an earlier run with "
                  "`--salt <salt>`._")
        print()
        print("---")
        print()
        print(experiment.render(summary, alpha=args.alpha))
        if revisions:
            print()
            print("_Revision under test — the exact lesson text each effect above is "
                  "about. A lesson edited after this ran is no longer the lesson these "
                  "numbers describe; `commontrace lesson history <slug>` shows what "
                  "changed._")
            print()
            for slug, revs in revisions.items():
                mark = "" if len(revs) == 1 else "  ← CHANGED MID-RUN, see the validity section"
                print(f"- `{slug}` @ {', '.join(revs)}{mark}")
        if n_no_outcome:
            print(
                f"\n_{n_no_outcome} assignment(s) skipped: no recorded outcome for that occasion yet._"
            )
        if n_duplicate:
            print(
                f"\n_{n_duplicate} duplicate assignment(s) collapsed: the same lesson was logged "
                "more than once for one occasion (a retry). Counting them would inflate the arms._"
            )

    if args.strict:
        # A compromised experiment fails --strict too, and it has to: the flag
        # means "stop the build if the memory is making things worse", and a
        # biased comparison cannot answer that either way. Passing it silently
        # is the worse error -- it converts "we could not tell" into "we
        # checked and it was fine", which is the claim nobody should make.
        if not report.readable:
            print(
                "\n[commontrace] --strict: the experiment's validity is COMPROMISED, so "
                "no verdict below can be trusted:\n"
                + "\n".join(f"  - {f.headline}" for f in report.blocking),
                file=sys.stderr,
            )
            return 1
        hurts = [e for e in effects if e.verdict == experiment.VERDICT_HURTS]
        if hurts:
            print(
                f"\n[commontrace] --strict: {len(hurts)} lesson(s) significantly hurt outcomes: "
                + ", ".join(e.lesson_slug for e in hurts),
                file=sys.stderr,
            )
            return 1
    return 0
