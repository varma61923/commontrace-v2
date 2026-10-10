"""Validate Graphify graph integrity and report, rather than delete, possible dead code."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque


def audit(graph: dict) -> dict:
    nodes = {node["id"]: node for node in graph["nodes"]}
    adjacency = defaultdict(set)
    missing = []
    for edge in graph.get("links", graph.get("edges", [])):
        if edge["source"] not in nodes or edge["target"] not in nodes:
            missing.append([edge["source"], edge["target"]])
        else:
            adjacency[edge["source"]].add(edge["target"])
    roots = {
        identity
        for identity, node in nodes.items()
        if node.get("source_file", "").startswith(("commontrace/commands/", "commontrace/integrations/", "hub/"))
        or node.get("source_file")
        in (
            "commontrace/cli.py",
            "commontrace/gateway.py",
            "commontrace/mcp_tools.py",
            "commontrace/client.py",
            "commontrace/framework_middleware.py",
            "commontrace/record_api.py",
            "commontrace/hook_kit.py",
            "commontrace/hook_service.py",
        )
    }
    reachable, queue = set(roots), deque(roots)
    while queue:
        for target in adjacency[queue.popleft()]:
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    candidates = [
        {"id": identity, "file": node.get("source_file"), "label": node.get("label")}
        for identity, node in nodes.items()
        if identity not in reachable
        and node.get("_callable")
        and node.get("source_file", "").startswith("commontrace/")
    ]
    return {
        "nodes": len(nodes),
        "missing_endpoints": missing,
        "possible_unreachable": candidates,
        "limitations": [
            "Dynamic dispatch, SDK exports, callbacks and generated entry points are incomplete in static graphs.",
            "No function is deleted or declared dead solely from this report.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.graph, encoding="utf-8") as stream:
        result = audit(json.load(stream))
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return bool(result["missing_endpoints"])


if __name__ == "__main__":
    raise SystemExit(main())
