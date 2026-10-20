"""Report UniMem managed tier post-warm p50/p95/p99 without external services."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time

from commontrace import hierarchical, tiering


def run(iterations: int = 100) -> dict:
    if not 10 <= iterations <= 1000:
        raise ValueError("iterations must be between 10 and 1000")
    with tempfile.TemporaryDirectory() as root:
        memory = hierarchical.append_facts(root, [{"statement": "Inspect wrist load before lifting."}])[0][0]
        tiering.configure(root, "robot", region="local", allowed_regions=["local"], cold_region="local")
        tiering.put(root, "robot", "payload", {"frames": [1, 2, 3]}, region="local", sources=[memory.id])
        tiering.hibernate(root, "robot", region="local")
        tiering.prewarm(root, "robot", region="local", trigger="robot-boot")
        timings = []
        for _ in range(iterations):
            started = time.perf_counter()
            tiering.get(root, "robot", "payload", region="local")
            timings.append(1000 * (time.perf_counter() - started))
    timings.sort()
    return {
        "iterations": iterations,
        "p50_ms": statistics.median(timings),
        "p95_ms": timings[int(0.95 * (iterations - 1))],
        "p99_ms": timings[int(0.99 * (iterations - 1))],
        "scope": "local reference tier, admitted source and durable access journal; pre-warmed payload",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=100)
    print(json.dumps(run(parser.parse_args().iterations), indent=2))
