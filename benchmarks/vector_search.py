"""Compare actual SQLite exact retrieval; print measurements to stdout only.

Run ``python -m benchmarks.vector_search --vectors 10000 --dimension 384``.
Alternating baseline/candidate trials share one database and require identical
returned score/ranking checksums. Both legacy and pinned MVCC scans run with
random and duplicate-vector workloads. This excludes embedding inference,
network/LLM time, and cold ingestion; timings are specific to the local host.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import heapq
import json
import math
import os
import platform
import random
import statistics
import struct
import tempfile
import time
from collections.abc import Iterable
from typing import Any

from commontrace import sqlite_vector_snapshots, vector_scoring
from commontrace.vector_store import SQLiteVectorIndex, VectorHit, VectorRecord


def original_scorer(rows: Iterable[tuple[str, bytes]], query: tuple[float, ...], *,
                    dimension: int, top_k: int) -> list[tuple[str, float]]:
    """Original pre-optimization scoring/ranking, including repeated query norm."""
    def scores() -> Iterable[VectorHit]:
        for key, packed in rows:
            if len(packed) != dimension * 4:
                raise ValueError("stored vector dimension is corrupt")
            stored = struct.unpack(f"<{dimension}f", packed)
            norm = math.hypot(*stored) * math.hypot(*query)
            if not norm or not math.isfinite(norm):
                raise ValueError("stored vector is corrupt")
            raw = math.fsum(a * b for a, b in zip(stored, query)) / norm
            yield VectorHit(key, min(1.0, max(-1.0, raw)))
    return [(hit.key, hit.score) for hit in heapq.nsmallest(top_k, scores(), key=lambda hit: (-hit.score, hit.key))]


async def measure(*, vectors: int, dimension: int, top_k: int, trials: int, seed: int,
                  workload: str) -> list[dict[str, Any]]:
    fd, path = tempfile.mkstemp(prefix="commontrace-vector-benchmark-", suffix=".db")
    os.close(fd)
    candidate = vector_scoring.cosine_topk
    snapshot_candidate = getattr(sqlite_vector_snapshots, "cosine_topk")
    index: SQLiteVectorIndex | None = None
    try:
        index = await SQLiteVectorIndex.open(path, tenant="benchmark", namespace="memory",
                                            model=f"seeded-float32-{dimension}", dimension=dimension)
        rng = random.Random(seed)
        query = tuple(rng.uniform(-1, 1) for _ in range(dimension))
        for start in range(0, vectors, 500):
            records = [VectorRecord(f"{i:010}", query if workload == "duplicates" else
                                    tuple(rng.uniform(-1, 1) for _ in range(dimension)))
                       for i in range(start, min(start + 500, vectors))]
            await index.upsert(records)
        outputs: list[dict[str, Any]] = []
        for engine in ("SQLite exact legacy", "SQLite exact MVCC"):
            if engine.endswith("MVCC"):
                # Stage after legacy measurements: large benchmark scans may
                # outlast a build's bounded idle lease.
                build = await index.snapshots.begin(1, "benchmark", full=True)
                assert build is not None
                rng = random.Random(seed)
                query = tuple(rng.uniform(-1, 1) for _ in range(dimension))
                for start in range(0, vectors, 500):
                    records = [VectorRecord(f"{i:010}", query if workload == "duplicates" else
                                            tuple(rng.uniform(-1, 1) for _ in range(dimension)))
                               for i in range(start, min(start + 500, vectors))]
                    await index.snapshots.stage(build, records)
                assert await index.snapshots.publish(build)
            samples: dict[str, list[float]] = {"baseline": [], "candidate": []}
            digests: dict[str, str] = {}
            functions = {"baseline": original_scorer, "candidate": candidate}
            for trial in range(trials + 1):
                order = ("baseline", "candidate") if trial % 2 == 0 else ("candidate", "baseline")
                for name in order:
                    vector_scoring.cosine_topk = functions[name]
                    setattr(sqlite_vector_snapshots, "cosine_topk", functions[name])
                    started = time.perf_counter_ns()
                    hits = await index.search(query, top_k=top_k)
                    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                    digest = hashlib.sha256(json.dumps([(hit.key, hit.score) for hit in hits]).encode()).hexdigest()
                    if name in digests and digests[name] != digest:
                        raise RuntimeError("retrieval changed across repetitions")
                    digests[name] = digest
                    if trial:  # One warm-up pair, excluded from samples.
                        samples[name].append(elapsed_ms)
            if digests["baseline"] != digests["candidate"]:
                raise RuntimeError("candidate changed exact scores or rank ordering")
            medians = {name: statistics.median(values) for name, values in samples.items()}
            outputs.append({"engine": engine, "workload": workload, "vectors": vectors, "dimension": dimension,
                            "top_k": top_k, "trials": trials, "seed": seed, "python": platform.python_version(),
                            "native_screening": vector_scoring._SUMPROD is not None,
                            "baseline_median_ms": medians["baseline"], "candidate_median_ms": medians["candidate"],
                            "speedup": medians["baseline"] / medians["candidate"],
                            "scores_and_ranking_sha256": digests["candidate"]})
        return outputs
    finally:
        vector_scoring.cosine_topk = candidate
        setattr(sqlite_vector_snapshots, "cosine_topk", snapshot_candidate)
        if index is not None:
            await index.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=int, default=10000)
    parser.add_argument("--dimension", type=int, default=384)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--seed", type=int, default=61923)
    parser.add_argument("--workloads", choices=("random", "duplicates", "both"), default="both")
    args = parser.parse_args()
    if not 1 <= args.vectors <= 1_000_000 or not 1 <= args.dimension <= 2000 \
            or not 0 <= args.top_k <= 10000 or not 1 <= args.trials <= 100:
        parser.error("vectors=1..1000000, dimension=1..2000, top-k=0..10000, trials=1..100 required")
    for workload in ("random", "duplicates") if args.workloads == "both" else (args.workloads,):
        for result in asyncio.run(measure(vectors=args.vectors, dimension=args.dimension, top_k=args.top_k,
                                          trials=args.trials, seed=args.seed, workload=workload)):
            print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
