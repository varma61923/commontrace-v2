from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from commontrace import evidence_io, impact, paths
from commontrace.commands._traces import load_trace_instances


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "impact",
        help="Impact Dashboard: errors avoided, lessons reused, and value generated "
        "or saved (measured; see --cost-per-1k-tokens/--value-per-error-avoided to "
        "estimate a dollar figure).",
    )
    p.add_argument("--agent-type", default=None, help="Only consider traces of this agent_type.")
    p.add_argument(
        "--cost-per-1k-tokens", type=float, default=None,
        help="Your own $/1k-tokens rate, to estimate a dollar value from measured token savings. "
        "Omit to see the measured token counts with no dollar figure attached.",
    )
    p.add_argument(
        "--value-per-error-avoided", type=float, default=None,
        help="Your own $ estimate of one avoided error, to estimate a dollar value from the "
        "measured errors-avoided count. Omit to see the count with no dollar figure attached.",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true", help="Write HTML to memory/benchmark_reports/")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.json and args.html:
        print("[commontrace] --json and --html are mutually exclusive.", file=sys.stderr)
        return 2

    root = paths.resolve_root(args.dest)
    evidence = evidence_io.load_evidence(root)
    traces = load_trace_instances(root, args.agent_type)

    if not evidence and not traces:
        print(
            "[commontrace] no traces or retrieval evidence found -- nothing to measure yet.\n"
            "  Capture some traces (`commontrace capture ...`) and record retrievals\n"
            "  (extensions.lessons_retrieved, or the code-review profile's episodes)\n"
            "  before this dashboard has anything to show.",
            file=sys.stderr,
        )
        return 0

    report = impact.compute_impact(
        evidence, traces,
        cost_per_1k_tokens=args.cost_per_1k_tokens,
        value_per_error_avoided=args.value_per_error_avoided,
    )

    if args.json:
        print(json.dumps(impact.to_dict(report), indent=2))
        return 0

    if args.html:
        ts_display = datetime.datetime.now().isoformat(timespec="seconds")
        out_dir = os.path.join(paths.memory_dir(root), "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts_file = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"impact_{ts_file}.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(impact.render_html(report, ts_display))
        print(f"HTML report written: {out_path}")
        return 0

    print(impact.render_markdown(report))
    return 0
