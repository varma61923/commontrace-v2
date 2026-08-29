from __future__ import annotations

import argparse
import json
import sys

from commontrace import evidence_io, paths, reliability


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "reliability",
        help="Score lessons against outcomes (do they actually work?) and detect "
        "active lessons that contradict each other.",
    )
    p.add_argument("--min-evidence", type=int, default=reliability.DEFAULT_MIN_EVIDENCE)
    p.add_argument("--precision-floor", type=float, default=reliability.DEFAULT_PRECISION_FLOOR)
    p.add_argument(
        "--activation-overlap", type=float, default=reliability.DEFAULT_ACTIVATION_OVERLAP,
        help="How similar two activation conditions must be before a contradiction is possible.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any lesson is HARMFUL or any high-severity contradiction exists. "
        "For CI on a curated corpus.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    lessons = evidence_io.load_active_lessons(root)
    evidence = evidence_io.load_evidence(root)

    if not evidence:
        print(
            "[commontrace] no retrieval evidence found, so no lesson can be scored.\n"
            "  This report needs to know which lessons were injected into which decisions.\n"
            "  Sources it reads:\n"
            "    - memory/episodes/*.md  -> lessons_retrieved_by_alpha + lessons_hit + verdict\n"
            "    - memory/traces/*.md    -> extensions.lessons_retrieved + extensions.lessons_hit\n"
            "  Until a retriever records what it injected, lesson quality can only be\n"
            "  judged by opinion.",
            file=sys.stderr,
        )
        return 0

    scores = reliability.score_lessons(
        evidence, min_evidence=args.min_evidence, precision_floor=args.precision_floor
    )
    contradictions = reliability.find_contradictions(
        lessons, reliability=scores, activation_overlap=args.activation_overlap
    )

    if args.json:
        import dataclasses

        print(json.dumps({
            "n_occasions": len(evidence),
            "lessons": [dataclasses.asdict(s) for s in scores],
            "contradictions": [dataclasses.asdict(c) for c in contradictions],
        }, indent=2))
    else:
        print(reliability.render(scores, contradictions, args.min_evidence))

    if args.strict:
        harmful = [s for s in scores if s.verdict == reliability.VERDICT_HARMFUL]
        high = [c for c in contradictions if c.severity == "high"]
        if harmful or high:
            print(
                f"\n[commontrace] --strict: {len(harmful)} harmful lesson(s), "
                f"{len(high)} high-severity contradiction(s).",
                file=sys.stderr,
            )
            return 1
    return 0
