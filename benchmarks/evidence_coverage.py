"""Paired lexical-coverage oracle: exact semantic equality and workload latency."""
from __future__ import annotations

import argparse
import json
import statistics
import time

from unimem.conversation import coverage, profile


def reference(question, evidence, *, labels=()):
    """Pre-optimization scan, independently retaining passage-level scoring."""
    asked = {w for w in coverage._terms(question) if (len(w) > 2 or coverage.has_cjk(w))
             and w not in profile.STOPWORDS and w not in coverage._QUESTIONS}
    entities = {w for name in profile.entities(question) for w in coverage._terms(name)}
    subjects = asked - coverage._ATTRIBUTES - entities
    if not subjects:
        subjects = asked - coverage._QUALIFIERS - entities
    words = set().union(*(coverage._terms(text) for text in evidence)) if evidence else set()
    missing = {w for w in subjects if not coverage._covered(w, words)}
    if not asked or not evidence:
        return coverage.EvidenceCoverage(0.0, True, "no recalled evidence covers the question", 0, len(asked),
                                         tuple(sorted(subjects)), tuple(sorted(missing)))
    if (subjects and missing == subjects) or any(w not in words for w in asked & coverage._IDENTIFIERS):
        return coverage.EvidenceCoverage(0.0, True,
            "recalled entities do not establish the requested subject or identifier", 0, len(asked),
            tuple(sorted(subjects)), tuple(sorted(missing)))
    label_words = {w for label in labels for w in coverage._terms(label)}
    matched = max(sum(coverage._covered(w, coverage._terms(text) | label_words) for w in asked) for text in evidence)
    value = round(matched / len(asked), 3)
    return coverage.EvidenceCoverage(value, value == 0.0,
        "lexical evidence covers part of the question; answer correctness is unverified", matched, len(asked),
        tuple(sorted(subjects)), tuple(sorted(missing)))


def run(trials=5):
    vocabulary = [f"facet{i:03d}" for i in range(80)]
    question = "What is recorded about " + " ".join(vocabulary)
    evidence = [" ".join(vocabulary) + " supporting evidence" * 50 for _ in range(16)]
    samples = {"reference": [], "optimized": []}
    results = {}
    for trial in range(trials):
        order = ("reference", "optimized") if trial % 2 == 0 else ("optimized", "reference")
        for name in order:
            start = time.perf_counter()
            results[name] = (reference if name == "reference" else coverage.assess)(question, evidence)
            samples[name].append(1000 * (time.perf_counter() - start))
    old, new = (statistics.median(samples[name]) for name in ("reference", "optimized"))
    return {"workload": "80-facet-16-passage-lexical-coverage", "trials": trials,
            "equivalent": results["reference"] == results["optimized"],
            "reference_median_ms": old, "optimized_median_ms": new, "speedup": old / max(new, 1e-9),
            "samples_ms": samples, "scope": "coverage gate only; not end-to-end or competitor latency"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.trials <= 100:
        parser.error("trials must be in 1..100")
    report = run(args.trials)
    print(json.dumps(report, sort_keys=True))
    return int(args.check and (not report["equivalent"] or report["speedup"] < 10))


if __name__ == "__main__":
    raise SystemExit(main())
