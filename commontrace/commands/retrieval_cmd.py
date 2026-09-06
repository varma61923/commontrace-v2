from __future__ import annotations

import argparse
import sys

from commontrace import holdout_io, paths, retrieval, retrieval_io


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "retrieval",
        help="Show or set how this store ranks lessons (scorer, relevance floor).",
    )
    p.add_argument(
        "--floor", type=float, default=None,
        help="Minimum relevance (0-1) a lesson must reach to be retrieved. Raising it "
             "trades recall for precision; under a running experiment it also decides "
             "which lessons are logged as eligible.",
    )
    p.add_argument(
        "--scorer", default=None, choices=[retrieval.SCORER_IDF, retrieval.SCORER_COUNT],
        help=f"{retrieval.SCORER_IDF}: IDF-weighted and length-normalized, comparable "
             f"across fields (default). {retrieval.SCORER_COUNT}: the historical raw "
             "word-overlap sum, kept so a store mid-experiment can stay on what its "
             "existing assignments were made under.",
    )
    p.add_argument("--note", default="", help="Why these settings, recorded alongside them.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    if args.floor is None and args.scorer is None:
        config = retrieval_io.load_config(root)
        print(f"[commontrace] retrieval settings for {root}")
        print(f"  scorer : {config.scorer}")
        print(f"  floor  : {config.floor:.2f}")
        if config.note:
            print(f"  note   : {config.note}")
        if config.pinned_for_running_experiment:
            print(
                "\n  These were inferred, not chosen: this store already has holdout\n"
                "  assignments, so retrieval stays on the scorer they were made under.\n"
                "  Changing it re-decides which lessons are eligible, which is a new\n"
                "  experiment -- see the warning printed when you do."
            )
        return 0

    before = retrieval_io.load_config(root)
    try:
        config = retrieval_io.configure(
            root, scorer=args.scorer, floor=args.floor, note=args.note,
        )
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(f"[commontrace] retrieval: scorer={config.scorer} floor={config.floor:.2f}")

    # The consequence, stated at the moment it is caused -- the same posture
    # holdout_io.configure takes about rotating the salt.
    #
    # Gated on assignments actually EXISTING, not on holdout_io's `running`:
    # an unconfigured store's config defaults to DEFAULT_HOLDOUT_RATE, so
    # `running` is True for a store that has never run an experiment at all,
    # and warning there would teach people to ignore the warning.
    changed_eligibility = (
        config.scorer != before.scorer or abs(config.floor - before.floor) > 1e-9
    )
    has_history = retrieval_io.has_recorded_assignments(root)
    if changed_eligibility and has_history and holdout_io.load_config(root).running:
        print()
        print(
            "  WARNING: an experiment is running on this store, and both of these settings\n"
            "  decide which lessons are ELIGIBLE on an occasion. Assignments made before and\n"
            "  after this change describe two different treatments, so pooling them is not a\n"
            "  bigger sample -- `commontrace experiment` will report it as compromised.\n"
            "  Start a fresh randomization before collecting more:\n"
            "    commontrace experiment --configure --rate <rate>"
        )
    return 0
