"""Validate Graphify graph integrity and report, rather than delete, possible dead code."""

from __future__ import annotations

import argparse
import json
import os
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
        if node.get("source_file", "").startswith(("unimem/commands/", "unimem/integrations/", "hub/"))
        or node.get("source_file")
        in (
            "unimem/cli.py",
            "unimem/gateway.py",
            "unimem/mcp_tools.py",
            "unimem/client.py",
            "unimem/framework_middleware.py",
            "unimem/record_api.py",
            "unimem/hook_kit.py",
            "unimem/hook_service.py",
            "unimem/hook_bridge.py",  # Absolute script entry point in generated native manifests.
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
        and node.get("source_file", "").startswith("unimem/")
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
    parser.add_argument("--source-root")
    parser.add_argument("--max-modules", type=int)
    parser.add_argument("--max-unreachable", type=int)
    args = parser.parse_args()
    with open(args.graph, encoding="utf-8") as stream:
        result = audit(json.load(stream))
    violations = []
    if args.source_root:
        result["python_modules"] = sum(
            name.endswith(".py")
            for _, _, names in os.walk(os.path.join(args.source_root, "unimem"))
            for name in names
        )
        if args.max_modules is not None and result["python_modules"] > args.max_modules:
            violations.append("Python module surface exceeds the reviewed budget")
    if args.max_unreachable is not None and len(result["possible_unreachable"]) > args.max_unreachable:
        violations.append("Static reachability candidates exceed the reviewed baseline; inspect entry points")
    result["policy_violations"] = violations
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(f"modules={result.get('python_modules', '?')} unreachable={len(result['possible_unreachable'])} "
          f"missing_endpoints={len(result['missing_endpoints'])}")
    for violation in violations:
        print("policy violation: " + violation)
    return bool(result["missing_endpoints"] or violations)


if __name__ == "__main__":
    raise SystemExit(main())
