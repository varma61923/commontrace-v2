from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from commontrace import consolidate, evidence_io, paths, redundancy, reliability


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "consolidate",
        help="Propose fusions, archive candidates, and contradictions in the active "
        "corpus. Reports only -- never modifies a lesson.",
    )
    p.add_argument(
        "--redundancy-threshold", type=float, default=redundancy.DEFAULT_THRESHOLD,
        help="Similarity (commontrace/redundancy.py) at or above which two active "
             "lessons are proposed as a fusion candidate. Same default this store's "
             "injection budget uses (`commontrace retrieval --redundancy-threshold`).",
    )
    p.add_argument(
        "--activation-overlap", type=float, default=reliability.DEFAULT_ACTIVATION_OVERLAP,
        help="Same flag `commontrace reliability` exposes -- how similar two "
             "activation conditions must be before a contradiction is possible.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if any fusion, archive, or high-severity contradiction "
        "candidate exists. For a periodic corpus-hygiene check in CI.",
    )
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    lessons = evidence_io.load_active_lessons(root)

    if not lessons:
        print(
            "[commontrace] no active lessons to consolidate.\n"
            "  `commontrace lesson list --status active` shows what this store has.",
        )
        return 0

    report = consolidate.build_report(
        lessons,
        redundancy_threshold=args.redundancy_threshold,
        activation_overlap=args.activation_overlap,
    )

    if args.json:
        print(json.dumps({
            "n_active": report.n_active,
            "fuse": [dataclasses.asdict(p) for p in report.fuse],
            "contradict": [dataclasses.asdict(c) for c in report.contradict],
            "archive": list(report.archive),
        }, indent=2))
    else:
        print(consolidate.render(report))

    if args.strict and not report.is_clean:
        high = report.high_severity_contradictions
        print(
            f"\n[commontrace] --strict: {len(report.fuse)} fusion candidate(s), "
            f"{len(high)} high-severity contradiction(s), "
            f"{len(report.archive)} never-retrieved lesson(s).",
            file=sys.stderr,
        )
        return 1
    return 0
