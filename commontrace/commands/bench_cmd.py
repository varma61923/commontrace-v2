from __future__ import annotations

import argparse
import sys

from commontrace import paths
from commontrace.commands._shellout import run_script


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "bench",
        help="Run the memory health benchmark (lesson_quality, implicit_retrieval, transfer_gap), "
        "or --pilot for the five business-outcome metrics.",
    )
    p.add_argument("--n", type=int, default=0, help="Number of recent episodes (default: all)")
    p.add_argument("--html", action="store_true", help="Output HTML to memory/benchmark_reports/")
    p.add_argument("--json", action="store_true", help="Raw JSON output to stdout")
    p.add_argument(
        "--save", action="store_true",
        help="Deprecated no-op: every run persists its JSON report by default now.",
    )
    p.add_argument(
        "--no-save", action="store_true",
        help="Do not save benchmark results to memory/benchmark_reports/",
    )
    p.add_argument(
        "--diff", action="store_true",
        help="Compare against last saved benchmark",
    )
    p.add_argument(
        "--history", action="store_true",
        help="Show historical benchmark trend",
    )
    p.add_argument(
        "--strict", action="store_true",
        help="Fail if any regression detected",
    )
    p.add_argument("--threshold-quality", type=float, default=None, help="lesson_quality alert threshold")
    p.add_argument("--threshold-retrieval", type=float, default=None, help="implicit_retrieval strict alert threshold")
    p.add_argument("--threshold-never-hit", type=float, default=None, help="Never-hit lesson ratio alert threshold")
    p.add_argument(
        "--threshold-unimodal", type=float, default=None,
        help="Unimodal importance-distribution alert threshold",
    )
    p.add_argument("--threshold-semantic", type=float, default=None, help="Semantic similarity threshold")
    p.add_argument("--threshold-lexical", type=float, default=None,
                   help="Warn on near-duplicate lessons at or above this lexical similarity (0-1).")
    p.add_argument("--threshold-freshness", type=float, default=None,
                   help="Warn when the fraction of recently-hit lessons falls below this (0-1).")
    p.add_argument("--threshold-composite", type=float, default=None,
                   help="Warn when the combined health score falls below this (0-1).")
    p.add_argument("--dest", default=None, help="Override store root directory")
    p.add_argument(
        "--pilot", action="store_true",
        help="Compute the five pilot business-outcome metrics from Trace.outcome data "
        "(repeated-error, resolution, escalation, frustration rate, token/LLM-call cost), "
        "baseline vs. current. See protocol/PROTOCOL.md#11-pilot-outcome-metrics.",
    )
    p.add_argument("--agent-type", default=None, help="--pilot only: filter to one agent_type")
    p.add_argument(
        "--retrieval", action="store_true",
        help="Measure retrieval quality PER FIELD against the labelled cross-field "
             "corpus (commontrace/fixtures/fields/): precision@1, recall@k, MRR, and the "
             "pollution ratio that turns retrieval imprecision into a wrong causal "
             "verdict. This is what keeps 'works for any agent type' a tested property "
             "rather than a claim -- see benchmark/STATUS.md.",
    )
    p.add_argument(
        "--max-pollution", type=float, default=None,
        help="--retrieval only: fail if any field's pollution ratio exceeds this.",
    )
    p.add_argument(
        "--max-spread", type=float, default=None,
        help="--retrieval only: fail if the worst field's pollution exceeds the best "
             "field's by more than this multiple.",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.retrieval:
        if args.pilot:
            print(
                "[commontrace] --retrieval and --pilot measure different things "
                "(retrieval quality per field vs. this fleet's business outcomes). "
                "Run them separately.",
                file=sys.stderr,
            )
            return 2
        extra = []
        if args.json:
            extra.append("--json")
        if args.max_pollution is not None:
            extra += ["--max-pollution", str(args.max_pollution)]
        if args.max_spread is not None:
            extra += ["--max-spread", str(args.max_spread)]
        return run_script(
            root,
            "benchmark/measure_retrieval.py",
            extra,
            "measure_retrieval.py ships inside the commontrace package, so this "
            "usually means a damaged install -- try `pip install --force-reinstall "
            "commontrace`. It needs nothing beyond PyYAML.",
        )
    if args.pilot:
        if args.strict:
            # pilot_metrics.py has no --strict flag and implements no
            # threshold-based pass/fail logic at all -- it only reports
            # baseline-vs-current numbers. Silently dropping --strict here
            # (never forwarding it) meant `bench --pilot --strict` in a CI
            # regression gate ALWAYS exited 0, regardless of what the pilot
            # metrics actually showed: the gate looked wired up and enforced
            # nothing. Refusing outright is safer than accepting a flag this
            # mode cannot honor.
            print(
                "[commontrace] --strict is not supported with --pilot: pilot_metrics.py "
                "reports baseline-vs-current numbers but implements no threshold-based "
                "pass/fail logic to enforce. Drop --strict, or use `bench` (without "
                "--pilot) for a --strict-gated CI run.",
                file=sys.stderr,
            )
            return 2
        extra = []
        if args.html:
            extra.append("--html")
        if args.json:
            extra.append("--json")
        if args.dest:
            extra += ["--dest", args.dest]
        if args.agent_type:
            extra += ["--agent-type", args.agent_type]
        return run_script(
            root,
            "benchmark/pilot_metrics.py",
            extra,
            "pilot_metrics.py ships inside the commontrace package, so this usually "
            "means a damaged install -- try `pip install --force-reinstall commontrace`. "
            "It needs nothing beyond PyYAML.",
        )
    extra = []
    if args.n:
        extra += [f"--n={args.n}"]
    if args.html:
        extra.append("--html")
    if args.json:
        extra.append("--json")
    if args.save:
        extra.append("--save")
    if args.no_save:
        extra.append("--no-save")
    if args.diff:
        extra.append("--diff")
    if args.history:
        extra.append("--history")
    if args.strict:
        extra.append("--strict")
    if args.threshold_quality is not None:
        extra += [f"--threshold-quality={args.threshold_quality}"]
    if args.threshold_retrieval is not None:
        extra += [f"--threshold-retrieval={args.threshold_retrieval}"]
    if args.threshold_never_hit is not None:
        extra += [f"--threshold-never-hit={args.threshold_never_hit}"]
    if args.threshold_unimodal is not None:
        extra += [f"--threshold-unimodal={args.threshold_unimodal}"]
    if args.threshold_semantic is not None:
        extra += [f"--threshold-semantic={args.threshold_semantic}"]
    # These three used to be accepted here, forwarded, and then read by
    # nothing -- a fleet could set a quality gate, watch it never fire, and
    # conclude quality was fine. The warning that stood here said
    # "implement or delete them deliberately; do not leave them quiet";
    # they are now implemented (measure_performance.py:
    # compute_lexical_duplicates / compute_freshness / compute_composite),
    # each raising a real alert and, under --strict, a real non-zero exit.
    for flag, value in (
        ("--threshold-lexical", args.threshold_lexical),
        ("--threshold-freshness", args.threshold_freshness),
        ("--threshold-composite", args.threshold_composite),
    ):
        if value is not None:
            extra += [f"{flag}={value}"]

    return run_script(
        root,
        "benchmark/measure_performance.py",
        extra,
        "measure_performance.py ships inside the commontrace package, so this usually "
        "means a damaged install -- try `pip install --force-reinstall commontrace`. "
        "It needs nothing beyond PyYAML.",
    )
