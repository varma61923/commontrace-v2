"""`commontrace viz`: self-contained interactive graph visualization."""
from __future__ import annotations

import argparse
import os
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "viz",
        help="Render the temporal knowledge graph as a self-contained "
             "interactive HTML page (offline force-directed layout).",
    )
    p.add_argument("--as-of", default=None,
                   help="Point-in-time filter (valid_from <= as_of < valid_until).")
    p.add_argument("--out", default=None,
                   help="Output HTML file (default: <store>/graph.html).")
    p.add_argument("--dest", default=None)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from commontrace import graph_viz

    root = paths.resolve_root(args.dest)
    try:
        html = graph_viz.render_html(root, as_of=args.as_of or None)
    except ValueError as exc:
        print(f"[commontrace] viz: {exc}", file=sys.stderr)
        return 2
    out = args.out or os.path.join(paths.memory_dir(root), "graph.html")
    try:
        out = paths.safe_prepare_output_path(out)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(html)
    except (OSError, ValueError) as exc:
        print(f"[commontrace] viz: could not write {out!r}: {exc}", file=sys.stderr)
        return 1
    print(f"[commontrace] viz: wrote {out}")
    return 0
