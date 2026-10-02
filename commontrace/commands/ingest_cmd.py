"""`commontrace ingest`: Unified multimodal ingestion command.

Ingests code repositories, Markdown documentation, JSON structured logs, and
failure transcripts into governed lessons, atomic facts, and the knowledge graph.
"""
from __future__ import annotations

import argparse
import json
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ingest",
        help="Ingest a source (code, markdown, json-logs, transcript) into governed memory.",
    )
    p.add_argument("source", help="Path to file or directory to ingest.")
    p.add_argument(
        "--type", dest="source_type",
        choices=["code", "markdown", "json-logs", "transcript"],
        required=True,
        help="Format of the source to ingest.",
    )
    p.add_argument("--scope", default="", help="Routing scope (e.g. payments, infra).")
    p.add_argument("--dest", default=None, help="CommonTrace store root (default: auto-detected).")
    p.add_argument("--service", default="", help="Service name for json-logs (optional label).")
    p.add_argument(
        "--max-files", type=int, default=200,
        help="Max files to process (code and markdown connectors).",
    )
    p.add_argument("--json", dest="output_json", action="store_true",
                   help="Output results as JSON.")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    from commontrace.ingest import IngestionPipeline

    root = args.dest or paths.store_root()
    source_type = args.source_type.replace("-", "_")  # json-logs → json_logs

    pipeline = IngestionPipeline()
    kwargs: dict = {}
    if source_type == "code":
        kwargs["max_files"] = args.max_files
    if source_type == "json_logs":
        kwargs["service_name"] = args.service
    if source_type == "markdown":
        kwargs["max_files"] = args.max_files

    print(
        f"[commontrace] ingest: {source_type} source={args.source!r} "
        f"scope={args.scope!r} dest={root!r}",
        file=sys.stderr,
    )

    result = pipeline.ingest_source(
        path=args.source,
        source_type=source_type,
        dest_root=root,
        scope=args.scope,
        **kwargs,
    )

    if args.output_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        d = result.to_dict()
        print("[commontrace] ingest complete:")
        print(f"  chunks extracted:   {d['chunks_extracted']}")
        print(f"  facts written:      {d['facts_written']}")
        print(f"  graph nodes:        {d['graph_nodes']}")
        print(f"  graph edges:        {d['graph_edges']}")
        print(f"  lessons drafted:    {d['lessons_drafted']}")
        print(f"  traces written:     {d['traces_written']}")
        if d["errors"]:
            print("  errors:")
            for e in d["errors"]:
                print(f"    - {e}", file=sys.stderr)

    return 1 if result.errors else 0
