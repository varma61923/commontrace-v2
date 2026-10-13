"""Paired exact-oracle benchmark for warm rare-relation graph lookup.

This measures a high-degree local graph workload, not overall agent latency or
competitor performance. Cold index construction is reported separately.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time

from commontrace import graph

BASE = "2024-01-01T00:00:00+00:00"
AS_OF = "2025-01-01"


def scan_oracle(index: graph._GraphFullIndex) -> list[dict]:
    """The previous neighbor algorithm: scan incident edges before filtering."""
    moment = graph.lesson_cache.parse_moment(AS_OF)
    rows = []
    for position in index.by_entity.get("hub", []):
        edge = index.edges[position]
        if not graph._is_active_edge(edge, moment) or edge.relation != "resolves":
            continue
        neighbor = edge.target if edge.source == "hub" else edge.source
        node = index.nodes[neighbor]
        rows.append({"neighbor_id": neighbor, "neighbor_name": node.name, "neighbor_type": node.entity_type,
                     "direction": "out" if edge.source == "hub" else "in",
                     "relation": edge.relation, "weight": edge.weight})
    return rows


def measure(*, edges: int = 20_000, trials: int = 5) -> dict:
    if isinstance(edges, bool) or not isinstance(edges, int) or not 100 <= edges <= 1_000_000:
        raise ValueError("edges must be an integer in 100..1000000")
    if isinstance(trials, bool) or not isinstance(trials, int) or not 1 <= trials <= 100:
        raise ValueError("trials must be an integer in 1..100")
    with tempfile.TemporaryDirectory(prefix="commontrace-typed-graph-") as root:
        nodes = {identity: graph.GraphNode(identity, "concept", identity, {}, BASE, BASE)
                 for identity in ("hub", "peer")}
        records = [graph.GraphEdge("hub", "peer", "resolves" if i < 5 else "uses", 1,
                                  BASE, "2024-06-01T00:00:00Z" if i == 0 else None,
                                  None, created_at=BASE) for i in range(edges)]
        graph.save_nodes(root, nodes)
        graph.save_edges(root, records)
        start = time.perf_counter()
        expected = scan_oracle(graph._full_index(root))
        oracle_cold_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        actual = graph.get_neighbors(root, "hub", relation="resolves", as_of=AS_OF)
        typed_index_ms = (time.perf_counter() - start) * 1000
        if actual != expected:
            raise AssertionError("indexed graph lookup differs from the exact scan oracle")
        index = graph._full_index(root)
        scan_times, indexed_times = [], []
        for trial in range(trials):
            operations = [(scan_times, lambda: scan_oracle(index)),
                          (indexed_times, lambda: graph.get_neighbors(root, "hub", relation="resolves", as_of=AS_OF))]
            # Alternate order to reduce systematic cache/CPU timing bias.
            for samples, operation in operations[::1 if trial % 2 == 0 else -1]:
                start = time.perf_counter()
                result = operation()
                samples.append((time.perf_counter() - start) * 1000)
                if result != expected:
                    raise AssertionError("warm lookup differs from the exact scan oracle")
        scan_ms, indexed_ms = statistics.median(scan_times), statistics.median(indexed_times)
        return {"workload": "warm-high-degree-rare-relation", "edges": edges, "trials": trials,
                "matching_edges": len(expected), "equivalent": True, "oracle_cold_ms": oracle_cold_ms,
                "typed_index_build_ms": typed_index_ms, "scan_median_ms": scan_ms,
                "indexed_median_ms": indexed_ms, "speedup": scan_ms / max(indexed_ms, 1e-9),
                "scan_samples_ms": scan_times, "indexed_samples_ms": indexed_times}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=int, default=20_000)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--check", action="store_true", help="Require at least 10x paired warm lookup speedup.")
    args = parser.parse_args()
    result = measure(edges=args.edges, trials=args.trials)
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(args.check and result["speedup"] < 10)


if __name__ == "__main__":
    raise SystemExit(main())
