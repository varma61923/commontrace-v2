"""Reproducible P0 workload; reports actual API timings, never answer accuracy.

python -m benchmarks.next_generation_storage --sizes 1000,10000,30000,1000000 \
    --trials 20 --output docs/strategy/measurements/p0-storage.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

from commontrace import fact_store
from commontrace import hierarchical as h


def statement(index: int) -> str:
    return f"component item{index} uses durable storage for its event journal"


def items(start: int, count: int) -> list[dict]:
    return [{"statement": statement(index), "source_trace_id": f"trace-{index}"}
            for index in range(start, start + count)]


def percentile(values: list[float], quantile: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)]


def legacy_seed(root: str, size: int) -> None:
    now = "2026-01-01T00:00:00+00:00"
    facts = {}
    for index in range(size):
        text = statement(index)
        fact = h.AtomicFact(h._fact_id(text, []), text, "general", [], 0.8, 1, now, None,
                            source_traces=[f"trace-{index}"], created_at=now, updated_at=now)
        fact.revision = h._compute_revision(fact.to_dict())
        facts[fact.id] = fact
    h.save_facts(root, facts)


def measure(root: str, size: int, trials: int) -> dict:
    writes, searches = [], []
    for trial in range(trials):
        start = time.perf_counter()
        h.add_fact(root, statement(size + trial), source_trace_id=f"trial-{trial}")
        writes.append((time.perf_counter() - start) * 1000)
    for trial in range(trials):
        start = time.perf_counter()
        result = h.search_facts(root, f"item{trial}", limit=10)
        searches.append((time.perf_counter() - start) * 1000)
        if not result or result[0][0].statement != statement(trial):
            raise AssertionError("selective retrieval failed")
    batch = items(size + trials, 1000)
    start = time.perf_counter()
    result = h.add_facts(root, batch)
    elapsed = time.perf_counter() - start
    if len(result) != 1000 or any(action != "ADD" for _, action in result):
        raise AssertionError("batch did not durably admit every fact")
    return {"facts_before_trials": size, "single_write_p50_ms": percentile(writes, 0.5),
            "single_write_p95_ms": percentile(writes, 0.95), "single_write_samples_ms": writes,
            "sparse_overlap_search_p50_ms": percentile(searches, 0.5),
            "sparse_overlap_search_p95_ms": percentile(searches, 0.95),
            "sparse_overlap_search_samples_ms": searches, "batch_size": 1000,
            "batch_facts_per_second": 1000 / elapsed, "batch_elapsed_seconds": elapsed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000,30000,1000000")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--legacy-max", type=int, default=30000)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args()
    sizes = sorted(set(int(value) for value in args.sizes.split(",")))
    if not sizes or min(sizes) < 1 or args.trials < 1:
        parser.error("sizes and trials must be positive")
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    result = {
        "workload": "synthetic independent journal facts; exact selective queries; 1000-fact batches",
        "measurement": "wall-clock, real hierarchical APIs including conflict recording and entity linking",
        "limitations": ["No dense/hybrid quality measurement", "No server tier measurement",
                        "Synthetic facts do not establish answer accuracy or causal lift",
                        "Legacy one-million-fact baseline not executed", "Manifest is integrity-only, not signed"],
        "python": sys.version, "platform": platform.platform(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "command": " ".join([sys.executable, "-m", "benchmarks.next_generation_storage", *sys.argv[1:]]),
        "trials": args.trials, "legacy": [], "sqlite": [],
    }
    def persist():
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
    failure = None
    size, offset = 0, 0
    try:
        with tempfile.TemporaryDirectory(prefix="commontrace-p0-", dir=args.work_dir) as directory:
            sqlite_root = os.path.join(directory, "sqlite")
            fact_store.migrate(sqlite_root)
            cursor = 0
            for size in sizes:
                start = time.perf_counter()
                for offset in range(cursor, size, 1000):
                    h.add_facts(sqlite_root, items(offset, min(1000, size - offset)))
                    if (offset - cursor) % 100000 == 0:
                        print(f"SQLite seeding: {offset}/{size}", flush=True)
                seeded_seconds = time.perf_counter() - start
                measured = measure(sqlite_root, size, args.trials)
                measured["seed_elapsed_seconds"] = seeded_seconds
                result["sqlite"].append(measured)
                print("SQLite", size, json.dumps({key: value for key, value in measured.items() if "samples" not in key}),
                      flush=True)
                # Timing writes and the batch are part of the same growing store.
                cursor = size + args.trials + 1000
                if size <= args.legacy_max:
                    legacy_root = os.path.join(directory, f"legacy-{size}")
                    legacy_seed(legacy_root, size)
                    measured = measure(legacy_root, size, args.trials)
                    result["legacy"].append(measured)
                    print("JSONL", size, json.dumps({key: value for key, value in measured.items() if "samples" not in key}),
                          flush=True)
                persist()
    except (sqlite3.Error, OSError) as exc:
        # Temporary-directory cleanup has released failed-run storage before
        # writing the report. Incomplete runs cannot silently pass the gates.
        failure = {"type": type(exc).__name__, "message": str(exc),
                   "target_facts": size, "last_started_batch_offset": offset}
        result["failure"] = failure
        print("Storage gate incomplete:", json.dumps(failure), flush=True)
    million = next((row for row in result["sqlite"] if row["facts_before_trials"] == 1000000), None)
    result["gates"] = {
        "single_write_p95_lt_20ms_at_1m": bool(million and million["single_write_p95_ms"] < 20),
        "batch_gte_2000_facts_per_second_at_1m": bool(million and million["batch_facts_per_second"] >= 2000),
        "hybrid_search_at_1m": "unverified: sparse timings are not hybrid timings",
        "postgres_server": "not implemented in P0 local migration",
        "restart_zero_reembedding": "no embedding writes; existing vector snapshot engine unchanged",
    }
    persist()
    with open(output, "rb") as source:
        digest = hashlib.sha256(source.read()).hexdigest()
    with open(output + ".sha256", "w", encoding="utf-8") as manifest:
        manifest.write(digest + "  " + os.path.basename(output) + "\n")
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
