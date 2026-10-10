"""`commontrace ingest`: Unified multimodal ingestion command."""
from __future__ import annotations

import argparse
import json
import os
import sys

from commontrace import paths


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "ingest",
        help="Ingest a source (code, markdown, json-logs, transcript) into governed memory.",
    )
    p.add_argument("source", nargs="?", default=None, help="Path to file or directory to ingest.")
    p.add_argument("--list-docs", action="store_true", help="List lightweight document summaries in catalog.")
    p.add_argument("--get-doc", default=None, help="Retrieve full content of a document by ID or path.")
    p.add_argument("--job-status", default=None, help="Check status and lifecycle stage of an ingestion job.")
    choices = [
        "code", "markdown", "json-logs", "logs", "transcript",
        "fact-triples", "fact_triples", "triples", "multimodal",
        "pipeline", "modular", "docs", "relations",
    ]
    p.add_argument(
        "--type", dest="source_type",
        choices=choices,
        default=None,
        help="Format of the source to ingest.",
    )
    p.add_argument(
        "--format", dest="source_format",
        choices=choices,
        default=None,
        help="Format alias (same as --type).",
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
    p.add_argument("--preview", action="store_true",
                   help="Dry-run: parse/chunk with zero writes, report would-write counts.")
    p.add_argument("--space", default=None,
                   help="docs: write into this conversation space instead of atomic facts")
    p.add_argument("--contextualize", choices=("none", "heuristic", "model"), default="heuristic",
                   help="docs: prefix chunks with where they sit (model uses COMMONTRACE_LLM_*)")
    p.add_argument("--force", action="store_true", help="docs: re-read files the ledger says are unchanged")
    p.add_argument("--background", action="store_true",
                   help="docs: queue the ingestion as a durable job and return its id at once; "
                        "`commontrace jobs run --watch` processes it and survives crashes")
    p.set_defaults(func=run)



def _queue_docs(args: argparse.Namespace, root: str) -> int:
    from commontrace import jobs

    source = os.path.abspath(args.source)
    if not os.path.exists(source):
        print(f"[commontrace] ingest source does not exist: {args.source}", file=sys.stderr)
        return 2
    payload = {"source": source, "scope": args.scope, "space": args.space,
               "contextualize": args.contextualize, "force": bool(args.force)}
    job = jobs.enqueue(root, "ingest", payload, dedupe_key="ingest:" + source)
    if args.output_json:
        print(json.dumps(job.to_dict(), indent=2))
    else:
        print(f"[commontrace] queued ingest job {job.id} ({job.status}). Run `commontrace jobs run --watch` "
              f"to process it; `commontrace jobs show {job.id}` for its result.")
    return 0


def _run_docs(args: argparse.Namespace, root: str) -> int:
    from commontrace import llm
    from commontrace.ingest.pipeline import create_document_pipeline

    if getattr(args, "background", False):
        if args.preview:
            print("[commontrace] --background and --preview cannot be combined.", file=sys.stderr)
            return 2
        return _queue_docs(args, root)

    try:
        pipeline = create_document_pipeline(
            args.source, root, scope=args.scope, space=args.space, contextualize=args.contextualize,
            force=args.force, max_files=args.max_files)
    except (ValueError, llm.LLMUnavailable) as exc:
        print(f"[commontrace] {exc}", file=sys.stderr)
        return 2
    if args.preview:
        report = pipeline.preview(limit=None)
        payload = {"preview": True, **report.would_write, "warnings": report.warnings,
                   "sample": [{"source": c.source_path, "chunk": c.chunk_id, "text": c.content[:160]}
                              for c in report.chunks[:5]]}
    else:
        result = pipeline.run()
        warnings = pipeline.last_warnings
        payload = {**result.to_dict(), **pipeline.last_stats, "warnings": warnings,
                   "errors": [e for e in result.errors if e not in warnings]}
    if args.output_json:
        print(json.dumps(payload, indent=2))
    else:
        title = "ingest preview (no writes)" if args.preview else "ingest complete"
        print(f"[commontrace] {title}:")
        for key in ("files", "unchanged", "chunks", "chunks_extracted", "facts_written", "duplicates",
                    "screened", "headed", "model_calls"):
            if key in payload:
                print(f"  {key.replace('_', ' '):18s} {payload[key]}")
        if payload.get("embeddings"):
            emb = payload["embeddings"]
            print(f"  {'embeddings':18s} {emb['embedded']} embedded, {emb['cached']} cached, "
                  f"{emb['failed']} failed ({emb['embedder']})")
        for problem in payload.get("warnings", []) + payload.get("errors", []):
            print(f"    - {problem}", file=sys.stderr)
    return 1 if payload.get("errors") else 0


def run(args: argparse.Namespace) -> int:
    from commontrace.ingest import IngestionPipeline, catalog

    root = paths.resolve_root(args.dest)

    if getattr(args, "job_status", None):
        job = catalog.get_ingest_job(root, args.job_status)
        if not job:
            print(f"[commontrace] Job '{args.job_status}' not found.", file=sys.stderr)
            return 1
        if args.output_json:
            print(json.dumps(job.to_dict(), indent=2))
        else:
            print(f"[commontrace] Ingest Job: {job.id} [{job.stage.upper()}]")
            print(f"  Source:     {job.source}")
            print(f"  Message:    {job.message}")
            if job.progress:
                print(f"  Progress:   {job.progress}")
            if job.error:
                print(f"  Error:      {job.error}")
        return 0

    if getattr(args, "list_docs", False) or args.source == "list":
        docs = catalog.list_documents(root)
        if args.output_json:
            print(json.dumps(docs, indent=2))
        else:
            if not docs:
                print("[commontrace] No ingested documents found.")
            else:
                print(f"[commontrace] {len(docs)} document summary(ies):")
                for d in docs:
                    print(f"  [{d['id'][:8]}] {d['title']:28s} ({d['token_count']} toks, {d['chunk_count']} chunks)")
                    print(f"         Summary: {d['summary']}")
        return 0

    if getattr(args, "get_doc", None) or args.source == "get":
        target = args.get_doc or (args.scope if args.scope else "")
        if not target and getattr(args, "service", ""):
            target = args.service
        doc = catalog.get_document(root, target)
        if not doc:
            print(f"[commontrace] Document {target!r} not found.", file=sys.stderr)
            return 1
        if args.output_json:
            print(json.dumps(doc, indent=2))
        else:
            print(f"# {doc.get('title')} ({doc.get('id')})")
            print(f"Source: {doc.get('source_path')} ({doc.get('token_count')} tokens)")
            print("-" * 60)
            print(doc.get("content", ""))
        return 0

    if not args.source:
        print(
            "[commontrace] error: must provide source path or one of --list-docs, --get-doc, --job-status",
            file=sys.stderr,
        )
        return 2


    raw_type = args.source_type or args.source_format or "code"
    source_type = raw_type.replace("-", "_")
    if source_type == "logs":
        source_type = "json_logs"
    elif source_type == "triples":
        source_type = "fact_triples"

    if source_type == "docs":
        return _run_docs(args, root)
    if getattr(args, "background", False):
        print("[commontrace] --background is available for --type docs.", file=sys.stderr)
        return 2


    pipeline = IngestionPipeline()
    kwargs: dict = {}
    if source_type in ("code", "markdown", "multimodal", "relations"):
        kwargs["max_files"] = args.max_files
    if source_type == "json_logs":
        kwargs["service_name"] = args.service

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
        preview=bool(getattr(args, "preview", False)),
        **kwargs,
    )

    if getattr(args, "preview", False):
        if args.output_json:
            payload = result.to_dict()
            payload["preview"] = True
            print(json.dumps(payload, indent=2))
        else:
            d = result.to_dict()
            print("[commontrace] ingest preview (no writes):")
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
