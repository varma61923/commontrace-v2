#!/usr/bin/env python3
"""Reproducible exact lexical ranking profile; no model or network calls.

Pass --checkout to compare an immutable baseline with a working candidate.
Wall and process CPU times are distinct: shared-host scheduling affects tails.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--output", required=True)
    parser.add_argument("--size", type=int, default=6400)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    sys.path.insert(0, os.path.abspath(args.checkout))
    from commontrace import lesson_cache, retrieval
    from commontrace.reference.measure_local_latency import _queries, build_store

    def stats(samples):
        ordered = sorted(samples)
        return {"median_ms": statistics.median(ordered),
                "p95_ms": ordered[int(0.95 * (len(ordered) - 1))]}

    result = {"checkout_sha": subprocess.check_output(
        ["git", "-C", args.checkout, "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version, "size": args.size, "query_seed": 7,
        "corpus_seed": 20260907, "runs": args.runs, "scorers": {}}
    with tempfile.TemporaryDirectory(prefix="ct-ranking-") as root:
        build_store(root, args.size)
        started = time.perf_counter()
        lessons, tc = lesson_cache.load_active_with_terms(root)
        result["cold_load_ms"] = (time.perf_counter() - started) * 1000
        qs = _queries(random.Random(7), 36)
        for scorer in retrieval.LEXICAL_SCORERS:
            started = time.perf_counter()
            retrieval.rank_lessons(qs[0], lessons, top_k=3, term_cache=tc, scorer=scorer)
            cold = (time.perf_counter() - started) * 1000
            for query in qs:
                retrieval.rank_lessons(query, lessons, top_k=3, term_cache=tc, scorer=scorer)
            wall, cpu, answers = [], [], []
            for query in qs * args.runs:
                w, c = time.perf_counter(), time.process_time()
                rows = retrieval.rank_lessons(query, lessons, top_k=3, term_cache=tc, scorer=scorer)
                cpu.append((time.process_time() - c) * 1000)
                wall.append((time.perf_counter() - w) * 1000)
                answers.append([{**row.__dict__, "path": os.path.basename(row.path)} for row in rows])
            result["scorers"][scorer] = {"cold_index_rank_ms": cold, "wall": stats(wall),
                                         "cpu": stats(cpu), "answers": answers}

    # The warmed rare-query path must remain proportional to postings, rather
    # than allocate several whole-corpus arrays on a 100k-document store.
    lessons = [(f"p{i}", {"name": str(i), "description": f"baseline needle{i}"})
               for i in range(100_000)]
    tc = lesson_cache.TermCache({})
    tc.stamps = {path: (1, i) for i, (path, _fm) in enumerate(lessons)}
    tc.lessons = lessons
    tc.fingerprint = tuple((path, tc.stamps[path]) for path, _fm in lessons)
    tc.fingerprint_hash = hash(tc.fingerprint)
    retrieval.rank_lessons("needle23", lessons, term_cache=tc, floor=0.0)
    cpu = []
    for _ in range(100):
        started = time.process_time()
        rows = retrieval.rank_lessons("needle23", lessons, term_cache=tc, floor=0.0)
        cpu.append((time.process_time() - started) * 1000)
        assert [row.slug for row in rows] == ["23"]
    tracemalloc.start()
    retrieval.rank_lessons("needle23", lessons, term_cache=tc, floor=0.0)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["rare_100k"] = {"cpu": stats(cpu), "query_peak_allocated_bytes": peak}
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)


if __name__ == "__main__":
    main()
