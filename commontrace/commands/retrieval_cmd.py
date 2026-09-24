from __future__ import annotations

import argparse
import sys

from commontrace import harm, holdout_io, paths, retrieval, retrieval_io


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "retrieval",
        help="Show or set how this store ranks lessons (scorer, floor, arm fusion, budget).",
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
    p.add_argument(
        "--fusion", default=None, choices=list(retrieval_io.FUSIONS),
        help=(
            f"{retrieval_io.FUSION_NONE}: pick ONE retriever -- semantic when the "
            "attention extra is installed and the index is fresh, lexical otherwise "
            f"(default). {retrieval_io.FUSION_RRF}: run both arms and fuse them by "
            "rank (Reciprocal Rank Fusion), so a lesson either arm surfaces is "
            "retrievable. The arms fail on different queries, which is exactly when "
            "fusing beats picking -- but it also changes which lessons are ELIGIBLE, "
            "so a store mid-experiment starts a new randomization by switching."
        ),
    )
    p.add_argument(
        "--max-lessons", type=int, default=None,
        help="How many lessons may be injected at once (commontrace/dosage.py).",
    )
    p.add_argument(
        "--max-chars", type=int, default=None,
        help="Character budget across all injected lessons. `top_k` bounds the count "
             "and says nothing about the size.",
    )
    p.add_argument(
        "--redundancy-threshold", type=float, default=None,
        help="Similarity (0-1, commontrace/redundancy.py) at or above which a lesson "
             "competing for the budget is dropped for restating one already admitted, "
             "freeing its slot for the next distinct lesson. 0 (the default) disables "
             "this -- see commontrace/dosage.py for why suppression is opt-in. Like "
             "the budget, this does not change which lessons are ELIGIBLE, only how "
             "many of the eligible set are actually injected, so it does not start a "
             "new randomization.",
    )
    p.add_argument(
        "--reliability-weight", type=float, default=None,
        help="How much a lesson's measured track record (commontrace/reliability.py's "
             "RELIABLE/UNPROVEN/MISCALIBRATED/HARMFUL verdict) moves its rank among "
             "lessons that already cleared --floor. 0 (the default) disables this. Does "
             "not change which lessons are ELIGIBLE, only their order, but -- like the "
             "budget -- that can change which of the eligible set the budget actually "
             "admits, so it does not start a new randomization by itself.",
    )
    p.add_argument(
        "--recency-weight", type=float, default=None,
        help="How much a lesson's `last_hit` freshness (commontrace/recency.py) moves "
             "its rank among lessons that already cleared --floor. 0 (the default) "
             "disables this. Same non-eligibility-changing scope as --reliability-weight.",
    )
    p.add_argument(
        "--on-harm", dest="harm_policy", default=None, choices=list(harm.POLICIES),
        help=f"{harm.POLICY_INFORM}: a lesson the experiment measured making outcomes "
             "WORSE is still injected, with its verdict attached (default). "
             f"{harm.POLICY_WITHDRAW}: it is no longer injected, and is named with its "
             "evidence wherever it matched instead (commontrace/harm.py). Acts only on "
             "the anytime-valid verdict of a readable experiment, before arms are "
             "assigned, so it does not start a new randomization.",
    )
    p.add_argument("--note", default="", help="Why these settings, recorded alongside them.")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)

    setting = (
        args.floor, args.scorer, args.fusion, args.max_lessons, args.max_chars,
        args.redundancy_threshold, args.reliability_weight, args.recency_weight,
        args.harm_policy,
    )
    if all(value is None for value in setting):
        config = retrieval_io.load_config(root)
        print(f"[commontrace] retrieval settings for {root}")
        print(f"  scorer : {config.scorer}")
        print(f"  floor  : {config.floor:.2f}")
        print(f"  fusion : {config.fusion}"
              + (f"  (k={config.rrf_k})" if config.fusion == retrieval_io.FUSION_RRF
                 else ""))
        print(f"  budget : {config.max_lessons} lessons, {config.max_chars:,} chars")
        print(
            "  redundancy: "
            + ("off (0 admits every ranked lesson regardless of overlap)"
               if config.redundancy_threshold <= 0
               else f"{config.redundancy_threshold:.2f} "
                    "(a lesson this similar to one already admitted is dropped)")
        )
        print(
            "  reliability weight: "
            + ("off" if config.reliability_weight <= 0 else f"{config.reliability_weight:.2f}")
        )
        print(
            "  recency weight    : "
            + ("off" if config.recency_weight <= 0 else f"{config.recency_weight:.2f}")
        )
        print(
            "  on harm           : "
            + ("withdraw (a lesson measured HURTS is not injected)"
               if config.harm_policy == harm.POLICY_WITHDRAW
               else "inform (a lesson measured HURTS is injected, with its verdict)")
        )
        print(f"  logged as: {config.eligibility}")
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
            root, scorer=args.scorer, floor=args.floor, fusion=args.fusion,
            max_lessons=args.max_lessons, max_chars=args.max_chars,
            redundancy_threshold=args.redundancy_threshold,
            reliability_weight=args.reliability_weight,
            recency_weight=args.recency_weight,
            harm_policy=args.harm_policy,
            note=args.note,
        )
    except ValueError as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 1

    print(
        f"[commontrace] retrieval: scorer={config.scorer} floor={config.floor:.2f} "
        f"fusion={config.fusion} budget={config.max_lessons}/{config.max_chars:,} "
        f"redundancy={config.redundancy_threshold:.2f} "
        f"reliability_weight={config.reliability_weight:.2f} "
        f"recency_weight={config.recency_weight:.2f} "
        f"on_harm={config.harm_policy}"
    )

    # The consequence, stated at the moment it is caused -- the same posture
    # holdout_io.configure takes about rotating the salt.
    #
    # Gated on assignments actually EXISTING, not on holdout_io's `running`:
    # an unconfigured store's config defaults to DEFAULT_HOLDOUT_RATE, so
    # `running` is True for a store that has never run an experiment at all,
    # and warning there would teach people to ignore the warning.
    # Fusion belongs here for the same reason scorer and floor do: it decides
    # which lessons are eligible on an occasion. The budget deliberately does
    # NOT -- it changes how many of the eligible set are injected, which the
    # holdout already records per lesson, not which lessons have an arm.
    changed_eligibility = (
        config.scorer != before.scorer
        or abs(config.floor - before.floor) > 1e-9
        or config.fusion != before.fusion
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
