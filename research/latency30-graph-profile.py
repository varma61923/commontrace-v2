"""Measure exact graph APIs with cold process caches and warm repeated calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone


def _stats(values):
    ordered = sorted(values)
    return {"samples_ms": values, "p50_ms": statistics.median(values),
            "p95_ms": ordered[max(0, math.ceil(0.95 * len(values)) - 1)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--edges", type=int, default=20000)
    parser.add_argument("--nodes", type=int, default=512)
    parser.add_argument("--cold-runs", type=int, default=5)
    parser.add_argument("--warm-runs", type=int, default=50)
    parser.add_argument("--operations", help="Comma-separated API names; omitted runs all")
    args = parser.parse_args()
    if min(args.edges, args.nodes, args.cold_runs, args.warm_runs) < 1:
        parser.error("All workload counts must be positive")
    sys.path.insert(0, args.checkout)
    from commontrace import graph

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory(prefix="commontrace-latency-graph-") as root:
        nodes = {f"peer:{index}": graph.GraphNode(f"peer:{index}", "tool", f"Peer {index}", {},
                                                  base.isoformat(), base.isoformat())
                 for index in range(args.nodes)}
        nodes["service:target"] = graph.GraphNode("service:target", "service", "Target Service", {},
                                                  base.isoformat(), base.isoformat())
        nodes["service:unrelated"] = graph.GraphNode("service:unrelated", "service", "Unrelated", {},
                                                     base.isoformat(), base.isoformat())
        for version in range(1, 7):
            identity = f"memory:chain:{version}"
            nodes[identity] = graph.GraphNode(identity, "memory", f"Chain {version}", {}, base.isoformat(),
                                               base.isoformat(), root_id="memory:chain:1", version=version)
        edges = [graph.GraphEdge("service:target" if index < 8 else "service:unrelated",
                                 f"peer:{index % args.nodes}", "uses", 1,
                                 (base + timedelta(days=index % 365)).isoformat(), None,
                                 "2025-01-01T00:00:00Z" if index % 19 == 0 else None,
                                 created_at=base.isoformat()) for index in range(args.edges)]
        graph.save_nodes(root, nodes)
        graph.save_edges(root, edges)
        calls = {
            "entity_extraction": lambda: graph.extract_entities_from_text(root, "Target Service uses Peer 1"),
            "version_chain": lambda: [node.to_dict() for node in graph.get_version_chain(root, "memory:chain:1")],
            "neighbors_current": lambda: graph.get_neighbors(root, "service:target"),
            "neighbors_historical": lambda: graph.get_neighbors(root, "service:target", as_of="2024-01-04"),
            "bounded_multihop": lambda: graph.multi_hop_subgraph(root, ["service:target"], max_hops=1),
            "entity_interval": lambda: [edge.to_dict() for edge in graph.edges_between(
                root, "2024-01-02", "2024-01-06", entity="service:target")],
            "entity_timeline": lambda: graph.timeline(root, "service:target"),
        }
        if args.operations:
            requested = args.operations.split(",")
            if any(name not in calls for name in requested):
                parser.error("Unknown operation; choose from " + ",".join(calls))
            calls = {name: calls[name] for name in requested}
        output = {"revision": subprocess.check_output(["git", "-C", args.checkout, "rev-parse", "HEAD"],
                                                         text=True).strip(),
                  "edges": args.edges, "nodes": len(nodes), "target_degree": min(8, args.edges),
                  "cold_definition": "clear Python graph caches; source files remain in OS page cache",
                  "results": {}}
        for name, query in calls.items():
            cold, warm, hashes = [], [], set()
            for _trial in range(args.cold_runs):
                graph._clear_graph_cache()
                started = time.perf_counter()
                value = query()
                cold.append((time.perf_counter() - started) * 1000)
                hashes.add(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
            for _trial in range(args.warm_runs):
                started = time.perf_counter()
                value = query()
                warm.append((time.perf_counter() - started) * 1000)
                hashes.add(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
            output["results"][name] = {"cold": _stats(cold), "warm": _stats(warm), "output_hashes": sorted(hashes)}
        print(json.dumps(output))


if __name__ == "__main__":
    main()
