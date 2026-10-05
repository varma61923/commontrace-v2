"""Profile exact community adjacency under repeated shared membership signals."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--members", type=int, default=500)
    parser.add_argument("--groups", type=int, default=24)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if min(args.members, args.groups, args.runs) < 1:
        parser.error("All workload counts must be positive")
    sys.path.insert(0, args.checkout)
    from commontrace import communities

    tags = [f"shared:{index}" for index in range(args.groups)]
    nodes = {f"node:{index}": {"source_traces": [], "tags": tags}
             for index in range(args.members)}
    output = {"revision": subprocess.check_output(["git", "-C", args.checkout, "rev-parse", "HEAD"],
                                                  text=True).strip(),
              "members": args.members, "groups": args.groups, "runs": args.runs,
              "samples_ms": [], "output_hashes": []}
    with tempfile.TemporaryDirectory(prefix="commontrace-community-profile-") as root:
        for _trial in range(args.runs):
            started = time.perf_counter()
            adjacency = communities._build_adjacency(root, nodes)
            output["samples_ms"].append((time.perf_counter() - started) * 1000)
            labels = communities._label_propagation(list(nodes), adjacency)
            output["output_hashes"].append(hashlib.sha256(json.dumps(
                {"adjacency": adjacency, "labels": labels}, sort_keys=True).encode()).hexdigest())
        tracemalloc.start()
        communities._build_adjacency(root, nodes)
        _current, output["traced_peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    output["median_ms"] = statistics.median(output["samples_ms"])
    print(json.dumps(output))


if __name__ == "__main__":
    main()
