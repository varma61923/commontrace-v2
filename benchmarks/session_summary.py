"""Paired production rolling-summary workload with exact persisted-state equality.

The reference recreates the previous full-candidate ordering, then keeps its
prefix. Both arms run real SQLite source paging, evidence hashing and summary
persistence. This measures offline extractive summaries, not model latency or
competitor performance. Run in a dedicated process: the reference temporarily
replaces the selector to reproduce the old implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time

from unimem.conversation import Store, summary


def run(turns: int = 1000, trials: int = 3) -> dict:
    if not 1 <= turns <= 20_000 or not 1 <= trials <= 20:
        raise ValueError("turns must be in 1..20000 and trials in 1..20")
    samples: dict[str, list[float]] = {"reference": [], "optimized": []}
    fingerprints: dict[str, str] = {}
    selector = summary._select_candidates
    actions = (
        "deployed the release after checking the rollback procedure",
        "rotated the service credential and recorded its owner",
        "recovered the database replica after a regional incident",
        "updated the retention policy after reviewing a legal hold",
        "confirmed the incident handoff and scheduled a recovery drill",
    )

    def full_order(rows: list[dict], _count: int) -> list[dict]:
        return selector(rows, len(rows))

    with tempfile.TemporaryDirectory(prefix="unimem-summary-") as root, Store(root, "operations") as store:
        store.add("operations", [
            {"speaker": f"Operator {i % 4}",
             "text": f"Team {i % 9} {actions[i % len(actions)]} for service {i % 17} in region {i % 3}. "
                     f"The incident reference is OPS-{i:05d} and its follow-up owner is team {i % 9}."}
            for i in range(turns)
        ], session_at="2026-10-01")
        summary.rolling(store, "operations", force=True)
        try:
            for trial in range(trials):
                order = ("reference", "optimized") if trial % 2 == 0 else ("optimized", "reference")
                for arm in order:
                    summary._select_candidates = full_order if arm == "reference" else selector
                    started = time.perf_counter()
                    result = summary.rolling(store, "operations", force=True)
                    samples[arm].append((time.perf_counter() - started) * 1000)
                    state = summary.rolling_state(store, "operations") or {}
                    state.pop("updated_at", None)
                    encoded = json.dumps({"result": result, "state": state}, sort_keys=True).encode()
                    fingerprint = hashlib.sha256(encoded).hexdigest()
                    if arm in fingerprints and fingerprints[arm] != fingerprint:
                        raise AssertionError("summary output changed across identical trials")
                    fingerprints[arm] = fingerprint
        finally:
            summary._select_candidates = selector
    old, new = (statistics.median(samples[arm]) for arm in ("reference", "optimized"))
    with open(summary.__file__, "rb") as source:
        source_hash = hashlib.sha256(source.read()).hexdigest()
    return {"workload": "chronological-operational-session-summary", "source_turns": turns,
            "source_sentences": turns * 2, "trials": trials, "equivalent": len(set(fingerprints.values())) == 1,
            "reference_median_ms": old, "optimized_median_ms": new, "speedup": old / max(new, 1e-9),
            "fingerprints": fingerprints, "samples_ms": samples, "summary_source_sha256": source_hash,
            "scope": "SQLite extractive rolling summary; excludes model inference, remote I/O and competitor systems"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=1000)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--check", action="store_true", help="Require exact output and at least 10x on this workload.")
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= 20_000 or not 1 <= args.trials <= 20:
        parser.error("turns must be in 1..20000 and trials in 1..20")
    result = run(args.turns, args.trials)
    print(json.dumps(result, sort_keys=True))
    return int(args.check and (not result["equivalent"] or result["speedup"] < 10))


if __name__ == "__main__":
    raise SystemExit(main())
