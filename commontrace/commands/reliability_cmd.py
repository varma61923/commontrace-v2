from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from commontrace import frontmatter, paths, reliability, trace_io


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


def _active_lessons(root: str) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(paths.lessons_dir(root), "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        fm, _ = frontmatter.read(path)
        out.append(fm)
    return out


def _evidence(root: str) -> list[reliability.Evidence]:
    """Collect occasions on which lessons were injected, from both shapes the
    protocol supports.

    Episodes (the code-review profile) carry retrieval + hit + verdict
    directly. Generic traces carry outcomes but not, today, which lessons
    were injected -- so they contribute outcome evidence only where a
    profile has recorded retrievals in `extensions`. That asymmetry is real
    and is surfaced to the user rather than hidden, because it determines
    whether this report can say anything at all.
    """
    ev: list[reliability.Evidence] = []

    for path in sorted(glob.glob(os.path.join(paths.episodes_dir(root), "*.md"))):
        if os.path.basename(path).startswith("_") or "template" in os.path.basename(path):
            continue
        fm, _ = frontmatter.read(path)
        retrieved = list(fm.get("lessons_retrieved_by_alpha") or [])
        if not retrieved:
            continue
        # Mapping a code-review verdict onto a binary "did the task succeed"
        # is a modeling choice, not a fact, so it is stated rather than
        # buried: CONFORM is success, ABANDON is failure, and ARBITRATION
        # (the A/B loop failed to converge and the orchestrator had to
        # decide) is counted as failure because the pipeline did not resolve
        # it on its own. That is defensible but debatable -- ARBITRATION is a
        # degraded outcome rather than an outright loss. Anything else maps
        # to None and is excluded from lift entirely rather than guessed at.
        verdict = str(fm.get("verdict", "")).upper()
        succeeded = True if verdict == "CONFORM" else (False if verdict in ("ABANDON", "ARBITRATION") else None)
        ev.append(
            reliability.Evidence(
                occasion_id=str(fm.get("name", os.path.basename(path))),
                retrieved=retrieved,
                hit=list(fm.get("lessons_hit") or []),
                succeeded=succeeded,
            )
        )

    for path in sorted(glob.glob(os.path.join(paths.traces_dir(root), "*.md"))):
        if os.path.basename(path) == "README.md":
            continue
        inst, _ = trace_io.read(path)
        ext = inst.get("extensions") or {}
        retrieved = list(ext.get("lessons_retrieved") or [])
        if not retrieved:
            continue
        outcome = inst.get("outcome") or {}
        succeeded = outcome.get("resolved")
        if outcome.get("repeated_error") is True:
            succeeded = False
        ev.append(
            reliability.Evidence(
                occasion_id=str(inst.get("id", ""))[:12],
                retrieved=retrieved,
                hit=list(ext.get("lessons_hit") or []),
                succeeded=succeeded if isinstance(succeeded, bool) else None,
            )
        )

    return ev


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    lessons = _active_lessons(root)
    evidence = _evidence(root)

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
