"""Target D: approximate vector recall at scale through the product's Postgres index.

Loads N seeded vectors into ``vector_store.PostgresVectorIndex`` (exact mode),
builds the same HNSW index the approximate mode declares, then times queries
through the product ``search`` call in both modes. Exact search is the ground
truth for recall@k.

The vectors are synthetic: unit-normalized points around seeded cluster
centres, so the numbers measure the index and the query path, not an embedding
model. Queries are perturbed copies of stored points.

    python -m benchmarks.vector_scale_bench --dsn postgresql://user:pw@localhost/db --n 1000000 --out scale.json

The database needs the pgvector extension (0.8 or later for iterative scans).
The benchmark drops its own tenant's rows when it finishes unless --keep.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time

import numpy as np

from commontrace import vector_store

TENANT, NAMESPACE, MODEL = "scale-bench", "synthetic", "gaussian-mixture"


def _vectors(rng: np.random.Generator, centres: np.ndarray, n: int, spread: float) -> np.ndarray:
    picks = rng.integers(0, len(centres), size=n)
    points = centres[picks] + rng.normal(0.0, spread, size=(n, centres.shape[1])).astype(np.float32)
    return points / np.linalg.norm(points, axis=1, keepdims=True)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


async def run(dsn: str, *, n: int, dim: int, queries: int, top_k: int, clusters: int, spread: float,
              seed: int, batch: int, ef_search: list[int], m: int, ef_construction: int, keep: bool) -> dict:
    import asyncpg

    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(clusters, dim)).astype(np.float32)
    centres /= np.linalg.norm(centres, axis=1, keepdims=True)
    exact = await vector_store.PostgresVectorIndex.open(dsn, tenant=TENANT, namespace=NAMESPACE, model=MODEL,
                                                        dimension=dim, max_size=4, command_timeout=3600)
    report: dict = {"benchmark": "vector-scale", "n": n, "dim": dim, "queries": queries, "top_k": top_k,
                    "clusters": clusters, "spread": spread, "seed": seed, "cpu_count": os.cpu_count()}
    try:
        digest = hashlib.sha256(str(dim).encode()).hexdigest()[:16]
        connection = await asyncpg.connect(dsn)
        try:
            if await connection.fetchval("SELECT to_regclass($1)", f"commontrace_vectors_hnsw_{digest}"):
                raise RuntimeError(f"an HNSW index for dimension {dim} already exists; loading would insert into "
                                   "it row by row instead of building it in bulk. Use a fresh database.")
        finally:
            await connection.close()
        started = time.perf_counter()
        sample = []
        for start in range(0, n, batch):
            count = min(batch, n - start)
            block = _vectors(rng, centres, count, spread)
            if len(sample) < queries:
                sample.extend(block[: max(0, queries - len(sample))])
            await exact.upsert(vector_store.VectorRecord(f"m{start + i}", block[i].tolist()) for i in range(count))
        report["load_seconds"] = round(time.perf_counter() - started, 1)

        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("SET maintenance_work_mem='3GB'")
            await connection.execute(f"SET max_parallel_maintenance_workers={max(0, (os.cpu_count() or 1) - 1)}")
            started = time.perf_counter()
            # The DDL PostgresVectorIndex.open(approximate=True) declares, built in bulk after loading.
            await connection.execute(
                f"CREATE INDEX IF NOT EXISTS commontrace_vectors_hnsw_{digest} ON commontrace_vectors USING hnsw "
                f"((embedding::vector({dim})) vector_cosine_ops) WITH (m={m}, ef_construction={ef_construction}) "
                f"WHERE dimension={dim}")
            report["index_build_seconds"] = round(time.perf_counter() - started, 1)
            report["index_bytes"] = await connection.fetchval(
                f"SELECT pg_relation_size('commontrace_vectors_hnsw_{digest}')")
        finally:
            await connection.close()
        noise = np.random.default_rng(seed + 1)
        probes = []
        for point in sample[:queries]:
            q = point + noise.normal(0.0, spread / 2, size=dim).astype(np.float32)
            probes.append((q / np.linalg.norm(q)).tolist())
        exact_ms, truths = [], []
        for q in probes:
            t0 = time.perf_counter()
            truths.append({h.key for h in await exact.search(q, top_k=top_k)})
            exact_ms.append((time.perf_counter() - t0) * 1000)
        report["exact_ms"] = {"p50": round(statistics.median(exact_ms), 1),
                              "p95": round(_percentile(exact_ms, 0.95), 1)}
        report["hnsw"] = {"m": m, "ef_construction": ef_construction, "iterative_scan": "strict_order"}
        report["sweep"] = []
        for ef in ef_search:
            approximate = await vector_store.PostgresVectorIndex.open(
                dsn, tenant=TENANT, namespace=NAMESPACE, model=MODEL, dimension=dim, max_size=4,
                approximate=True, command_timeout=3600, ef_search=ef)
            try:
                for q in probes[:5]:
                    await approximate.search(q, top_k=top_k)
                ann_ms, recalls = [], []
                for q, want in zip(probes, truths):
                    t0 = time.perf_counter()
                    hits = await approximate.search(q, top_k=top_k)
                    ann_ms.append((time.perf_counter() - t0) * 1000)
                    recalls.append(len(want & {h.key for h in hits}) / max(1, len(want)))
            finally:
                await approximate.close()
            report["sweep"].append({
                "ef_search": ef, "ann_ms": {"p50": round(statistics.median(ann_ms), 2),
                                            "p95": round(_percentile(ann_ms, 0.95), 2)},
                f"recall_at_{top_k}": round(statistics.fmean(recalls), 4), "recall_min": round(min(recalls), 4)})
    finally:
        if not keep:
            connection = await asyncpg.connect(dsn)
            try:
                await connection.execute("DELETE FROM commontrace_vectors WHERE tenant=$1", TENANT)
            finally:
                await connection.close()
        await exact.close()
    with open(__file__, "rb") as fh:
        report["source_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    report["note"] = ("Synthetic unit vectors around seeded cluster centres through the product Postgres index; "
                      "measures index and query-path latency and recall against exhaustive search, not an "
                      "embedding model or lexical fusion.")
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dsn", default=os.environ.get("COMMONTRACE_SCALE_DSN", ""))
    p.add_argument("--n", type=int, default=1_000_000)
    p.add_argument("--dim", type=int, default=384)
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--clusters", type=int, default=2000)
    p.add_argument("--spread", type=float, default=0.05)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--batch", type=int, default=vector_store.MAX_BATCH)
    p.add_argument("--ef-search", default="40,100,200,400,800",
                   help="comma-separated hnsw.ef_search values to measure")
    p.add_argument("--m", type=int, default=16)
    p.add_argument("--ef-construction", type=int, default=64)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    if not args.dsn:
        p.error("--dsn (or COMMONTRACE_SCALE_DSN) is required")
    report = asyncio.run(run(args.dsn, n=args.n, dim=args.dim, queries=args.queries, top_k=args.top_k,
                             clusters=args.clusters, spread=args.spread, seed=args.seed, batch=args.batch,
                             ef_search=[int(x) for x in args.ef_search.split(",")], m=args.m, ef_construction=args.ef_construction,
                             keep=args.keep))
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if report.get("sweep") else 1


if __name__ == "__main__":
    sys.exit(main())
