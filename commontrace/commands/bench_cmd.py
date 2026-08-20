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
    p.add_argument("--n", type=int, default=0)
    p.add_argument("--html", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--save", action="store_true")
    p.add_argument("--dest", default=None)
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
    return run_script(
        root,
        "benchmark/measure_performance.py",
        extra,
        "measure_performance.py ships inside the commontrace package, so this usually "
        "means a damaged install -- try `pip install --force-reinstall commontrace`. "
        "It needs nothing beyond PyYAML.",
    )
