"""Measure the production indexed fact-hybrid API and restart embedding reuse.

Production-scale example (real local provider, no synthetic adapter):

    python -m benchmarks.hybrid_acceptance --facts 1000000 --trials 30 \
        --embedder bge-small --output /tmp/hybrid-1m.json

The timed operation is ``commontrace.search_recipes.search`` with FTS5,
``fact_vector_projection.search``, canonical admission and RRF. Index build,
component timings and restart checks are reported separately. ``--synthetic-
adapter`` is only a deterministic plumbing fixture; it cannot establish model
quality or production embedding performance. The facts-channel
``commontrace.recall.recall`` API is intentionally not measured here.

The server gate remains unsupported until the governed fact-hybrid path has a
server-backed full-text + vector projection adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

API_PATH = "commontrace.search_recipes.search"
EMBEDDER = "synthetic:fixture-v1"
LOCAL_P95_MS = 50.0


class _SyntheticProvider:
    """Known semantic axes for plumbing tests, never a quality model."""

    def __init__(self, spec):
        self.spec = spec

    @staticmethod
    def _axis(text: str) -> int:
        lowered = text.casefold()
        if any(word in lowered for word in ("feline", "lynx", "cat")):
            return 0
        if any(word in lowered for word in ("rocket", "orbit", "space")):
            return 1
        if any(word in lowered for word in ("runtime", "policy")):
            return 2
        return 3

    def embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        output = []
        for text in texts:
            vector = [0.0, 0.0, 0.0, 0.0]
            vector[self._axis(text)] = 1.0
            output.append(vector)
        return output


def _register_synthetic() -> None:
    from commontrace import embeddings

    if "synthetic" not in embeddings.known()["custom"]:
        embeddings.register("synthetic", _SyntheticProvider)


def _clock_ms(call) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    value = call()
    return value, (time.perf_counter_ns() - started) / 1_000_000


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one sample")
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def _fixture_rows(count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"statement": "A lynx rests beside the window after dusk.", "scopes": ["agent:a"],
         "source_trace_id": "hybrid-fixture-lynx"},
        {"statement": "Runtime policy requires a bounded restart window.", "scopes": ["agent:a"],
         "source_trace_id": "hybrid-fixture-runtime"},
        {"statement": "A lynx rests in a private workspace.", "scopes": ["agent:b"],
         "source_trace_id": "hybrid-fixture-private"},
    ]
    for index in range(max(0, count - len(rows))):
        rows.append({"statement": f"Unique archival record {index:09d} stores neutral audit detail {index % 31}.",
                     "source_trace_id": f"hybrid-fixture-{index:09d}"})
    return rows


def _seed(root: str, count: int) -> dict[str, str]:
    from commontrace import fact_store, hierarchical

    fact_store.migrate(root)
    identities: dict[str, str] = {}
    rows = _fixture_rows(count)
    for start in range(0, len(rows), 1000):
        admitted = hierarchical.add_facts(root, rows[start:start + 1000])
        if len(admitted) != len(rows[start:start + 1000]) or any(action != "ADD" for _, action in admitted):
            raise RuntimeError("fixture facts were not uniquely admitted")
        for fact, _action in admitted:
            if "lynx rests beside" in fact.statement:
                identities["dense_only"] = fact.id
            elif "Runtime policy" in fact.statement:
                identities["lexical_only"] = fact.id
            elif "private workspace" in fact.statement:
                identities["out_of_scope"] = fact.id
    return identities


def _projection_drain(root: str, embedder: str) -> dict[str, int]:
    from commontrace import fact_vector_projection

    processed = 0
    last = {"processed": 0}
    while True:
        last = fact_vector_projection.drain(root, model=embedder, limit=10_000)
        processed += int(last.get("processed", 0))
        if not last.get("processed"):
            break
    return {"processed": processed, "through": int(last.get("through", 0))}


def _hybrid_search(root: str, query: str, embedder: str, *, context: list[str]) -> tuple[list[dict], dict]:
    from commontrace import search_recipes

    explain: dict = {}
    rows = search_recipes.search(root, query, embedder=embedder, reranker="none", recipe="balanced",
                                 context=context, limit=20, explain=explain)
    return rows, explain


def _implementation_hash(source: str) -> dict[str, str]:
    names = sorted(Path(source, "commontrace").rglob("*.py"))
    names.append(Path(source, "benchmarks/hybrid_acceptance.py"))
    return {
        os.path.relpath(path, source): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in names
    }


def _restart_worker(source: str, root: str, embedder: str, sample_ids: list[str],
                    synthetic: bool) -> dict[str, Any]:
    from commontrace import fact_embeddings, fact_store, fact_vector_projection

    counts = {"document_vectors_requested": 0, "query_vectors_requested": 0}
    original_encode = fact_embeddings._encode

    def counted_encode(model, texts, *, query):
        key = "query_vectors_requested" if query else "document_vectors_requested"
        counts[key] += len(texts)
        return original_encode(model, texts, query=query)

    fact_embeddings._encode = counted_encode
    drain = fact_vector_projection.drain(root, model=embedder, limit=10_000)
    sample = []
    with fact_store.transaction(root) as facts:
        for identity in sample_ids:
            sample.append(facts[identity])
    # Exercise the on-disk content-hash vector cache in this fresh process.
    _, cached = fact_embeddings.vectors(sample, model=embedder, root=root)
    if len(cached) != len(sample):
        raise RuntimeError("restart cache did not return every sampled vector")
    rows, explain = _hybrid_search(root, "feline runtime", embedder, context=["agent:a"])
    vector_db = os.path.join(os.path.dirname(fact_store.database_path(root)),
                             "embeddings-" + "".join(c if c.isalnum() or c in ".-_" else "_" for c in
                                                       ("synthetic:fixture-v1" if synthetic else embedder))[:120]
                             + ".db")
    import sqlite3

    with sqlite3.connect(vector_db) as db:
        cache_entries = db.execute("SELECT COUNT(*) FROM vec").fetchone()[0]
    return {"projection_drain": drain, **counts, "sample_cache_vectors": len(sample),
            "content_hash_cache_entries": cache_entries, "hybrid_rows": len(rows),
            "hybrid_dense_candidates": bool(explain.get("dense_candidates")),
            "provider_kind": "synthetic_adapter" if synthetic else "configured_embedding_provider"}


def _run_restart(source: str, root: str, embedder: str, sample_ids: list[str], synthetic: bool) -> dict[str, Any]:
    output_file = os.path.join(root, "restart-report.json")
    command = [sys.executable, "-I", os.path.abspath(__file__), "--worker-restart", "--source", source,
               "--root", root, "--embedder", embedder, "--sample-ids", ",".join(sample_ids),
               "--restart-output", output_file]
    if synthetic:
        command.append("--synthetic-adapter")
    completed = subprocess.run(command, cwd=source, capture_output=True, text=True, check=True)
    with open(output_file, encoding="utf-8") as stream:
        report = json.load(stream)
    report["process_exit_code"] = completed.returncode
    report["process_stderr"] = completed.stderr.strip()
    return report


def restart_status(report: dict[str, Any], *, facts: int = 0, synthetic: bool = False) -> str:
    if report.get("document_vectors_requested") != 0:
        return "fail: document vectors were recomputed after restart"
    if report.get("sample_cache_vectors", 0) <= 0 or report.get("content_hash_cache_entries", 0) <= 0:
        return "fail: persistent content-hash cache was not exercised"
    if report.get("projection_drain", {}).get("processed") != 0:
        return "fail: durable projection watermark replayed events"
    if report.get("content_hash_cache_entries", 0) < facts:
        return "fail: content-hash cache is missing one or more corpus vectors"
    if facts == 1_000_000 and not synthetic:
        return "pass: zero document re-embedding; query embedding counted separately"
    return "pass_on_fixture: zero document re-embedding; query embedding counted separately"


def hybrid_status(*, facts: int, p95_ms: float, backend: str = "sqlite") -> str:
    if facts != 1_000_000:
        return "unverified: requires exactly 1,000,000 canonical facts"
    if backend not in ("sqlite", "lance"):
        return "unsupported: unknown local vector backend"
    return "pass" if p95_ms < LOCAL_P95_MS else "fail"


def worker_restart(source: str, root: str, embedder: str, sample_ids: list[str],
                   synthetic: bool, output: str) -> int:
    sys.path.insert(0, source)
    if synthetic:
        _register_synthetic()
    report = _restart_worker(source, root, embedder, sample_ids, synthetic)
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return 0


def measure(*, source: str, count: int, trials: int, embedder: str, synthetic: bool,
            run_restart: bool, work_dir: str | None = None) -> dict[str, Any]:
    sys.path.insert(0, source)
    if synthetic:
        _register_synthetic()
    from commontrace import fact_store, fact_vector_projection

    source_hashes = _implementation_hash(source)
    with tempfile.TemporaryDirectory(prefix="commontrace-hybrid-", dir=work_dir) as temp_root:
        root = os.path.abspath(temp_root)
        identities = _seed(root, count)
        build_started = time.perf_counter_ns()
        projection = _projection_drain(root, embedder)
        build_ms = (time.perf_counter_ns() - build_started) / 1_000_000
        if projection["processed"] != count:
            raise RuntimeError(f"projection indexed {projection['processed']} of {count} fixture facts")

        query = "feline runtime"
        warm_rows, explain = _hybrid_search(root, query, embedder, context=["agent:a"])
        row_ids = {row["id"] for row in warm_rows}
        if identities["dense_only"] not in row_ids or identities["lexical_only"] not in row_ids:
            raise RuntimeError("hybrid candidate union lost its dense-only or lexical-only fixture")
        if identities["out_of_scope"] in row_ids:
            raise RuntimeError("canonical scope filter admitted an out-of-scope dense candidate")
        signals = {row["id"]: set(row.get("signals", {})) for row in warm_rows}
        if "dense" not in signals[identities["dense_only"]] or \
                "bm25" not in signals[identities["lexical_only"]]:
            raise RuntimeError("hybrid RRF omitted expected dense or lexical provenance")

        from commontrace import search_recipes

        # Component timings are diagnostic only and excluded from the API p95.
        fts_samples, dense_samples, hybrid_samples = [], [], []
        expected_order = [row["id"] for row in warm_rows]
        expected_signals = {row["id"]: sorted(row.get("signals", {})) for row in warm_rows}
        for trial in range(trials + 1):
            _fts, fts_ms = _clock_ms(lambda: fact_store.fts_candidates(
                root, query, context=["agent:a"], limit=200))
            _dense, dense_ms = _clock_ms(lambda: fact_vector_projection.search(
                root, query, model=embedder, limit=200))
            timed_rows, hybrid_ms = _clock_ms(lambda: search_recipes.search(
                root, query, embedder=embedder, reranker="none", recipe="balanced",
                context=["agent:a"], limit=20))
            if [row["id"] for row in timed_rows] != expected_order or \
                    {row["id"]: sorted(row.get("signals", {})) for row in timed_rows} != expected_signals:
                raise RuntimeError("stable-source hybrid candidates or provenance changed between trials")
            if trial:
                fts_samples.append(fts_ms)
                dense_samples.append(dense_ms)
                hybrid_samples.append(hybrid_ms)

        restart = None
        if run_restart:
            restart = _run_restart(source, root, embedder,
                                   [identities["dense_only"], identities["lexical_only"]], synthetic)
            restart["status"] = restart_status(restart, facts=count, synthetic=synthetic)

        vector_backend = fact_vector_projection._backend(root)
        report: dict[str, Any] = {
            "scope": "full production indexed facts hybrid API; no answer generation or model-quality claim",
            "api_path": API_PATH,
            "timed_path_includes": ["FTS5 candidate retrieval", "dense projection search", "canonical admission",
                                    "RRF and recipe ordering"],
            "timed_path_excludes": ["fact seeding", "vector projection build", "cross-encoder reranking",
                                    "model answer generation"],
            "embedding_provider_kind": "synthetic_adapter" if synthetic else "configured_embedding_provider",
            "embedding_model": embedder,
            "backend": vector_backend,
            "measurement_class": "production_api_with_synthetic_adapter" if synthetic
                                  else "production_api_with_configured_embedding_provider",
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source,
                                                     text=True).strip(),
            "source_sha256": source_hashes,
            "python": sys.version,
            "platform": platform.platform(),
            "facts": count,
            "trials": trials,
            "warmup_queries": 1,
            "query": query,
            "query_context": ["agent:a"],
            "fixture_checks": {"dense_only_id_found": identities["dense_only"] in row_ids,
                               "lexical_only_id_found": identities["lexical_only"] in row_ids,
                               "out_of_scope_id_excluded": identities["out_of_scope"] not in row_ids,
                               "dense_and_bm25_signals_present": True,
                               "result_count": len(warm_rows),
                               "explain": explain},
            "index_build": {"projection_ms": build_ms, "projection": projection},
            "components_excluded_from_gate": {
                "fts5_candidates": {"p50_ms": statistics.median(fts_samples),
                                     "p95_ms": percentile(fts_samples, .95), "samples_ms": fts_samples},
                "dense_projection": {"p50_ms": statistics.median(dense_samples),
                                     "p95_ms": percentile(dense_samples, .95), "samples_ms": dense_samples},
            },
            "hybrid_api": {"p50_ms": statistics.median(hybrid_samples),
                           "p95_ms": percentile(hybrid_samples, .95), "samples_ms": hybrid_samples,
                           "gate": hybrid_status(facts=count, p95_ms=percentile(hybrid_samples, .95),
                                                  backend=vector_backend)},
            "restart": restart,
            "server_gate": {"status": "unsupported_by_current_fact_projection",
                            "reason": "fact_vector_projection supports SQLite/Lance only; no Postgres full-text + "
                                      "governed vector-hybrid adapter is wired to search_recipes.search"},
        }
        if _implementation_hash(source) != source_hashes:
            raise RuntimeError("product or benchmark source changed while the workload was running")
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, default=1000, help="canonical facts, minimum 3; 1M is a long workload")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--embedder",
                        help="CommonTrace embedding provider tag (default: bge-small; ignored by the synthetic fixture)")
    parser.add_argument("--synthetic-adapter", action="store_true",
                        help="use a deterministic plumbing fixture; reported separately from real-provider runs")
    parser.add_argument("--skip-restart", action="store_true")
    parser.add_argument("--work-dir", help="parent directory for the temporary canonical fact store")
    parser.add_argument("--output", help="JSON report path; defaults to stdout")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parent.parent), help=argparse.SUPPRESS)
    parser.add_argument("--worker-restart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", help=argparse.SUPPRESS)
    parser.add_argument("--sample-ids", default="", help=argparse.SUPPRESS)
    parser.add_argument("--restart-output", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker_restart:
        if not all((args.root, args.restart_output)):
            parser.error("restart worker requires --root and --restart-output")
        return worker_restart(os.path.realpath(args.source), args.root, args.embedder,
                              [value for value in args.sample_ids.split(",") if value],
                              args.synthetic_adapter, args.restart_output)
    if args.facts < 3 or args.trials < 1:
        parser.error("--facts must be >=3 and --trials must be positive")
    if args.synthetic_adapter and args.embedder not in (None, EMBEDDER):
        parser.error(f"--synthetic-adapter uses the fixed provider tag {EMBEDDER}")
    embedder = EMBEDDER if args.synthetic_adapter else (args.embedder or "bge-small")
    report = measure(source=os.path.realpath(args.source), count=args.facts, trials=args.trials,
                     embedder=embedder, synthetic=args.synthetic_adapter,
                     run_restart=not args.skip_restart, work_dir=args.work_dir)
    report["command"] = " ".join(sys.argv if argv is None else [sys.argv[0], *argv])
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(encoded)
    else:
        print(encoded, end="")
    measured_gate = report["hybrid_api"]["gate"]
    restart_gate = report["restart"] and report["restart"]["status"].startswith("fail:")
    if args.facts == 1_000_000 and measured_gate == "fail" or restart_gate:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
