"""Measure wall-clock p50/p95/p99 for the local robot memory and STL monitor.

Run separately from parallel unit tests. Results describe this host, dataset and
vector dimension; the limits are regression budgets, not real-time guarantees.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections.abc import Callable
from typing import Any

from unimem import frontier_protocol as fp
from unimem.robotics import shield
from unimem.robotics.working_memory import WorkingMemory, percentile_latency


def _measure(operation: Callable[[], Any], samples: int) -> dict[str, float]:
    operation()
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        timings.append((time.perf_counter() - started) * 1000)
    return {"p50_ms": percentile_latency(timings, .5), "p95_ms": percentile_latency(timings, .95),
            "p99_ms": percentile_latency(timings)}


def run(samples: int = 100, *, check: bool = False) -> dict[str, Any]:
    fp.count(samples, 10, 1000, "benchmark samples")
    memory = WorkingMemory(capacity_tokens=100_000)
    for index in range(500):
        memory.add(f"frame-{index}", [float(dim == index % 32) for dim in range(32)],
                   tokens=1, episode_id="benchmark")

    def retrieve() -> None:
        result = memory.retrieve([1.0] + [0.0] * 31, budget_ms=50)
        if result["result"] is None:
            raise AssertionError("local retrieval returned no matching keyframe")

    retrieval = _measure(retrieve, samples)
    with tempfile.TemporaryDirectory(prefix="unimem-shield-bench-") as root:
        predicate = {"op": "predicate", "signal": "force", "cmp": "<=", "value": 10}
        shield.register(root, "force", formula={"op": "until", "from": 0, "to": 1,
            "left": predicate, "right": predicate}, memory_id="safe-memory",
            embodiment="reference", description="Measure bounded monitor and durable audit.")
        trace = [{"t": index / 1000, "force": 1.0} for index in range(1001)]

        def monitor() -> None:
            result = shield.check(root, "force", action={"move": "start"}, trace=trace,
                                  source_memory_ids=["safe-memory"])
            if result["action"] != "continue":
                raise AssertionError("valid full-horizon trace was held")

        monitoring = _measure(monitor, samples)
    if check and (retrieval["p99_ms"] >= 50 or monitoring["p99_ms"] >= 500):
        raise AssertionError(f"robot memory latency regression: retrieval={retrieval}, monitor={monitoring}")
    return {"samples": samples, "keyframes": 500, "dimensions": 32, "retrieval": retrieval,
            "shield_with_audit": monitoring, "classification": "host-specific wall-clock measurement"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.samples, check=args.check), indent=2, allow_nan=False))
