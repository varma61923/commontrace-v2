"""AST code graph ingestion independent of optional LLMs."""
from __future__ import annotations

import json

from commontrace import code_graph, paths


def add_parser(subparsers):
    parser = subparsers.add_parser("codegraph",
                                  help="Parse a Python/TypeScript file into source-bound graph relationships.")
    parser.add_argument("file")
    parser.add_argument("--project", default="")
    parser.add_argument("--context", action="append", required=True)
    parser.add_argument("--dest")
    parser.set_defaults(func=run)


def run(args):
    print(json.dumps(code_graph.ingest_file(paths.resolve_root(args.dest), args.file,
                                          project=args.project, context=args.context), indent=2))
    return 0
