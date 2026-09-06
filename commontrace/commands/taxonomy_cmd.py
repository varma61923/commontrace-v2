from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from commontrace import evidence_io, paths, taxonomy
from commontrace.commands._traces import load_trace_candidates
from commontrace.commands._validators import similarity_threshold as _similarity_threshold


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "taxonomy",
        help="Map the issues: group recurring failures into a clear, structured "
        "taxonomy (read-only leave-behind; see `commontrace distill` to propose "
        "lessons from the gaps it finds).",
    )
    p.add_argument("--agent-type", default=None, help="Only consider traces of this agent_type.")
    p.add_argument("--similarity-threshold", type=_similarity_threshold, default=0.3)
    p.add_argument("--min-cluster-size", type=int, default=2)
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true", help="Write HTML to memory/benchmark_reports/")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if args.json and args.html:
        print("[commontrace] --json and --html are mutually exclusive.", file=sys.stderr)
        return 2

    root = paths.resolve_root(args.dest)
    traces = load_trace_candidates(root, args.agent_type)
    if not traces:
        print("[commontrace] no traces found under memory/traces/ -- nothing to map.")
        return 0

    lessons = evidence_io.load_active_lessons(root)
    tax = taxonomy.build_taxonomy(
        traces,
        lessons,
        similarity_threshold=args.similarity_threshold,
        min_cluster_size=args.min_cluster_size,
    )

    if args.json:
        print(json.dumps(taxonomy.to_dict(tax), indent=2))
        return 0

    if args.html:
        ts_display = datetime.datetime.now().isoformat(timespec="seconds")
        out_dir = os.path.join(paths.memory_dir(root), "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts_file = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"taxonomy_{ts_file}.html")
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(taxonomy.render_html(tax, ts_display))
        print(f"HTML report written: {out_path}")
        return 0

    print(taxonomy.render_markdown(tax))
    return 0
