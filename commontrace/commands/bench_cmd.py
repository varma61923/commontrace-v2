from __future__ import annotations

import argparse

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
    p.add_argument("--threshold-lexical", type=float, default=None, help="Lexical similarity threshold")
    p.add_argument("--threshold-freshness", type=float, default=None, help="Freshness threshold")
    p.add_argument("--threshold-composite", type=float, default=None, help="Composite threshold")
    p.add_argument("--dest", default=None, help="Override store root directory")
    p.add_argument(
        "--pilot", action="store_true",
        help="Compute the five pilot business-outcome metrics from Trace.outcome data "
        "(repeated-error, resolution, escalation, frustration rate, token/LLM-call cost), "
        "baseline vs. current. See protocol/PROTOCOL.md#11-pilot-outcome-metrics.",
    )
    p.add_argument("--agent-type", default=None, help="--pilot only: filter to one agent_type")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = paths.resolve_root(args.dest)
    if args.pilot:
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
    if args.threshold_lexical is not None:
        extra += [f"--threshold-lexical={args.threshold_lexical}"]
    if args.threshold_freshness is not None:
        extra += [f"--threshold-freshness={args.threshold_freshness}"]
    if args.threshold_composite is not None:
        extra += [f"--threshold-composite={args.threshold_composite}"]

    return run_script(
        root,
        "benchmark/measure_performance.py",
        extra,
        "measure_performance.py ships inside the commontrace package, so this usually "
        "means a damaged install -- try `pip install --force-reinstall commontrace`. "
        "It needs nothing beyond PyYAML.",
    )
