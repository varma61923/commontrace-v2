"""Profile exact, low-degree historical graph retrieval across distinct dates."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--edges", type=int, default=20000)
    parser.add_argument("--dates", type=int, default=20)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if min(args.edges, args.dates, args.runs) < 1:
        parser.error("All workload counts must be positive")
    sys.path.insert(0, args.checkout)
    from commontrace import graph

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="commontrace-graph-profile-") as root:
        nodes = {"service:target": graph.GraphNode("service:target", "service", "Target", {},
                                                 base.isoformat(), base.isoformat())}
        edges = []
        for i in range(args.edges):
            source = "service:target" if i < 8 else "service:unrelated"
            edges.append(graph.GraphEdge(source, f"tool:{i}", "uses", 1.0, base.isoformat(),
                                         None, None, created_at=base.isoformat()))
        graph.save_nodes(root, nodes)
        graph.save_edges(root, edges)
        output = {"revision": subprocess.check_output(["git", "-C", args.checkout, "rev-parse", "HEAD"],
                                                        text=True).strip(), "edges": args.edges,
                  "target_degree": 8, "dates": args.dates, "runs": args.runs,
                  "samples_ms": [], "cold_samples_ms": [], "output_hashes": []}
        for trial in range(args.runs):
            graph._clear_graph_cache()
            cold_started = time.perf_counter()
            graph.get_neighbors(root, "service:target")
            output["cold_samples_ms"].append((time.perf_counter() - cold_started) * 1000)
            dates = [(base + timedelta(days=1 + day + args.dates * trial)).isoformat()
                     for day in range(args.dates)]
            started = time.perf_counter()
            values = [graph.get_neighbors(root, "service:target", as_of=date) for date in dates]
            output["samples_ms"].append((time.perf_counter() - started) * 1000)
            output["output_hashes"].append(hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest())
        output["median_ms"] = statistics.median(output["samples_ms"])
        output["cold_median_ms"] = statistics.median(output["cold_samples_ms"])
        output["cold_inclusive_samples_ms"] = [cold + batch for cold, batch in
                                               zip(output["cold_samples_ms"], output["samples_ms"])]
        output["cold_inclusive_median_ms"] = statistics.median(output["cold_inclusive_samples_ms"])
        print(json.dumps(output))


if __name__ == "__main__":
    main()
