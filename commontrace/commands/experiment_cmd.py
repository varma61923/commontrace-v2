from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from commontrace import experiment, frontmatter, paths, trace_io


def holdout_log_path(root: str) -> str:
    """Append-only record of every holdout assignment the retriever made.

    Written by `commontrace query --experiment` and read here. It has to be
    persisted rather than recomputed because the analysis needs to know a
    lesson was *eligible* on an occasion -- that it matched the activation
    condition and was then either injected or deliberately withheld.
    Recomputing eligibility later would silently change it as the corpus
    changes, and comparing against occasions a lesson never matched
    reintroduces exactly the confound the holdout removes.
    """
    return os.path.join(paths.memory_dir(root), "holdout_log.jsonl")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "experiment",
        help="Measure whether injecting lessons CAUSES better outcomes, via randomized holdout.",
    )
    p.add_argument("--min-arm", type=int, default=experiment.DEFAULT_MIN_ARM)
    p.add_argument("--alpha", type=float, default=0.05, help="False discovery rate.")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any lesson significantly HURTS outcomes.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def _outcomes_by_occasion(root: str) -> dict[str, bool]:
    """occasion_id -> did the underlying task succeed."""
    out: dict[str, bool] = {}

    for path in sorted(glob.glob(os.path.join(paths.episodes_dir(root), "*.md"))):
        if "template" in os.path.basename(path):
            continue
        fm, _ = frontmatter.read(path)
        verdict = str(fm.get("verdict", "")).upper()
        if verdict == "CONFORM":
            out[str(fm.get("name", ""))] = True
        elif verdict in ("ABANDON", "ARBITRATION"):
            out[str(fm.get("name", ""))] = False

    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        inst, _ = trace_io.read(path)
        outcome = inst.get("outcome") or {}
        resolved = outcome.get("resolved")
        if outcome.get("repeated_error") is True:
            resolved = False
        if isinstance(resolved, bool):
            out[str(inst.get("id", ""))] = resolved

    return out


def _load_observations(root: str) -> tuple[list[experiment.HoldoutObservation], float, int, int, int]:
    log = holdout_log_path(root)
    if not os.path.isfile(log):
        return [], experiment.DEFAULT_HOLDOUT_RATE, 0, 0, 0

    outcomes = _outcomes_by_occasion(root)
    obs: list[experiment.HoldoutObservation] = []
    rate = experiment.DEFAULT_HOLDOUT_RATE
    n_lines = 0
    n_no_outcome = 0
    # (lesson, occasion) is the unit of assignment, and the log is append-only,
    # so a retried task writes the same pair again. Counting it twice inflates
    # the arm and deflates the p-value -- a retry storm would manufacture
    # significance out of nothing. Assignment is a deterministic hash of the
    # pair, so duplicates are always identical and keeping the first is safe.
    seen_pairs: set[tuple[str, str]] = set()
    n_duplicate = 0

    with open(log, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_lines += 1
            rate = float(rec.get("rate", rate))
            occ = str(rec.get("occasion_id", ""))
            slug = str(rec.get("lesson", ""))
            if (slug, occ) in seen_pairs:
                n_duplicate += 1
                continue
            seen_pairs.add((slug, occ))
            if occ not in outcomes:
                n_no_outcome += 1
                continue
            obs.append(
                experiment.HoldoutObservation(
                    lesson_slug=slug,
                    occasion_id=occ,
                    injected=bool(rec.get("injected", True)),
                    succeeded=outcomes[occ],
                )
            )
    return obs, rate, n_lines, n_no_outcome, n_duplicate


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    obs, rate, n_lines, n_no_outcome, n_duplicate = _load_observations(root)

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

    effects = experiment.analyze(obs, min_arm=args.min_arm, alpha=args.alpha)
    summary = experiment.ExperimentSummary(
        n_observations=len(obs),
        n_lessons=len({o.lesson_slug for o in obs}),
        holdout_rate=rate,
        effects=effects,
    )

    if args.json:
        import dataclasses

        print(json.dumps(dataclasses.asdict(summary), indent=2))
    else:
        print(experiment.render(summary, alpha=args.alpha))
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
        hurts = [e for e in effects if e.verdict == experiment.VERDICT_HURTS]
        if hurts:
            print(
                f"\n[commontrace] --strict: {len(hurts)} lesson(s) significantly hurt outcomes: "
                + ", ".join(e.lesson_slug for e in hurts),
                file=sys.stderr,
            )
            return 1
    return 0
