"""Reproducible lexical ranking latency; run against baseline and changed checkouts.

python -m benchmarks.runtime_comparison --output /tmp/baseline.json
The corpus index is warm in both workloads. Repeated and distinct queries are
reported separately; this is not an end-to-end gateway or LLM benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from commontrace import lesson_cache, retrieval


def run(size: int, runs: int) -> dict:
    rng = random.Random(61923)
    words = [f"term{i}" for i in range(100)]
    lessons = [(f"/benchmark/{i}.md", {
        "name": f"lesson-{i}", "description": " ".join(rng.sample(words, 12)),
        "tags": rng.sample(words, 3), "importance": i % 5 + 1, "uses": i % 7,
    }) for i in range(size)]
    terms = lesson_cache.TermCache({p: lesson_cache.field_terms(fm) for p, fm in lessons})
    terms.stamps = {p: (1, i) for i, (p, _) in enumerate(lessons)}
    terms.lessons = lessons
    terms.fingerprint = tuple(terms.stamps.items())
    terms.fingerprint_hash = hash(terms.fingerprint)
    retrieval.rank_lessons("term1 term2 term3", lessons, term_cache=terms)
    workloads = {
        "repeated": ["term1 term2 term3"] * runs,
        "distinct": [" ".join(rng.sample(words, 4)) for _ in range(runs)],
    }
    result = {"documents": size}
    for name, queries in workloads.items():
        samples, rows = [], []
        for query in queries:
            start = time.perf_counter_ns()
            ranked = retrieval.rank_lessons(query, lessons, term_cache=terms)
            samples.append((time.perf_counter_ns() - start) / 1_000_000)
            rows.append([r.__dict__ for r in ranked])
        samples.sort()
        result[name] = {
            "median_ms": statistics.median(samples),
            "p95_ms": samples[min(len(samples) - 1, int(len(samples) * .95))],
            "ranking_sha256": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000")
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--output", required=True)
    parser.add_argument("--baseline-checkout", help="Compare two checkouts in alternating subprocess trials")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 1 or args.trials < 1:
        parser.error("--runs and --trials must be positive")
    if args.baseline_checkout:
        report = compare(args)
    else:
        report = measure(args)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")
    print(json.dumps(report.get("summary", report), indent=2))


def measure(args) -> dict:
    source = Path(retrieval.__file__).resolve()
    report = {
        "python": platform.python_version(), "platform": platform.platform(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "query_cache_setting": os.environ.get("COMMONTRACE_QUERY_CACHE", "1"),
        "source_sha256": {source.name: hashlib.sha256(source.read_bytes()).hexdigest()},
        "runs": args.runs,
        "results": [run(int(n), args.runs) for n in args.sizes.split(",")],
    }
    runtime_cache = source.with_name("runtime_cache.py")
    if runtime_cache.exists():
        report["source_sha256"][runtime_cache.name] = hashlib.sha256(runtime_cache.read_bytes()).hexdigest()
    return report


def compare(args) -> dict:
    script = Path(__file__).resolve()
    roots = {"baseline": Path(args.baseline_checkout).resolve(), "candidate": script.parents[1]}
    for name, root in roots.items():
        if not (root / "commontrace" / "retrieval.py").is_file():
            raise ValueError(f"{name} must be a CommonTrace checkout")
    trials = []
    with tempfile.TemporaryDirectory(prefix="commontrace-runtime-benchmark-") as directory:
        for trial in range(args.trials):
            pair = {}
            order = ("baseline", "candidate") if trial % 2 == 0 else ("candidate", "baseline")
            for name in order:
                target = os.path.join(directory, name + ".json")
                subprocess.run(
                    [sys.executable, str(script), "--sizes", args.sizes, "--runs", str(args.runs), "--output", target],
                    cwd=roots[name], env={**os.environ, "PYTHONPATH": str(roots[name])},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=120,
                )
                with open(target, encoding="utf-8") as fh:
                    pair[name] = json.load(fh)
            trials.append(pair)
    summary = []
    for position, size in enumerate(args.sizes.split(",")):
        for workload in ("repeated", "distinct"):
            pages = [pair[version]["results"][position][workload]
                     for pair in trials for version in ("baseline", "candidate")]
            if len({page["ranking_sha256"] for page in pages}) != 1:
                raise RuntimeError(f"ranking mismatch for {size} documents / {workload}")
            before, after = (statistics.median(pair[version]["results"][position][workload]["median_ms"]
                                               for pair in trials) for version in ("baseline", "candidate"))
            summary.append({"documents": int(size), "workload": workload,
                            "baseline_median_ms": before, "candidate_median_ms": after,
                            "speedup": before / after, "ranking_sha256": pages[0]["ranking_sha256"]})
    return {"trials": trials, "summary": summary}


if __name__ == "__main__":
    main()
