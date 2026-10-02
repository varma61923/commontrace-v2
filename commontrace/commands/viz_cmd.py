"""`commontrace viz`: self-contained interactive graph visualization."""
from __future__ import annotations

import argparse
import os

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
    html = graph_viz.render_html(root, as_of=args.as_of or None)
    out = args.out or os.path.join(paths.memory_dir(root), "graph.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"[commontrace] viz: wrote {out}")
    return 0
